"""Definition -> a real PostgreSQL table in the company's schema, and safe changes to it later.

Example: Sales (key voucher_no + line_no, account_code links to Chart of Accounts) becomes

    CREATE TABLE co_acme.sales (
        voucher_no text NOT NULL, line_no bigint NOT NULL, account_code text, amount numeric(18,2),
        _batch_id bigint, _updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT fd_pk_7 PRIMARY KEY (voucher_no, line_no),
        CONSTRAINT fd_fk_31 FOREIGN KEY (account_code) REFERENCES co_acme.chart_of_accounts (account_code))

Names always go through psycopg's sql.Identifier, never string joining. Constraint names use the
column's id (fd_fk_31), so renaming a column never breaks them. Every function runs inside the
caller's transaction: PostgreSQL undoes table changes too when something fails.
"""

import logging
from dataclasses import dataclass

from django.db import connection
from psycopg import sql

from .models import Dataset, DatasetColumn

logger = logging.getLogger(__name__)

AS_OF = "_as_of_date"  # periodic reports: part of the key
SYSTEM_COLUMNS = ("_batch_id", "_updated_at", AS_OF)


class SchemaBlocked(Exception):
    """The change can't be made to a table with data. `problems` are shown to the user."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class ColumnSpec:
    id: int
    name: str
    label: str
    data_type: str
    decimal_places: int
    is_key: bool
    required: bool
    unique: bool
    references_id: int | None

    @property
    def pg_type(self) -> str:
        """PostgreSQL type, e.g. numeric(18,2). Built only from fixed choices and an int."""
        if self.data_type == DatasetColumn.DECIMAL:
            return f"numeric(18,{int(self.decimal_places)})"
        return {
            DatasetColumn.TEXT: "text",
            DatasetColumn.INTEGER: "bigint",
            DatasetColumn.DATE: "date",
            DatasetColumn.DATETIME: "timestamptz",
            DatasetColumn.BOOLEAN: "boolean",
        }[self.data_type]

    @property
    def not_null(self) -> bool:
        return self.required or self.is_key

    @property
    def type_label(self) -> str:
        label = dict(DatasetColumn.TYPE_CHOICES)[self.data_type]
        if self.data_type == DatasetColumn.DECIMAL:
            label += f" ({self.decimal_places} places)"
        return label


@dataclass(frozen=True)
class TableState:
    """What a dataset's table looks like, captured before an edit to compare with after."""

    dataset_id: int
    schema: str
    table_name: str
    kind: str
    columns: tuple[ColumnSpec, ...]

    @classmethod
    def of(cls, dataset: Dataset) -> "TableState":
        columns = tuple(
            ColumnSpec(
                id=column.id,
                name=column.name,
                label=column.label,
                data_type=column.data_type,
                decimal_places=column.decimal_places,
                is_key=column.is_key,
                required=column.required,
                unique=column.unique,
                references_id=column.references_id,
            )
            for column in dataset.columns.order_by("position", "id")
        )
        return cls(dataset.id, dataset.schema, dataset.table_name, dataset.kind, columns)

    @property
    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.schema, self.table_name)

    @property
    def is_snapshot(self) -> bool:
        return self.kind == Dataset.SNAPSHOT

    @property
    def key_ids(self) -> list[int]:
        return [column.id for column in self.columns if column.is_key]

    def needs_unique(self, column: ColumnSpec) -> bool:
        """A unique rule of its own, unless the column alone already is the key."""
        return column.unique and self.key_ids != [column.id]

    def scoped(self, *names: str) -> sql.Composed:
        """Column list; periodic reports add the as-of date (unique per date, not overall)."""
        parts = ([AS_OF] if self.is_snapshot else []) + list(names)
        return sql.SQL(", ").join(map(sql.Identifier, parts))


# ---------- running SQL ----------


def _run(query: sql.Composable, params: tuple = ()) -> list[tuple]:
    connection.ensure_connection()
    text = query.as_string(connection.connection)
    with connection.cursor() as cursor:
        cursor.execute(text, params or None)
        return cursor.fetchall() if cursor.description else []


