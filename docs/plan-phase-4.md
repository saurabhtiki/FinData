# Phase 4: Browse data, upload history, change log viewer

Example outcome for ACME:
1. The user clicks **Sales** in the dataset list. The dataset page opens on the **Data** tab: all rows, sorted by
   `voucher_no + line_no`, with a filter on every column (Amount 1,000–5,000, Date 01-04 to 30-04, Party "dom").
   **Download CSV** gives the filtered rows in template order, ready to edit and upload back.
2. Hovering over account `4010` shows "Sales – domestic"; clicking it opens that account in Chart of Accounts.
3. A row's **History** shows: "Added 02-04 by Ravi (sales_mar.xlsx)", then "Amount 500 → 550 on 10-04 by Asha".
4. The **Changes** tab lists every change: `Changed · 1023/1 · Amount 500 → 550`. Filters: date, user, upload,
   New / Changed / Deleted, key. History's **Changes** button opens the same list for one upload.
5. **Recent uploads** (top menu) lists every upload of the company, newest first.

Earlier plans: `plan-phase-2a.md`, `plan-phase-2b.md`, `plan-phase-3.md`.

## Decisions (from the question rounds)
| # | Topic | Decision |
|---|---|---|
| 1 | Browse data | **Data** tab: all saved rows, a filter per column (text part-match, number / date range, Yes / No), sort, pages of 50, CSV. Read-only. django-datatable on the real table. |
| 2 | Periodic reports | One As-of date at a time (date box, default latest). A green day on the calendar opens that date. |
| 3 | Who can browse | Anyone who can open the company's dataset list. |
| 4 | Changes of one upload | History → **Changes**: one line per row (New / Changed / Deleted + key); for Changed, only the columns that changed, old → new. Filter by action, search by key. |
| 5 | History of one row | Data row → **History**: every change to that row with when, who and which upload. |
| 6 | All changes of a dataset | **Changes** tab with filters: date range, user, upload, action, key. The per-upload view is this page filtered to one upload. |
| 7 | Undone uploads | Their changes stay, marked **Undone**. The undo shows as its own line ("Undo of sales_mar.xlsx"). |
| 8 | Recent uploads | Company-wide page (all datasets, newest first; filter dataset, user, status). |
| 9 | Extra column | **Last changed** (date; tooltip names the upload). Internal IDs hidden. |
| 10 | Links | Code shown; hover shows the linked row's name; click opens it in the other dataset. Filter by code. |
| 11 | Download | CSV only. |
| 12 | Keeping the log | Forever, no clean-up yet. |
| 13 | Dataset page | Click a dataset's name → page with tabs **Data · Changes · History · Dates** (Dates for periodic reports). The list row keeps Upload (+ Pull again) and a "⋯" menu: Edit, Template, Delete. |
| 14 | Order and filters | Sorted by key; text filters match part of a word; Yes / No columns get a pick list. |
| 15 | CSV contents | Data CSV = the dataset's own columns in template order (no Last changed). Changes CSV = when, who, upload, action, key, column, old, new. |
| 16 | Deleted rows | Their history opens from the key on the Changes page. |
| 17 | Removed columns | Old changes show "gst_no (removed column)"; renamed labels show the new label. |
| 18 | Definition edits | Not in the change log (row data only). |
| 19 | Recent uploads link | Top menu, next to Datasets and Connections. Each line links to that upload's Changes. |
| 20 | Empty states | "No rows yet" + Upload button; "Nothing changed in this upload". |

Defaults chosen without a question (say if you want them different):
- For a periodic report, a row's History covers that key on **one** As-of date. Changes show an As-of column.
- An upload's "Remove a date" lines show as Deleted, like any other removal.

## Build
**Read models for the company tables** (`findata/browse.py`, new):
- `row_model(dataset)`: an unmanaged Django model made at run time from the definition
  (`db_table` = `co_acme"."sales`), with the key as primary key, so django-datatable can filter, sort, page and
  stream CSV in SQL. Cached per dataset + `updated_at`.
- `change_model(dataset)`: the same for `_changes`, filtered to the dataset. The key shows as text ("1023 / 1").
- `row_history(dataset, key)`: the change log lines of one key (for periodic reports, one key + As-of date).
- `link_names(dataset, codes)`: one query per linked dataset for the hover names on the current page only.

**Views and templates** (HTMX + Alpine + DaisyUI, tooltips everywhere):
- `datasets/<id>/` dataset page with tabs; `?tab=data|changes|history|dates`. The tabs load by HTMX
  (one table per page, as django-datatable needs).
- Data tab: column filters by type, Last changed, link hover / click, History per row (a dialog), As-of date box for periodic reports.
- Changes tab: action badge, key (click → row history), "Amount 500 → 550" per changed column, Undone badge,
  undo lines from `Upload.undone_at`, filters, CSV.
- History tab: the current history table plus a **Changes** button per saved upload.
- Dates tab: the current calendar; a loaded day links to the Data tab for that date.
- `uploads/` Recent uploads page (django-datatable), linked in the top menu.
- Dataset list: name links to the dataset page; buttons trimmed to Upload / Pull again + "⋯" menu.
- Old URLs (`/history/`, `/dates/`) keep working by redirecting to the matching tab.

## Tests
- Data: rows shown, text / number / date / Yes-No filters, sort by key, CSV columns in template order, empty state.
- Periodic report: one date at a time, default latest, other dates hidden.
- Links: hover names come from the linked dataset; the click URL filters it to that code.
- Changes: one upload vs all, only changed columns, removed-column label, Undone mark, undo line, CSV.
- Row history: a changed row, a deleted row, a periodic-report row on one date.
- Recent uploads: all datasets of the company only; filters.
- Isolation: another company gets 404 on every new page; a viewer (no upload right) can browse.

## Not in this phase
Editing rows by hand, Excel download, an "All dates" view and trends (Phase 5), definition history, log clean-up.

## Known limits and changes (after build)
- Tabs are separate addresses, not `?tab=`: `datasets/<id>/` (Data), `/changes/`, `/history/`, `/dates/`.
  The old History and Dates links keep working as they are.
- **Undo** (decision 7): in the Changes table an undone upload's lines carry an **Undone** badge (when and who
  in its tooltip) instead of a separate line; the row's **History** shows the undo as its own line
  ("Undone … undo of sales_mar.xlsx"). Who undid it is saved from now on (`Upload.undone_by`).
- **Recent uploads** is a button on the Datasets page (next to Connections): the top menu belongs to the host app.
- A link click opens the linked dataset filtered to exactly that row ("Showing one row: 4010 · Show all rows").
- The hover name of a link is the linked dataset's first text column that isn't the key (e.g. Account name).
- Row history covers rows saved since the change log started; older rows say so.
- The Data page also has a search box over all text columns and a "Columns" menu to hide columns.
