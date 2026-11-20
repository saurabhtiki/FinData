"""Browse screens: a dataset's page (Data and Changes tabs; History and Dates live in
upload_views), one row's history, and the company's Recent uploads. All read-only."""

import csv
import logging
from datetime import date
from urllib.parse import urlencode

from django.db.models.functions import Coalesce
from django.http import Http404, StreamingHttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET
from django_datatable.datatable import (
    CHOICE,
    DATE,
    NUMBER,
    TEXT,
    Column,
    DataTable,
    formula_safe,
    table_response,
)

from . import browse, conf, db_source
from . import company as companies
from . import upload as uploads
from .models import Dataset, DatasetColumn, DbConnection, Upload
from .schema_builder import AS_OF
from .views import _own_dataset, company_required

logger = logging.getLogger(__name__)

FILTER_TYPES = {
    DatasetColumn.TEXT: TEXT,
    DatasetColumn.INTEGER: NUMBER,
    DatasetColumn.DECIMAL: NUMBER,
    DatasetColumn.DATE: DATE,
    DatasetColumn.DATETIME: DATE,
    DatasetColumn.BOOLEAN: CHOICE,
}
YES_NO = {"True": "Yes", "False": "No"}
KIND_LABELS = dict(Dataset.KIND_CHOICES)


def page_context(request, dataset: Dataset, tab: str) -> dict:
    """What the dataset header and its tabs need (Data · Changes · History · Dates)."""
    saved = DbConnection.objects.filter(
        company_key=dataset.company_key, pk=dataset.remembered_source.get("connection") or 0
    ).exists()
    return {
        "base_template": conf.get("BASE_TEMPLATE"),
        "company": request.findata_company,
        "dataset": dataset,
        "kind_label": KIND_LABELS[dataset.kind],
        "tab": tab,
        "can_upload": companies.can_upload(request),
        "can_edit": companies.can_edit_definitions(request),
        "pull_again": saved and db_source.servers_allowed(),
    }


def _when(upload: Upload):
    return upload.confirmed_at or upload.created_at


# ---------- Data tab ----------


def _pick_as_of(text: str, loaded: list[date]) -> date | None:
    """The As-of date asked for in the address, else the latest loaded one."""
    if text:
        try:
            return date.fromisoformat(text)
        except ValueError:
            logger.info("Data page: bad As-of date %r, showing the latest", text)
    return loaded[-1] if loaded else None


def _data_columns(columns: list[DatasetColumn]) -> list[Column]:
    table_columns = [
        Column(
            browse.attr(column),
            column.label,
            type=FILTER_TYPES[column.data_type],
            choices=YES_NO if column.data_type == DatasetColumn.BOOLEAN else None,
        )
        for column in columns
    ]
    table_columns.append(Column("_updated_at", "Last changed", type=DATE, csv=False))
    return table_columns


def _cell_text(value) -> str:
    return "" if value is None or value == "" else uploads.display(value)


def _decorate_rows(dataset: Dataset, columns: list[DatasetColumn], page) -> None:
    """Gives each row on the page its cells (with link names and links), its key for History
    and the upload that last changed it."""
    rows = list(page)
    names, target_keys = {}, {}
    for column in columns:
        if column.references_id:
            codes = {getattr(row, browse.attr(column)) for row in rows}
            names[column.id] = browse.link_names(column.references, codes)
            target_keys[column.id] = column.references.key_columns()[0].name
    batches = Upload.objects.in_bulk({row._batch_id for row in rows if row._batch_id})
    for row in rows:
        cells = []
        for column in columns:
            value = getattr(row, browse.attr(column))
            cell = {
                "name": browse.attr(column),
                "text": _cell_text(value),
                "right": column.data_type in (DatasetColumn.INTEGER, DatasetColumn.DECIMAL),
                "tip": "",
                "url": "",
            }
            if column.references_id and value not in (None, ""):
                target = column.references
                name = names[column.id].get(str(value), "")
                cell["tip"] = (
                    f"{name} · open it in {target.name}" if name else f"Open it in {target.name}"
                )
                cell["url"] = (
                    reverse("findata:data", args=[target.id])
                    + "?"
                    + urlencode({"key": browse.key_json({target_keys[column.id]: value})})
                )
            cells.append(cell)
        row.cells = cells
        row.key_json = browse.key_json(browse.row_key(dataset, row))
        row.last_changed = row._updated_at  # templates can't read names starting with _
        batch = batches.get(row._batch_id)
        row.changed_tip = (
            f"By upload {batch.file_name}" + (f" · {batch.user}" if batch.user else "")
            if batch
            else "Last changed time"
        )