def table_exists(schema: str, table_name: str) -> bool:
    return bool(_run(sql.SQL("SELECT to_regclass(%s)"), (f'"{schema}"."{table_name}"',))[0][0])


def row_count(dataset: Dataset) -> int | None:
    """Rows in the dataset's table (None when the table isn't there)."""
    if not table_exists(dataset.schema, dataset.table_name):
        return None
    ident = sql.Identifier(dataset.schema, dataset.table_name)
    return _run(sql.SQL("SELECT count(*) FROM {}").format(ident))[0][0]


def approximate_row_counts(schema: str) -> dict[str, int]:
    """Rows per table in a company's schema, from PostgreSQL's statistics: one quick query
    instead of counting every table (exact enough for a list; may lag a moment)."""
    return dict(
        _run(
            sql.SQL("SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE schemaname = %s"),
            (schema,),
        )
    )


def _constraint(prefix: str, object_id: int) -> sql.Identifier:
    return sql.Identifier(f"fd_{prefix}_{object_id}")


def _target_key(column: ColumnSpec) -> tuple[sql.Identifier, sql.Identifier, Dataset]:
    """The table and key column a link points at."""
    target = Dataset.objects.get(pk=column.references_id)
    key = target.key_columns()[0]
    return sql.Identifier(target.schema, target.table_name), sql.Identifier(key.name), target


# ---------- create / drop ----------


def create_table(state: TableState) -> None:
    parts = []
    if state.is_snapshot:
        parts.append(sql.SQL("{} date NOT NULL").format(sql.Identifier(AS_OF)))
    for column in state.columns:
        parts.append(
            sql.SQL("{} {}{}").format(
                sql.Identifier(column.name),
                sql.SQL(column.pg_type),
                sql.SQL(" NOT NULL" if column.not_null else ""),
            )
        )
    parts.append(sql.SQL("_batch_id bigint"))
    parts.append(sql.SQL("_updated_at timestamptz NOT NULL DEFAULT now()"))
    keys = [column.name for column in state.columns if column.is_key]
    parts.append(
        sql.SQL("CONSTRAINT {} PRIMARY KEY ({})").format(
            _constraint("pk", state.dataset_id), state.scoped(*keys)
        )
    )
    for column in state.columns:
        if state.needs_unique(column):
            parts.append(
                sql.SQL("CONSTRAINT {} UNIQUE ({})").format(
                    _constraint("uq", column.id), state.scoped(column.name)
                )
            )
        if column.references_id:
            target_table, target_key, _ = _target_key(column)
            parts.append(
                sql.SQL("CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({})").format(
                    _constraint("fk", column.id),
                    sql.Identifier(column.name),
                    target_table,
                    target_key,
                )
            )
    _run(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(state.schema)))
    _run(sql.SQL("CREATE TABLE {} ({})").format(state.ident, sql.SQL(", ").join(parts)))
    add_case_rules(state, check=False)
    logger.info("Created table %s.%s", state.schema, state.table_name)


def drop_table(state: TableState) -> None:
    """Drop the table. Callers check first that no other dataset links to it."""
    _run(sql.SQL("DROP TABLE IF EXISTS {}").format(state.ident))
    logger.info("Dropped table %s.%s", state.schema, state.table_name)


# ---------- changing a definition ----------


def apply_changes(dataset: Dataset, before: TableState | None) -> None:
    """Make the table match the (already saved) definition. `before` = None for a new dataset.

    An empty table that no other dataset links to is simply rebuilt. A table with rows is
    changed step by step; risky steps are checked against the data first, and every problem
    found is reported together (SchemaBlocked), so the user can fix them in one go.
    """
    after = TableState.of(dataset)
    if before is None or not table_exists(before.schema, before.table_name):
        create_table(after)
        return
    rows = _run(sql.SQL("SELECT count(*) FROM {}").format(before.ident))[0][0]
    linking = sorted(
        {column.dataset.name for column in dataset.linked_from.select_related("dataset")}
    )
    if rows == 0 and not linking:
        drop_table(before)
        create_table(after)
        return
    _alter(before, after, rows, linking)


