"""The dataset editor's data: checking what the user typed, saving it, and pre-filling the grid.

The editor posts the whole definition as JSON, e.g.
    {"name": "Sales", "kind": "transaction", "description": "",
     "columns": [{"id": null, "label": "Voucher no", "name": "voucher_no", "data_type": "text",
                  "decimal_places": 2, "is_key": true, "required": true, "unique": false,
                  "references": null, "help_text": ""}, ...]}
"""

import json
import logging
from dataclasses import dataclass, field

from django.db import transaction

from . import naming
from .models import Dataset, DatasetColumn
from .schema_builder import TableState, apply_changes

logger = logging.getLogger(__name__)

MAX_COLUMNS = 200
MAX_DECIMAL_PLACES = 6
TYPES = dict(DatasetColumn.TYPE_CHOICES)
KINDS = dict(Dataset.KIND_CHOICES)


@dataclass
class Checked:
    """A definition after checking: cleaned values plus problems, per row and overall."""

    payload: dict
    form_errors: list[str] = field(default_factory=list)
    row_errors: dict[int, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.form_errors and not self.row_errors

    def row_error(self, index: int, message: str) -> None:
        self.row_errors.setdefault(index, []).append(message)


def blank_column() -> dict:
    return {
        "id": None, "label": "", "name": "", "data_type": DatasetColumn.TEXT,
        "decimal_places": 2, "is_key": False, "required": False, "unique": False,
        "references": None, "help_text": "", "samples": [],
    }  # fmt: skip


def blank_payload() -> dict:
    return {"name": "", "kind": Dataset.MASTER, "description": "", "columns": [blank_column()]}


def payload_of(dataset: Dataset) -> dict:
    """An existing dataset, in the editor's shape."""
    return {
        "name": dataset.name,
        "kind": dataset.kind,
        "description": dataset.description,
        "columns": [
            {
                "id": column.id,
                "label": column.label,
                "name": column.name,
                "data_type": column.data_type,
                "decimal_places": column.decimal_places,
                "is_key": column.is_key,
                "required": column.required,
                "unique": column.unique,
                "references": column.references_id,
                "help_text": column.help_text,
                "samples": [],
            }
            for column in dataset.columns.all()
        ],
    }


def copy_payload(source: Dataset, company_key: str) -> tuple[dict, list[str]]:
    """Another company's dataset as a new, unsaved definition for this company.
    A link is kept when this company has a dataset with the same table name."""
    payload = payload_of(source)
    notes = []
    here = {
        dataset.table_name: dataset.id
        for dataset in Dataset.objects.filter(company_key=company_key)
    }
    for column in payload["columns"]:
        column["id"] = None
        if column["references"]:
            target = Dataset.objects.get(pk=column["references"])
            column["references"] = here.get(target.table_name)
            if column["references"] is None:
                notes.append(
                    f"“{column['label']}” linked to {target.name}, which this company doesn't "
                    "have yet, so the link was removed. Add it again after defining that dataset."
                )
    return payload, notes


def link_targets(company_key: str, exclude: Dataset | None = None) -> list[dict]:
    """Datasets a column can link to, with their key, for the editor's "Links to" list."""
    targets = []
    datasets = Dataset.objects.filter(company_key=company_key).prefetch_related("columns")
    for dataset in datasets:
        if exclude is not None and dataset.pk == exclude.pk:
            continue
        if dataset.is_snapshot:  # its key repeats on every As-of date, so it can't be linked
            continue
        keys = dataset.key_columns()
        key = keys[0] if len(keys) == 1 else None
        targets.append(
            {
                "id": dataset.id,
                "name": dataset.name,
                "key_label": key.label if key else "",
                "data_type": key.data_type if key else "",
                "decimal_places": key.decimal_places if key else 2,
                "usable": key is not None,
            }
        )
    return targets


def parse(raw: str) -> dict | None:
    try:
        payload = json.loads(raw or "")
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def check(company_key: str, payload: dict, dataset: Dataset | None = None) -> Checked:
    """Clean the posted definition and list every problem in plain words."""
    columns_in = payload.get("columns")
    columns_in = columns_in if isinstance(columns_in, list) else []
    clean = {
        "name": _text(payload.get("name"), 100),
        "kind": payload.get("kind") if payload.get("kind") in KINDS else "",
        "description": _text(payload.get("description"), 2000),
        "columns": [],
    }
    checked = Checked(clean)

    if not clean["name"]:
        checked.form_errors.append("Give the dataset a name, e.g. Chart of Accounts.")
    if not clean["kind"]:
        checked.form_errors.append(
            "Pick what kind of data this is: Master, Transaction or Periodic report."
        )
    table_name = naming.to_db_name(clean["name"])
    clean["table_name"] = table_name
    if clean["name"]:
        problem = naming.name_problem(table_name, "Table name")
        if problem:
            checked.form_errors.append(problem)
    others = Dataset.objects.filter(company_key=company_key)
    if dataset is not None:
        others = others.exclude(pk=dataset.pk)
    if clean["name"] and others.filter(name__iexact=clean["name"]).exists():
        checked.form_errors.append(f"This company already has a dataset called “{clean['name']}”.")
    elif table_name and others.filter(table_name=table_name).exists():
        checked.form_errors.append(
            f"Another dataset already uses the table name “{table_name}”. Pick a different name."
        )

    if not columns_in:
        checked.form_errors.append("Add at least one column.")
    if len(columns_in) > MAX_COLUMNS:
        checked.form_errors.append(f"A dataset can have at most {MAX_COLUMNS} columns.")
        columns_in = columns_in[:MAX_COLUMNS]

    own_ids = set(dataset.columns.values_list("id", flat=True)) if dataset else set()
    targets = {target["id"]: target for target in link_targets(company_key, exclude=dataset)}
    seen_names: dict[str, int] = {}
    for index, item in enumerate(columns_in):
        item = item if isinstance(item, dict) else {}
        column = blank_column()
        column["id"] = item.get("id") if item.get("id") in own_ids else None
        column["label"] = _text(item.get("label"), 100)
        column["name"] = _text(item.get("name"), 60) or naming.to_db_name(column["label"])
        column["help_text"] = _text(item.get("help_text"), 200)
        column["data_type"] = item.get("data_type") if item.get("data_type") in TYPES else ""
        try:
            column["decimal_places"] = int(item.get("decimal_places", 2))
        except (TypeError, ValueError):
            column["decimal_places"] = -1
        column["is_key"] = bool(item.get("is_key"))
        column["required"] = bool(item.get("required")) or column["is_key"]
        column["unique"] = bool(item.get("unique"))
        column["references"] = item.get("references") or None
        column["samples"] = item.get("samples") if isinstance(item.get("samples"), list) else []
        clean["columns"].append(column)

        if not column["label"]:
            checked.row_error(index, "Type a column name, e.g. Account code.")
        problem = naming.name_problem(column["name"])
        if column["label"] and problem:
            checked.row_error(index, problem)
        if column["name"] in seen_names:
            checked.row_error(
                index,
                f"Database name “{column['name']}” is already used by row {seen_names[column['name']] + 1}.",
            )
        seen_names.setdefault(column["name"], index)
        if not column["data_type"]:
            checked.row_error(index, "Pick a type.")
        if column["data_type"] == DatasetColumn.DECIMAL and not (
            0 <= column["decimal_places"] <= MAX_DECIMAL_PLACES
        ):
            checked.row_error(index, f"Decimal places must be 0 to {MAX_DECIMAL_PLACES}.")
        if column["data_type"] != DatasetColumn.DECIMAL:
            column["decimal_places"] = 2
        if column["references"] is not None:
            _check_link(checked, index, column, targets, dataset)

    if columns_in and not any(column["is_key"] for column in clean["columns"]):
        checked.form_errors.append(
            "Tick Key on the column(s) that identify one row, e.g. Account code, "
            "or Voucher no + Line no. Uploads use the key to find new and changed rows."
        )
    return checked


def _check_link(checked: Checked, index: int, column: dict, targets: dict, dataset) -> None:
    if dataset is not None and column["references"] == dataset.id:
        checked.row_error(
            index, "A link to the same dataset (e.g. parent account) isn't supported yet."
        )
        return
    target = targets.get(column["references"])
    if target is None:
        if Dataset.objects.filter(pk=column["references"], kind=Dataset.SNAPSHOT).exists():
            checked.row_error(
                index,
                "A periodic report can't be linked to: its key repeats on every As-of date. "
                "Link to a master instead.",
            )
            return
        checked.row_error(index, "The linked dataset no longer exists. Pick another one.")
        return
    if not target["usable"]:
        checked.row_error(
            index,
            f"{target['name']} needs a key of exactly one column before anything can link to it.",
        )
        return
    same_type = target["data_type"] == column["data_type"] and (
        column["data_type"] != DatasetColumn.DECIMAL
        or target["decimal_places"] == column["decimal_places"]
    )
    if not same_type:
        wanted = TYPES[target["data_type"]]
        checked.row_error(
            index,
            f"{target['name']}'s key ({target['key_label']}) is {wanted}; make this column {wanted} too.",
        )


def save(company_key: str, checked: Checked, dataset: Dataset | None = None) -> Dataset:
    """Save the definition and build / change its table, all or nothing.
    Raises schema_builder.SchemaBlocked when the table's data doesn't allow a change."""
    clean = checked.payload
    with transaction.atomic():
        before = TableState.of(dataset) if dataset is not None else None
        if dataset is None:
            dataset = Dataset(company_key=company_key)
        dataset.name = clean["name"]
        dataset.table_name = clean["table_name"]
        dataset.kind = clean["kind"]
        dataset.description = clean["description"]
        dataset.save()

        existing = {column.id: column for column in dataset.columns.all()}
        old_names = {column_id: column.name for column_id, column in existing.items()}
        kept = []
        for position, item in enumerate(clean["columns"]):
            column = existing.get(item["id"]) or DatasetColumn(dataset=dataset)
            column.label = item["label"]
            column.name = item["name"]
            column.data_type = item["data_type"]
            column.decimal_places = item["decimal_places"]
            column.is_key = item["is_key"]
            column.required = item["required"]
            column.unique = item["unique"]
            column.references_id = item["references"]
            column.help_text = item["help_text"]
            column.position = position
            kept.append(column)
        dataset.columns.exclude(id__in=[column.id for column in kept if column.id]).delete()
        # Renamed columns first get a temporary name, so two columns can swap names.
        for column in kept:
            if column.id and old_names[column.id] != column.name:
                DatasetColumn.objects.filter(pk=column.id).update(name=f"_rename_{column.id}")
        for column in kept:
            column.save()
        apply_changes(dataset, before)
    logger.info("Saved definition %s (%s) for %s", dataset.name, dataset.table_name, company_key)
    return dataset
