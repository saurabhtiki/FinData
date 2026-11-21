# FinData — scope & requirements

## What it is
A reusable Django add-on that **builds and keeps a financial database per company**, for financial
controlling and analysis. Later, queries and dashboards (My Insights) read from it.

Unlike Transform Data and My Insights (data only in memory), **FinData keeps data at rest** in PostgreSQL.

```
define datasets → upload files (or a SQL database, or another app's JSON API) → map columns → check → preview changes → confirm → saved
```

## Three kinds of dataset
| Kind | Examples | How a row is matched | Upload rule |
|---|---|---|---|
| **Master** | Chart of accounts, debtors, creditors, stock items | Its **key**, e.g. `account_code` | Add / change / delete, after preview + confirm |
| **Transaction** | Sales, purchase, cash / bank | Its key, e.g. `voucher_no + line_no` | Same as master |
| **Periodic report** | Receivables, payables, daily trial balance, stock summary | **As-of date** + key | Same as-of date uploaded again = that date is replaced. Trends: day / week / month |

## Decisions
| Topic | Decision |
|---|---|
| Database | **PostgreSQL** stores the data (separate databases for development, test, production). **DuckDB** reads it for fast reports (later phase). |
| Companies & users | The **host app owns them**. FinData asks the host "which company is this user working on?" through a settings hook. |
| Company isolation | **One Postgres schema per company**: `co_acme.chart_of_accounts`, `co_acme.sales`. |
| Definitions | **Each company defines its own datasets** (columns can differ between companies). |
| Matching rows | Every dataset has a **key** (one or more columns, e.g. `voucher_no + line_no`). |
| Rows missing from a file | **User picks per upload**: *Full refresh* (missing = delete) or *Add / update only* (missing = keep). Always previewed and confirmed. |
| Key spelling | Keys, links and Unique columns match **without case** (`ab` = `AB`); the first spelling is kept. |
| Links (foreign keys) | Real database links. An upload is **blocked** with a clear message if a link is broken: "Sales row 45: account 4010 is not in Chart of Accounts. Upload Chart of Accounts first." |
| History | **Change log** (old → new value per upload) and **undo the last upload**. |
| Changing a definition that has data | **Safe changes only.** Add a column, rename, relax a rule: allowed. Change a type, the key, or make a column required: only if the existing data passes a check first, otherwise the screen names the rows that fail. |
| Volume | Under 1 million rows per company per year. |
| Money | Exact decimals (`numeric`), never floating point. |

## Defining a dataset (simple UI)
- **From a sample file** (main path): drop a CSV / XLSX. The app reads the headers, guesses each type
  (`Amount` → Decimal, `Date` → Date, `Qty` → Whole number) and suggests a key (a column with no blanks
  and no repeats). The sample's rows are not saved.
- **Copy from another company**, or start blank.
- One grid, one row per column: `Column name | Type | Key | Required | Unique | Links to | Help text`.
  The user types a label ("Ledger Name"), and the app makes the database name `ledger_name`.
- Who can define: host hook (default: Django staff users). Everyone else can only upload.
- Download a blank Excel template for any dataset.

## Uploading (phase 2+)
- Sources: CSV / XLSX / JSON. Also read-only SQL: a SQLite `.db` file, or a live PostgreSQL / MySQL server.
- **Column mapping**: file column → dataset column, e.g. `ledg_name` → `Ledger name`. Guessed by name,
  fixed by the user, **remembered** per dataset + source, so next month's file maps itself.
- Preview: "12 new · 3 changed (old → new) · 2 will be deleted", then **Confirm**.

## Settings (host `settings.py`)
```python
FINDATA = {
    "BASE_TEMPLATE": "base.html",  # htmx 2, Alpine 3, DaisyUI 5, django-datatable
    "GET_COMPANY": "myapp.hooks.current_company",  # request -> ("acme", "ACME Ltd") or None
    "LIST_COMPANIES": "myapp.hooks.my_companies",  # optional: request -> [("acme", "ACME Ltd"), ...]
    "CAN_EDIT_DEFINITIONS": None,  # optional: request -> bool (default: staff users)
}
```

## Roadmap
1. Foundation + define datasets.
2. Upload masters & transactions.
   - 2a: files, column mapping, checks, preview, confirm, change log, undo.
   - 2b: SQL sources (done).
3. Periodic reports: as-of date uploads, "dates loaded" calendar (done).
4. Browse data, upload history, change log viewer (done).
5. Reporting bridge (skipped: use My Insights → From database; plan kept in `plan-phase-5.md`).
6. Pull rows from another application's JSON API (GET / POST, Fetch again) (done). The earlier "API keys to push data in" idea was dropped.

Every phase: plan → questions (grill-me) → user confirms → build → test.
