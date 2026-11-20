# Phase 2a: Upload files into a dataset

Example outcome: on ACME, the user opens **Chart of Accounts** and clicks **Upload**, then picks `tally_ledgers.xlsx` and its sheet.
- **Mapping** (filled in by itself): `ledg_code` → Account code, `ledg_name` → Account name.
- **Mode:** Add / update.
- **Preview:** **12 new · 3 changed · 0 deleted · 480 unchanged**. The user presses **Confirm** and the rows are saved.
- **Next month:** the same file maps itself. If needed, the user can **Undo** the latest upload.

SQL sources (SQLite file, Postgres / MySQL server) are **Phase 2b**, planned after this phase.

## Decisions (from the question rounds)
| # | Topic | Decision |
|---|---|---|
| 1 | Scope | 2a = CSV / XLSX / JSON files. 2b = SQL sources (move My Insights' db code into data-core first). |
| 2 | One upload = one dataset | Pick the dataset, then the file, then the sheet if there are several. |
| 3 | Remembered mapping | Saved per dataset, by header text: `ledg_name` → Ledger name. New headers show as unmapped. |
| 4 | Guessing | Saved mapping → exact label / db name → close spelling. Any file column can be set to "skip". |
| 5 | Columns missing from the file | **Required** column missing → blocked: "Account name is required but isn't in this file." Optional column missing → existing rows keep their value, new rows get blank. A blank *cell* in a mapped column = set blank. |
| 6 | Reading values | Dates day-first (`01-04-2026` = 1 Apr), ISO and Excel dates OK. Numbers like `1,50,000.50`. Yes/No, Y/N, True/False, 1/0. Text trimmed. |
| 6b | Keys without case | `ab` = `AB`. The database keeps the spelling it saw first; a different spelling alone is not a change. |
| 7 | Bad data | All or nothing: bad value, blank key, repeated key in the file, broken link. The first 50 problems are shown with row numbers; the full list downloads as Excel. |
| 8 | Modes (with tooltips) | **Add / update**: rows not in the file are kept. **Full refresh**: rows not in the file are deleted. No period mode. |
| 9 | Deleting a linked row | Blocked: "4010 is used by 12 Sales rows. Keep it in the file, or remove those Sales first." |
| 10 | Preview | Counts, then tabs New / Changed / Deleted (django-datatable). Changed shows only the changed cells, old → new. "Nothing to update" when no changes. |
| 11 | Change log | Each confirmed upload = a batch (who, when, file, mode, counts). Old and new values per added / changed / deleted row, stored in the company's schema, kept forever. |
| 12 | Undo | Latest batch of a dataset only, after a preview. Blocked if the definition changed after that upload, or if another dataset now links to rows undo would remove. |
| 13 | Who | Anyone the host lets work on the company. Optional hook `CAN_UPLOAD(request)` for later limits. |
| 14 | Size and timing | Runs in the request with a spinner, using data-core's file size limit. One upload per dataset at a time ("Another upload of Sales is in progress."). |
| 16 | Database rule | `AB` and `ab` can never both be stored, for keys and **Unique** columns. Saving a definition is blocked if existing data has look-alikes. |
| 17 | Links without case | Sales `acc01` matches Chart `ACC01` and is stored as `ACC01`. |
| 19 | Full refresh safety | If a refresh deletes more than half the rows: red warning, and Confirm stays off until the user ticks "Yes, delete 8,400 rows". |

## Build
**Models** (public schema)
- `Dataset.remembered_mapping` (JSON: header → column name, or `""` = skip).
- `Upload`:
  - dataset, user (text), file name, sheet, mode;
  - status: preview / done / cancelled / undone;
  - counts (new, changed, deleted, unchanged), created_at, confirmed_at.

**In each company schema** (made with the first upload)
- `_changes`: upload_id, dataset_id, action (added / changed / deleted), key (json), old (json), new (json).
- `_stage_<upload id>`: holds the uploaded rows between preview and confirm. It is dropped after confirm or cancel; leftovers older than a day are cleaned up on the next upload.

**Code**
- `findata/mapping.py`: guess the mapping, save it, and find required columns that aren't mapped.
- `findata/values.py`: convert a file cell to the column type, with a plain-words error per bad cell.
- `findata/upload.py`:
  1. Read the file with `data_core.load_file`.
  2. Convert the values and fill the stage table.
  3. Check repeats, blanks and links, comparing keys and links without case.
  4. Work out the changes with SQL joins.
  5. On confirm, make the changes in one transaction under a per-dataset lock, and write `_changes`.
  6. Undo.
- `schema_builder.py`: add case-insensitive unique indexes (`lower(col)`) for text keys and Unique columns, plus a check for existing look-alikes.

**Screens** (HTMX + Alpine + DaisyUI, tooltip on every control)
1. Dataset list: **Upload** and **History** buttons on each row.
2. Upload: choose the file, the sheet and the mode (both modes have tooltips).
3. Mapping grid: file column | sample values | dataset column ▾ (or Skip). Required columns that aren't mapped are shown in red.
4. Preview: counts, New / Changed / Deleted tabs, the Full refresh warning tick box, Confirm / Cancel.
5. Problems: the first 50, plus an Excel download of all of them.
6. History: the upload list per dataset (django-datatable). The latest one has **Undo**, which opens a preview first.

## Tests
- Mapping guess, remembered mapping, missing required column.
- Value reading: Indian numbers, day-first dates, Yes/No; bad cell messages.
- Add / update vs Full refresh counts; an optional column missing keeps its old values.
- Keys and links without case: `acc01` matches `ACC01` and the stored spelling is kept. Look-alikes are blocked in the file and in the definition check.
- Problems block everything: repeated keys, blank keys, broken links, deleting a row that something links to.
- Full refresh deleting more than half the rows needs the tick box.
- Change log rows written; undo restores the data; undo is blocked after a definition change or a new link.
- The second upload at the same time is refused; company A can't upload into company B.

## Not in this phase
SQL sources (2b), periodic report as-of dates (Phase 3), the change log viewer (Phase 4), API (Phase 6).

## Known limits (after build)
- Rows are read with data-core's `keep_text=True`, so text keeps its exact form (code `1.10` stays `1.10`).
- Problem rows are the real rows in the file: Excel sheet row, or JSON record + 1. In a CSV, blank lines are not counted.
- Two uploads at once are refused with the advisory lock. This was checked by hand only (a test would need a second database connection).