def _alter(before: TableState, after: TableState, rows: int, linking: list[str]) -> None:
    problems: list[str] = []
    if before.kind != after.kind:
        reason = f"the table has {rows:,} rows" if rows else f"{', '.join(linking)} link(s) to it"
        raise SchemaBlocked([f"The kind can't change while {reason}. Keep it as it was."])
    old = {column.id: column for column in before.columns}
    new = {column.id: column for column in after.columns}
    old_keys = [old[i] for i in before.key_ids]
    new_keys = [new[i] for i in after.key_ids]
    key_changed = [c.id for c in old_keys] != [c.id for c in new_keys] or any(
        new.get(c.id) and new[c.id].pg_type != c.pg_type for c in old_keys
    )
    if key_changed and linking:
        raise SchemaBlocked(
            [
                (
                    f"{', '.join(linking)} link(s) to this dataset's key, so the key can't "
                    "change. Remove those links first."
                )
            ]
        )
    if before.table_name != after.table_name:
        _run(
            sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                before.ident, sql.Identifier(after.table_name)
            )
        )
    table = after.ident
    # Rebuilt at the end: a type change (text -> number) can't keep an index on lower(column).
    _drop_case_rules(after)

    def alter(clause: sql.Composable) -> None:
        _run(sql.SQL("ALTER TABLE {} ").format(table) + clause)

    # 1. Remove rules that are going away or will be rebuilt.
    for column_id, column in old.items():
        changed = new.get(column_id)
        if column.references_id and (
            changed is None
            or changed.references_id != column.references_id
            or changed.pg_type != column.pg_type
        ):
            alter(sql.SQL("DROP CONSTRAINT IF EXISTS {}").format(_constraint("fk", column_id)))
        if before.needs_unique(column) and (changed is None or not after.needs_unique(changed)):
            alter(sql.SQL("DROP CONSTRAINT IF EXISTS {}").format(_constraint("uq", column_id)))
    if key_changed:
        alter(sql.SQL("DROP CONSTRAINT {}").format(_constraint("pk", after.dataset_id)))

    # 2. Removed and renamed columns.
    for column_id, column in old.items():
        if column_id not in new:
            alter(sql.SQL("DROP COLUMN {}").format(sql.Identifier(column.name)))
    # Through a temporary name, so two columns can swap names (debit <-> credit).
    renamed = [i for i, column in new.items() if i in old and old[i].name != column.name]
    for column_id in renamed:
        alter(
            sql.SQL("RENAME COLUMN {} TO {}").format(
                sql.Identifier(old[column_id].name), sql.Identifier(f"_fd_rename_{column_id}")
            )
        )
    for column_id in renamed:
        alter(
            sql.SQL("RENAME COLUMN {} TO {}").format(
                sql.Identifier(f"_fd_rename_{column_id}"), sql.Identifier(new[column_id].name)
            )
        )

    # 3. Type changes: only when every filled value converts.
    for column_id, column in new.items():
        if column_id in old and old[column_id].pg_type != column.pg_type:
            name = sql.Identifier(column.name)
            bad, example = _run(
                sql.SQL(
                    "SELECT count(*), min({0}::text) FROM {1} "
                    "WHERE {0} IS NOT NULL AND NOT pg_input_is_valid({0}::text, %s)"
                ).format(name, table),
                (column.pg_type,),
            )[0]
            if bad:
                problems.append(
                    f"“{column.label}”: {bad:,} row(s) can't become {column.type_label}, "
                    f"e.g. “{example}”."
                )
            else:
                alter(
                    sql.SQL("ALTER COLUMN {0} TYPE {1} USING {0}::text::{1}").format(
                        name, sql.SQL(column.pg_type)
                    )
                )

    # 4. New columns (empty in every existing row).
    for column_id, column in new.items():
        if column_id not in old:
            alter(
                sql.SQL("ADD COLUMN {} {}").format(
                    sql.Identifier(column.name), sql.SQL(column.pg_type)
                )
            )
            if column.not_null and rows == 0:
                alter(sql.SQL("ALTER COLUMN {} SET NOT NULL").format(sql.Identifier(column.name)))
            elif column.not_null:
                problems.append(
                    f"“{column.label}” is new and {'a key' if column.is_key else 'Required'}, "
                    f"but the {rows:,} rows already saved would have it blank. Add it without "
                    "Key / Required first, fill it with an upload, then tick it."
                )

    # 5. Required on / off.
    for column_id, column in new.items():
        was = old.get(column_id)
        if was is None:
            continue
        name = sql.Identifier(column.name)
        if column.not_null and not was.not_null:
            blanks = _run(sql.SQL("SELECT count(*) FROM {} WHERE {} IS NULL").format(table, name))[
                0
            ][0]
            if blanks:
                problems.append(
                    f"“{column.label}” is blank in {blanks:,} row(s), so it can't be required yet."
                )
            else:
                alter(sql.SQL("ALTER COLUMN {} SET NOT NULL").format(name))
        elif was.not_null and not column.not_null:
            alter(sql.SQL("ALTER COLUMN {} DROP NOT NULL").format(name))

    # 6. Unique rules and the key.
    for column_id, column in new.items():
        was = old.get(column_id)
        if after.needs_unique(column) and not (was and before.needs_unique(was)):
            duplicate = _first_duplicate(after, column.name)
            if duplicate:
                problems.append(
                    f"“{column.label}” can't be unique: “{duplicate[0]}” appears {duplicate[1]:,} times."
                )
            else:
                alter(
                    sql.SQL("ADD CONSTRAINT {} UNIQUE ({})").format(
                        _constraint("uq", column_id), after.scoped(column.name)
                    )
                )
    if key_changed:
        names = [column.name for column in new_keys]
        duplicate = _first_duplicate(after, *names)
        if duplicate:
            labels = " + ".join(column.label for column in new_keys)
            problems.append(
                f"The new key ({labels}) is not unique: “{duplicate[0]}” appears {duplicate[1]:,} times."
            )
        else:
            alter(
                sql.SQL("ADD CONSTRAINT {} PRIMARY KEY ({})").format(
                    _constraint("pk", after.dataset_id), after.scoped(*names)
                )
            )

    # 7. Links: every filled value must exist in the linked dataset.
    for column_id, column in new.items():
        was = old.get(column_id)
        if not column.references_id or (
            was and was.references_id == column.references_id and was.pg_type == column.pg_type
        ):
            continue
        target_table, target_key, target = _target_key(column)
        name = sql.Identifier(column.name)
        missing, example = _run(
            sql.SQL(
                "SELECT count(*), min(t.{0}::text) FROM {1} t WHERE t.{0} IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM {2} x WHERE x.{3} = t.{0})"
            ).format(name, table, target_table, target_key)
        )[0]
        if missing:
            problems.append(
                f"“{column.label}”: {missing:,} row(s) have values not in {target.name}, "
                f"e.g. “{example}”. Add them to {target.name} first."
            )
        else:
            alter(
                sql.SQL("ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({})").format(
                    _constraint("fk", column_id), name, target_table, target_key
                )
            )

    problems.extend(add_case_rules(after, check=True))
    if problems:
        logger.warning("Change to %s blocked: %s", after.table_name, problems)
        raise SchemaBlocked(problems)


