"""Uploading a file into a dataset: read → map → stage → check → preview → confirm, and undo.

Example (Chart of Accounts, mode Add / update):
1. The file is kept in UPLOAD_DIR until confirm or cancel.
2. data-core reads it; each mapped cell is converted (values.py). Problems stop here.
3. Rows go into a stage table next to the dataset, e.g. co_acme._stage_12.
4. SQL checks: repeated keys, Unique columns, links ("4010 is not in Chart of Accounts"),
   rows a Full refresh would delete that another dataset still uses.
5. Counts for the preview: 12 new · 3 changed · 0 deleted · 480 unchanged.
6. Confirm: one transaction writes the change log (_changes: old / new values per row),
   then deletes, updates and adds rows. The stage table and the file are removed.
Keys, links and Unique columns ignore capitals: acc01 matches ACC01, and the saved spelling stays.

Periodic reports (mode "Replace dates"): the As-of date is part of the key. It comes from the
date picked for the whole file, or from a file column. Only the dates in the upload are
replaced: rows of 31-03-2026 not in the file are removed, 28-02-2026 is never touched.
"""

import logging
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from data_core.loading import FILE_ROW, UploadRejected, load_file, quote_name
from data_core.store import DataStore
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from psycopg import sql

from . import conf, values
from .models import Dataset, DatasetColumn, Upload
from .schema_builder import (
    AS_OF,
    ColumnSpec,
    TableState,
    _run,
    ensure_case_rules,
    match_expr,
    table_exists,
)

logger = logging.getLogger(__name__)

LOCK_SPACE = 4207  # advisory-lock namespace for "one upload per dataset at a time"
READ_STORE_MB = 300
MAX_PROBLEMS = 10_000
SHOWN_PROBLEMS = 50
PREVIEW_ROWS = 2_000
STALE_AFTER = timedelta(days=1)
CHANGES = "_changes"
# The As-of date of a periodic report, handled like a key column of the upload.
AS_OF_SPEC = ColumnSpec(
    id=0,
    name=AS_OF,
    label="As-of date",
    data_type=DatasetColumn.DATE,
    decimal_places=0,
    is_key=True,
    required=True,
    unique=False,
    references_id=None,
)


class UploadProblems(Exception):
    """The upload can't go ahead. `problems` = [{"row": 45 or None, "text": "..."}]."""

    def __init__(self, problems: list[dict]):
        super().__init__(f"{len(problems)} problem(s)")
        self.problems = problems


def problem(text: str, row: int | None = None, key: str = "") -> dict:
    return {"row": row, "text": text, "key": key}


# ---------- the uploaded file ----------


def _folder() -> Path:
    folder = Path(conf.get("UPLOAD_DIR") or Path(tempfile.gettempdir()) / "findata_uploads")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def file_path(upload: Upload) -> Path:
    """The uploaded file; for a server pull, the snapshot of what was read."""
    if upload.source_kind == Upload.SERVER:
        return _folder() / f"{upload.id}.snapshot.sqlite"
    if upload.source_kind == Upload.API:
        return _folder() / f"{upload.id}.json"
    return _folder() / f"{upload.id}{Path(upload.file_name).suffix.lower()}"


def save_file(upload: Upload, chunks) -> None:
    with file_path(upload).open("wb") as target:
        for chunk in chunks:
            target.write(chunk)


def _remove_file(upload: Upload) -> None:
    file_path(upload).unlink(missing_ok=True)


@dataclass
class Sheet:
    label: str  # "tally.xlsx › Ledgers"
    headers: list[str]
    samples: dict[str, list[str]]
    row_count: int
    table_name: str  # in the read store ("" for a database result)
    # read(headers) -> (row number, cell, cell, ...) per row, in row order
    read: Callable[[list[str]], Iterator[tuple]]


def _store_reader(store: DataStore, table_name: str):
    def read(wanted: list[str]):
        select = ", ".join([FILE_ROW] + [quote_name(header) for header in wanted])
        cursor = store.connection.execute(f"SELECT {select} FROM {table_name} ORDER BY {FILE_ROW}")
        while batch := cursor.fetchmany(5_000):
            yield from batch

    return read


@contextmanager
def opened(upload: Upload):
    """The upload's sheets (a file's, or a database result's). Raises UploadProblems."""
    if upload.from_database:
        from .db_source import sheets  # db_source builds on this module

        with sheets(upload) as found:
            yield found
        return
    path = file_path(upload)
    if not path.exists():
        logger.warning("Upload %s: file %s is gone", upload.id, path)
        raise UploadProblems([problem("The uploaded file is no longer here. Upload it again.")])
    store = DataStore(memory_mb=READ_STORE_MB)
    try:
        try:
            # An API reply is read as a .json file, from where its rows are
            name = (
                f"{upload.file_name}.json" if upload.source_kind == Upload.API else upload.file_name
            )
            loaded = load_file(
                store, name, path.read_bytes(), keep_text=True, rows_at=upload.rows_at
            )
        except UploadRejected as error:
            logger.warning("Upload %s rejected by the reader: %s", upload.id, error)
            raise UploadProblems([problem(str(error))]) from error
        sheets = [
            Sheet(
                label=item.source_file.replace(name, upload.file_name, 1),
                headers=[profile.name for profile in item.columns],
                samples={profile.name: profile.samples[:3] for profile in item.columns},
                row_count=item.row_count,
                table_name=item.table_name,
                read=_store_reader(store, item.table_name),
            )
            for item in loaded
        ]
        yield sheets
    finally:
        store.close()


