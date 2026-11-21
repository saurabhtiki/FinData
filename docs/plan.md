# FinData — Phase 6: pull rows from a JSON API (done 21-11-2026, 178 tests)

Built as planned. After the code review: a redirect to another host never gets the headers (the key); Fetch again keeps the list picked last time.

## Context
The removed Phase 6 API was the wrong feature: other apps sending data *into* FinData with keys.
What the user actually wants is the other way round: FinData **calls another application's API** (GET or POST) that returns
JSON, and loads those rows into a dataset, just like a file or a database pull.
Example: `GET https://erp.example.com/api/ledgers` with the header `Authorization: Bearer abc123` returns
`{"data": {"ledgers": [...]}}`, so the user sets Rows at = `data.ledgers` → mapping → preview → confirm → undo, the same as today.

The code already exists: My Insights `my_insights/api_import.py` does the fetching (GET/POST, headers, body, size and time
limits, internal-address check, friendly HTTP error messages). data-core `json_loading.py` does the flattening and is already
used by FinData's `load_file` for `.json` files. So most of this is reuse.

## Decisions (agreed)
| Topic | Decision |
|---|---|
| Saved for "Fetch again" | URL, GET/POST, Body and Rows at, remembered per dataset. **Headers (the key) are typed again each time**, like DB passwords. Tooltip: "Don't put a key in the URL: it is saved." |
| Pages | **One request only.** For a big list, put a date range or page size in the URL or Body. |
| Addresses | **Any address**, on by default (an internal ERP on 192.168.x works). The admin can set `API_BLOCK_INTERNAL: True` and list hosts in `API_ALLOWED_HOSTS`. Switch: `ALLOW_API_FETCH` (default True). |
| Who | Anyone who may upload (`CAN_UPLOAD`), same as files. |
| Several lists in the reply | Each list is a "sheet", picked like an Excel sheet (existing `pick_sheet`). Rows at narrows it to one list. |

## Build steps
1. **data-core: shared fetcher.** Move My Insights' fetch logic into `data-core/data_core/api_fetch.py`:
   `ApiRequest(url, method, headers_text, body)` and `ApiLimits(timeout, max_bytes, block_internal, allowed_hosts, user_agent)`.
   Keep `safe_url`, `parse_headers`, `check_host`, the redirect check, `STATUS_HELP` and the messages. `ApiFailed` subclasses `UploadRejected`.
   Leave My Insights unchanged for now; it keeps its copy, as data-core's CLAUDE.md already notes for other copied code. Tests are copied and adapted
   (patched `_open`, no real calls).
2. **data-core: Rows at for files.** Add `rows_at=""` to `load_file` → `load_json` → `parse_json`, which already accepts it. Add one test.
3. **FinData model + migration 0006**: `Upload.API = "api"` added to `SOURCE_CHOICES`; `from_database` becomes
   `source_kind in (SQLITE, SERVER)`, so an API upload reads its saved `.json` like a file. `upload.file_path` → `<id>.json` for API.
   Add `Upload.rows_at` (CharField, blank) so re-opening the file uses the same path. `file_name` = `safe_url(url)` (no `?query`).
4. **FinData conf**: `ALLOW_API_FETCH: True`, `API_BLOCK_INTERNAL: False`, `API_ALLOWED_HOSTS: []`, `API_TIMEOUT_SECONDS: 30`;
   the size limit reuses data-core `MAX_FILE_MB`.
5. **`findata/api_source.py` (new, small)**: `fetch(dataset, request_fields, user) -> Upload`: fetch → save bytes to
   `<id>.json` → check that `parse_json(rows_at=…)` works (problems page if not) → remember
   `{"kind": "api", url, method, body, rows_at}` in `dataset.remembered_source`. On any failure the half-made upload is deleted (same as `db_source.pull`).
   `opened()` passes `upload.rows_at` to `load_file`.
6. **Screens**: on the upload page, a third tab **API** next to File / Database (`upload_start.html`):
   URL, Method (GET/POST), Headers (one `Name: value` per line), Body (JSON, POST only, shown with Alpine), Rows at, and as-of date for periodic reports.
   Every field gets a tooltip. A **Test** button (htmx) shows "OK: 120 rows, columns: ledger_name, amount…" or the plain error.
   **Fetch** → mapping page. **Fetch again** (when remembered) pre-fills everything but Headers and goes straight to preview
   through the existing `_straight_to_preview` (moved to a shared spot so DB and API both use it).
   Views in `findata/api_views.py`: `api_test`, `api_fetch`, with `sensitive_post_parameters("headers")`. URLs `upload/<pk>/api/test/` and `upload/<pk>/api/fetch/`.
   Headers are never logged, saved or put in the session.
7. **History / Recent uploads**: the source shows as "API › erp.example.com/api/ledgers".
8. **Docs**: `docs/plan.md` (this phase), `docs/project.md` roadmap item 6 = "Pull from a JSON API", README settings, `CLAUDE.md` current phase.

## Reused, not rewritten
- `my_insights/api_import.py`: fetch, safety, messages (moved to data-core).
- `data_core.json_loading.parse_json` / `load_file`: flattening and child lists.
- FinData `upload.opened`, `mapping.guess`, `stage`, `confirm`, undo, `db_views._straight_to_preview`, `upload_views._failed`.

## Verification
- data-core tests: fetcher (GET, POST body, headers, 401/404/500, timeout, too large, not JSON, internal host blocked when on), Rows at.
- FinData tests (patched `_open`): fetch → mapping → preview → confirm puts rows in Postgres; Rows at picks the list; bad Rows at shows the problems page;
  Headers are not in `remembered_source`, the upload or the logs; Fetch again goes to preview; switched off returns 403; periodic report with an as-of date; another company's dataset returns 404.
- Full suite stays green (163 + new), `makemigrations --check`, ruff.
- Demo: serve a local JSON file (e.g. a tiny test view in the demo, `/demo/api/ledgers/`) and pull it into a dataset in the browser.
- End: `/code-review`, update `FinData/CLAUDE.md` current phase. Committing and pushing (including the uncommitted API removal) only when you ask.