def _first_duplicate(state: TableState, *names: str) -> tuple[str, int] | None:
    """The first value (or combination) that appears more than once, and how often.
    Blanks don't count: a unique rule allows many blank rows."""
    shown = sql.SQL(" || ' + ' || ").join(
        sql.SQL("{}::text").format(sql.Identifier(name)) for name in names
    )
    filled = sql.SQL(" AND ").join(
        sql.SQL("{} IS NOT NULL").format(sql.Identifier(name)) for name in names
    )
    found = _run(
        sql.SQL(
            "SELECT {shown}, count(*) FROM {table} WHERE {filled} GROUP BY {scope} "
            "HAVING count(*) > 1 ORDER BY 2 DESC LIMIT 1"
        ).format(shown=shown, table=state.ident, filled=filled, scope=state.scoped(*names))
    )
    return (found[0][0], found[0][1]) if found else None


# ---------- keys that ignore capitals (ab = AB) ----------


def match_expr(column, alias: str = "") -> sql.Composable:
    """How a key / unique value is compared: text without capitals, others as they are."""
    name = sql.Identifier(alias, column.name) if alias else sql.Identifier(column.name)
    return sql.SQL("lower({})").format(name) if column.data_type == DatasetColumn.TEXT else name


def _case_rules(state: TableState) -> list[tuple[str, str, list[ColumnSpec]]]:
    """(index name, label, columns) for the key and every Unique text column."""
    rules = []
    keys = [column for column in state.columns if column.is_key]
    if any(column.data_type == DatasetColumn.TEXT for column in keys):
        labels = " + ".join(column.label for column in keys)
        rules.append((f"fd_ci_pk_{state.dataset_id}", labels, keys))
    for column in state.columns:
        if state.needs_unique(column) and column.data_type == DatasetColumn.TEXT:
            rules.append((f"fd_ci_uq_{column.id}", column.label, [column]))
    return rules