def pick_sheet(sheets: list[Sheet], label: str) -> Sheet:
    return next((sheet for sheet in sheets if sheet.label == label), sheets[0])


# ---------- small SQL helpers ----------


def _ident(dataset: Dataset, table: str) -> sql.Identifier:
    return sql.Identifier(dataset.schema, table)


def _match(columns, left: str, right: str) -> sql.Composable:
    return sql.SQL(" AND ").join(
        sql.SQL("{} = {}").format(match_expr(column, left), match_expr(column, right))
        for column in columns
    )


def _shown(columns, alias: str) -> sql.Composable:
    """Key as readable text, e.g. S1 + 2."""
    return sql.SQL(" || ' + ' || ").join(
        sql.SQL("coalesce({}::text, '')").format(sql.Identifier(alias, column.name))
        for column in columns
    )


def _json(columns, alias: str) -> sql.Composable:
    return sql.SQL("jsonb_build_object({})").format(
        sql.SQL(", ").join(
            sql.SQL("{}, {}").format(sql.Literal(column.name), sql.Identifier(alias, column.name))
            for column in columns
        )
    )


def _lock(dataset: Dataset) -> None:
    got = _run(sql.SQL("SELECT pg_try_advisory_xact_lock(%s, %s)"), (LOCK_SPACE, dataset.id))[0][0]
    if not got:
        logger.warning("Upload into %s refused: another one is running", dataset.name)
        raise UploadProblems(
            [problem(f"Another upload of {dataset.name} is in progress. Try again in a minute.")]
        )


def _ensure_changes_table(dataset: Dataset) -> None:
    table = _ident(dataset, CHANGES)
    _run(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} (id bigserial PRIMARY KEY, upload_id bigint NOT NULL, "
            "dataset_id bigint NOT NULL, action text NOT NULL, key jsonb NOT NULL, old jsonb, "
            "new jsonb, at timestamptz NOT NULL DEFAULT now())"
        ).format(table)
    )
    _run(
        sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (upload_id)").format(
            sql.Identifier(f"{CHANGES}_upload"), table
        )
    )
    _run(  # one row's history (Phase 4)
        sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (dataset_id, key)").format(
            sql.Identifier(f"{CHANGES}_key"), table
        )
    )


def latest_done(dataset: Dataset) -> Upload | None:
    return (
        Upload.objects.filter(dataset=dataset, status=Upload.DONE)
        .order_by("-confirmed_at", "-id")
        .first()
    )


def sweep_stale() -> None:
    """Uploads left half-way for a day: drop their stage table and file."""
    for old in Upload.objects.filter(
        status__in=[Upload.MAPPING, Upload.PREVIEW], created_at__lt=timezone.now() - STALE_AFTER
    ).select_related("dataset"):
        cancel(old)
        logger.info("Cleaned up unfinished upload %s", old.id)


# ---------- staging ----------


@dataclass
class Plan:
    """The dataset's table and the columns this upload fills."""

    dataset: Dataset
    state: TableState
    mapped: list[ColumnSpec]  # in table order

    @property
    def by_date(self) -> bool:
        return self.state.is_snapshot

    @property
    def keys(self) -> list[ColumnSpec]:
        own = [column for column in self.state.columns if column.is_key]
        return ([AS_OF_SPEC] if self.by_date else []) + own

    @property
    def all_columns(self) -> list[ColumnSpec]:
        return ([AS_OF_SPEC] if self.by_date else []) + list(self.state.columns)

    @property
    def values(self) -> list[ColumnSpec]:
        """Mapped columns that aren't the key: the ones an update changes."""
        return [column for column in self.mapped if not column.is_key]

    @property
    def target(self) -> sql.Identifier:
        return self.state.ident

    def stage(self, upload: Upload) -> sql.Identifier:
        return _ident(self.dataset, upload.stage_table)

    def in_scope(self, upload: Upload, alias: str) -> sql.Composable:
        """Saved rows this upload may remove: all of them (Full refresh), or only those of the
        dates in the upload (periodic reports)."""
        if not self.by_date:
            return sql.SQL("TRUE")
        return sql.SQL("{} IN (SELECT DISTINCT {} FROM {})").format(
            sql.Identifier(alias, AS_OF), sql.Identifier(AS_OF), self.stage(upload)
        )

    def differs(self) -> sql.Composable:
        if not self.values:
            return sql.SQL("FALSE")
        return sql.SQL("({})").format(
            sql.SQL(" OR ").join(
                sql.SQL("s.{0} IS DISTINCT FROM t.{0}").format(sql.Identifier(column.name))
                for column in self.values
            )
        )


def plan_for(dataset: Dataset, mapping: dict[str, str]) -> Plan:
    state = TableState.of(dataset)
    chosen = set(mapping.values())
    mapped = [column for column in state.columns if column.name in chosen]
    if state.is_snapshot:
        mapped.insert(0, AS_OF_SPEC)  # from a file column, or the date picked for the file
    return Plan(dataset, state, mapped)


