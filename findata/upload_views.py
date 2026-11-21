"""Upload screens: choose file → map columns → preview → confirm; problems; history; undo."""

import calendar
import io
import logging
from datetime import date
from pathlib import Path

from data_core import conf as data_core_conf
from data_core.databases import KINDS
from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_datatable.datatable import CHOICE, DATE, NUMBER, TEXT, Column, RowsTable, table_response
from openpyxl import Workbook

from . import api_source, conf, db_source, mapping, periods
from . import company as companies
from . import upload as uploads
from .browse_views import page_context
from .models import Dataset, DatasetColumn, DbConnection, Upload
from .schema_builder import AS_OF
from .views import _own_dataset, _page, company_required

logger = logging.getLogger(__name__)

FILE_TYPES = (".csv", ".xlsx", ".xlsm", ".json", ".jsonl")
SQLITE_TYPES = (".db", ".sqlite", ".sqlite3")
COLUMN_TYPES = {
    DatasetColumn.INTEGER: NUMBER,
    DatasetColumn.DECIMAL: NUMBER,
    DatasetColumn.DATE: DATE,
}
TABS = [("new", "New"), ("changed", "Changed"), ("deleted", "Deleted"), ("dates", "Per date")]


def _user(request) -> str:
    user = getattr(request, "user", None)
    return user.get_username() if user is not None and user.is_authenticated else ""


def _own_upload(request, upload_id: int) -> Upload:
    return get_object_or_404(
        Upload.objects.select_related("dataset"),
        pk=upload_id,
        dataset__company_key=request.findata_company.key,
    )


def dates_text(upload: Upload) -> str:
    """The As-of dates an upload touched: "31-03-2026", or "3 dates: 31-01-2026 – 31-03-2026"."""
    days = [date.fromisoformat(item["date"]) for item in upload.dates]
    if not days:
        return ""
    if len(days) == 1:
        return f"{days[0]:%d-%m-%Y}"
    return f"{len(days)} dates: {days[0]:%d-%m-%Y} – {days[-1]:%d-%m-%Y}"


# ---------- 1. choose the file ----------


@require_http_methods(["GET", "POST"])
@company_required(upload=True)
def start(request, pk: int):
    dataset = _own_dataset(request, pk)
    context = _start_context(request, dataset)
    if request.method == "GET":
        return _page(request, "findata/upload_start.html", context)
    upload_file = request.FILES.get("file")
    max_mb = data_core_conf.get("MAX_FILE_MB")
    error = ""
    suffix = Path(upload_file.name).suffix.lower() if upload_file is not None else ""
    if upload_file is None:
        error = "Choose a file first."
    elif suffix not in FILE_TYPES + SQLITE_TYPES:
        error = (
            f"{upload_file.name}: only CSV, Excel (.xlsx), JSON and SQLite (.db) files "
            "can be uploaded."
        )
    elif upload_file.size > max_mb * 1024 * 1024:
        error = f"{upload_file.name} is larger than the {max_mb} MB limit."
    as_of = None
    if not error and dataset.is_snapshot:
        try:
            as_of = uploads.picked_date(request.POST.get("as_of"))
        except uploads.UploadProblems as problem:
            error = problem.problems[0]["text"]
    if error:
        logger.warning("Upload into %s refused: %s", dataset.name, error)
        context = {
            **context,
            "error": error,
            "tab": "database" if suffix in SQLITE_TYPES else "file",
            "as_of": request.POST.get("as_of", context.get("as_of", "")),
        }
        return _page(request, "findata/upload_start.html", context, status=400)
    uploads.sweep_stale()
    is_sqlite = suffix in SQLITE_TYPES
    upload = Upload.objects.create(
        dataset=dataset,
        user=_user(request),
        file_name=upload_file.name[:255],
        source_kind=Upload.SQLITE if is_sqlite else Upload.FILE,
        as_of_date=as_of,
    )
    uploads.save_file(upload, upload_file.chunks())
    logger.info("Upload %s: %s into %s", upload.id, upload.file_name, dataset.name)
    return redirect("findata:upload_sqlite" if is_sqlite else "findata:upload_map", upload.id)


