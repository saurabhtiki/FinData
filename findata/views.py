"""FinData screens: dataset list, new dataset (sample file / copy / blank), editor, delete,
blank Excel template."""

import logging
from functools import wraps

from data_core import conf as data_core_conf
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django_datatable.datatable import CHOICE, DATE, NUMBER, Column, RowsTable, table_response

from . import api_source, conf, db_source, definition, naming
from . import company as companies
from .models import Dataset, DatasetColumn, DbConnection
from .sample import SampleRejected, guess_from_file
from .schema_builder import (
    SchemaBlocked,
    TableState,
    approximate_row_counts,
    drop_table,
    row_count,
)
from .template_file import build_template
from .upload import forget_dataset

logger = logging.getLogger(__name__)

TABLE_ID = "findata-datasets"
KIND_LABELS = dict(Dataset.KIND_CHOICES)


def _page(request, template: str, context: dict, status: int = 200):
    context = {"base_template": conf.get("BASE_TEMPLATE"), **context}
    return render(request, template, context, status=status)


def company_required(edit: bool = False, upload: bool = False):
    """Every page works on the host's current company; editing and uploading pages also need
    the host's permission."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            current = companies.current_company(request)
            if current is None:
                return _page(request, "findata/no_company.html", {}, status=200)
            if edit and not companies.can_edit_definitions(request):
                logger.warning("%s may not edit definitions", request.user)
                return _page(
                    request,
                    "findata/message.html",
                    {
                        "title": "Not allowed",
                        "text": "You can view datasets, but only people allowed by your "
                        "administrator can create, change or delete them.",
                    },
                    status=403,
                )
            if upload and not companies.can_upload(request):
                logger.warning("%s may not upload", request.user)
                return _page(
                    request,
                    "findata/message.html",
                    {
                        "title": "Not allowed",
                        "text": "You can view datasets, but only people allowed by your "
                        "administrator can upload or undo uploads.",
                    },
                    status=403,
                )
            request.findata_company = current
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def _own_dataset(request, pk: int) -> Dataset:
    """A dataset of the current company only (another company's id gives "not found")."""
    return get_object_or_404(Dataset, pk=pk, company_key=request.findata_company.key)


# ---------- list ----------


@require_GET
@company_required()
def datasets(request):
    current = request.findata_company
    counts = approximate_row_counts(current.schema)
    servers_on = db_source.servers_allowed()
    saved = set(DbConnection.objects.filter(company_key=current.key).values_list("id", flat=True))
    rows = []
    for dataset in Dataset.objects.filter(company_key=current.key).prefetch_related(
        "columns__references", "linked_from__dataset"
    ):
        columns = list(dataset.columns.all())
        rows.append(
            {
                "id": dataset.id,
                "name": dataset.name,
                "table": f"{dataset.schema}.{dataset.table_name}",
                "kind": dataset.kind,
                "columns": len(columns),
                "key": " + ".join(column.label for column in columns if column.is_key),
                "links": ", ".join(sorted({c.references.name for c in columns if c.references_id})),
                "linked_from": sorted({c.dataset.name for c in dataset.linked_from.all()}),
                "rows": counts.get(dataset.table_name, 0),
                "updated": timezone.localdate(dataset.updated_at),
                "description": dataset.description,
                "pull_again": api_source.again_tab(dataset, saved),
            }
        )
    table = RowsTable(
        request.GET,
        rows,
        [
            Column("name", "Dataset"),
            Column("kind", "Kind", type=CHOICE, choices=KIND_LABELS),
            Column("key", "Key"),
            Column("links", "Links to"),
            Column("columns", "Columns", type=NUMBER),
            Column("rows", "Rows (approx.)", type=NUMBER),
            Column("updated", "Changed", type=DATE),
        ],
        table_id=TABLE_ID,
        base_url=request.path,
        default_sort=("name",),
        search_fields=("name", "key", "links", "description"),
        search_placeholder="Find a dataset",
        noun="datasets",
    )
    context = {
        "base_template": conf.get("BASE_TEMPLATE"),
        "company": current,
        "table": table,
        "can_edit": companies.can_edit_definitions(request),
        "can_upload": companies.can_upload(request),
        "kind_help": Dataset.KIND_HELP,
        "servers_on": servers_on,
    }
    return table_response(
        request, table, "findata/datasets.html", "findata/partials/datasets_table.html", context
    )


# ---------- new dataset ----------


@require_GET
@company_required(edit=True)
def new_dataset(request):
    current = request.findata_company
    sources = []
    for other in companies.other_companies(request, current):
        names = list(Dataset.objects.filter(company_key=other.key).values_list("id", "name"))
        if names:
            sources.append({"company": other, "datasets": names})
    return _page(request, "findata/new.html", {"company": current, "copy_sources": sources})


@require_GET
@company_required(edit=True)
def new_blank(request):
    return _editor(request, definition.blank_payload())


@require_POST
@company_required(edit=True)
def new_from_sample(request):
    upload = request.FILES.get("sample")
    if upload is None:
        messages.error(request, "Choose a CSV or Excel file first.")
        return redirect("findata:new")
    max_mb = data_core_conf.get("MAX_FILE_MB")
    if upload.size > max_mb * 1024 * 1024:
        logger.warning("Sample %s too large: %s bytes", upload.name, upload.size)
        messages.error(request, f"{upload.name} is larger than the {max_mb} MB limit.")
        return redirect("findata:new")
    try:
        guesses = guess_from_file(upload.name, upload.read())
    except SampleRejected as error:
        messages.error(request, f"{upload.name}: {error}")
        return redirect("findata:new")
    first = guesses[0]
    payload = {
        "name": first.name,
        "kind": Dataset.MASTER,
        "description": "",
        "columns": first.columns,
    }
    return _editor(request, payload, sheets=[guess.as_dict() for guess in guesses])


@require_GET
@company_required(edit=True)
def new_from_copy(request, pk: int):
    current = request.findata_company
    allowed = {other.key for other in companies.other_companies(request, current)}
    source = get_object_or_404(Dataset, pk=pk, company_key__in=allowed)
    payload, notes = definition.copy_payload(source, current.key)
    return _editor(request, payload, notes=notes)


@require_POST
@company_required(edit=True)
def create(request):
    return _save(request, None)


# ---------- edit / delete / template ----------


@company_required(edit=True)
def edit(request, pk: int):
    dataset = _own_dataset(request, pk)
    if request.method == "POST":
        return _save(request, dataset)
    return _editor(request, definition.payload_of(dataset), dataset=dataset)


@require_POST
@company_required(edit=True)
def delete(request, pk: int):
    dataset = _own_dataset(request, pk)
    linking = sorted(
        {column.dataset.name for column in dataset.linked_from.select_related("dataset")}
    )
    if linking:
        message = (
            f"{dataset.name} can't be deleted: {', '.join(linking)} link(s) to it. "
            "Remove those links first."
        )
        logger.warning(message)
        messages.error(request, message)
        return redirect("findata:datasets")
    with transaction.atomic():
        forget_dataset(dataset)
        drop_table(TableState.of(dataset))
        dataset.delete()
    messages.success(request, f"Deleted {dataset.name} and its table.")
    return redirect("findata:datasets")


@require_GET
@company_required()
def template(request, pk: int):
    dataset = _own_dataset(request, pk)
    response = HttpResponse(
        build_template(dataset),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{dataset.table_name}_template.xlsx"'
    return response


# ---------- editor ----------


def _save(request, dataset: Dataset | None):
    current = request.findata_company
    payload = definition.parse(request.POST.get("definition", ""))
    if payload is None:
        logger.warning("Editor sent unreadable definition")
        messages.error(request, "The form could not be read. Reload the page and try again.")
        return redirect("findata:datasets")
    checked = definition.check(current.key, payload, dataset)
    if not checked.ok:
        return _editor(request, checked.payload, dataset=dataset, checked=checked, status=400)
    try:
        saved = definition.save(current.key, checked, dataset)
    except SchemaBlocked as error:
        checked.form_errors.extend(error.problems)
        return _editor(request, checked.payload, dataset=dataset, checked=checked, status=400)
    verb = "Saved changes to" if dataset else "Created"
    messages.success(request, f"{verb} {saved.name} (table {saved.schema}.{saved.table_name}).")
    return redirect("findata:datasets")


def _editor(request, payload, *, dataset=None, checked=None, sheets=None, notes=None, status=200):
    current = request.findata_company
    rows = row_count(dataset) if dataset else None
    linking = (
        sorted({c.dataset.name for c in dataset.linked_from.select_related("dataset")})
        if dataset
        else []
    )
    context = {
        "company": current,
        "dataset": dataset,
        "payload_json": payload,
        "targets_json": definition.link_targets(current.key, exclude=dataset),
        "sheets_json": sheets or [],
        "types_json": [
            {"value": value, "label": label, "help": DatasetColumn.TYPE_HELP[value]}
            for value, label in DatasetColumn.TYPE_CHOICES
        ],
        "kinds": [
            (value, label, Dataset.KIND_HELP[value]) for value, label in Dataset.KIND_CHOICES
        ],
        "form_errors": checked.form_errors if checked else [],
        "row_errors_json": {str(k): v for k, v in (checked.row_errors if checked else {}).items()},
        "notes": notes or [],
        "row_count": rows or 0,
        "linking": linking,
        "reserved_json": sorted(naming.RESERVED),
    }
    return _page(request, "findata/editor.html", context, status=status)
