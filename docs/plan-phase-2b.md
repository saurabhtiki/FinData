# Phase 2b: Pull a dataset from a SQL database

Example outcome: on ACME, staff save a connection **"Tally server"**:
- PostgreSQL at 192.168.1.20, database `erp`, user `report`;
- no password is saved.

Then the user opens **Sales** → **Upload** → **Database** tab:
1. Picks "Tally server" and types the password.
2. Picks the view `vouchers`, or writes `SELECT * FROM vouchers WHERE date >= '2026-04-01'`.
3. Goes through the same steps as a file: mapping → preview **40 new · 2 changed** → Confirm.

Next month: **Pull again** → type the password → preview → Confirm.

The Phase 2a plan is kept in `plan-phase-2a.md`.

## Decisions (from the question rounds)
| # | Topic | Decision |
|---|---|---|
| 1 | Shared code | Move My Insights' `db_connect` / `db_loading` into data-core (a new `databases` module). My Insights then uses the data-core copy; its tests must still pass. |
| 2 | Databases | Uploaded SQLite file (`.db`, `.sqlite`, `.sqlite3`), PostgreSQL server, MySQL / MariaDB server. SQL Server comes later. |
| 3 | Saved connections | Saved per company: kind, host, port, database, user, Require SSL. **Never the password**: it is typed for each pull. |
| 4 | Who | Only staff (the `CAN_EDIT_DEFINITIONS` hook) add, edit or delete connections. Anyone who can upload can use them. |
| 5 | What to read | A table or view from a list, or one SELECT (WITH … SELECT allowed). Read-only. The choice is remembered per dataset ("Pull again"). |
| 6 | Same steps | Mapping → problems → preview → Add / update or Full refresh → confirm, with the same change log and undo. |
| 7 | Values | Real numbers and dates are used as they come, not turned into text first. Text is trimmed and keys ignore capitals. A date-time with a time zone is changed to the server's time zone. |
| 8 | Limits | `SQL_MAX_ROWS` 500,000, `SQL_QUERY_SECONDS` 60, `SQL_CONNECT_SECONDS` 10 (all in `FINDATA` settings). Going past a limit gives a clear message. |
| 9 | Safety switch | Live servers are **off** by default: `ALLOW_DB_CONNECT: True` turns them on. `DB_ALLOWED_HOSTS: [...]` limits which servers (empty = any). SQLite files are always allowed. |
| 10 | Rows and history | Problems say "Result row 45 (key 4010): …". The history shows "Tally server › vouchers" or "erp.db › ledgers". The Excel list of problems works the same. |
| 11 | Screens | The Upload page has tabs **File** and **Database**. A dataset with a remembered source gets a **Pull again** button. |
| 12 | Test connection | A button that answers in plain words ("Connected. 42 tables found." / "The user name or password is wrong."). Saving doesn't require passing the test. |
| 13 | Changing the read | Uploaders may pick another table or edit the SELECT. Only staff change the connection. |
| 14 | Deleting a connection | Allowed. The screen first names the datasets that use it ("Used by Sales, Ledgers"). Their remembered source is cleared, and the history keeps the old name. |
| 15 | SQLite file | Stored like an uploaded file until Confirm or Cancel, and cleaned up after a day if left unfinished. One table or query fills one dataset. |

## Build
**data-core** (separate repo; commit and push, then update FinData's lock):
- New `data_core/databases.py`, moved from My Insights:
  - `Server` (its repr hides the password), the `Postgres` / `MySql` dialects, plain-words error messages;
  - `check_select`, `reading(server, settings)`, `list_tables`;
  - a SQLite reader that opens a file read-only.
- It reads rows in batches as **raw Python values**. My Insights keeps its text conversion (`cell_text`) on its side.
- Limits and the allowed-hosts list are passed in by the caller (no Django settings inside data-core).
- Drivers stay optional extras: `data-core[postgres]` and `data-core[mysql]`.
- Tests: `check_select`, error messages, the SQLite reader, the row limit, the host allow-list.

**My Insights**: import from `data_core.databases`, delete its own copies, run all its tests.

**FinData**:
- Models:
  - `DbConnection`: company_key, name, kind, host, port, database, user, require_ssl.
  - `Dataset.remembered_source`: connection id + table, or SELECT.
  - `Upload`: `source_kind` (file / sqlite / server), `connection`, `source_label`, `select_sql`.
- `findata/db_source.py`:
  - read a table or query into the same stage step as a file: rows → `convert()` → stage table;
  - the row number is the result row, and the key is shown in problems.
  - `upload.stage` takes rows from a file or a database (one path).
- Views and templates:
  - connections list, add, edit, delete and Test (staff only);
  - the Database tab: pick connection + password, or SQLite file → pick table / write SELECT;
  - **Pull again** on the dataset row;
  - the history shows `source_label`.
- Settings: `ALLOW_DB_CONNECT`, `DB_ALLOWED_HOSTS`, `SQL_MAX_ROWS`, `SQL_QUERY_SECONDS`, `SQL_CONNECT_SECONDS`.
- The password is only in the POST of that one request: never saved, logged or put in the session. The password box has `autocomplete="new-password"`.

## Tests
- A SQLite file → mapping → preview → confirm, then Pull again with the remembered table.
- A SELECT that tries to change data is refused. Going past the row limit gives a clear message.
- A server pull when switched off, or a host not in the list, is refused before connecting.
- A real Postgres pull: the local test database reads its own `co_` schema as the source.
- Connections: only staff manage them. Delete clears the remembered source; the history keeps the label.
- The password is not in the database, the session or the logs.
- Numbers and dates from the database stay exact. Problems name the result row and key.

## Not in this phase
Saved (encrypted) passwords, scheduled pulls, SQL Server, API sources (Phase 6).

## Known limits (after build)
- MySQL is not tested end to end here (no MySQL server on this machine). Its error messages and table quoting are tested.
- A server pull is copied into a small SQLite file ("snapshot") in UPLOAD_DIR until Confirm or Cancel, so the password is only needed once. Exact decimals and dates are kept as text there (1500.50, 2026-04-01) and read back exactly.
- My Insights still has its own copy of the database code (it does not use data-core yet; switching it is its own task).
