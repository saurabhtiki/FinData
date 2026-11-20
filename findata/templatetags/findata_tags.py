"""Template helpers for tables whose columns are only known at run time (uploaded rows)."""

from django import template

from ..upload import display

register = template.Library()


@register.filter
def cell(row: dict, name: str) -> str:
    """A row's value for a column, written as the user would type it (01-04-2026, Yes)."""
    value = row.get(name)
    return "" if value is None or value == "" else display(value)