def _convert_rows(sheet: Sheet, plan: Plan, mapping: dict[str, str], as_of: date | None = None):
    """(rows, problems). Row numbers are the rows in the file (data-core's FILE_ROW), or of a
    database result. Each problem also names the row's key, e.g. 4010. A periodic report with
    no file column for the As-of date gets `as_of` on every row."""
    by_name = {column.name: column for column in plan.mapped}
    picked = [(header, by_name[name]) for header, name in mapping.items() if name in by_name]
    if plan.by_date and all(column.name != AS_OF for _, column in picked):
        picked.append((None, AS_OF_SPEC))  # no file column: the picked date
    picked.sort(key=lambda pair: plan.mapped.index(pair[1]))
    key_places = [
        index for index, (_, column) in enumerate(picked) if column.is_key and column.name != AS_OF
    ]
    today = timezone.localdate()
    rows, problems = [], []
    for row_no, *read_cells in sheet.read([header for header, _ in picked if header is not None]):
        from_file = iter(read_cells)
        cells = [as_of if header is None else next(from_file) for header, _ in picked]
        converted = [row_no]
        key = " + ".join("" if cells[index] is None else str(cells[index]) for index in key_places)
        for (_header, column), cell in zip(picked, cells, strict=True):
            try:
                value = values.convert(cell, column)
            except values.BadValue as error:
                value = None
                if len(problems) < MAX_PROBLEMS:
                    shown = str(cell)[:40]
                    problems.append(problem(f"{column.label}: “{shown}” {error}.", row_no, key))
            else:
                if value is None and column.not_null and len(problems) < MAX_PROBLEMS:
                    problems.append(
                        problem(f"{column.label} is blank, but it's required.", row_no, key)
                    )
                elif (
                    column.name == AS_OF
                    and value
                    and value > today
                    and len(problems) < MAX_PROBLEMS
                ):
                    problems.append(
                        problem(f"As-of date {value:%d-%m-%Y} is in the future.", row_no, key)
                    )
            converted.append(value)
        rows.append(converted)
    return rows, problems


def stage(upload: Upload, sheet: Sheet, mapping: dict[str, str], mode: str) -> None:
    """Convert, check and count an opened sheet. Leaves the upload in "preview" with its
    stage table, or raises UploadProblems (nothing kept in the database)."""
    dataset = upload.dataset
    plan = plan_for(dataset, mapping)
    if plan.by_date:
        mode = Upload.DATES  # periodic reports always replace their dates
    mapping = {header: mapping.get(header, "") for header in sheet.headers}
    rows, problems = _convert_rows(sheet, plan, mapping, upload.as_of_date)

    # Bad cells are staged as blank, so the checks below still run: every problem is listed
    # together (a bad amount in row 3 and a missing account in row 4), not one round at a time.
    with transaction.atomic():
        _lock(dataset)
        look_alikes = ensure_case_rules(plan.state)
        if look_alikes:
            raise UploadProblems([problem(text) for text in look_alikes])
        _ensure_changes_table(dataset)
        _run(sql.SQL("DROP TABLE IF EXISTS {}").format(plan.stage(upload)))
        _run(
            sql.SQL("CREATE UNLOGGED TABLE {} (_row_no bigint NOT NULL, {})").format(
                plan.stage(upload),
                sql.SQL(", ").join(
                    sql.SQL("{} {}").format(sql.Identifier(column.name), sql.SQL(column.pg_type))
                    for column in plan.mapped
                ),
            )
        )
        names = sql.SQL(", ").join(
            [sql.Identifier("_row_no")] + [sql.Identifier(column.name) for column in plan.mapped]
        )
        copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(plan.stage(upload), names)
        connection.ensure_connection()
        with connection.connection.cursor() as cursor, cursor.copy(copy_sql) as copy:
            for row in rows:
                copy.write_row(row)
        problems += _check(upload, plan, mode, cells_ok=not problems)
        if problems:
            problems.sort(key=lambda item: (item["row"] is None, item["row"] or 0))
            raise UploadProblems(problems[:MAX_PROBLEMS])
        _count(upload, plan, mode)
        previous = latest_done(dataset)
        upload.sheet = sheet.label
        upload.mapping = mapping
        upload.mode = mode
        upload.problems = []
        upload.status = Upload.PREVIEW
        upload.base_upload_id = previous.id if previous else None
        upload.definition_at = dataset.updated_at
        upload.save()
    logger.info(
        "Upload %s into %s staged: %s new, %s changed, %s deleted",
        upload.id, dataset.name, upload.added, upload.changed, upload.deleted,
    )  # fmt: skip