def _start_context(request, dataset: Dataset) -> dict:
    """The File, Database and API tabs. "Pull again" / "Fetch again" open Database / API with
    the last read filled in (never a password or the API's headers)."""
    remembered = dataset.remembered_source or {}
    saved = DbConnection.objects.filter(company_key=request.findata_company.key)
    servers_on = db_source.servers_allowed()
    api_on = api_source.allowed()
    tab = request.GET.get("tab", "file")
    return {
        "company": request.findata_company,
        "dataset": dataset,
        "tab": tab if tab in ("file", "database") or (tab == "api" and api_on) else "file",
        "api_on": api_on,
        "api": remembered if remembered.get("kind") == Upload.API else {"method": "GET"},
        "servers_on": servers_on,
        "connections": list(saved) if servers_on else [],
        "can_edit": companies.can_edit_definitions(request),
        "kinds": KINDS,
        "again": request.GET.get("again") == "1",
        "remembered": remembered if remembered.get("kind") == Upload.SERVER else {},
        "limits": db_source.limits(),
        **_as_of_context(request, dataset),
    }


def _as_of_context(request, dataset: Dataset) -> dict:
    """The As-of date box of a periodic report: the suggested date, e.g. 31-03-2026 (missing)."""
    if not dataset.is_snapshot:
        return {}
    loaded = [day for day, _ in uploads.loaded_dates(dataset)]
    suggested, missing = periods.suggested_date(dataset.how_often, loaded, timezone.localdate())
    remembered = dataset.remembered_mapping or {}
    date_column = next((header for header, name in remembered.items() if name == AS_OF), "")
    as_of = request.GET.get("as_of") or ("" if date_column else suggested.isoformat())
    return {
        "as_of": as_of,
        "as_of_missing": missing and as_of == suggested.isoformat(),
        "date_column": date_column,
        "today": timezone.localdate().isoformat(),
    }


# ---------- 2. map the columns ----------


@require_http_methods(["GET", "POST"])
@company_required(upload=True)
def map_columns(request, upload_id: int):
    upload = _own_upload(request, upload_id)
    if upload.status == Upload.PREVIEW:  # "← Change mapping" from the preview
        uploads.reopen(upload)
    if upload.status != Upload.MAPPING:
        return redirect("findata:history", upload.dataset_id)
    if upload.source_kind == Upload.SQLITE and not upload.select_sql:
        return redirect("findata:upload_sqlite", upload.id)
    dataset = upload.dataset
    source = request.POST if request.method == "POST" else request.GET
    mode = source.get("mode", upload.mode)
    if dataset.is_snapshot:
        mode = Upload.DATES
    elif mode not in (Upload.ADD_UPDATE, Upload.FULL_REFRESH):
        mode = Upload.ADD_UPDATE
    problems: list[str] = []
    try:
        with uploads.opened(upload) as sheets:  # the file is read once per request
            sheet = uploads.pick_sheet(sheets, source.get("sheet", upload.sheet))
            if request.method == "POST":
                chosen = {
                    header: request.POST.get(f"map_{index}", "")
                    for index, header in enumerate(sheet.headers)
                }
                problems = mapping.problems(dataset, chosen)
                if dataset.is_snapshot:
                    kept = upload.as_of_date.isoformat() if upload.as_of_date else ""
                    problems += _take_as_of(upload, chosen, request.POST.get("as_of", kept))
                if problems:
                    logger.warning("Upload %s mapping refused: %s", upload.id, problems)
                else:
                    mapping.remember(dataset, chosen)
                    upload.sheet, upload.mode, upload.mapping = sheet.label, mode, chosen
                    uploads.stage(upload, sheet, chosen, mode)
                    return redirect("findata:upload_preview", upload.id)
            else:
                same_sheet = upload.mapping and sheet.label == upload.sheet
                chosen = upload.mapping if same_sheet else mapping.guess(dataset, sheet.headers)
    except uploads.UploadProblems as error:
        if request.method == "POST":
            upload.save(update_fields=["sheet", "mode", "mapping", "as_of_date"])
        return _failed(upload, error.problems)
    types = {column.name: column.type_label for column in dataset.columns.all()}
    rows = [
        {
            "index": index,
            "header": header,
            "samples": ", ".join(sheet.samples.get(header, [])),
            "chosen": chosen.get(header, ""),
        }
        for index, header in enumerate(sheet.headers)
    ]
    context = {
        "company": request.findata_company,
        "dataset": dataset,
        "upload": upload,
        "sheets": sheets,
        "sheet": sheet,
        "mode": mode,
        "modes": [
            (value, label, Upload.MODE_HELP[value])
            for value, label in Upload.MODE_CHOICES
            if value in (Upload.ADD_UPDATE, Upload.FULL_REFRESH)
        ],
        "dates_help": Upload.MODE_HELP[Upload.DATES],
        "rows": rows,
        "problems": problems,
        "columns_json": [
            {"name": name, "label": label, "required": required, "type": types.get(name, "Date")}
            for name, label, required in mapping.targets(dataset)
        ],
        "as_of_name": AS_OF,
        "as_of": request.POST.get("as_of")
        if request.method == "POST"
        else (upload.as_of_date.isoformat() if upload.as_of_date else ""),
        "today": timezone.localdate().isoformat(),
        "chosen_json": [row["chosen"] for row in rows],
        "headers_json": sheet.headers,
    }
    return _page(request, "findata/upload_map.html", context, status=400 if problems else 200)


