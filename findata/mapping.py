"""Which file column goes into which dataset column, e.g. ledg_name -> Ledger name.

A mapping is a dict: file header -> column database name, or "" for "skip this file column".
Guessing, best first:
1. what this dataset remembered from an earlier upload (same header text);
2. the same name: "Ledger Name", "ledger_name" and "LEDGER NAME" all match column ledger_name;
3. a close spelling: "Ledg Name" -> Ledger name.
Every dataset column takes at most one file column.
A periodic report can also take its As-of date from a file column ("Report date" → As-of date);
without one, the date picked for the whole file is used.
"""

import difflib

from . import naming
from .models import Dataset
from .schema_builder import AS_OF

CLOSE_ENOUGH = 0.8
AS_OF_LABEL = "As-of date"
AS_OF_HEADERS = ("As of date", "As on date", "Report date", "As of")


def targets(dataset: Dataset) -> list[tuple[str, str, bool]]:
    """(name, label, required) a file column can go into. A periodic report adds As-of date
    (not required: the date picked for the whole file is used without it)."""
    found = [(AS_OF, AS_OF_LABEL, False)] if dataset.is_snapshot else []
    return found + [
        (column.name, column.label, column.required or column.is_key)
        for column in dataset.columns.all()
    ]


def _plain(text: str) -> str:
    return naming.to_db_name(text).replace("_", "")


def guess(dataset: Dataset, headers: list[str], close: bool = True) -> dict[str, str]:
    """File header -> column name ("" = skip): the saved mapping, then the same name or label,
    then (close=True, the screen only, where a person checks it) a similar name."""
    columns = list(dataset.columns.all())
    names = {name for name, _, _ in targets(dataset)}
    remembered = dataset.remembered_mapping or {}
    mapping: dict[str, str] = {}
    used: set[str] = set()

    def take(header: str, name: str) -> None:
        mapping[header] = name
        if name:
            used.add(name)

    for header in headers:
        name = remembered.get(header)
        if name is not None and (name == "" or (name in names and name not in used)):
            take(header, name)
    by_plain = {}
    for column in columns:
        by_plain.setdefault(_plain(column.label), column.name)
        by_plain.setdefault(_plain(column.name), column.name)
    if dataset.is_snapshot:
        for header in AS_OF_HEADERS:
            by_plain.setdefault(_plain(header), AS_OF)
    for header in headers:
        if header in mapping:
            continue
        name = by_plain.get(_plain(header))
        if name and name not in used:
            take(header, name)
    for header in headers:
        if header in mapping:
            continue
        if not close:
            take(header, "")
            continue
        free = {plain: name for plain, name in by_plain.items() if name not in used}
        similar = difflib.get_close_matches(_plain(header), list(free), n=1, cutoff=CLOSE_ENOUGH)
        take(header, free[similar[0]] if similar else "")
    return mapping


def problems(dataset: Dataset, mapping: dict[str, str]) -> list[str]:
    """What stops this mapping: a column picked twice, or a required column left out."""
    found = []
    by_column: dict[str, list[str]] = {}
    for header, name in mapping.items():
        if name:
            by_column.setdefault(name, []).append(header)
    columns = targets(dataset)
    for name, label, required in columns:
        headers = by_column.get(name, [])
        if len(headers) > 1:
            found.append(
                f"{label} is picked for {len(headers)} file columns "
                f"({', '.join(headers)}). Pick it once and skip the others."
            )
        if not headers and required:
            found.append(
                f"{label} is required but isn't in this file. Map a file column to it, "
                "or add it to the file."
            )
    unknown = set(by_column) - {name for name, _, _ in columns}
    if unknown:
        found.append("The mapping names columns this dataset doesn't have. Reload the page.")
    if not set(by_column) - {AS_OF}:
        found.append("Map at least one file column.")
    return found


def remember(dataset: Dataset, mapping: dict[str, str]) -> None:
    """Keep this mapping for next time, without touching the definition's "changed" time."""
    merged = {**(dataset.remembered_mapping or {}), **mapping}
    Dataset.objects.filter(pk=dataset.pk).update(remembered_mapping=merged)
    dataset.remembered_mapping = merged
