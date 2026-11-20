"""Dataset definitions. The definitions live in Django's own tables; each dataset's rows live in
a real PostgreSQL table in the company's schema (built by schema_builder), e.g. co_acme.sales."""

from django.db import models

from .company import schema_for


class Dataset(models.Model):
    MASTER, TRANSACTION, SNAPSHOT = "master", "transaction", "snapshot"
    KIND_CHOICES = [
        (MASTER, "Master"),
        (TRANSACTION, "Transaction"),
        (SNAPSHOT, "Periodic report"),
    ]
    KIND_HELP = {
        MASTER: "A list that changes slowly, e.g. chart of accounts, debtors, stock items.",
        TRANSACTION: "Day-to-day entries, e.g. sales, purchase, cash / bank vouchers.",
        SNAPSHOT: "A report taken on a date, kept per as-of date for trends, "
        "e.g. receivables, daily trial balance.",
    }

    # Periodic reports: which as-of dates are expected (shows missing dates, suggests the next).
    ANY, DAILY, WEEKDAYS, MON_SAT, MONTH_END, QUARTER_END = (
        "any", "daily", "weekdays", "mon_sat", "month_end", "quarter_end",
    )  # fmt: skip
    HOW_OFTEN_CHOICES = [
        (ANY, "Any date"),
        (DAILY, "Every day"),
        (WEEKDAYS, "Weekdays (Mon–Fri)"),
        (MON_SAT, "Mon–Sat"),
        (MONTH_END, "Month-end"),
        (QUARTER_END, "Quarter-end"),
    ]

    company_key = models.CharField(max_length=40, db_index=True)
    name = models.CharField(max_length=100)
    table_name = models.CharField(max_length=50)
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=MASTER)
    description = models.TextField(blank=True)
    how_often = models.CharField(max_length=12, choices=HOW_OFTEN_CHOICES, default=ANY)
    # File header -> column name ("" = skip), so next month's file maps itself.
    remembered_mapping = models.JSONField(default=dict, blank=True)
    # The last database read, for "Pull again": {"kind": "server", "connection": 3,
    # "table": "vouchers", "select": ""} (kind "sqlite" only pre-fills the table pick).
    remembered_source = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)  # the definition's last change

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company_key", "table_name"], name="findata_unique_table_per_company"
            ),
            models.UniqueConstraint(
                fields=["company_key", "name"], name="findata_unique_name_per_company"
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def schema(self) -> str:
        return schema_for(self.company_key)

    @property
    def is_snapshot(self) -> bool:
        return self.kind == self.SNAPSHOT

    def key_columns(self) -> list["DatasetColumn"]:
        return [column for column in self.columns.all() if column.is_key]


class DatasetColumn(models.Model):
    TEXT, INTEGER, DECIMAL, DATE, DATETIME, BOOLEAN = (
        "text", "integer", "decimal", "date", "datetime", "boolean",
    )  # fmt: skip
    TYPE_CHOICES = [
        (TEXT, "Text"),
        (INTEGER, "Whole number"),
        (DECIMAL, "Decimal"),
        (DATE, "Date"),
        (DATETIME, "Date and time"),
        (BOOLEAN, "Yes / No"),
    ]
    TYPE_HELP = {
        TEXT: "Names, codes, narration. Codes like 00123 stay as typed.",
        INTEGER: "Counts and numbers without decimals, e.g. quantity 12.",
        DECIMAL: "Money, rates, quantities with decimals, stored exactly (no rounding errors).",
        DATE: "A day, e.g. 20-11-2026.",
        DATETIME: "A day and a time, e.g. 20-11-2026 14:30.",
        BOOLEAN: "Yes or No, e.g. Active.",
    }

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="columns")
    label = models.CharField(max_length=100)
    name = models.CharField(max_length=50)
    data_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default=TEXT)
    decimal_places = models.PositiveSmallIntegerField(default=2)
    is_key = models.BooleanField(default=False)
    required = models.BooleanField(default=False)
    unique = models.BooleanField(default=False)
    references = models.ForeignKey(
        Dataset, null=True, blank=True, on_delete=models.PROTECT, related_name="linked_from"
    )
    help_text = models.CharField(max_length=200, blank=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["dataset", "name"], name="findata_unique_column_name")
        ]

    def __str__(self):
        return f"{self.dataset.name} · {self.label}"

    @property
    def type_label(self) -> str:
        if self.data_type == self.DECIMAL:
            return f"Decimal ({self.decimal_places} places)"
        return self.get_data_type_display()


class DbConnection(models.Model):
    """A saved database server of one company, e.g. "Tally server". Never the password: it is
    typed for each pull."""

    POSTGRES, MYSQL = "postgres", "mysql"
    KIND_CHOICES = [(POSTGRES, "PostgreSQL"), (MYSQL, "MySQL / MariaDB")]
    DEFAULT_PORTS = {POSTGRES: 5432, MYSQL: 3306}

    company_key = models.CharField(max_length=40, db_index=True)
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=POSTGRES)
    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField(default=5432)
    database = models.CharField(max_length=128)
    user = models.CharField(max_length=128)
    require_ssl = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company_key", "name"], name="findata_unique_connection_name"
            )
        ]

    def __str__(self):
        return self.name

    @property
    def place(self) -> str:
        """e.g. erp on 192.168.1.20:5432"""
        return f"{self.database} on {self.host}:{self.port}"


