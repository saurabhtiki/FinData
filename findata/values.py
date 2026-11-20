"""One file cell -> the value a dataset column stores, or a plain-words problem.

data-core reads the file with keep_text=True, so a cell arrives as the text in the file
(Excel dates as 2026-04-01); the dataset column's type alone decides how it is read, so a
Text code 1.10 stays "1.10". Other Python values are accepted too. Examples for each type:
    Decimal       "1,50,000.50" -> 150000.50    1500.5 -> 1500.50
    Whole number  "12" -> 12                    "12.5" -> problem
    Date          "01-04-2026" -> 1 Apr 2026    "2026-04-01" -> 1 Apr 2026
    Yes / No      Yes, Y, True, 1 -> True       No, N, False, 0 -> False
    Text          "  Cash " -> "Cash"           1001 -> "1001"
"""

import re
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from data_core.loading import COMMA_NUMBER, DATE_FORMATS, TIME_PARTS
from django.utils import timezone

from .models import DatasetColumn

TRUE_WORDS = {"yes", "y", "true", "1"}
FALSE_WORDS = {"no", "n", "false", "0"}
NUMBER = re.compile(r"[+-]?(\d+(\.\d+)?|\.\d+)")
GROUPED_NUMBER = re.compile(COMMA_NUMBER)  # 1,50,000.50 or 150,000.50 (not 1,5)
MAX_DIGITS = 18  # numeric(18, places)
# data-core's date formats, day first only: 03-04-2026 is 3 April (month-first ones dropped).
DAY_FIRST_FORMATS = tuple(fmt for fmt, _label in DATE_FORMATS if not fmt.startswith("%m"))


class BadValue(ValueError):
    """The cell can't become the column's type. The message is shown to the user."""


def is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def convert(value, column):
    """`column` is a DatasetColumn or ColumnSpec. Blank -> None (the caller checks Required)."""
    if is_blank(value):
        return None
    if isinstance(value, str):
        value = value.strip()
    return CONVERTERS[column.data_type](value, column)


def _text(value, column) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _number(value) -> Decimal:
    if isinstance(value, bool):
        raise BadValue("is Yes / No, not a number")
    if isinstance(value, int | Decimal):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    text = str(value).replace(" ", "")
    if not (NUMBER.fullmatch(text) or GROUPED_NUMBER.fullmatch(text)):
        raise BadValue("isn't a number (use e.g. 1500.50 or 1,500.50)")
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation as error:
        raise BadValue("isn't a number") from error


def _integer(value, column) -> int:
    number = _number(value)
    if number != number.to_integral_value():
        raise BadValue("has decimals, but the column is a whole number")
    if abs(number) >= 2**63:
        raise BadValue("is too large")
    return int(number)


def _decimal(value, column) -> Decimal:
    places = int(column.decimal_places)
    number = _number(value)
    if abs(number) >= Decimal(10) ** (MAX_DIGITS - places):
        raise BadValue("is too large")
    return number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _parse_date_text(text: str, with_time: bool) -> datetime:
    parts = TIME_PARTS if with_time else ("",)
    for date_format in DAY_FIRST_FORMATS:
        for time_part in parts:
            try:
                return datetime.strptime(text, date_format + time_part)
            except ValueError:
                continue
    raise BadValue("isn't a date (use e.g. 01-04-2026 or 2026-04-01)")


def _date(value, column) -> date:
    if isinstance(value, datetime):
        if value.time() != time(0, 0):
            raise BadValue("has a time, but the column is a date")
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise BadValue("isn't a date (use e.g. 01-04-2026 or 2026-04-01)")
    return _parse_date_text(value, with_time=False).date()


def _datetime(value, column) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time(0, 0))
    elif isinstance(value, str):
        moment = _parse_date_text(value, with_time=True)
    else:
        raise BadValue("isn't a date and time (use e.g. 01-04-2026 14:30)")
    return timezone.make_aware(moment) if timezone.is_naive(moment) else moment


def _boolean(value, column) -> bool:
    if isinstance(value, bool):
        return value
    word = str(_text(value, column)).strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise BadValue("isn't Yes / No (use Yes, No, Y, N, True, False, 1 or 0)")


CONVERTERS = {
    DatasetColumn.TEXT: _text,
    DatasetColumn.INTEGER: _integer,
    DatasetColumn.DECIMAL: _decimal,
    DatasetColumn.DATE: _date,
    DatasetColumn.DATETIME: _datetime,
    DatasetColumn.BOOLEAN: _boolean,
}
