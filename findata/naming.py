"""Turning labels people type ("Ledger Name") into safe database names (ledger_name)."""

import re

MAX_NAME = 50
# PostgreSQL's reserved words: a column called `order` works only when quoted, which breaks
# hand-written queries and dashboards later, so these are refused with a suggestion.
_RESERVED_WORDS = (
    "all analyse analyze and any array as asc asymmetric both case cast check collate column "
    "constraint create current_catalog current_date current_role current_time current_timestamp "
    "current_user default deferrable desc distinct do else end except false fetch for foreign "
    "from grant group having in initially intersect into lateral leading limit localtime "
    "localtimestamp not null offset on only or order placing primary references returning "
    "select session_user some symmetric system_user table then to trailing true union unique "
    "user using variadic when where window with"
)
RESERVED = set(_RESERVED_WORDS.split())
VALID = re.compile(r"[a-z][a-z0-9_]*")


def to_db_name(label: str) -> str:
    """'Ledger Name' -> 'ledger_name', 'GST No.' -> 'gst_no', '2024 Sales' -> 'c_2024_sales',
    'Group' -> 'group_name' (a reserved word gets _name, so common labels just work)."""
    name = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    if name and not name[0].isalpha():
        name = f"c_{name}"
    if name in RESERVED:
        name = f"{name}_name"
    return name[:MAX_NAME].rstrip("_")


def name_problem(name: str, what: str = "Database name") -> str:
    """Why a database name can't be used, in plain words ('' = it is fine)."""
    if not name:
        return f"{what} is empty. Type a label, e.g. Account code."
    if len(name) > MAX_NAME:
        return f"{what} “{name}” is longer than {MAX_NAME} characters."
    if name.startswith("_"):
        return (
            f"{what} “{name}” can't start with _ (those names are kept for FinData's own columns)."
        )
    if not VALID.fullmatch(name):
        return (
            f"{what} “{name}” may use only small letters, digits and _, "
            "and must start with a letter, e.g. account_code."
        )
    if name in RESERVED:
        return f"“{name}” is a reserved word in databases. Try {name}_no or {name}_name."
    return ""