def _take_as_of(upload: Upload, chosen: dict[str, str], typed: str | None) -> list[str]:
    """A periodic report's As-of date: from a file column, or the date picked for the file.
    Sets upload.as_of_date; returns what stops it."""
    if AS_OF in chosen.values():
        upload.as_of_date = None  # each row has its own date
        return []
    try:
        upload.as_of_date = uploads.picked_date(typed)
    except uploads.UploadProblems as problem:
        return [problem.problems[0]["text"]]
    if upload.as_of_date is None:
        return ["Pick the As-of date for the whole file, or the file column that has it."]
    return []


def _failed(upload: Upload, found: list[dict]):
    logger.warning(
        "Upload %s refused with %s problem(s), first: %s", upload.id, len(found), found[0]["text"]
    )
    upload.problems = found
    upload.save(update_fields=["problems"])
    return redirect("findata:upload_problems", upload.id)


def _straight_to_preview(upload: Upload):
    """Pull / fetch again: the remembered mapping and the last mode, then the preview. Anything
    unclear (a new column, a problem) stops on the mapping or problems page instead."""
    dataset = upload.dataset
    previous = uploads.latest_done(dataset)
    if dataset.is_snapshot:
        mode = Upload.DATES
    elif previous and previous.mode in (Upload.ADD_UPDATE, Upload.FULL_REFRESH):
        mode = previous.mode
    else:
        mode = Upload.ADD_UPDATE
    try:
        with uploads.opened(upload) as sheets:
            # The list / sheet picked last time (an API reply can have several)
            wanted = previous.sheet if previous else ""
            sheet = next((item for item in sheets if item.label == wanted), None)
            if sheet is None and len(sheets) > 1:
                upload.mode = mode
                upload.save(update_fields=["mode"])
                return redirect("findata:upload_map", upload.id)
            sheet = sheet or sheets[0]
            chosen = mapping.guess(dataset, sheet.headers)
            new_column = any(h not in dataset.remembered_mapping for h in sheet.headers)
            from_column = AS_OF in chosen.values()
            # A periodic report needs exactly one source for its date: a typed date and a
            # date column both (or neither) → the mapping page, where the user picks.
            unclear_date = dataset.is_snapshot and bool(upload.as_of_date) == from_column
            if new_column or unclear_date or mapping.problems(dataset, chosen):
                upload.mode = mode
                upload.save(update_fields=["mode"])
                return redirect("findata:upload_map", upload.id)
            upload.sheet, upload.mode, upload.mapping = sheet.label, mode, chosen
            uploads.stage(upload, sheet, chosen, mode)
    except uploads.UploadProblems as error:
        upload.save(update_fields=["sheet", "mode", "mapping"])
        return _failed(upload, error.problems)
    return redirect("findata:upload_preview", upload.id)


def _problems_page(request, upload: Upload, problems: list[dict]):
    table = RowsTable(
        request.GET,
        problems,
        [
            Column("row", "Result row" if upload.from_database else "Row", type=NUMBER),
            Column("key", "Key"),
            Column("text", "Problem"),
        ],
        table_id="findata-problems",
        base_url=request.path,
        default_sort=(),
        search_fields=("text", "key"),
        search_placeholder="Find a problem",
        noun="problems",
    )
    context = {
        "base_template": conf.get("BASE_TEMPLATE"),
        "company": request.findata_company,
        "dataset": upload.dataset,
        "upload": upload,
        "table": table,
        "count": len(problems),
        "limit_hit": len(problems) >= uploads.MAX_PROBLEMS,
    }
    return table_response(
        request,
        table,
        "findata/upload_problems.html",
        "findata/partials/problems_table.html",
        context,
    )