class Upload(models.Model):
    """One file uploaded into one dataset. Confirmed uploads are the "batches" of the change log
    (the old / new values themselves live in the company's schema, table _changes)."""

    MAPPING, PREVIEW, DONE, CANCELLED, UNDONE = "mapping", "preview", "done", "cancelled", "undone"
    STATUS_CHOICES = [
        (MAPPING, "Mapping columns"),
        (PREVIEW, "Waiting for confirm"),
        (DONE, "Saved"),
        (CANCELLED, "Cancelled"),
        (UNDONE, "Undone"),
    ]
    ADD_UPDATE, FULL_REFRESH = "add_update", "full_refresh"
    DATES, REMOVE_DATE = "dates", "remove_date"  # periodic reports
    MODE_CHOICES = [
        (ADD_UPDATE, "Add / update"),
        (FULL_REFRESH, "Full refresh"),
        (DATES, "Replace dates"),
        (REMOVE_DATE, "Remove a date"),
    ]
    MODE_HELP = {
        ADD_UPDATE: "Adds new rows and updates changed ones. Rows not in your file are kept. "
        "Use it for monthly files.",
        FULL_REFRESH: "Makes the dataset exactly match your file. Rows not in your file are "
        "deleted. Use it when the file has everything, e.g. a full ledger list.",
        DATES: "Each As-of date in the upload is replaced: rows of that date not in your file are "
        "removed. Other dates are not touched.",
        REMOVE_DATE: "Every row of one As-of date was removed.",
    }

    FILE, SQLITE, SERVER, API = "file", "sqlite", "server", "api"
    SOURCE_CHOICES = [
        (FILE, "File"),
        (SQLITE, "SQLite file"),
        (SERVER, "Database server"),
        (API, "API"),
    ]

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="uploads")
    user = models.CharField(max_length=150, blank=True)
    # The file name, or the connection's name for a server pull, e.g. "Tally server".
    file_name = models.CharField(max_length=255)
    source_kind = models.CharField(max_length=6, choices=SOURCE_CHOICES, default=FILE)
    connection = models.ForeignKey(
        DbConnection, null=True, blank=True, on_delete=models.SET_NULL, related_name="uploads"
    )
    source_table = models.CharField(max_length=255, blank=True)  # e.g. vouchers
    select_sql = models.TextField(blank=True)  # the SELECT that was run
    sheet = models.CharField(max_length=255, blank=True)
    # Periodic reports: the date picked for the whole file (empty when a file column has it),
    # and per date touched: [{"date": "2026-03-31", "added": 2, "changed": 5, "deleted": 1,
    # "existing": 120}] ("existing" 0 = a new date).
    as_of_date = models.DateField(null=True, blank=True)
    dates = models.JSONField(default=list, blank=True)
    mode = models.CharField(max_length=12, choices=MODE_CHOICES, default=ADD_UPDATE)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=MAPPING)
    mapping = models.JSONField(default=dict, blank=True)  # header -> column name used
    problems = models.JSONField(default=list, blank=True)  # [{"row": 45, "text": "..."}]
    added = models.PositiveIntegerField(default=0)
    changed = models.PositiveIntegerField(default=0)
    deleted = models.PositiveIntegerField(default=0)
    unchanged = models.PositiveIntegerField(default=0)
    existing = models.PositiveIntegerField(default=0)  # rows in the table at preview time
    # What the preview was worked out against; confirm refuses if either moved on.
    base_upload_id = models.BigIntegerField(null=True, blank=True)
    definition_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    undone_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"{self.dataset.name} · {self.file_name} ({self.status})"

    @property
    def from_database(self) -> bool:
        return self.source_kind in (self.SQLITE, self.SERVER)

    @property
    def stage_table(self) -> str:
        return f"_stage_{self.id}"

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.changed or self.deleted)

    @property
    def needs_delete_tick(self) -> bool:
        """Full refresh that deletes more than half the saved rows: ask once more."""
        return self.deleted > 0 and self.deleted * 2 > self.existing


class ApiKey(models.Model):
    """A key another system sends with API calls, e.g. "Tally sync". It works for one company.
    Only a hash of the key is kept: the key itself is shown once, when it is made."""

    company_key = models.CharField(max_length=40, db_index=True)
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=11, unique=True)  # "fd_" + 8 characters, to find the key
    key_hash = models.CharField(max_length=64)  # SHA-256 of the whole key
    can_send = models.BooleanField(default=False)
    can_read = models.BooleanField(default=False)
    created_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)  # the last day it works
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company_key", "name"], name="findata_unique_api_key_name"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"

    @property
    def rights(self) -> str:
        """e.g. "Send + Read"."""
        return " + ".join(
            label for label, on in (("Send", self.can_send), ("Read", self.can_read)) if on
        )

    @property
    def as_user(self) -> str:
        """Who an upload by this key is, in History: "API: Tally sync"."""
        return f"API: {self.name}"[:150]
