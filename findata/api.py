"""The API: other systems send rows into a dataset, or read them, with a key.

Send (a key with "Send data"):
    POST /findata/api/datasets/sales/upload/
    Authorization: Bearer fd_…
    {"mode": "update", "rows": [{"voucher_no": "1023", "line_no": 1, "amount": 550}]}
    → 201 {"upload": 57, "added": 0, "changed": 1, "deleted": 0, "changes_url": "…"}
  The rows go through the same steps as a screen upload (stage → checks → counts → confirm),
  but there is no preview to click: all checks pass → saved at once, all or nothing.
  "dry_run": true only answers with the counts. A CSV / XLSX file can be sent instead
  (multipart field "file"), mapped like the mapping screen would.

Read (a key with "Read data"):
    GET /findata/api/datasets/sales/rows/?voucher_date__from=2026-04-01&page=2
    → {"count": 4210, "page": 2, "pages": 5, "next": "…", "rows": [{…}, …]}

A key works for its own company only; another company's dataset is "not found".
"""

import hashlib
import hmac
import json
import logging
import secrets
from datetime import date, datetime
from decimal import Decimal
from functools import wraps
from pathlib import Path

from data_core import conf as data_core_conf
from django.db.models import Max, Q
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import browse, conf, mapping, values
from . import upload as uploads
from .models import ApiKey, Dataset, DatasetColumn, Upload
from .schema_builder import AS_OF

logger = logging.getLogger(__name__)

KEY_START = "fd_"
PREFIX_LENGTH = 11  # "fd_" + 8 characters
SHOWN_PROBLEMS = 100
FILE_TYPES = (".csv", ".xlsx", ".xlsm", ".json", ".jsonl")
MODES = {
    "update": Upload.ADD_UPDATE,
    "add_update": Upload.ADD_UPDATE,
    "full": Upload.FULL_REFRESH,
    "full_refresh": Upload.FULL_REFRESH,
}
SEND, READ = "send", "read"


class Refused(Exception):
    """The call can't go ahead: answered with `status` and a plain message."""

    def __init__(self, status: int, text: str, **extra):
        super().__init__(text)
        self.status, self.text, self.extra = status, text, extra


# ---------- keys ----------


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def make_key(company_key: str, name: str, *, can_send: bool, can_read: bool, created_by: str,
             expires_on: date | None) -> tuple[ApiKey, str]:  # fmt: skip
    """A new key and its text, e.g. fd_Xy12…; the text is never stored, only its hash."""
    while True:
        raw = KEY_START + secrets.token_urlsafe(30)
        if not ApiKey.objects.filter(prefix=raw[:PREFIX_LENGTH]).exists():
            break
    key = ApiKey.objects.create(
        company_key=company_key,
        name=name,
        prefix=raw[:PREFIX_LENGTH],
        key_hash=_hash(raw),
        can_send=can_send,
        can_read=can_read,
        created_by=created_by,
        expires_on=expires_on,
    )
    logger.info("API key %s made for %s by %s", key, company_key, created_by)
    return key, raw


def _key(request, right: str) -> ApiKey:
    """The key the call came with, checked. Raises Refused."""
    if conf.get("API_REQUIRE_HTTPS") and not request.is_secure():
        raise Refused(403, "Use https:// for API calls, so the key can't be read on the way.")
    scheme, _, raw = request.headers.get("Authorization", "").partition(" ")
    raw = raw.strip()
    if scheme.lower() != "bearer" or not raw.startswith(KEY_START):
        raise Refused(
            401, "Send the API key in the header: Authorization: Bearer fd_… (see API help)."
        )
    key = ApiKey.objects.filter(prefix=raw[:PREFIX_LENGTH]).first()
    if key is None or not hmac.compare_digest(key.key_hash, _hash(raw)):
        raise Refused(401, "This API key isn't valid. Check it was copied in full.")
    if not key.active:
        raise Refused(401, f"The API key “{key.name}” is switched off.")
    if key.expires_on and key.expires_on < timezone.localdate():
        raise Refused(
            401, f"The API key “{key.name}” stopped working on {key.expires_on:%d-%m-%Y}."
        )
    if right == SEND and not key.can_send:
        raise Refused(403, f"The API key “{key.name}” may read data, but not send it.")
    if right == READ and not key.can_read:
        raise Refused(403, f"The API key “{key.name}” may send data, but not read it.")
    ApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    return key


