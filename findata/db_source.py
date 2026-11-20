"""Databases as an upload source: an uploaded SQLite file, or a saved server connection.

Example (Sales from "Tally server", SELECT * FROM vouchers):
1. `pull` connects read-only with the password typed for this pull, runs the SELECT and copies
   the result into a small SQLite file in UPLOAD_DIR (a "snapshot"). The password is gone after
   that request; the next steps (mapping, preview) read the snapshot.
2. `sheets` gives the same Sheet the file reader gives, so mapping → preview → confirm → undo
   are the same as for a file. Row numbers are the rows of the result (1 = first row).

Values keep their meaning: a Decimal 1500.50 is kept as the exact text "1500.50", a date as
2026-04-01 (never day / month guessing), a date-time with a time zone in this site's zone.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal

from data_core import databases
from data_core.databases import Limits, Server, open_sqlite, quote_sqlite
from data_core.loading import UploadRejected
from django.utils import timezone

from . import conf
from .models import Dataset, DbConnection, Upload
from .upload import Sheet, UploadProblems, file_path, problem

logger = logging.getLogger(__name__)

SNAPSHOT_TABLE = "result"
SAMPLE_ROWS = 200
INSERT_ROWS = 5_000


def limits() -> Limits:
    return Limits(
        connect_seconds=int(conf.get("SQL_CONNECT_SECONDS")),
        query_seconds=int(conf.get("SQL_QUERY_SECONDS")),
        max_rows=int(conf.get("SQL_MAX_ROWS")),
        allow_servers=bool(conf.get("ALLOW_DB_CONNECT")),
        allowed_hosts=tuple(conf.get("DB_ALLOWED_HOSTS") or ()),
    )


def servers_allowed() -> bool:
    return bool(conf.get("ALLOW_DB_CONNECT"))


def server_for(connection: DbConnection, password: str) -> Server:
    return Server(
        kind=connection.kind,
        host=connection.host,
        port=connection.port,
        database=connection.database,
        user=connection.user,
        password=password,
        require_ssl=connection.require_ssl,
    )


def plain_value(value):
    """A database value → what values.convert reads, without losing anything."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Decimal):
        return str(value)  # exact: 1500.50 stays 1500.50
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return f"(binary, {len(value):,} bytes)"
    if isinstance(value, dict | list):  # JSON / array columns
        return json.dumps(value, ensure_ascii=False, default=str)
    if value is None or isinstance(value, int | float | str):
        return value
    return str(value)  # e.g. UUID


# ---------- tables, test, pull ----------


def _rejected(text: str) -> UploadProblems:
    return UploadProblems([problem(text)])


def check_connection(connection: DbConnection, password: str) -> str:
    """ "Connected. 42 tables found." Raises UploadProblems with the plain-words reason."""
    try:
        tables = databases.list_tables(server_for(connection, password), limits())
    except UploadRejected as error:
        logger.info("Test of connection %s failed: %s", connection.name, error)
        raise _rejected(str(error)) from error
    return f"Connected. {len(tables):,} table{'s' if len(tables) != 1 else ''} found."


def server_tables(connection: DbConnection, password: str) -> list[databases.Table]:
    try:
        return databases.list_tables(server_for(connection, password), limits())
    except UploadRejected as error:
        logger.info("Listing tables of %s failed: %s", connection.name, error)
        raise _rejected(str(error)) from error


def sqlite_tables(upload: Upload) -> list[databases.Table]:
    try:
        with open_sqlite(file_path(upload), limits()) as reader:
            tables = reader.tables()
    except UploadRejected as error:
        logger.warning("Upload %s: SQLite file refused: %s", upload.id, error)
        raise _rejected(str(error)) from error
    if not tables:
        raise _rejected("The database file has no tables.")
    return tables


def select_for(
    table_name: str, own_query: str, tables: list[databases.Table] | None, quote=quote_sqlite
) -> str:
    """The SELECT to run: a picked table's, or the user's own (checked). `quote` is the
    database's own name quoting (MySQL uses `name`). Raises UploadProblems."""
    try:
        if own_query.strip():
            return databases.check_select(own_query)
    except UploadRejected as error:
        raise _rejected(str(error)) from error
    if tables is not None:
        table = next((item for item in tables if item.name == table_name), None)
        if table is None:
            raise _rejected("Pick a table, or write your own query.")
        return table.select
    if not table_name:
        raise _rejected("Pick a table, or write your own query.")
    # sales.orders → "sales"."orders" (as the pick list shows it)
    return "SELECT * FROM " + ".".join(quote(part) for part in table_name.split(".", 1))