def _check(upload: Upload, plan: Plan, mode: str, cells_ok: bool) -> list[dict]:
    found: list[dict] = []
    stage_table = plan.stage(upload)

    # 1. The same key twice in the file (ignoring capitals).
    key_exprs = sql.SQL(", ").join(match_expr(column, "s") for column in plan.keys)
    for rows, shown in _run(
        sql.SQL(
            "SELECT array_agg(s._row_no ORDER BY s._row_no), min({shown}) FROM {stage} s "
            "WHERE {filled} GROUP BY {keys} HAVING count(*) > 1 ORDER BY min(s._row_no) LIMIT %s"
        ).format(
            shown=_shown(plan.keys, "s"),
            stage=stage_table,
            filled=sql.SQL(" AND ").join(
                sql.SQL("{} IS NOT NULL").format(sql.Identifier("s", column.name))
                for column in plan.keys
            ),
            keys=key_exprs,
        ),
        (MAX_PROBLEMS,),
    ):
        found.append(
            problem(
                f"Key “{shown}” is in rows {_row_list(rows)}. Each key may appear only once.",
                rows[0],
            )
        )

    # 2. Unique columns: twice in the file, or already used by a saved row that stays.
    for column in plan.values:
        if not plan.state.needs_unique(column):
            continue
        name = sql.Identifier(column.name)
        per_date = [sql.Identifier("s", AS_OF)] if plan.by_date else []  # unique per date
        for rows, shown in _run(
            sql.SQL(
                "SELECT array_agg(s._row_no ORDER BY s._row_no), min(s.{name}::text) "
                "FROM {stage} s WHERE s.{name} IS NOT NULL GROUP BY {expr} HAVING count(*) > 1 "
                "ORDER BY min(s._row_no) LIMIT %s"
            ).format(
                name=name,
                stage=stage_table,
                expr=sql.SQL(", ").join(per_date + [match_expr(column, "s")]),
            ),
            (MAX_PROBLEMS,),
        ):
            found.append(
                problem(
                    f"{column.label} “{shown}” is in rows {_row_list(rows)}, but it must be unique.",
                    rows[0],
                )
            )
        if mode == Upload.ADD_UPDATE:
            for row_no, shown in _run(
                sql.SQL(
                    "SELECT s._row_no, s.{name}::text FROM {stage} s JOIN {target} t "
                    "ON {same} WHERE NOT ({key}) "
                    "AND NOT EXISTS (SELECT 1 FROM {stage} s2 WHERE {key2}) "
                    "ORDER BY 1 LIMIT %s"
                ).format(
                    name=name,
                    stage=stage_table,
                    target=plan.target,
                    same=sql.SQL("{} = {}").format(
                        match_expr(column, "s"), match_expr(column, "t")
                    ),
                    key=_match(plan.keys, "s", "t"),
                    key2=_match(plan.keys, "s2", "t"),
                ),
                (MAX_PROBLEMS,),
            ):
                found.append(
                    problem(
                        f"{column.label} “{shown}” is already used by another saved row, "
                        "but it must be unique.",
                        row_no,
                    )
                )

    # 3. Links: every value must be in the linked dataset; then use its spelling.
    for column in plan.mapped:
        if not column.references_id:
            continue
        target = Dataset.objects.get(pk=column.references_id)
        target_key = TableState.of(target).columns
        target_key = next(spec for spec in target_key if spec.is_key)
        target_table = sql.Identifier(target.schema, target.table_name)
        name = sql.Identifier(column.name)
        same = sql.SQL("{} = {}").format(match_expr(target_key, "x"), match_expr(column, "s"))
        for row_no, shown, key in _run(
            sql.SQL(
                "SELECT s._row_no, s.{name}::text, {key} FROM {stage} s "
                "WHERE s.{name} IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM {target} x WHERE {same}) ORDER BY 1 LIMIT %s"
            ).format(
                name=name,
                key=_shown(plan.keys, "s"),
                stage=stage_table,
                target=target_table,
                same=same,
            ),
            (MAX_PROBLEMS,),
        ):
            found.append(
                problem(
                    f"{column.label} “{shown}” is not in {target.name}. "
                    f"Upload {target.name} first.",
                    row_no,
                    key,
                )
            )
        if column.data_type == DatasetColumn.TEXT:
            _run(
                sql.SQL(
                    "UPDATE {stage} s SET {name} = x.{key} FROM {target} x "
                    "WHERE {same} AND s.{name} IS DISTINCT FROM x.{key}"
                ).format(
                    stage=stage_table,
                    name=name,
                    key=sql.Identifier(target_key.name),
                    target=target_table,
                    same=same,
                )
            )

    # 4. Full refresh: rows it would delete that another dataset still uses.
    # (Skipped while some keys are unreadable: those rows would look "deleted".)
    if mode == Upload.FULL_REFRESH and len(plan.keys) == 1 and cells_ok:
        key = sql.Identifier(plan.keys[0].name)
        for link in plan.dataset.linked_from.select_related("dataset"):
            other = link.dataset
            for shown, uses in _run(
                sql.SQL(
                    "SELECT t.{key}::text, count(*) FROM {target} t JOIN {other} o "
                    "ON o.{link} = t.{key} WHERE NOT EXISTS (SELECT 1 FROM {stage} s WHERE {match}) "
                    "GROUP BY t.{key} ORDER BY 1 LIMIT %s"
                ).format(
                    key=key,
                    target=plan.target,
                    other=sql.Identifier(other.schema, other.table_name),
                    link=sql.Identifier(link.name),
                    stage=stage_table,
                    match=_match(plan.keys, "s", "t"),
                ),
                (MAX_PROBLEMS,),
            ):
                found.append(
                    problem(
                        f"“{shown}” isn't in the file, so Full refresh would delete it, but "
                        f"{other.name} uses it in {uses:,} row(s). Keep it in the file, or "
                        f"remove those {other.name} rows first."
                    )
                )
    return found


def _row_list(rows: list[int]) -> str:
    shown = ", ".join(str(row) for row in rows[:10])
    return shown + (f" and {len(rows) - 10} more" if len(rows) > 10 else "")