@require_GET
@company_required(upload=True)
def problems(request, upload_id: int):
    upload = _own_upload(request, upload_id)
    if not upload.problems:
        return redirect("findata:upload_map", upload.id)
    return _problems_page(request, upload, upload.problems)


@require_GET
@company_required(upload=True)
def problems_file(request, upload_id: int):
    upload = _own_upload(request, upload_id)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Problems"
    sheet.append(["Result row" if upload.from_database else "Row", "Key", "Problem"])
    for item in upload.problems:
        sheet.append([item["row"], item.get("key", ""), item["text"]])
    sheet.column_dimensions["B"].width = 20
    sheet.column_dimensions["C"].width = 100
    buffer = io.BytesIO()
    workbook.save(buffer)
    response = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="upload_{upload.id}_problems.xlsx"'
    return response


# ---------- 3. preview, confirm, cancel ----------


@require_GET
@company_required(upload=True)
def preview(request, upload_id: int):
    upload = _own_upload(request, upload_id)
    if upload.status != Upload.PREVIEW:
        return redirect("findata:history", upload.dataset_id)
    counts = _tab_counts(upload)
    tab = request.GET.get("tab", "")
    if tab not in dict(counts):
        many_dates = len(upload.dates) > 1
        tab = "dates" if many_dates else next((key for key, count in counts if count), "new")
    if tab == "dates":
        rows = [
            {
                **item,
                "day": date.fromisoformat(item["date"]),
                "note": "" if item["existing"] else "new date",
            }
            for item in upload.dates
        ]
        columns = [
            Column("day", "As-of date", type=DATE),
            Column("note", ""),
            Column("added", "New", type=NUMBER),
            Column("changed", "Changed", type=NUMBER),
            Column("deleted", "Removed", type=NUMBER),
            Column("existing", "Saved before", type=NUMBER),
        ]
        total, search_fields = len(rows), ()
    else:
        specs, rows, total = uploads.preview_rows(upload, tab)
        columns = [
            Column(
                spec.name,
                spec.label,
                type=TEXT if tab == "changed" else COLUMN_TYPES.get(spec.data_type, TEXT),
            )
            for spec in specs
        ]
        search_fields = tuple(spec.name for spec in specs)
    table = RowsTable(
        request.GET,
        rows,
        columns,
        table_id="findata-preview",
        base_url=request.path,
        default_sort=(),
        search_fields=search_fields,
        search_placeholder="Find a row",
        noun="dates" if tab == "dates" else "rows",
        keep=("tab",),
    )
    context = {
        "base_template": conf.get("BASE_TEMPLATE"),
        "company": request.findata_company,
        "dataset": upload.dataset,
        "upload": upload,
        "table": table,
        "tab": tab,
        "tabs": [(key, label, dict(counts)[key]) for key, label in TABS if key in dict(counts)],
        "total": total,
        "shown_limit": uploads.PREVIEW_ROWS,
        "mode_label": upload.get_mode_display(),
        "mode_help": Upload.MODE_HELP[upload.mode],
        "dates": [{**item, "day": date.fromisoformat(item["date"])} for item in upload.dates],
        "has_changes": upload.has_changes or len(upload.dates) > 1,
    }
    return table_response(
        request, table, "findata/upload_preview.html", "findata/partials/values_table.html", context
    )


def _tab_counts(upload: Upload) -> list[tuple[str, int]]:
    """(tab, rows) for the preview tabs; a periodic report adds "Per date"."""
    counts = [("new", upload.added), ("changed", upload.changed), ("deleted", upload.deleted)]
    return counts + ([("dates", len(upload.dates))] if upload.dates else [])


