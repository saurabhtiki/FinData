"""API screens: the company's keys (definition editors) and the API help page (anyone)."""

import json
import logging
from datetime import date

from django.contrib import messages
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django_datatable.datatable import DATE, Column, RowsTable, table_response

from . import browse, conf, mapping
from . import company as companies
from .api import json_value, make_key
from .models import ApiKey, Dataset, DatasetColumn
from .schema_builder import AS_OF
from .upload_views import _user
from .views import _page, company_required

logger = logging.getLogger(__name__)


def _switched_off(request):
    return _page(
        request,
        "findata/message.html",
        {
            "title": "The API is switched off",
            "text": "Other systems can't send or read data on this site. The admin can turn it on "
            "with ALLOW_API in settings.py.",
        },
        status=403,
    )


# ---------- keys ----------


def _keys_page(request, extra: dict | None = None, status: int = 200):
    rows = [
        {
            "id": key.id,
            "name": key.name,
            "rights": key.rights,
            "prefix": key.prefix,
            "created_by": key.created_by,
            "created_at": key.created_at,
            "last_used_at": key.last_used_at,
            "expires_on": key.expires_on,
            "state": _state(key),
        }
        for key in ApiKey.objects.filter(company_key=request.findata_company.key)
    ]
    table = RowsTable(
        request.GET,
        rows,
        [
            Column("name", "Name"),
            Column("rights", "May"),
            Column("prefix", "Starts with"),
            Column("created_at", "Made", type=DATE),
            Column("last_used_at", "Last used", type=DATE),
            Column("expires_on", "Works until", type=DATE),
            Column("state", "State"),
        ],
        table_id="findata-api-keys",
        base_url=reverse("findata:api_keys"),
        default_sort=("name",),
        search_fields=("name", "created_by", "prefix"),
        search_placeholder="Find a key",
        noun="keys",
    )
    context = {
        "base_template": conf.get("BASE_TEMPLATE"),
        "company": request.findata_company,
        "table": table,
        "today": timezone.localdate().isoformat(),
        "form": {},
        **(extra or {}),
    }
    response = table_response(
        request, table, "findata/api_keys.html", "findata/partials/api_keys_table.html", context
    )
    response.status_code = status
    return response


def _state(key: ApiKey) -> str:
    if not key.active:
        return "Switched off"
    if key.expires_on and key.expires_on < timezone.localdate():
        return "Ended"
    return "Works"


@require_GET
@company_required(edit=True)
def api_keys(request):
    if not conf.get("ALLOW_API"):
        return _switched_off(request)
    return _keys_page(request)


@require_POST
@company_required(edit=True)
def api_key_new(request):
    if not conf.get("ALLOW_API"):
        return _switched_off(request)
    form = {
        "name": " ".join(request.POST.get("name", "").split())[:100],
        "can_send": request.POST.get("can_send") == "on",
        "can_read": request.POST.get("can_read") == "on",
        "expires_on": request.POST.get("expires_on", "").strip(),
    }
    errors = []
    company_key = request.findata_company.key
    if not form["name"]:
        errors.append("Give the key a name, e.g. “Tally sync”, so you know who uses it.")
    elif ApiKey.objects.filter(company_key=company_key, name=form["name"]).exists():
        errors.append(f"There is already a key named “{form['name']}”. Pick another name.")
    if not (form["can_send"] or form["can_read"]):
        errors.append("Tick what the key may do: Send data, Read data, or both.")
    expires_on = None
    if form["expires_on"]:
        try:
            expires_on = date.fromisoformat(form["expires_on"])
        except ValueError:
            errors.append(f"“{form['expires_on'][:20]}” isn't a date.")
        else:
            if expires_on < timezone.localdate():
                errors.append("The end date is in the past. Pick today or a later date.")
    if not errors:
        try:
            key, raw = make_key(
                company_key,
                form["name"],
                can_send=form["can_send"],
                can_read=form["can_read"],
                created_by=_user(request),
                expires_on=expires_on,
            )
        except IntegrityError:  # the same name, saved a moment ago in another tab
            errors.append(f"There is already a key named “{form['name']}”. Pick another name.")
        else:
            # The key's text is in this response only: it isn't stored, logged or put in a message.
            return _keys_page(request, {"new_key": raw, "new_key_name": key.name})
    logger.warning("New API key for %s refused: %s", company_key, errors)
    return _keys_page(request, {"errors": errors, "form": form}, status=400)