def _count(upload: Upload, plan: Plan, mode: str) -> None:
    if plan.by_date:  # the per-date counts add up to the totals
        upload.dates = _date_counts(upload, plan)
        total = {
            name: sum(item[name] for item in upload.dates)
            for name in ("rows", "added", "changed", "deleted", "existing")
        }
        upload.added, upload.changed, upload.deleted = (
            total["added"], total["changed"], total["deleted"],
        )  # fmt: skip
        upload.unchanged = total["rows"] - total["added"] - total["changed"]
        upload.existing = total["existing"]
        return
    upload.dates = []
    found = sql.Identifier("t", plan.keys[0].name)
    added, changed, unchanged = _run(
        sql.SQL(
            "SELECT count(*) FILTER (WHERE {found} IS NULL), "
            "count(*) FILTER (WHERE {found} IS NOT NULL AND {differs}), "
            "count(*) FILTER (WHERE {found} IS NOT NULL AND NOT {differs}) "
            "FROM {stage} s LEFT JOIN {target} t ON {match}"
        ).format(
            found=found,
            differs=plan.differs(),
            stage=plan.stage(upload),
            target=plan.target,
            match=_match(plan.keys, "s", "t"),
        )
    )[0]
    scope = plan.in_scope(upload, "t")
    existing = _run(sql.SQL("SELECT count(*) FROM {} t WHERE {}").format(plan.target, scope))[0][0]
    deleted = 0
    if _replaces(mode):
        deleted = _run(
            sql.SQL(
                "SELECT count(*) FROM {target} t WHERE {scope} AND NOT EXISTS "
                "(SELECT 1 FROM {stage} s WHERE {match})"
            ).format(
                target=plan.target,
                scope=scope,
                stage=plan.stage(upload),
                match=_match(plan.keys, "s", "t"),
            )
        )[0][0]
    upload.added, upload.changed, upload.unchanged = added, changed, unchanged
    upload.deleted, upload.existing = deleted, existing


def _replaces(mode: str) -> bool:
    """Does this mode remove saved rows that aren't in the upload?"""
    return mode in (Upload.FULL_REFRESH, Upload.DATES)


def _date_counts(upload: Upload, plan: Plan) -> list[dict]:
    """Per As-of date in the upload: rows in the upload, new / changed / removed rows, and the
    rows saved before."""
    found = sql.Identifier("t", plan.keys[-1].name)
    by_day: dict[date, dict] = {}
    for day, rows, added, changed in _run(
        sql.SQL(
            "SELECT s.{as_of}, count(*), count(*) FILTER (WHERE {found} IS NULL), "
            "count(*) FILTER (WHERE {found} IS NOT NULL AND {differs}) "
            "FROM {stage} s LEFT JOIN {target} t ON {match} GROUP BY 1"
        ).format(
            as_of=sql.Identifier(AS_OF),
            found=found,
            differs=plan.differs(),
            stage=plan.stage(upload),
            target=plan.target,
            match=_match(plan.keys, "s", "t"),
        )
    ):
        by_day[day] = {
            "rows": rows, "added": added, "changed": changed, "deleted": 0, "existing": 0,
        }  # fmt: skip
    for day, existing, deleted in _run(
        sql.SQL(
            "SELECT t.{as_of}, count(*), count(*) FILTER (WHERE NOT EXISTS "
            "(SELECT 1 FROM {stage} s WHERE {match})) FROM {target} t WHERE {scope} GROUP BY 1"
        ).format(
            as_of=sql.Identifier(AS_OF),
            stage=plan.stage(upload),
            match=_match(plan.keys, "s", "t"),
            target=plan.target,
            scope=plan.in_scope(upload, "t"),
        )
    ):
        by_day[day].update(existing=existing, deleted=deleted)
    return [{"date": day.isoformat(), **counts} for day, counts in sorted(by_day.items())]


# ---------- preview rows ----------


def preview_rows(upload: Upload, tab: str) -> tuple[list[ColumnSpec], list[dict], int]:
    """(columns, rows, total) for the New / Changed / Deleted tab, at most PREVIEW_ROWS rows.
    Changed rows hold "old → new" text for the cells that change, "" for the others."""
    plan = plan_for(upload.dataset, upload.mapping)
    stage_table, target = plan.stage(upload), plan.target
    match = _match(plan.keys, "s", "t")
    if tab == "deleted":
        columns = plan.all_columns
        query = sql.SQL(
            "SELECT {cols} FROM {target} t WHERE {scope} AND NOT EXISTS "
            "(SELECT 1 FROM {stage} s WHERE {match}) ORDER BY {order} LIMIT %s"
        ).format(
            cols=sql.SQL(", ").join(sql.Identifier("t", c.name) for c in columns),
            target=target,
            scope=plan.in_scope(upload, "t"),
            stage=stage_table,
            match=match,
            order=sql.SQL(", ").join(sql.Identifier("t", c.name) for c in plan.keys),
        )
        rows = [
            dict(zip((c.name for c in columns), row, strict=True))
            for row in _run(query, (PREVIEW_ROWS,))
        ]
        return columns, rows, upload.deleted
    if tab == "changed":
        pairs = sql.SQL(", ").join(
            sql.SQL("t.{0}, s.{0}").format(sql.Identifier(c.name)) for c in plan.values
        )
        keys = sql.SQL(", ").join(sql.Identifier("t", c.name) for c in plan.keys)
        found = _run(
            sql.SQL(
                "SELECT {keys}{comma}{pairs} FROM {stage} s JOIN {target} t ON {match} "
                "WHERE {differs} ORDER BY s._row_no LIMIT %s"
            ).format(
                keys=keys,
                comma=sql.SQL(", ") if plan.values else sql.SQL(""),
                pairs=pairs,
                stage=stage_table,
                target=target,
                match=match,
                differs=plan.differs(),
            ),
            (PREVIEW_ROWS,),
        )
        rows, touched = [], set()
        for record in found:
            row = {c.name: value for c, value in zip(plan.keys, record, strict=False)}
            rest = record[len(plan.keys) :]
            for index, column in enumerate(plan.values):
                old, new = rest[index * 2], rest[index * 2 + 1]
                if old != new:
                    row[column.name] = f"{display(old)} → {display(new)}"
                    touched.add(column.name)
                else:
                    row[column.name] = ""
            rows.append(row)
        columns = plan.keys + [c for c in plan.values if c.name in touched]
        return columns, rows, upload.changed
    query = sql.SQL(
        "SELECT {cols} FROM {stage} s WHERE NOT EXISTS (SELECT 1 FROM {target} t WHERE {match}) "
        "ORDER BY s._row_no LIMIT %s"
    ).format(
        cols=sql.SQL(", ").join(sql.Identifier("s", c.name) for c in plan.mapped),
        stage=stage_table,
        target=target,
        match=match,
    )
    rows = [
        dict(zip((c.name for c in plan.mapped), row, strict=True))
        for row in _run(query, (PREVIEW_ROWS,))
    ]
    return plan.mapped, rows, upload.added