@require_POST
@company_required(upload=True)
def confirm(request, upload_id: int):
    upload = _own_upload(request, upload_id)
    try:
        uploads.confirm(upload, _user(request), request.POST.get("delete_ok") == "yes")
    except uploads.UploadProblems as error:
        for item in error.problems:
            messages.error(request, item["text"])
        if upload.status == Upload.PREVIEW:
            return redirect("findata:upload_preview", upload.id)
        if upload.status == Upload.MAPPING:  # the preview was out of date
            return redirect("findata:upload_map", upload.id)
        return redirect("findata:history", upload.dataset_id)
    when = f" ({dates_text(upload)})" if upload.dates else ""
    messages.success(
        request,
        f"Saved into {upload.dataset.name}{when}: {upload.added:,} new · "
        f"{upload.changed:,} changed · {upload.deleted:,} deleted.",
    )
    return redirect("findata:history", upload.dataset_id)


@require_POST
@company_required(upload=True)
def cancel(request, upload_id: int):
    upload = _own_upload(request, upload_id)
    uploads.cancel(upload)
    messages.info(request, f"Cancelled. Nothing in {upload.dataset.name} was changed.")
    return redirect("findata:datasets")


# ---------- history and undo ----------


@require_GET
@company_required()
def history(request, pk: int):
    dataset = _own_dataset(request, pk)
    may_upload = companies.can_upload(request)
    latest = uploads.latest_done(dataset) if may_upload else None
    rows = [
        {
            "id": item.id,
            "when": item.confirmed_at or item.created_at,
            "file": item.file_name
            + (f" · {item.sheet.split('›')[-1].strip()}" if "›" in item.sheet else "")
            + (f" · as of {dates_text(item)}" if item.dates and item.mode == Upload.DATES else ""),
            "mode": item.mode,
            "mode_label": item.get_mode_display(),
            "status": item.status,
            "user": item.user,
            "added": item.added,
            "changed": item.changed,
            "deleted": item.deleted,
            "can_undo": latest is not None and item.id == latest.id,
            "open": item.status in (Upload.MAPPING, Upload.PREVIEW),
        }
        for item in dataset.uploads.exclude(status=Upload.CANCELLED)
    ]
    table = RowsTable(
        request.GET,
        rows,
        [
            Column("when", "When", type=DATE),
            Column("file", "File or database"),
            Column("mode", "Mode", type=CHOICE, choices=dict(Upload.MODE_CHOICES)),
            Column("status", "Status", type=CHOICE, choices=dict(Upload.STATUS_CHOICES)),
            Column("user", "By"),
            Column("added", "New", type=NUMBER),
            Column("changed", "Changed", type=NUMBER),
            Column("deleted", "Deleted", type=NUMBER),
        ],
        table_id="findata-history",
        base_url=request.path,
        default_sort=(),
        search_fields=("file", "user"),
        search_placeholder="Find an upload",
        noun="uploads",
    )
    context = {**page_context(request, dataset, "history"), "table": table}
    return table_response(
        request, table, "findata/history.html", "findata/partials/history_table.html", context
    )


@require_http_methods(["GET", "POST"])
@company_required(upload=True)
def undo(request, upload_id: int):
    upload = _own_upload(request, upload_id)
    if request.method == "POST":
        try:
            uploads.undo(upload, _user(request))
        except uploads.UploadProblems as error:
            for item in error.problems:
                messages.error(request, item["text"])
            return redirect("findata:upload_undo", upload.id)
        messages.success(
            request, f"Undone: {upload.dataset.name} is back as it was before {upload.file_name}."
        )
        return redirect("findata:history", upload.dataset_id)
    problems = uploads.undo_problems(upload)
    if problems:
        logger.warning("Undo of upload %s not possible: %s", upload.id, problems)
    context = {
        "company": request.findata_company,
        "dataset": upload.dataset,
        "upload": upload,
        "problems": problems,
    }
    return _page(request, "findata/undo.html", context)


# ---------- periodic reports: dates loaded ----------