def _drop_case_rules(state: TableState) -> None:
    found = _run(
        sql.SQL(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = %s "
            "AND left(indexname, 6) = 'fd_ci_'"
        ),
        (state.schema, state.table_name),
    )
    for (index_name,) in found:
        _run(sql.SQL("DROP INDEX {}").format(sql.Identifier(state.schema, index_name)))


def ensure_case_rules(state: TableState) -> list[str]:
    """Tables made before keys ignored capitals get their indexes now; look-alikes already
    saved ("AB" and "ab") are returned as problems instead."""
    wanted = {index_name for index_name, _, _ in _case_rules(state)}
    have = {
        row[0]
        for row in _run(
            sql.SQL("SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = %s"),
            (state.schema, state.table_name),
        )
    }
    if wanted <= have:
        return []
    _drop_case_rules(state)
    return add_case_rules(state, check=True)


def add_case_rules(state: TableState, check: bool) -> list[str]:
    """Unique indexes on lower(text), so "AB" and "ab" can't both be saved.
    With `check`, existing look-alikes are reported instead of failing."""
    problems = []
    as_of = [sql.Identifier(AS_OF)] if state.is_snapshot else []
    for index_name, label, columns in _case_rules(state):
        exprs = sql.SQL(", ").join(as_of + [match_expr(column) for column in columns])
        if check:
            shown = sql.SQL(" || ' + ' || ").join(
                sql.SQL("{}::text").format(sql.Identifier(column.name)) for column in columns
            )
            filled = sql.SQL(" AND ").join(
                sql.SQL("{} IS NOT NULL").format(sql.Identifier(column.name)) for column in columns
            )
            found = _run(
                sql.SQL(
                    "SELECT string_agg(DISTINCT {shown}, ' / '), count(DISTINCT {shown}) "
                    "FROM {table} WHERE {filled} GROUP BY {exprs} HAVING count(*) > 1 "
                    "ORDER BY 2 DESC LIMIT 1"
                ).format(shown=shown, table=state.ident, filled=filled, exprs=exprs)
            )
            if found:  # exact repeats are reported by the unique / key check already
                if found[0][1] > 1:
                    problems.append(
                        f"“{label}” has values that differ only in capitals: {found[0][0]}. "
                        "Keys and Unique columns ignore capitals (ab = AB), so make them the "
                        "same or clearly different first."
                    )
                continue
        _run(
            sql.SQL("CREATE UNIQUE INDEX {} ON {} ({})").format(
                sql.Identifier(index_name), state.ident, exprs
            )
        )
    return problems