@require_POST
@company_required(edit=True)
def api_key_off(request, key_id: int):
    key = get_object_or_404(ApiKey, pk=key_id, company_key=request.findata_company.key)
    if key.active:
        key.active = False
        key.save(update_fields=["active"])
        logger.info("API key %s switched off by %s", key, _user(request))
        messages.success(request, f"“{key.name}” is switched off. Calls with it are refused now.")
    return redirect("findata:api_keys")


# ---------- help ----------


@require_GET
@company_required()
def api_help(request):
    if not conf.get("ALLOW_API"):
        return _switched_off(request)
    company = request.findata_company
    datasets = list(Dataset.objects.filter(company_key=company.key).prefetch_related("columns"))
    try:
        picked_id = int(request.GET.get("dataset", "0"))
    except ValueError:
        picked_id = 0
    dataset = next((item for item in datasets if item.id == picked_id), None)
    if dataset is None and datasets:
        dataset = datasets[0]
    rows = []
    if dataset is not None:
        required = {name: needed for name, _, needed in mapping.targets(dataset)}
        saved = _first_row(dataset)
        if dataset.is_snapshot:
            rows.append(
                {
                    "name": AS_OF,
                    "label": "As-of date",
                    "type": "Date",
                    "rule": "Key · or send “as_of” once for all rows",
                    "example": "2026-03-31",
                }
            )
        for column in dataset.columns.all():
            rules = ["Key"] if column.is_key else []
            if required.get(column.name) and not column.is_key:
                rules.append("Required")
            if column.references_id:
                rules.append(f"Links to {column.references.name}")
            rows.append(
                {
                    "name": column.name,
                    "label": column.label,
                    "type": column.type_label,
                    "rule": " · ".join(rules),
                    "example": _example(column, saved),
                }
            )
    table = RowsTable(
        request.GET,
        rows,
        [
            Column("name", "Send as"),
            Column("label", "Column"),
            Column("type", "Type"),
            Column("rule", "Rules"),
            Column("example", "Example"),
        ],
        table_id="findata-api-columns",
        base_url=request.path,
        default_sort=(),
        search_fields=("name", "label"),
        search_placeholder="Find a column",
        noun="columns",
        keep=("dataset",),
    )
    sample = {row["name"]: row["example"] for row in rows if row["name"] != AS_OF}

    def address(name: str) -> str:
        return request.build_absolute_uri(reverse(name, args=[dataset.table_name]))

    context = {
        "base_template": conf.get("BASE_TEMPLATE"),
        "company": company,
        "datasets": datasets,
        "dataset": dataset,
        "table": table,
        "send_url": address("findata:api_send") if dataset else "",
        "rows_url": address("findata:api_rows") if dataset else "",
        "sample_json": json.dumps(sample, ensure_ascii=False),
        "sample_python": repr(sample),
        "max_rows": conf.get("API_MAX_ROWS"),
        "page_rows": conf.get("API_PAGE_ROWS"),
        "https_only": conf.get("API_REQUIRE_HTTPS"),
        "can_edit": companies.can_edit_definitions(request),
    }
    return table_response(
        request, table, "findata/api_help.html", "findata/partials/api_columns_table.html", context
    )


EXAMPLES = {
    DatasetColumn.TEXT: "text",
    DatasetColumn.INTEGER: 12,
    DatasetColumn.DECIMAL: "1500.50",
    DatasetColumn.DATE: "2026-03-31",
    DatasetColumn.DATETIME: "2026-03-31 14:30:00",
    DatasetColumn.BOOLEAN: True,
}


def _first_row(dataset: Dataset):
    """The first saved row (by key), so the examples look like the company's own data."""
    order = [browse.attr(column) for column in dataset.key_columns()] + [browse.ROW_ID]
    return browse.rows(dataset).order_by(*order).first()


def _example(column: DatasetColumn, saved) -> object:
    value = getattr(saved, browse.attr(column), None) if saved is not None else None
    return EXAMPLES[column.data_type] if value is None else json_value(value)