def _dataset(key: ApiKey, table: str) -> Dataset:
    found = Dataset.objects.filter(company_key=key.company_key, table_name=table).first()
    if found is None:
        names = ", ".join(
            Dataset.objects.filter(company_key=key.company_key).values_list("table_name", flat=True)
        )
        raise Refused(404, f"There is no dataset “{table}”. Datasets: {names or 'none yet'}.")
    return found


def api_view(right: str):
    """JSON in, JSON out: checks the switch and the key; every refusal is logged and answered
    as {"error": "…"} (the key itself is never logged)."""

    def decorator(view):
        @csrf_exempt  # a key in a header, not a browser cookie
        @wraps(view)
        def wrapper(request, table: str):
            if not conf.get("ALLOW_API"):
                raise Http404("The API is switched off.")
            try:
                key = _key(request, right)
                return view(request, key, _dataset(key, table))
            except Refused as refused:
                logger.warning(
                    "API %s %s refused (%s): %s", request.method, request.path, refused.status,
                    refused.text,
                )  # fmt: skip
                return JsonResponse({"error": refused.text, **refused.extra}, status=refused.status)

        return wrapper

    return decorator


# ---------- send ----------


def _shown_problems(found: list[dict]) -> dict:
    return {
        "problem_count": len(found),
        "problems": [
            {"row": item.get("row"), "key": item.get("key", ""), "problem": item["text"]}
            for item in found[:SHOWN_PROBLEMS]
        ],
    }


def _flag(body, name: str) -> bool:
    value = body.get(name, False)
    return value is True or str(value).strip().lower() in ("true", "1", "yes")


def _json_sheet(dataset: Dataset, body: dict) -> tuple[uploads.Sheet, dict[str, str]]:
    """The JSON rows as a sheet the upload steps can read, mapped name → same name."""
    rows = body.get("rows")
    if not isinstance(rows, list):
        raise Refused(400, 'Send the rows as a list: {"rows": [{"column": value, …}, …]}.')
    if not rows:
        raise Refused(400, "“rows” is empty: there is nothing to save.")
    limit = conf.get("API_MAX_ROWS")
    if len(rows) > limit:
        raise Refused(
            413, f"{len(rows):,} rows is more than {limit:,} in one call. Send a file instead."
        )
    names = [name for name, _, _ in mapping.targets(dataset)]
    headers: dict[str, None] = {}
    for number, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise Refused(400, f'Row {number} isn\'t an object like {{"column": value}}.')
        for name, value in row.items():
            if name not in names:
                raise Refused(
                    400,
                    f"Row {number}: “{name}” isn't a column of {dataset.name}. "
                    f"Columns: {', '.join(names)}.",
                )
            if isinstance(value, dict | list):
                raise Refused(400, f"Row {number}, {name}: send a single value, not a list.")
            headers.setdefault(name)
    order = list(headers)

    def read(wanted: list[str]):
        for number, row in enumerate(rows, start=1):
            yield (number, *(row.get(name) for name in wanted))

    sheet = uploads.Sheet(
        label="API", headers=order, samples={}, row_count=len(rows), table_name="", read=read
    )
    return sheet, {name: name for name in order}


def _file(request) -> tuple:
    upload_file = request.FILES["file"]
    suffix = Path(upload_file.name).suffix.lower()
    if suffix not in FILE_TYPES:
        raise Refused(400, f"{upload_file.name}: send a CSV, Excel (.xlsx) or JSON file.")
    max_mb = data_core_conf.get("MAX_FILE_MB")
    if upload_file.size > max_mb * 1024 * 1024:
        raise Refused(413, f"{upload_file.name} is larger than the {max_mb} MB limit.")
    return upload_file


def _no_constant(name: str):
    raise ValueError(f"{name} isn't a number JSON allows")


def _json_body(request) -> dict:
    """The JSON object sent. Read here, not with request.body, so the limit is the upload size
    limit (Django's own DATA_UPLOAD_MAX_MEMORY_SIZE, 2.5 MB, would stop at about 20,000 rows)."""
    max_mb = data_core_conf.get("MAX_FILE_MB")
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length > max_mb * 1024 * 1024:
        raise Refused(413, f"The body is larger than the {max_mb} MB limit. Send a file instead.")
    try:
        body = json.loads(
            request.read(max_mb * 1024 * 1024 + 1) or b"null",
            parse_float=Decimal,
            parse_constant=_no_constant,
        )
    except (ValueError, UnicodeDecodeError) as error:
        raise Refused(400, f"The body isn't valid JSON: {error}.") from error
    if not isinstance(body, dict):
        raise Refused(400, 'Send a JSON object: {"mode": "update", "rows": […]}.')
    return body


