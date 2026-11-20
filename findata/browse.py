"""Reading saved rows and the change log for the browse screens (read-only).

A company's tables have no Django model (each company defines its own columns), so this module
builds one at run time from the definition, e.g. Sales -> a model on "co_acme"."sales" with
fields voucher_no, line_no, amount... django-datatable can then filter, sort, page and stream
CSV in SQL. The models live in their own registry, so they never reach migrations.

The change log (_changes, one per company schema) is read the same way. Each line holds
key {"voucher_no": "S1", "line_no": 2}, old (the whole row before) and new (the values after).
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from itertools import count

from django.apps.registry import Apps
from django.db import connection, models
from django.db.models import Func, OuterRef, Subquery, TextField, Value
from django.db.models.fields.json import KT
from django.db.models.functions import Cast

from .models import Dataset, DatasetColumn, Upload
from .schema_builder import AS_OF, table_exists
from .upload import CHANGES, display

logger = logging.getLogger(__name__)

_registry = Apps()
_versions = count(1)
_row_models: dict[int, tuple] = {}
_change_models: dict[str, type] = {}
# Names a Django model can't use as a field: such columns get the name col<id> on the model.
_TAKEN = {
    "pk",
    "id",
    "objects",
    "save",
    "delete",
    "check",
    "clean",
    "full_clean",
    "refresh_from_db",
}
ACTION_LABELS = {"added": "New", "changed": "Changed", "deleted": "Deleted"}
SYSTEM = {"_batch_id", "_updated_at", AS_OF}
ROW_ID = "_rowid"  # PostgreSQL's ctid: unique per row, so counts and the tie-break sort are right


class _DecimalJSON(json.JSONDecoder):
    """Change-log numbers as exact Decimals (500.10 stays 500.10, not 500.1000000001)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, parse_float=Decimal, **kwargs)


def attr(column: DatasetColumn) -> str:
    """The model field name of a column (its database name unless Django can't use it)."""
    name = column.name
    if name in _TAKEN or "__" in name or name.endswith("_"):
        return f"col{column.id}"
    return name


def _field(column: DatasetColumn) -> models.Field:
    options = {"null": True, "db_column": column.name}
    kind = column.data_type
    if kind == DatasetColumn.INTEGER:
        return models.BigIntegerField(**options)
    if kind == DatasetColumn.DECIMAL:
        return models.DecimalField(max_digits=18, decimal_places=column.decimal_places, **options)
    if kind == DatasetColumn.DATE:
        return models.DateField(**options)
    if kind == DatasetColumn.DATETIME:
        return models.DateTimeField(**options)
    if kind == DatasetColumn.BOOLEAN:
        return models.BooleanField(**options)
    return models.TextField(**options)


def _model(name: str, table: str, fields: dict) -> type:
    meta = type(
        "Meta",
        (),
        {"apps": _registry, "app_label": "findata_browse", "managed": False, "db_table": table},
    )
    return type(name, (models.Model,), {"__module__": __name__, "Meta": meta, **fields})


def _table(schema: str, name: str) -> str:
    # Django quotes db_table as "<db_table>", so this becomes "co_acme"."sales".
    return f'{schema}"."{name}'


def row_model(dataset: Dataset) -> type:
    """A read-only model of the dataset's table, rebuilt when the definition changes."""
    columns = list(dataset.columns.all())
    stamp = (dataset.updated_at, dataset.schema, dataset.table_name, len(columns))
    cached = _row_models.get(dataset.id)
    if cached and cached[0] == stamp:
        return cached[1]
    fields = {ROW_ID: models.CharField(primary_key=True, db_column="ctid", max_length=40)}
    if dataset.is_snapshot:
        fields[AS_OF] = models.DateField(db_column=AS_OF)
    for column in columns:
        fields[attr(column)] = _field(column)
    fields["_batch_id"] = models.BigIntegerField(null=True, db_column="_batch_id")
    fields["_updated_at"] = models.DateTimeField(null=True, db_column="_updated_at")
    model = _model(
        f"Rows{dataset.id}v{next(_versions)}",
        _table(dataset.schema, dataset.table_name),
        fields,
    )
    if cached:  # forget the old version, or every definition edit would keep one more model
        _registry.all_models["findata_browse"].pop(cached[1]._meta.model_name, None)
        _registry.clear_cache()
    _row_models[dataset.id] = (stamp, model)
    return model