@require_GET
@company_required()
def data(request, pk: int):
    dataset = _own_dataset(request, pk)
    columns = list(dataset.columns.select_related("references"))
    queryset = browse.rows(dataset)
    loaded, as_of, one_key = [], None, None
    if request.GET.get("key"):  # one row, e.g. the account a Sales line links to
        one_key = browse.read_key(dataset, request.GET["key"])
        if one_key is None:
            raise Http404("That row's key isn't one of this dataset's keys.")
        queryset = queryset.filter(
            **{browse.attr(column): one_key[column.name] for column in dataset.key_columns()}
        )
    if dataset.is_snapshot:
        loaded = [day for day, _ in uploads.loaded_dates(dataset)]
        as_of = _pick_as_of(request.GET.get("as_of", ""), loaded)
        queryset = queryset.filter(**{AS_OF: as_of}) if as_of else queryset.none()
    table = DataTable(
        request.GET,
        queryset,
        _data_columns(columns),
        table_id="findata-data",
        default_sort=[browse.attr(column) for column in columns if column.is_key],
        search_fields=[
            browse.attr(column) for column in columns if column.data_type == DatasetColumn.TEXT
        ],
        search_placeholder="Find a row",
        base_url=request.path,
        keep=("as_of", "key"),
        noun="rows",
        default_per_page=50,
        column_toggle=True,
    )
    if table.wants_csv:
        suffix = f"_{as_of.isoformat()}" if as_of else ""
        logger.info("%s downloaded the rows of %s", request.user, dataset.name)
        return table.csv_response(filename=f"{dataset.table_name}{suffix}.csv")
    _decorate_rows(dataset, columns, table.page)
    context = page_context(request, dataset, "data")
    context.update(
        table=table,
        as_of=as_of,
        loaded=loaded,
        as_of_loaded=as_of in loaded,
        one_key=browse.key_text(dataset, one_key) if one_key else "",
        empty_text=(f"No rows for {as_of:%d-%m-%Y}." if as_of else "No rows yet."),
    )
    return table_response(
        request, table, "findata/data.html", "findata/partials/data_table.html", context
    )


# ---------- Changes tab ----------


def _changes_csv(request, dataset: Dataset, table: DataTable) -> StreamingHttpResponse:
    """One line per changed column (New / Deleted rows: one line each)."""
    labels = browse.Labels(dataset)
    writer = csv.writer(_Echo())
    heading = ["When", "By", "Upload", "Change", "Key"]
    heading += ["As-of date"] if dataset.is_snapshot else []
    heading += ["Column", "Old", "New"]

    def safe(values):
        return writer.writerow([formula_safe(str(value)) for value in values])

    def lines():
        yield "﻿"  # lets Excel read accents and ₹ correctly
        yield writer.writerow(heading)
        for line in table.ordered.iterator(chunk_size=2000):
            start = [
                timezone.localtime(line.at).strftime("%Y-%m-%d %H:%M"),
                line.user or "",
                line.file or "",
                browse.ACTION_LABELS.get(line.action, line.action),
                line.key_text,
            ]
            if dataset.is_snapshot:
                start.append(line.as_of.isoformat() if line.as_of else "")
            detail = labels.detail(line)
            if detail.changed:
                for item in detail.changed:
                    yield safe(start + [item.label, item.old, item.new])
            else:
                yield safe(start + ["", "", ""])

    logger.info("%s downloaded the change log of %s", request.user, dataset.name)
    response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
    name = f"{dataset.table_name}_changes_{timezone.localdate().isoformat()}.csv"
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


class _Echo:
    def write(self, value):
        return value


def _upload_label(upload: Upload) -> str:
    return f"{upload.file_name} · {timezone.localtime(_when(upload)):%d-%m-%Y %H:%M}"


