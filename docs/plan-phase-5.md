# Phase 5: Reporting bridge (FinData → My Insights) — SKIPPED

> Skipped on 20-11-2026: My Insights → **From database** already reads FinData's tables. Kept in case it is wanted later.
> If used, give each company a read-only login (e.g. `acme_reader` for `co_acme`) and set `RECIPE_KEEP_SECRETS = False`.

Example outcome for ACME:
1. On the **Sales** page the user clicks **Analyse in My Insights**. My Insights opens the **From FinData** box:
   Sales is ticked, and so are Chart of Accounts and Customers (Sales links to them). Sales has a date range box
   (01-04-2026 to 31-03-2027, empty = all rows).
2. **Import** copies the rows into the user's My Insights session (memory only). The links
   Sales → Chart of Accounts are ready, the columns are named "Account Code", "Amount", and Amount is an exact decimal.
3. The user builds "Sales by account name" and saves a recipe. The recipe says only "FinData: Sales, Chart of
   Accounts" (no database address, no password).
4. Next month, **Refresh** (or applying the recipe) reads FinData again, with the same date range.

Earlier plans: `plan-phase-2a.md`, `plan-phase-2b.md`, `plan-phase-3.md`, `plan-phase-4.md`.

## Decisions (from the question rounds)
| # | Topic | Decision |
|---|---|---|
| 1 | What the bridge is | Picked FinData datasets are copied into the user's My Insights session (memory only). FinData is never changed. |
| 2 | How the apps connect | A general hook in My Insights (`HOST_SOURCES`); FinData supplies one source. Neither imports the other's inner code. |
| 3 | How DuckDB reads | Copy: rows are read from Postgres and loaded into My Insights' in-memory DuckDB. No DuckDB extension. |
| 4 | Where to start | My Insights **From FinData** box (datasets + row counts, tick and import) and an **Analyse in My Insights** button on the FinData dataset page. |
| 5 | Links | FinData links become My Insights links automatically. |
| 6 | Periodic reports | All As-of dates with an **As-of date** column; a "latest date only" tick box. |
| 7 | Part of a transaction dataset | Optional date range on its date column (default all). Over 2,000,000 rows → clear message naming the dataset and count. |
| 8 | Column names | FinData labels ("Account Code"); system columns hidden except As-of date. |
| 9 | Refresh and recipes | Refresh reads FinData again; a recipe stores only "FinData: Sales" + range / latest choice. |
| 10 | Which company | Always the user's current company. A recipe from ACME applied in BETA reads BETA's Sales; a missing dataset is named on the match screen. |
| 11 | Who can | Anyone who can browse the company's datasets in FinData; My Insights switch `ALLOW_HOST_SOURCES`. |
| 12 | DuckDB helper for host code | Not now. |
| 13 | Demo | My Insights added to FinData's demo (port 8030); local folder while building, GitHub after you push. |
| 14 | Linked datasets | Ticked by default on "Analyse in My Insights"; can be unticked. |
| 15 | Same dataset twice | Shows "Already loaded · Refresh", no second copy. |
| 16 | Refresh after changes | Keeps range / latest choice; new rows and columns come in; a removed column used by a chart → My Insights' usual message, old data kept; a deleted dataset → "Sales no longer exists in FinData", old data kept. |
| 17 | Types | Exact decimals, whole numbers, dates, Yes / No kept as FinData defines them. My Insights doesn't guess. |

## Build
**My Insights** (`../my-insights`, separate project; you commit and push it):
- Setting `HOST_SOURCES` = list of dotted paths, plus `ALLOW_HOST_SOURCES` (on). A source is an object with:
  - `title` ("FinData"), `list(request)` → the datasets the user may import (id, name, rows, kind, links,
    date-range / latest options),
  - `read(request, id, options)` → column names + types + row batches, or a clear error message.
- Upload page: one **From <title>** box per source (django-datatable is not needed: a short tick list).
  Tick, set options, **Import**: all or nothing, through the same row reader as "From database".
- Dataset source `HostSource(source, id, options)`: **Refresh** and recipes use it; the recipe stores
  `{"host_source": "findata", "id": "sales", "options": {...}}` and nothing else.
- Links from the source become link suggestions already accepted; types come from the source, not guessed.
- Opening `/insights/?source=findata&pick=sales` opens the box with those ticked.

**FinData** (`findata/bridge.py`, new):
- `source`: lists the current company's datasets (only for users who can browse), reads rows with Django's own
  database connection (server-side cursor, batches of 10,000), labels as names, system columns hidden except
  As-of date, date range / latest filter in SQL.
- The dataset page gets **Analyse in My Insights** (shown only when My Insights is installed), with linked datasets
  ticked.
- Demo: My Insights installed and wired in the demo settings and URLs.

## Tests
- FinData: the list holds only the current company's datasets; another company's id → error; labels, hidden system
  columns, As-of date column, latest only, date range, exact decimals, links.
- My Insights: a fake host source imports, links, keeps types, refuses over-limit rows, Refresh keeps options,
  recipe holds no secrets, a missing dataset is named on the match screen, "Already loaded".
- Together (demo): Sales + Chart of Accounts → a query "Amount by Account Name".

## Not in this phase
A DuckDB helper for host code, live (no-copy) reads, writing back to FinData, the API (Phase 6).