def rows(dataset: Dataset):
    """All saved rows of a dataset (empty if its table isn't there)."""
    model = row_model(dataset)
    if not table_exists(dataset.schema, dataset.table_name):
        logger.warning("Browse %s: table %s is missing", dataset.name, dataset.table_name)
        return model.objects.none()
    return model.objects.all()


def _change_model(schema: str) -> type:
    if schema not in _change_models:
        _change_models[schema] = _model(
            f"Changes{len(_change_models) + 1}v{next(_versions)}",
            _table(schema, CHANGES),
            {
                "id": models.BigAutoField(primary_key=True),
                "upload_id": models.BigIntegerField(),
                "dataset_id": models.BigIntegerField(),
                "action": models.TextField(),
                "key": models.JSONField(decoder=_DecimalJSON),
                "old": models.JSONField(null=True, decoder=_DecimalJSON),
                "new": models.JSONField(null=True, decoder=_DecimalJSON),
                "at": models.DateTimeField(),
            },
        )
    return _change_models[schema]


def has_change_log(dataset: Dataset) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s)", [f'"{dataset.schema}"."{CHANGES}"'])
        return cursor.fetchone()[0] is not None


def changes(dataset: Dataset):
    """The dataset's change log lines, each with key_text ("S1 + 2"), user, file
    and (periodic reports) as_of, so the table can filter and search on them."""
    model = _change_model(dataset.schema)
    if not has_change_log(dataset):
        return model.objects.none()
    upload = Upload.objects.filter(pk=OuterRef("upload_id"))
    key_parts = [KT(f"key__{column.name}") for column in dataset.key_columns()]
    extra = {}
    if dataset.is_snapshot:
        extra["as_of"] = Cast(KT(f"key__{AS_OF}"), models.DateField())
    return model.objects.filter(dataset_id=dataset.id).annotate(
        key_text=Func(Value(" + "), *key_parts, function="concat_ws", output_field=TextField()),
        user=Subquery(upload.values("user")),
        file=Subquery(upload.values("file_name")),
        **extra,
    )


# ---------- keys ----------


def _plain(value):
    """A key value as JSON writes it (dates as 2026-03-31, exact numbers as numbers)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def row_key(dataset: Dataset, row) -> dict:
    """The change-log key of a saved row, e.g. {"voucher_no": "S1", "line_no": 2}."""
    key = {column.name: _plain(getattr(row, attr(column))) for column in dataset.key_columns()}
    if dataset.is_snapshot:
        key[AS_OF] = _plain(getattr(row, AS_OF))
    return key


def key_json(key: dict) -> str:
    return json.dumps({name: _plain(value) for name, value in key.items()}, sort_keys=True)


def read_key(dataset: Dataset, text: str) -> dict | None:
    """A key sent back by the page; None if it isn't one of this dataset's keys."""
    try:
        key = json.loads(text)
    except (TypeError, ValueError):
        logger.warning("Row history of %s: unreadable key %r", dataset.name, text)
        return None
    names = {column.name for column in dataset.key_columns()}
    if dataset.is_snapshot:
        names.add(AS_OF)
    if not isinstance(key, dict) or set(key) != names:
        logger.warning("Row history of %s: key %r doesn't fit", dataset.name, text)
        return None
    return key


def key_text(dataset: Dataset, key: dict) -> str:
    return " + ".join(shown(column, key.get(column.name)) for column in dataset.key_columns())


# ---------- values ----------


def _typed(column: DatasetColumn | None, value):
    """A change-log value back as the column's type (JSON keeps dates as text)."""
    if value is None or column is None:
        return value
    try:
        if column.data_type == DatasetColumn.DATE and isinstance(value, str):
            return date.fromisoformat(value[:10])
        if column.data_type == DatasetColumn.DATETIME and isinstance(value, str):
            return datetime.fromisoformat(value)
    except ValueError:
        return value
    return value