def display(value) -> str:
    """A saved value as the user would type it: 1500.50, 01-04-2026, Yes, (blank)."""
    if value is None or value == "":
        return "(blank)"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if hasattr(value, "strftime"):
        if hasattr(value, "hour"):
            return timezone.localtime(value).strftime("%d-%m-%Y %H:%M")
        return value.strftime("%d-%m-%Y")
    return str(value)


# ---------- confirm / cancel ----------


def _stale_reason(upload: Upload) -> str:
    dataset = Dataset.objects.get(pk=upload.dataset_id)
    previous = latest_done(dataset)
    if (previous.id if previous else None) != upload.base_upload_id:
        return f"{dataset.name} was changed by another upload after this preview."
    if dataset.updated_at != upload.definition_at:
        return f"{dataset.name}'s definition was changed after this preview."
    return ""


def confirm(upload: Upload, user: str, delete_ticked: bool) -> None:
    if upload.status != Upload.PREVIEW:
        raise UploadProblems([problem("This upload is no longer waiting for confirm.")])
    if upload.needs_delete_tick and not delete_ticked:
        raise UploadProblems(
            [
                problem(
                    f"Tick “Yes, delete {upload.deleted:,} rows” to confirm "
                    f"this {upload.get_mode_display()}."
                )
            ]
        )
    reason = _stale_reason(upload)
    if reason:  # back to the mapping, so one click on "Next" gives a fresh preview
        logger.warning("Upload %s refused at confirm: %s", upload.id, reason)
        reopen(upload)
        raise UploadProblems([problem(f"{reason} Press “Next: preview” for a fresh preview.")])
    plan = plan_for(upload.dataset, upload.mapping)
    stage_table, target = plan.stage(upload), plan.target
    match = _match(plan.keys, "s", "t")
    not_in_file = sql.SQL("{} AND NOT EXISTS (SELECT 1 FROM {} s WHERE {})").format(
        plan.in_scope(upload, "t"), stage_table, match
    )
    not_saved = sql.SQL("NOT EXISTS (SELECT 1 FROM {} t WHERE {})").format(target, match)
    log = sql.SQL("INSERT INTO {} (upload_id, dataset_id, action, key, old, new) ").format(
        _ident(plan.dataset, CHANGES)
    )
    ids = (upload.id, plan.dataset.id)
    try:
        with transaction.atomic():
            _lock(plan.dataset)
            reason = _stale_reason(upload)  # again, now that no one else can upload
            if reason:
                logger.warning("Upload %s refused at confirm: %s", upload.id, reason)
                raise UploadProblems([problem(f"{reason} Upload the file again.")])
            full = _replaces(upload.mode)
            if full:
                _run(
                    log
                    + sql.SQL(
                        "SELECT %s, %s, 'deleted', {}, to_jsonb(t), NULL FROM {} t WHERE {}"
                    ).format(_json(plan.keys, "t"), target, not_in_file),
                    ids,
                )
            if plan.values:
                _run(
                    log + sql.SQL(
                        "SELECT %s, %s, 'changed', {}, to_jsonb(t), {} FROM {} t JOIN {} s ON {} WHERE {}"
                    ).format(
                        _json(plan.keys, "t"), _json(plan.values, "s"), target, stage_table, match,
                        plan.differs(),
                    ),
                    ids,
                )  # fmt: skip
            _run(
                log
                + sql.SQL("SELECT %s, %s, 'added', {}, NULL, {} FROM {} s WHERE {}").format(
                    _json(plan.keys, "s"), _json(plan.mapped, "s"), stage_table, not_saved
                ),
                ids,
            )
            if full:
                _run(sql.SQL("DELETE FROM {} t WHERE {}").format(target, not_in_file))
            if plan.values:
                _run(
                    sql.SQL(
                        "UPDATE {target} t SET {sets}, _batch_id = %s, _updated_at = now() "
                        "FROM {stage} s WHERE {match} AND {differs}"
                    ).format(
                        target=target,
                        sets=sql.SQL(", ").join(
                            sql.SQL("{0} = s.{0}").format(sql.Identifier(c.name))
                            for c in plan.values
                        ),
                        stage=stage_table,
                        match=match,
                        differs=plan.differs(),
                    ),
                    (upload.id,),
                )
            names = sql.SQL(", ").join(sql.Identifier(c.name) for c in plan.mapped)
            _run(
                sql.SQL(
                    "INSERT INTO {target} ({names}, _batch_id) SELECT {s_names}, %s FROM {stage} s "
                    "WHERE {not_saved}"
                ).format(
                    target=target,
                    names=names,
                    s_names=sql.SQL(", ").join(sql.Identifier("s", c.name) for c in plan.mapped),
                    stage=stage_table,
                    not_saved=not_saved,
                ),
                (upload.id,),
            )
            _run(sql.SQL("DROP TABLE {}").format(stage_table))
            upload.status = Upload.DONE
            upload.user = user or upload.user
            upload.confirmed_at = timezone.now()
            upload.save()
    except IntegrityError as error:
        logger.warning("Upload %s refused by the database: %s", upload.id, error)
        detail = getattr(getattr(error, "__cause__", None), "diag", None)
        text = getattr(detail, "message_detail", None) or str(error).splitlines()[0]
        raise UploadProblems(
            [problem(f"The database refused the change: {text} Upload the file again.")]
        ) from error
    _remove_file(upload)
    logger.info("Upload %s into %s confirmed by %s", upload.id, plan.dataset.name, user)


