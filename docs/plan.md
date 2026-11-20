# Phase 6: API (send and read data with a key)

Example outcome for ACME:
1. Staff open **API keys** (next to Connections), make "Tally sync" with **Send data** ticked, and copy the key
   `fd_…` (shown once).
2. Tally's nightly script sends
   `POST /findata/api/datasets/sales/upload/` with header `Authorization: Bearer fd_…` and
   `{"mode": "update", "rows": [{"voucher_no": "1023", "line_no": 1, "account_code": "4010", "amount": 550}]}`.
   Checks pass → saved at once. Answer: `{"upload": 57, "added": 0, "changed": 1, "deleted": 0, "changes_url": "…"}`.
3. History shows the upload: who "API: Tally sync", how "API". Undo works from the screen as usual.
4. A Power BI script with a **Read data** key calls `GET /findata/api/datasets/sales/rows/?from=2026-04-01&page=2`.

Earlier plans: `plan-phase-2a.md` … `plan-phase-4.md`; Phase 5 was skipped (`plan-phase-5.md`).

## Decisions (from the question rounds)
| # | Topic | Decision |
|---|---|---|
| 1 | Send | `POST …/datasets/<table>/upload/`, same checks as a screen upload. |
| 2 | Read | `GET …/datasets/<table>/rows/`, JSON, 1,000 rows a page, Data-tab filters. Read-only. |
| 3 | Pull from a web API | Not now (Send + database Pull cover it). |
| 4 | Scheduled pull command | Not now. |
| 5 | Keys | One company each, named, shown once, stored hashed, switch off any time, optional end date. |
| 6 | Key rights | Tick **Send data** and / or **Read data**; all datasets of the company. |
| 7 | No confirm screen | Checks pass → saved at once, all or nothing. `"dry_run": true` → only the preview counts. |
| 8 | What is sent | JSON rows with database column names, or a CSV / XLSX file. |
| 9 | Size | At most 200,000 rows per JSON call; files use the screen's limit. |
| 10 | Errors | Same plain messages as the screen, at most 100: `{"row": 14, "column": "amount", "problem": "…"}`. Nothing saved. |
| 11 | Answer | Upload number, counts, link to its Changes. Undo is in the screen only. |
| 12 | History | Who "API: Tally sync", how "API"; shown in History and Recent uploads. |
| 13 | Who manages keys | People who can edit definitions; **API keys** page next to Connections. |
| 14 | Help | **API help** page: per dataset its columns and types, `curl` and Python examples. |
| 15 | Full refresh safety | 0 rows refused; deleting more than half the rows needs `"allow_big_delete": true`. |
| 16 | Periodic reports (send) | `as_of` required unless a file column holds it. "Remove a date" stays in the screen. |
| 17 | Values (read) | Decimals as text `"1234.50"`, dates `"2026-03-31"`, Yes / No as `true` / `false`, whole numbers as numbers, links as the code. |
| 18 | Periodic reports (read) | Latest As-of date by default; `?as_of=` picks another. |
| 19 | Key in a header | `Authorization: Bearer fd_…`, never in the address. |
| 20 | HTTPS only | `API_REQUIRE_HTTPS` (on); the demo turns it off for localhost. |
| 21 | Main switch | `ALLOW_API` off until the host turns it on. |
| 22 | Busy dataset | Another upload running → 409 "busy, try again in a minute". |
| 23 | Files without a saved mapping | Matched by name like the mapping screen; a required column left unmatched is named, nothing saved. |
| 24 | Keys page | Name, rights, made when / by whom, last used, end date, Switch off. |

## Build
**Model** `ApiKey` (public schema): company_key, name, prefix (first 8 characters, to find it), hash (SHA-256 of
the key; the key itself is never stored), can_send, can_read, created_by, created_at, last_used_at, expires_on,
active. Key = `fd_` + 40 random characters (`secrets`). `Upload.source_kind` gets `API`; `Upload.user` = "API: Tally sync".

**`findata/api.py`** (plain Django views returning JSON, no new package):
- `_key(request, right)`: HTTPS check, header read, hash compared in constant time, active / end date / right,
  `last_used_at` updated. Every refusal is logged (never the key) and answered with a JSON message (401 / 403).
- Dataset found by table name **inside the key's company only** (another company's dataset → 404).
- `upload`: JSON or multipart file → the existing steps `stage` → checks → counts → `confirm` (same code as the
  screen; no copy of the rules). Dry run stops after counts and cancels the upload. Full refresh safety rules;
  dataset lock busy → 409. CSRF off for these views only (a key is not a browser cookie).
- `rows`: the Phase 4 row model + filters (`?from=` / `?to=` on the date column, `?key=`, `?as_of=`, column
  filters), sorted by key, 1,000 a page, `next` link, typed values as in decision 17.

**Screens** (HTMX + Alpine + DaisyUI, tooltips): API keys list (django-datatable), New key dialog (name, rights,
end date) → shows the key once with a Copy button; Switch off; API help page per dataset. Links on the Datasets
page next to Connections, shown only when `ALLOW_API` is on.

## Tests
- Keys: shown once, stored hashed, wrong / switched off / expired / missing right → refused, last used updated,
  HTTP refused when HTTPS required, `ALLOW_API` off → 404.
- Send: JSON add / update, full refresh, dry run saves nothing, check errors (row + column), 0-row full refresh
  refused, big delete needs the flag, periodic `as_of`, file with saved mapping and by name, 200,000-row limit,
  busy 409, History "API: …", undo from the screen works.
- Read: pages, filters, latest As-of default, value types, another company's dataset 404.
- Screens: only definition editors manage keys; help page lists columns.

## Not in this phase
Pulling from a web API, a scheduled pull command, undo by API, per-dataset key rights, rate limits.

## Known limits and changes (after build)
- **Problems** come as `{"row": 14, "key": "1023", "problem": "Amount: “abc” isn't a number"}`: the column is
  named at the start of the problem text, not in its own field.
- **Read filters** name the column: `?voucher_date__from=2026-04-01&voucher_date__to=…`, `?group_name=assets`
  (exact, capitals ignored), `?party__contains=dom`. `page` is reserved; `as_of` is reserved for periodic reports.
- **Date-times** are read as `"2026-03-31 14:30:00"` in the site's time, the same form Send accepts, so rows read
  can be sent back unchanged.
- **Files sent by API** are matched by the saved mapping or the exact column name / label only, never a "similar"
  name (nobody checks the mapping screen).
- The **more-than-half delete** rule also covers a periodic report date (send `allow_big_delete`).
- A call that saves nothing (dry run, problems, busy) leaves **no upload** in History.
- A JSON call may be as large as the upload size limit (data-core `MAX_FILE_MB`), not Django's 2.5 MB.
- Behind a proxy that ends HTTPS, the host must set `SECURE_PROXY_SSL_HEADER`, or every call counts as http://.
- The demo turns the API on and allows http:// (`demo/settings/development.py`).