def shown(column: DatasetColumn | None, value) -> str:
    """A value as the user would type it: 1500.50, 01-04-2026, Yes, (blank)."""
    return display(_typed(column, value))


@dataclass
class ColumnChange:
    label: str
    old: str
    new: str


@dataclass
class Detail:
    """What one change-log line did to the row, in words."""

    changed: list[ColumnChange] = field(default_factory=list)
    values: list[ColumnChange] = field(default_factory=list)  # for New / Deleted: the row

    @property
    def text(self) -> str:
        return " · ".join(f"{item.label}: {item.old} → {item.new}" for item in self.changed)


class Labels:
    """Column name -> column, also for columns removed since ("gst_no (removed column)")."""

    def __init__(self, dataset: Dataset):
        self.columns = {column.name: column for column in dataset.columns.all()}
        self.order = {name: index for index, name in enumerate(self.columns)}

    def _in_order(self, values: dict) -> list:
        """Columns in the definition's order (JSON keeps them in its own order), removed last."""
        return sorted(values.items(), key=lambda item: self.order.get(item[0], len(self.order)))

    def label(self, name: str) -> str:
        column = self.columns.get(name)
        return column.label if column else f"{name} (removed column)"

    def detail(self, line) -> Detail:
        old, new = line.old or {}, line.new or {}
        detail = Detail()
        if line.action == "changed":
            for name, value in self._in_order(new):
                if name in SYSTEM or old.get(name) == value:
                    continue
                column = self.columns.get(name)
                detail.changed.append(
                    ColumnChange(
                        self.label(name), shown(column, old.get(name)), shown(column, value)
                    )
                )
            return detail
        values = new if line.action == "added" else old
        for name, value in self._in_order(values):
            if name in SYSTEM or value in (None, ""):
                continue
            column = self.columns.get(name)
            text = shown(column, value)
            detail.values.append(ColumnChange(self.label(name), text, text))
        return detail


# ---------- one row's story ----------


@dataclass
class Event:
    when: datetime
    who: str
    file: str
    upload_id: int
    action: str  # added / changed / deleted / undone
    detail: Detail | None = None


def row_history(dataset: Dataset, key: dict) -> list[Event]:
    """Everything that happened to one row (for a periodic report: one key on one As-of date),
    oldest first. An undone upload adds its own "Undone" line."""
    if not has_change_log(dataset):
        return []
    lines = list(
        _change_model(dataset.schema).objects.filter(dataset_id=dataset.id, key=key).order_by("id")
    )
    uploads = Upload.objects.in_bulk({line.upload_id for line in lines})
    labels = Labels(dataset)
    events = []
    for line in lines:
        upload = uploads.get(line.upload_id)
        events.append(
            Event(
                when=(upload.confirmed_at if upload else None) or line.at,
                who=upload.user if upload else "",
                file=upload.file_name if upload else "(upload removed)",
                upload_id=line.upload_id,
                action=line.action,
                detail=labels.detail(line),
            )
        )
        if upload and upload.status == Upload.UNDONE and upload.undone_at:
            events.append(
                Event(upload.undone_at, upload.undone_by, upload.file_name, upload.id, "undone")
            )
    return sorted(events, key=lambda event: event.when)


# ---------- links ----------


def name_column(target: Dataset) -> DatasetColumn | None:
    """The column that names a linked row, e.g. Account name: the first text column that is
    not part of the key."""
    for column in target.columns.all():
        if column.data_type == DatasetColumn.TEXT and not column.is_key:
            return column
    return None


def link_names(target: Dataset, codes: set) -> dict[str, str]:
    """{code: name} for the codes on one page, e.g. {"4010": "Sales – domestic"}."""
    codes = {code for code in codes if code not in (None, "")}
    named_by = name_column(target)
    keys = target.key_columns()
    if not codes or named_by is None or not keys:
        return {}
    key = attr(keys[0])
    found = rows(target).filter(**{f"{key}__in": codes}).values_list(key, attr(named_by))
    return {str(code): name or "" for code, name in found}