def reopen(upload: Upload) -> None:
    """Back from the preview to the mapping: the stage table goes, the file stays."""
    _run(sql.SQL("DROP TABLE IF EXISTS {}").format(_ident(upload.dataset, upload.stage_table)))
    upload.status = Upload.MAPPING
    upload.save(update_fields=["status"])


def cancel(upload: Upload) -> None:
    if upload.status in (Upload.MAPPING, Upload.PREVIEW):
        _run(sql.SQL("DROP TABLE IF EXISTS {}").format(_ident(upload.dataset, upload.stage_table)))
        upload.status = Upload.CANCELLED
        upload.save(update_fields=["status"])
    _remove_file(upload)


# ---------- undo ----------


def undo_problems(upload: Upload) -> list[str]:
    """Why the upload can't be undone ([] = it can)."""
    dataset = upload.dataset
    if upload.status != Upload.DONE:
        return ["Only a saved upload can be undone."]
    if latest_done(dataset) != upload:
        return ["Only the latest upload of a dataset can be undone. Undo the newer ones first."]
    if dataset.updated_at > upload.confirmed_at:
        changed = (
            f"{dataset.name}'s definition was changed after this upload, so its old values "
            "can't be put back safely."
        )
        return [changed]
    found = []
    keys = dataset.key_columns()
    if upload.added and len(keys) == 1:
        key = keys[0]
        state = TableState.of(dataset)
        for link in dataset.linked_from.select_related("dataset"):
            other = link.dataset
            uses, example = _run(
                sql.SQL(
                    "SELECT count(*), min(o.{link}::text) FROM {other} o JOIN {changes} c "
                    "ON c.upload_id = %s AND c.action = 'added' "
                    "CROSS JOIN LATERAL jsonb_populate_record(NULL::{target}, c.key) r "
                    "WHERE o.{link} = r.{key}"
                ).format(
                    link=sql.Identifier(link.name),
                    other=sql.Identifier(other.schema, other.table_name),
                    changes=_ident(dataset, CHANGES),
                    target=state.ident,
                    key=sql.Identifier(key.name),
                ),
                (upload.id,),
            )[0]
            if uses:
                found.append(
                    f"{other.name} now uses rows this upload added (e.g. “{example}”, "
                    f"{uses:,} row(s)). Remove those {other.name} rows first, or keep this upload."
                )
    return found


def undo(upload: Upload, user: str) -> None:
    dataset = upload.dataset
    state = TableState.of(dataset)
    changes = _ident(dataset, CHANGES)
    keys = ([AS_OF_SPEC] if state.is_snapshot else []) + [c for c in state.columns if c.is_key]
    by_key = _match(keys, "t", "r")
    record = sql.SQL("NULL::{}").format(state.ident)
    all_names = ([AS_OF] if state.is_snapshot else []) + [c.name for c in state.columns]
    all_names += ["_batch_id", "_updated_at"]
    try:
        with transaction.atomic():
            _lock(dataset)
            problems = undo_problems(upload)
            if problems:
                raise UploadProblems([problem(text) for text in problems])
            _run(
                sql.SQL(
                    "DELETE FROM {t} t USING {c} c CROSS JOIN LATERAL "
                    "jsonb_populate_record({rec}, c.key) r "
                    "WHERE c.upload_id = %s AND c.action = 'added' AND {by_key}"
                ).format(t=state.ident, c=changes, rec=record, by_key=by_key),
                (upload.id,),
            )
            _run(
                sql.SQL(
                    "UPDATE {t} t SET ({names}) = ({r_names}) FROM {c} c CROSS JOIN LATERAL "
                    "jsonb_populate_record({rec}, c.old) r "
                    "WHERE c.upload_id = %s AND c.action = 'changed' AND {by_key}"
                ).format(
                    t=state.ident,
                    names=sql.SQL(", ").join(map(sql.Identifier, all_names)),
                    r_names=sql.SQL(", ").join(sql.Identifier("r", name) for name in all_names),
                    c=changes,
                    rec=record,
                    by_key=by_key,
                ),
                (upload.id,),
            )
            _run(
                sql.SQL(
                    "INSERT INTO {t} ({names}) SELECT {r_names} FROM {c} c CROSS JOIN LATERAL "
                    "jsonb_populate_record({rec}, c.old) r "
                    "WHERE c.upload_id = %s AND c.action = 'deleted'"
                ).format(
                    t=state.ident,
                    names=sql.SQL(", ").join(map(sql.Identifier, all_names)),
                    r_names=sql.SQL(", ").join(sql.Identifier("r", name) for name in all_names),
                    c=changes,
                    rec=record,
                ),
                (upload.id,),
            )
            upload.status = Upload.UNDONE
            upload.undone_at = timezone.now()
            upload.undone_by = user or ""
            upload.save(update_fields=["status", "undone_at", "undone_by"])
    except IntegrityError as error:
        logger.warning("Undo of upload %s refused by the database: %s", upload.id, error)
        raise UploadProblems(
            [
                problem(
                    "The old rows can't be put back: they clash with data saved since "
                    f"({str(error).splitlines()[0]})."
                )
            ]
        ) from error
    logger.info("Upload %s of %s undone by %s", upload.id, dataset.name, user)