@require_GET
@company_required()
def changes(request, pk: int):
    dataset = _own_dataset(request, pk)
    queryset = browse.changes(dataset)
    done = {
        upload.id: upload
        for upload in Upload.objects.filter(
            dataset=dataset, status__in=[Upload.DONE, Upload.UNDONE]
        )
    }
    one_upload = None
    if request.GET.get("upload"):
        try:
            one_upload = done[int(request.GET["upload"])]
        except (ValueError, KeyError):
            logger.warning("Changes of %s: no saved upload %r", dataset.name, request.GET["upload"])
            raise Http404("No such saved upload in this dataset.")
        queryset = queryset.filter(upload_id=one_upload.id)
    key_label = " + ".join(column.label for column in dataset.key_columns()) or "Key"
    columns = [
        Column("at", "When", type=DATE),
        Column("user", "By", type=CHOICE),
        Column(
            "upload_id",
            "Upload",
            type=CHOICE,
            choices={str(upload.id): _upload_label(upload) for upload in done.values()},
        ),
        Column("action", "Change", type=CHOICE, choices=browse.ACTION_LABELS),
        Column("key_text", key_label),
    ]
    if dataset.is_snapshot:
        columns.append(Column("as_of", "As-of date", type=DATE))
    columns.append(Column("detail", "What changed", sortable=False, filterable=False))
    table = DataTable(
        request.GET,
        queryset,
        columns,
        table_id="findata-changes",
        default_sort=("-id",),
        search_fields=("key_text",),
        search_placeholder=f"Find by {key_label.lower()}",
        base_url=request.path,
        keep=("upload",),
        noun="changes",
        default_per_page=50,
    )
    if table.wants_csv:
        return _changes_csv(request, dataset, table)
    labels = browse.Labels(dataset)
    for line in table.page:
        line.detail = labels.detail(line)
        line.key_json = browse.key_json(line.key)
        upload = done.get(line.upload_id)
        line.undone = upload is not None and upload.status == Upload.UNDONE
        line.undone_tip = (
            f"Undone on {timezone.localtime(upload.undone_at):%d-%m-%Y %H:%M}"
            + (f" by {upload.undone_by}" if upload.undone_by else "")
            if line.undone and upload.undone_at
            else ""
        )
    context = page_context(request, dataset, "changes")
    context.update(
        table=table,
        one_upload=one_upload,
        empty_text=(
            "Nothing changed in this upload."
            if one_upload
            else "No changes yet. Each confirmed upload adds its changes here."
        ),
    )
    return table_response(
        request, table, "findata/changes.html", "findata/partials/changes_table.html", context
    )


# ---------- one row ----------


@require_GET
@company_required()
def row_history(request, pk: int):
    dataset = _own_dataset(request, pk)
    key = browse.read_key(dataset, request.GET.get("key", ""))
    if key is None:
        raise Http404("That row's key isn't one of this dataset's keys.")
    try:
        as_of = date.fromisoformat(key[AS_OF]) if dataset.is_snapshot else None
    except (TypeError, ValueError):
        logger.warning("Row history of %s: bad As-of date in %r", dataset.name, key)
        raise Http404("That row's As-of date isn't a date.")
    context = {
        "dataset": dataset,
        "key_text": browse.key_text(dataset, key),
        "as_of": as_of,
        "events": browse.row_history(dataset, key),
    }
    return render(request, "findata/partials/row_history.html", context)


# ---------- Recent uploads ----------


@require_GET
@company_required()
def recent_uploads(request):
    current = request.findata_company
    queryset = (
        Upload.objects.filter(dataset__company_key=current.key)
        .exclude(status=Upload.CANCELLED)
        .select_related("dataset")
        .annotate(when=Coalesce("confirmed_at", "created_at"))
    )
    names = dict(Dataset.objects.filter(company_key=current.key).values_list("id", "name"))
    table = DataTable(
        request.GET,
        queryset,
        [
            Column("when", "When", type=DATE),
            Column(
                "dataset_id",
                "Dataset",
                type=CHOICE,
                choices={str(pk): name for pk, name in names.items()},
            ),
            Column("file_name", "File or database"),
            Column("mode", "Mode", type=CHOICE, choices=dict(Upload.MODE_CHOICES)),
            Column("status", "Status", type=CHOICE, choices=dict(Upload.STATUS_CHOICES)),
            Column("user", "By", type=CHOICE),
            Column("added", "New", type=NUMBER),
            Column("changed", "Changed", type=NUMBER),
            Column("deleted", "Deleted", type=NUMBER),
        ],
        table_id="findata-recent",
        default_sort=("-id",),
        search_fields=("file_name",),
        search_placeholder="Find a file",
        base_url=request.path,
        noun="uploads",
        default_per_page=50,
    )
    context = {
        "base_template": conf.get("BASE_TEMPLATE"),
        "company": current,
        "table": table,
    }
    return table_response(
        request, table, "findata/recent_uploads.html", "findata/partials/recent_table.html", context
    )