def _mode(dataset: Dataset, text) -> str:
    if dataset.is_snapshot:
        return Upload.DATES  # periodic reports replace the dates they bring
    mode = MODES.get(str(text or "update").strip().lower())
    if mode is None:
        raise Refused(400, f"“mode” is “{text}”: use “update” (add / update) or “full”.")
    return mode


def _as_of(dataset: Dataset, picked: dict[str, str], text) -> date | None:
    if not dataset.is_snapshot or AS_OF in picked.values():
        return None  # each row brings its own date
    try:
        day = uploads.picked_date(text)
    except uploads.UploadProblems as problem:
        raise Refused(400, problem.problems[0]["text"]) from problem
    if day is None:
        raise Refused(
            400,
            f'{dataset.name} is a periodic report: send “as_of” (e.g. "2026-03-31"), '
            f"or an “{AS_OF}” value in each row.",
        )
    return day


def _stage_and_save(
    request, key: ApiKey, upload: Upload, sheet, picked: dict, body
) -> JsonResponse:
    """Map check → stage → counts → (dry run stops here) → confirm."""
    dataset = upload.dataset
    found = mapping.problems(dataset, picked)
    if found:
        raise uploads.UploadProblems([uploads.problem(text) for text in found])
    if upload.mode == Upload.FULL_REFRESH and sheet.row_count == 0:
        raise Refused(400, "A full refresh with 0 rows would delete everything. Nothing was saved.")
    uploads.stage(upload, sheet, picked, upload.mode)
    counts = {
        "added": upload.added,
        "changed": upload.changed,
        "deleted": upload.deleted,
        "unchanged": upload.unchanged,
    }
    if dataset.is_snapshot:
        counts["dates"] = upload.dates
    if _flag(body, "dry_run"):
        _forget(upload)
        logger.info("API dry run %s into %s: %s", upload.id, dataset.name, counts)
        return JsonResponse({"dry_run": True, "saved": False, **counts})
    if upload.needs_delete_tick and not _flag(body, "allow_big_delete"):
        raise Refused(
            400,
            f"This would delete {upload.deleted:,} of {upload.existing:,} saved rows (more than "
            "half). Nothing was saved. If that is right, send again with "
            '"allow_big_delete": true.',
            **counts,
        )
    uploads.confirm(upload, key.as_user, delete_ticked=True)
    changes = reverse("findata:changes", args=[dataset.id]) + f"?upload={upload.id}"
    return JsonResponse(
        {"upload": upload.id, "saved": True, **counts,
         "changes_url": request.build_absolute_uri(changes)},
        status=201,
    )  # fmt: skip


@require_POST
@api_view(SEND)
def send(request, key: ApiKey, dataset: Dataset):
    is_file = request.content_type == "multipart/form-data"
    if is_file:
        if "file" not in request.FILES:
            raise Refused(400, "Send the file in the form field “file”.")
        body = request.POST
        upload_file = _file(request)
    else:
        body = _json_body(request)
        sheet, picked = _json_sheet(dataset, body)
    mode = _mode(dataset, body.get("mode"))
    uploads.sweep_stale()
    upload = Upload.objects.create(
        dataset=dataset,
        user=key.as_user,
        file_name=upload_file.name[:255] if is_file else f"API ({sheet.row_count:,} rows)",
        source_kind=Upload.FILE if is_file else Upload.API,
        mode=mode,
    )
    try:
        if not is_file:
            upload.as_of_date = _as_of(dataset, picked, body.get("as_of"))
            return _stage_and_save(request, key, upload, sheet, picked, body)
        uploads.save_file(upload, upload_file.chunks())
        with uploads.opened(upload) as sheets:
            sheet = uploads.pick_sheet(sheets, body.get("sheet", ""))
            picked = mapping.guess(dataset, sheet.headers, close=False)
            upload.as_of_date = _as_of(dataset, picked, body.get("as_of"))
            return _stage_and_save(request, key, upload, sheet, picked, body)
    except uploads.UploadBusy as busy:
        _forget(upload)
        raise Refused(409, busy.problems[0]["text"]) from busy
    except uploads.UploadProblems as error:
        _forget(upload)
        logger.warning(
            "API upload %s into %s refused: %s problem(s), first: %s",
            upload.id, dataset.name, len(error.problems), error.problems[0]["text"],
        )  # fmt: skip
        raise Refused(
            400,
            f"{len(error.problems):,} problem(s): nothing was saved.",
            **_shown_problems(error.problems),
        ) from error
    except Refused:
        _forget(upload)
        raise
    except Exception as error:  # e.g. a database error: answer in JSON, leave nothing half-way
        logger.exception("API upload %s into %s failed", upload.id, dataset.name)
        _forget(upload)
        raise Refused(500, "Something went wrong on the server. Nothing was saved.") from error