# ---------- periodic reports: dates loaded, remove a date ----------


def loaded_dates(dataset: Dataset) -> list[tuple[date, int]]:
    """(As-of date, rows), oldest first."""
    if not dataset.is_snapshot or not table_exists(dataset.schema, dataset.table_name):
        return []
    return _run(
        sql.SQL("SELECT {0}, count(*) FROM {1} GROUP BY 1 ORDER BY 1").format(
            sql.Identifier(AS_OF), sql.Identifier(dataset.schema, dataset.table_name)
        )
    )


def rows_on(dataset: Dataset, day: date) -> int:
    """Rows saved for one As-of date."""
    if not dataset.is_snapshot or not table_exists(dataset.schema, dataset.table_name):
        return 0
    return _run(
        sql.SQL("SELECT count(*) FROM {} WHERE {} = %s").format(
            sql.Identifier(dataset.schema, dataset.table_name), sql.Identifier(AS_OF)
        ),
        (day,),
    )[0][0]


def last_uploads(dataset: Dataset) -> dict[date, Upload]:
    """Per As-of date, the latest saved upload that loaded or replaced it."""
    found: dict[date, Upload] = {}
    for item in dataset.uploads.filter(status=Upload.DONE, mode=Upload.DATES).order_by(
        "confirmed_at", "id"
    ):
        for entry in item.dates:
            found[date.fromisoformat(entry["date"])] = item  # later ones win
    return found


def picked_date(text: str | None) -> date | None:
    """The As-of date box (2026-03-31), or None when it is empty. Raises UploadProblems for a
    date that isn't one, or is in the future."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError as error:
        logger.warning("As-of date refused: %r", text[:20])
        raise UploadProblems([problem(f"“{text[:20]}” isn't a date. Pick one.")]) from error
    if day > timezone.localdate():
        logger.warning("As-of date refused: %s is in the future", day)
        raise UploadProblems(
            [problem(f"{day:%d-%m-%Y} is in the future. Pick today or an earlier date.")]
        )
    return day


def remove_date(dataset: Dataset, day: date, user: str) -> Upload:
    """Remove every row of one As-of date. It is kept as an upload (mode "Remove a date") with
    a change log, so the history shows it and Undo puts the rows back."""
    state = TableState.of(dataset)
    keys = [AS_OF_SPEC] + [column for column in state.columns if column.is_key]
    shown = day.strftime("%d-%m-%Y")
    same_day = sql.SQL("{} = %s").format(sql.Identifier("t", AS_OF))
    try:
        with transaction.atomic():
            _lock(dataset)
            _ensure_changes_table(dataset)
            rows = rows_on(dataset, day)
            if not rows:
                logger.warning("Removing %s from %s refused: no rows", shown, dataset.name)
                raise UploadProblems([problem(f"{dataset.name} has no rows for {shown}.")])
            previous = latest_done(dataset)
            upload = Upload.objects.create(
                dataset=dataset,
                user=user,
                file_name=f"Removed {shown}",
                mode=Upload.REMOVE_DATE,
                status=Upload.DONE,
                as_of_date=day,
                deleted=rows,
                existing=rows,
                dates=[
                    {
                        **dict.fromkeys(("added", "changed"), 0),
                        "date": day.isoformat(),
                        "deleted": rows,
                        "existing": rows,
                    }
                ],
                base_upload_id=previous.id if previous else None,
                definition_at=dataset.updated_at,
                confirmed_at=timezone.now(),
            )
            _run(
                sql.SQL(
                    "INSERT INTO {changes} (upload_id, dataset_id, action, key, old, new) "
                    "SELECT %s, %s, 'deleted', {key}, to_jsonb(t), NULL FROM {target} t WHERE {day}"
                ).format(
                    changes=_ident(dataset, CHANGES),
                    key=_json(keys, "t"),
                    target=state.ident,
                    day=same_day,
                ),
                (upload.id, dataset.id, day),
            )
            _run(sql.SQL("DELETE FROM {} t WHERE {}").format(state.ident, same_day), (day,))
    except IntegrityError as error:
        logger.warning("Removing %s from %s refused: %s", shown, dataset.name, error)
        raise UploadProblems(
            [problem(f"{shown} can't be removed: other data still uses these rows.")]
        ) from error
    logger.info("Removed %s (%s rows) from %s by %s", shown, rows, dataset.name, user)
    return upload


def forget_dataset(dataset: Dataset) -> None:
    """Before a dataset is deleted: its change log rows and unfinished stage tables."""
    for upload in dataset.uploads.filter(status__in=[Upload.MAPPING, Upload.PREVIEW]):
        cancel(upload)
    changes = _ident(dataset, CHANGES)
    if _run(sql.SQL("SELECT to_regclass(%s)"), (f'"{dataset.schema}"."{CHANGES}"',))[0][0]:
        _run(sql.SQL("DELETE FROM {} WHERE dataset_id = %s").format(changes), (dataset.id,))