def pull(
    dataset: Dataset, connection: DbConnection, password: str, table: str, own_query: str, user
) -> Upload:
    """Read from the server into a snapshot and start an upload (status "mapping")."""
    select = select_for(table, own_query, None, databases.DIALECTS[connection.kind].quote)
    upload = Upload.objects.create(
        dataset=dataset,
        user=user,
        file_name=connection.name[:255],
        source_kind=Upload.SERVER,
        connection=connection,
        source_table="" if own_query.strip() else table[:255],
        select_sql=select,
        sheet=f"{connection.name} › {table if not own_query.strip() else 'own query'}"[:255],
    )
    target = file_path(upload)
    try:
        with databases.reading(server_for(connection, password), limits()) as reader:
            headers, rows = reader.rows(select)
            _write_snapshot(target, headers, rows)
    except BaseException as error:  # nothing half-read is left behind
        target.unlink(missing_ok=True)
        upload.delete()
        logger.info("Pull from %s into %s failed: %s", connection.name, dataset.name, error)
        if isinstance(error, UploadRejected | sqlite3.DatabaseError):
            raise _rejected(str(error)) from error
        raise
    _remember(dataset, connection.id, upload.source_table, select if own_query.strip() else "")
    logger.info("Upload %s: pulled from %s into %s", upload.id, connection.name, dataset.name)
    return upload


def choose_sqlite_read(upload: Upload, table: str, own_query: str) -> None:
    """The table or own query of an uploaded SQLite file. Raises UploadProblems."""
    tables = sqlite_tables(upload)
    select = select_for(table, own_query, tables)
    upload.source_table = "" if own_query.strip() else table[:255]
    upload.select_sql = select
    upload.sheet = f"{upload.file_name} › {upload.source_table or 'own query'}"[:255]
    upload.save(update_fields=["source_table", "select_sql", "sheet"])
    _remember(upload.dataset, None, upload.source_table, select if own_query.strip() else "")


def _remember(dataset: Dataset, connection_id, table: str, own_query: str) -> None:
    source = {
        "kind": Upload.SERVER if connection_id else Upload.SQLITE,
        "connection": connection_id,
        "table": table,
        "select": own_query,
    }
    Dataset.objects.filter(pk=dataset.pk).update(remembered_source=source)  # updated_at stays
    dataset.remembered_source = source


def forget_connection(connection: DbConnection) -> list[str]:
    """Before a connection is deleted: datasets that pull from it forget it. Their names."""
    using = Dataset.objects.filter(
        company_key=connection.company_key, remembered_source__connection=connection.id
    )
    names = list(using.values_list("name", flat=True))
    using.update(remembered_source={})  # updated_at stays
    return names


def datasets_using(connection: DbConnection) -> list[str]:
    return list(
        Dataset.objects.filter(
            company_key=connection.company_key, remembered_source__connection=connection.id
        ).values_list("name", flat=True)
    )


# ---------- the snapshot and the sheet ----------


def _write_snapshot(target, headers: list[str], rows) -> None:
    target.unlink(missing_ok=True)
    snapshot = sqlite3.connect(target)
    try:
        names = ", ".join(quote_sqlite(header) for header in headers)
        marks = ", ".join("?" for _ in headers)
        snapshot.execute(f"CREATE TABLE {SNAPSHOT_TABLE} ({names})")
        insert = f"INSERT INTO {SNAPSHOT_TABLE} VALUES ({marks})"
        batch = []
        for row in rows:
            batch.append([plain_value(value) for value in row])
            if len(batch) >= INSERT_ROWS:
                snapshot.executemany(insert, batch)
                batch.clear()
        if batch:
            snapshot.executemany(insert, batch)
        snapshot.commit()
    finally:
        snapshot.close()


def _select(upload: Upload) -> str:
    if upload.source_kind == Upload.SERVER:
        return f"SELECT * FROM {SNAPSHOT_TABLE}"
    return upload.select_sql


@contextmanager
def sheets(upload: Upload):
    """The read result as the one Sheet of this upload. Raises UploadProblems."""
    path = file_path(upload)
    if not path.exists():
        logger.warning("Upload %s: file %s is gone", upload.id, path)
        raise _rejected("The read data is no longer here. Read it from the database again.")
    if not upload.select_sql:
        raise _rejected("Pick a table, or write your own query.")
    select = _select(upload)
    try:
        with open_sqlite(path, limits()) as reader:
            _, counted = reader.rows(f"SELECT count(*) FROM ({select})")
            [(count,)] = list(counted)  # read to the end, so the cursor closes now
            if count > limits().max_rows:
                raise UploadRejected(f"It has more than {limits().max_rows:,} rows (the limit).")
            headers, first_rows = reader.rows(f"SELECT * FROM ({select}) LIMIT {SAMPLE_ROWS}")
            samples: dict[str, list[str]] = {header: [] for header in headers}
            for row in first_rows:
                for header, value in zip(headers, row, strict=True):
                    shown = plain_value(value)
                    if shown not in (None, "") and len(samples[header]) < 3:
                        text = str(shown)[:40]
                        if text not in samples[header]:
                            samples[header].append(text)

            def read(wanted: list[str]):
                """(row number, cells of the wanted headers) for each row of the result."""
                positions = [headers.index(header) for header in wanted]
                _, all_rows = reader.rows(select)
                for row_no, row in enumerate(all_rows, start=1):
                    yield (row_no, *[plain_value(row[index]) for index in positions])

            yield [
                Sheet(
                    label=upload.sheet or upload.file_name,
                    headers=headers,
                    samples=samples,
                    row_count=count,
                    table_name="",
                    read=read,
                )
            ]
    except UploadRejected as error:
        logger.warning("Upload %s: reading the database result failed: %s", upload.id, error)
        raise _rejected(str(error)) from error