def _forget(upload: Upload) -> None:
    """A call that saved nothing leaves nothing: no stage table, file or Upload row."""
    uploads.cancel(upload)
    upload.delete()


# ---------- read ----------


def json_value(value):
    """Decimals as text so they stay exact ("1234.50"), dates as "2026-03-31", date-times in the
    site's time as "2026-03-31 14:30:00": the same forms Send accepts, so rows read can be sent back."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _typed_filter(column: DatasetColumn, text: str):
    try:
        value = values.convert(text, column)
    except values.BadValue as error:
        raise Refused(400, f"Filter {column.name}: “{text}” {error}.") from error
    return value


def _filters(dataset: Dataset, columns: list[DatasetColumn], params) -> Q:
    """?account_code=4010 (exact, capitals ignored for text), ?party__contains=dom,
    ?amount__from=1000&amount__to=5000, ?voucher_date__from=2026-04-01, ?is_active=true."""
    by_name = {column.name: column for column in columns}
    wanted = Q()
    for name, text in params.items():
        if name == "page" or (name == "as_of" and dataset.is_snapshot):
            continue
        base, op = name, "is"
        for ending in ("__from", "__to", "__contains"):
            if name.endswith(ending) and name not in by_name:
                base, op = name[: -len(ending)], ending[2:]
        column = by_name.get(base)
        if column is None:
            raise Refused(
                400,
                f"“{name}” isn't a filter. Use a column name ({', '.join(by_name)}), "
                "with __from / __to for numbers and dates, or __contains for text.",
            )
        field = browse.attr(column)
        is_text = column.data_type == DatasetColumn.TEXT
        if op == "contains":
            if not is_text:
                raise Refused(400, f"“{name}”: __contains works on text columns only.")
            wanted &= Q(**{f"{field}__icontains": text})
        elif op in ("from", "to"):
            if is_text or column.data_type == DatasetColumn.BOOLEAN:
                raise Refused(400, f"“{name}”: __from / __to work on numbers and dates only.")
            lookup = "gte" if op == "from" else "lte"
            wanted &= Q(**{f"{field}__{lookup}": _typed_filter(column, text)})
        elif is_text:
            wanted &= Q(**{f"{field}__iexact": text})
        else:
            wanted &= Q(**{field: _typed_filter(column, text)})
    return wanted


@require_GET
@api_view(READ)
def rows(request, key: ApiKey, dataset: Dataset):
    columns = list(dataset.columns.all())
    queryset = browse.rows(dataset).filter(_filters(dataset, columns, request.GET))
    as_of = None
    if dataset.is_snapshot:
        try:
            as_of = uploads.picked_date(request.GET.get("as_of"))
        except uploads.UploadProblems as problem:
            raise Refused(400, problem.problems[0]["text"]) from problem
        if as_of is None:
            as_of = browse.rows(dataset).aggregate(latest=Max(AS_OF))["latest"]
        queryset = queryset.filter(**{AS_OF: as_of}) if as_of else queryset.none()
    order = [browse.attr(column) for column in dataset.key_columns()] + [browse.ROW_ID]
    queryset = queryset.order_by(*order)
    try:
        page = max(int(request.GET.get("page", "1")), 1)
    except ValueError as error:
        raise Refused(400, "“page” must be a whole number, e.g. page=2.") from error
    size = conf.get("API_PAGE_ROWS")
    count = queryset.count()
    pages = max((count + size - 1) // size, 1)
    found = queryset[(page - 1) * size : page * size]
    names = [(column.name, browse.attr(column)) for column in columns]
    out = []
    for row in found:
        item = {AS_OF: json_value(getattr(row, AS_OF))} if dataset.is_snapshot else {}
        item.update({name: json_value(getattr(row, field)) for name, field in names})
        out.append(item)
    following = None
    if page < pages:
        params = request.GET.copy()
        params["page"] = page + 1
        following = request.build_absolute_uri(f"{request.path}?{params.urlencode()}")
    return JsonResponse(
        {
            "dataset": dataset.table_name,
            "as_of": as_of.isoformat() if as_of else None,
            "count": count,
            "page": page,
            "pages": pages,
            "next": following,
            "rows": out,
        }
    )
