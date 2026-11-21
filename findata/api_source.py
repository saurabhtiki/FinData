"""Another application's JSON API as an upload source.

Example (Ledgers from GET https://erp.example.com/api/ledgers, Rows at "data.ledgers"):
1. `fetch` asks the API once (data-core's fetcher: GET / POST, size and time limits) and keeps
   the reply as <upload id>.json in UPLOAD_DIR. The headers (where the key usually is) are used
   for this request only: never saved, logged or put in the session.
2. The upload then reads that .json like an uploaded file (upload.opened), so mapping →
   preview → confirm → undo are the same as for a file.
"""

import logging

from data_core import conf as data_core_conf
from data_core.api_fetch import ApiLimits, ApiRequest, fetch_json, safe_url
from data_core.json_loading import ParsedJson, parse_json
from data_core.loading import UploadRejected

from . import conf, db_source
from .models import Dataset, Upload
from .upload import UploadProblems, file_path, problem

logger = logging.getLogger(__name__)


def allowed() -> bool:
    return bool(conf.get("ALLOW_API_FETCH"))


def limits() -> ApiLimits:
    return ApiLimits(
        timeout=int(conf.get("API_TIMEOUT_SECONDS")),
        max_bytes=int(data_core_conf.get("MAX_FILE_MB")) * 1024 * 1024,
        block_internal=bool(conf.get("API_BLOCK_INTERNAL")),
        allowed_hosts=tuple(conf.get("API_ALLOWED_HOSTS") or ()),
        user_agent="findata",
    )


def request_from(data) -> tuple[ApiRequest, str]:
    """The form's fields (a QueryDict or dict) → (request, rows_at)."""
    method = str(data.get("method", "GET")).upper()
    request = ApiRequest(
        url=str(data.get("url", "")).strip(),
        method=method,
        headers=str(data.get("headers", "")),
        body=str(data.get("body", "")) if method == "POST" else "",
    )
    return request, str(data.get("rows_at", "")).strip()


def _rejected(text: str) -> UploadProblems:
    return UploadProblems([problem(text)])


def read(request: ApiRequest, rows_at: str) -> tuple[bytes, list[ParsedJson]]:
    """Fetch and check the reply has rows (at rows_at). Raises UploadProblems."""
    if not allowed():
        raise _rejected("Fetching from an API is switched off on this site.")
    try:
        data = fetch_json(request, limits())
        return data, parse_json(data, rows_at=rows_at)
    except UploadRejected as error:  # ApiFailed is one too
        logger.info("API fetch from %s failed: %s", safe_url(request.url), error)
        raise _rejected(str(error)) from error


def fetch(dataset: Dataset, request: ApiRequest, rows_at: str, user: str) -> Upload:
    """Fetch the reply into a new upload (status "mapping"). Raises UploadProblems."""
    data, _ = read(request, rows_at)
    upload = Upload.objects.create(
        dataset=dataset,
        user=user,
        file_name=f"API › {safe_url(request.url)}"[:240],
        source_kind=Upload.API,
        rows_at=rows_at[:255],
    )
    try:
        file_path(upload).write_bytes(data)
    except BaseException:  # nothing half-written is left behind
        logger.exception("Upload %s: saving the API reply failed", upload.id)
        file_path(upload).unlink(missing_ok=True)
        upload.delete()
        raise
    _remember(dataset, request, rows_at)
    logger.info("Upload %s: fetched %s into %s", upload.id, upload.file_name, dataset.name)
    return upload


def _remember(dataset: Dataset, request: ApiRequest, rows_at: str) -> None:
    source = {
        "kind": Upload.API,
        "url": request.url,
        "method": request.method,
        "body": request.body,
        "rows_at": rows_at,
    }
    Dataset.objects.filter(pk=dataset.pk).update(remembered_source=source)  # updated_at stays
    dataset.remembered_source = source


def summary(groups: list[ParsedJson]) -> list[dict]:
    """For the Test button: per list, where it is, its rows and first columns."""
    return [
        {
            "rows_at": group.rows_at or "(top level)",
            "rows": len(group.main.rows),
            "columns": [name for name in group.main.columns if not name.startswith("_")][:12],
            "children": [(table.path, len(table.rows)) for table in group.children],
        }
        for group in groups
    ]


def again_tab(dataset: Dataset, saved_connections) -> str:
    """Where "Pull again" / "Fetch again" goes: "api", "database" (its saved connection still
    exists, among `saved_connections` ids) or "" (no button)."""
    source = dataset.remembered_source or {}
    if source.get("kind") == Upload.API:
        return "api" if allowed() else ""
    if db_source.servers_allowed() and source.get("connection") in saved_connections:
        return "database"
    return ""