@require_GET
@company_required()
def dates(request, pk: int):
    """A 12-month calendar of the As-of dates loaded (and missing), and the list of dates."""
    dataset = _own_dataset(request, pk)
    if not dataset.is_snapshot:
        raise Http404("Only periodic reports have As-of dates.")
    today = timezone.localdate()
    loaded = dict(uploads.loaded_dates(dataset))
    latest = uploads.last_uploads(dataset)
    missing = periods.missing_dates(dataset.how_often, loaded, today)
    try:
        year = int(request.GET.get("year", ""))
    except ValueError:
        year = max(loaded).year if loaded else today.year
    year = min(max(year, 1900), 2200)

    def tip(day: date) -> str:
        if day in loaded:
            rows = loaded[day]
            text = f"{day:%d-%m-%Y} · {rows:,} row{'s' if rows != 1 else ''}"
            upload = latest.get(day)
            if upload is not None:
                who = f" by {upload.user}" if upload.user else ""
                text += f" · uploaded{who} on {timezone.localtime(upload.confirmed_at):%d-%m-%Y}"
            return text
        if day in missing_set:
            return f"{day:%d-%m-%Y} · missing ({dataset.get_how_often_display()})"
        return f"{day:%d-%m-%Y}"

    missing_set = set(missing)
    months = []
    for month in range(1, 13):
        weeks = []
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
            cells = []
            for day in week:
                state = ""
                if day.month != month:
                    state = "outside"
                elif day in loaded:
                    state = "loaded"
                elif day in missing_set:
                    state = "missing"
                elif day > today:
                    state = "future"
                cells.append({"day": day, "state": state, "tip": tip(day)})
            weeks.append(cells)
        months.append({"name": calendar.month_name[month], "weeks": weeks})

    rows = []
    for day, count in sorted(loaded.items(), reverse=True):
        upload = latest.get(day)
        rows.append(
            {
                "date": day,
                "iso": day.isoformat(),
                "rows": count,
                "by": upload.user if upload else "",
                "when": upload.confirmed_at if upload else None,
            }
        )
    table = RowsTable(
        request.GET,
        rows,
        [
            Column("date", "As-of date", type=DATE),
            Column("rows", "Rows", type=NUMBER),
            Column("by", "Last upload by"),
            Column("when", "Last upload on", type=DATE),
        ],
        table_id="findata-dates",
        base_url=request.path,
        default_sort=(),
        search_fields=("by",),
        search_placeholder="Find by user",
        noun="dates",
        keep=("year",),
    )
    in_year = [day for day in loaded if day.year == year]
    context = {
        **page_context(request, dataset, "dates"),
        "table": table,
        "year": year,
        "months": months,
        "loaded_in_year": len(in_year),
        "missing_in_year": [day for day in missing if day.year == year],
        "missing_count": len(missing),
        "how_often_choices": Dataset.HOW_OFTEN_CHOICES,
    }
    return table_response(
        request, table, "findata/dates.html", "findata/partials/dates_table.html", context
    )


@require_POST
@company_required(edit=True)
def how_often(request, pk: int):
    """Staff change which dates are expected. No data changes, so the definition's "changed"
    time stays (undo and open previews keep working)."""
    dataset = _own_dataset(request, pk)
    chosen = request.POST.get("how_often", "")
    if not dataset.is_snapshot or chosen not in dict(Dataset.HOW_OFTEN_CHOICES):
        logger.warning("How often %r refused for %s", chosen[:20], dataset.name)
        messages.error(request, "Pick one of the listed choices.")
    else:
        Dataset.objects.filter(pk=dataset.pk).update(how_often=chosen)
        label = dict(Dataset.HOW_OFTEN_CHOICES)[chosen]
        messages.success(request, f"{dataset.name} is now expected: {label}.")
    return redirect("findata:dates", dataset.id)


@require_http_methods(["GET", "POST"])
@company_required(upload=True)
def remove_date(request, pk: int, day: str):
    dataset = _own_dataset(request, pk)
    try:
        as_of = date.fromisoformat(day)
    except ValueError as error:
        raise Http404("Not a date.") from error
    if not dataset.is_snapshot:
        raise Http404("Only periodic reports have As-of dates.")
    back = reverse("findata:dates", args=[dataset.id]) + f"?year={as_of.year}"
    if request.method == "POST":
        try:
            upload = uploads.remove_date(dataset, as_of, _user(request))
        except uploads.UploadProblems as error:
            for item in error.problems:
                messages.error(request, item["text"])
            return redirect(back)
        messages.success(
            request,
            f"Removed {as_of:%d-%m-%Y} from {dataset.name} ({upload.deleted:,} rows). "
            "Undo it from History if this was a mistake.",
        )
        return redirect(back)
    rows = uploads.rows_on(dataset, as_of)
    context = {
        "company": request.findata_company,
        "dataset": dataset,
        "as_of": as_of,
        "rows": rows,
        "back": back,
    }
    return _page(request, "findata/date_remove.html", context)
