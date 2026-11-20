"""Guess a definition from a sample CSV / XLSX: column names, types and a key.

Reading uses data-core (the same loader as Transform Data and My Insights) in a throw-away
in-memory DuckDB. Only the guesses are kept; the sample's rows are never saved.
Example: a sheet with "Account Code | Account Name | Opening Balance" becomes
account_code (Text, key) · account_name (Text) · opening_balance (Decimal).
"""

import itertools
import logging
import re
from dataclasses import dataclass, field

from data_core.loading import UploadRejected, load_file, quote_name
from data_core.store import DataStore

from . import naming
from .definition import blank_column
from .models import DatasetColumn

logger = logging.getLogger(__name__)

KIND_TO_TYPE = {
    "integer": DatasetColumn.INTEGER,
    "number": DatasetColumn.DECIMAL,
    "date": DatasetColumn.DATE,
    "datetime": DatasetColumn.DATETIME,
    "boolean": DatasetColumn.BOOLEAN,
    "text": DatasetColumn.TEXT,
}
KEY_WORDS = re.compile(r"(?:^|_)(code|id|no|num|number|key)(?:_|$)")  # on database names
PAIR_SEARCH_COLUMNS = 8  # look for a two-column key among the first few columns only
SAMPLE_STORE_MB = 200


class SampleRejected(Exception):
    """The sample can't be read. The message is shown as it is."""


@dataclass
class SheetGuess:
    label: str  # "sales.xlsx › Jan"
    name: str  # suggested dataset name, e.g. "Jan"
    row_count: int
    columns: list[dict] = field(default_factory=list)
    key_note: str = ""

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "name": self.name,
            "row_count": self.row_count,
            "columns": self.columns,
            "key_note": self.key_note,
        }


def guess_from_file(file_name: str, data) -> list[SheetGuess]:
    """One guess per CSV, or per Excel sheet."""
    if file_name.lower().endswith((".json", ".jsonl")):
        raise SampleRejected("Use a CSV or Excel file as the sample.")
    store = DataStore(memory_mb=SAMPLE_STORE_MB)
    try:
        try:
            loaded = load_file(store, file_name, data)
        except UploadRejected as error:
            logger.warning("Sample %s rejected: %s", file_name, error)
            raise SampleRejected(str(error)) from error
        return [_guess(store, dataset) for dataset in loaded]
    finally:
        store.close()


def _guess(store: DataStore, dataset) -> SheetGuess:
    # An Excel sheet's source is "tally.xlsx › Ledgers": the sheet alone makes the best name.
    # A CSV (or one-sheet file) is named after the file, e.g. "chart_of_accounts".
    source = dataset.source_file
    name = (source.split("›")[-1] if "›" in source else dataset.name).replace("_", " ").strip()
    guess = SheetGuess(
        label=dataset.source_file, name=name[:1].upper() + name[1:], row_count=dataset.row_count
    )
    used: set[str] = set()
    for profile in dataset.columns:
        column = blank_column()
        column["label"] = profile.name.strip()[:100]
        column["name"] = _unique(naming.to_db_name(profile.name) or "column", used)
        column["data_type"] = KIND_TO_TYPE.get(profile.kind, DatasetColumn.TEXT)
        column["samples"] = profile.samples[:3]
        if profile.note:
            column["help_text"] = profile.note[:200]
        guess.columns.append(column)

    key = _suggest_key(store, dataset)
    if key:
        for position in key:
            guess.columns[position]["is_key"] = True
            guess.columns[position]["required"] = True
        labels = " + ".join(guess.columns[position]["label"] for position in key)
        guess.key_note = f"Suggested key: {labels} (no blanks, no repeats in the sample)."
    else:
        guess.key_note = (
            "No column (or pair of columns) is unique in the sample. Tick Key yourself."
        )
    return guess


def _unique(name: str, used: set[str]) -> str:
    candidate, number = name, 2
    while candidate in used:
        candidate = f"{name}_{number}"
        number += 1
    used.add(candidate)
    return candidate


def _suggest_key(store: DataStore, dataset) -> list[int]:
    """The key most likely meant, in this order (only columns with no blanks):
    1. one key-like column (named code / id / no …) with no repeats, e.g. Account code
    2. a pair of key-like columns with no repeats, e.g. Voucher no + Line no
    3. one text column with no repeats, e.g. Ledger name
    4. any pair among the first columns.
    Plain number columns (Amount, Qty) are only used as part of a pair: in a small sample
    they are often unique by chance."""
    columns = dataset.columns
    filled = [
        index for index, profile in enumerate(columns)
        if profile.blanks == 0 and profile.kind in ("text", "integer", "date")
    ]  # fmt: skip
    if not filled:
        return []
    names = [quote_name(profile.name) for profile in columns]
    table = dataset.table_name

    def unique(*indexes: int) -> bool:
        listed = ", ".join(names[index] for index in indexes)
        distinct = store.connection.execute(
            f"SELECT count(*) FROM (SELECT DISTINCT {listed} FROM {table})"
        ).fetchall()[0][0]
        return distinct == dataset.row_count

    key_like = [i for i in filled if KEY_WORDS.search(naming.to_db_name(columns[i].name))]
    pairs = list(itertools.combinations(filled[:PAIR_SEARCH_COLUMNS], 2))
    tries = (
        [[i] for i in key_like]
        + [list(pair) for pair in itertools.combinations(key_like, 2)]
        + [[i] for i in filled if columns[i].kind == "text" and i not in key_like]
        + [list(pair) for pair in pairs]
    )
    return next((attempt for attempt in tries if unique(*attempt)), [])
