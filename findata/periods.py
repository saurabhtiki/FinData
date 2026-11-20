"""Which As-of dates a periodic report expects, and which of them are missing.

Example (How often: Month-end, loaded 31-01-2026 and 31-03-2026, today 20-04-2026):
    missing_dates(...)   -> [28-02-2026]          (counted from the first loaded date)
    suggested_date(...)  -> (31-03-2026, False)   (30-04 is still ahead: the latest expected
                                                   date, e.g. for a corrected file)
    Loaded only 31-01-2026: suggested_date(...) -> (28-02-2026, True)  (the next one after it)
Month-end and quarter-end are calendar days (31-03, 30-06), even on a Sunday.
"""

import calendar
from datetime import date, timedelta

from .models import Dataset


def _month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def is_expected(how_often: str, day: date) -> bool:
    if how_often == Dataset.DAILY:
        return True
    if how_often == Dataset.WEEKDAYS:
        return day.weekday() < 5
    if how_often == Dataset.MON_SAT:
        return day.weekday() < 6
    if how_often == Dataset.MONTH_END:
        return day == _month_end(day)
    if how_often == Dataset.QUARTER_END:
        return day.month % 3 == 0 and day == _month_end(day)
    return False  # any date: nothing is "expected"


def expected_dates(how_often: str, start: date, end: date) -> list[date]:
    """Expected dates from start to end (both included); [] for "Any date"."""
    if how_often == Dataset.ANY or start > end:
        return []
    found = []
    day = start
    if how_often in (Dataset.MONTH_END, Dataset.QUARTER_END):
        day = _month_end(start)
        while day <= end:
            if is_expected(how_often, day):
                found.append(day)
            day = _month_end(day + timedelta(days=1))
        return found
    while day <= end:
        if is_expected(how_often, day):
            found.append(day)
        day += timedelta(days=1)
    return found


def missing_dates(how_often: str, loaded, today: date) -> list[date]:
    """Expected dates with no data, from the first loaded date up to today."""
    if not loaded:
        return []
    have = set(loaded)
    return [day for day in expected_dates(how_often, min(have), today) if day not in have]


def suggested_date(how_often: str, loaded, today: date) -> tuple[date, bool]:
    """(date to suggest on the upload page, is it missing?).
    The first expected date after the latest loaded one; when none has passed yet, the latest
    expected date (already loaded, e.g. for a corrected file). "Any date": today."""
    if how_often == Dataset.ANY:
        return today, False
    have = set(loaded)
    if have:
        after = expected_dates(how_often, max(have) + timedelta(days=1), today)
        if after:
            return after[0], True
    # Nothing loaded yet, or up to date: the latest expected date up to today.
    looked_back = expected_dates(how_often, today - timedelta(days=100), today)
    if not looked_back:
        return today, False
    latest = looked_back[-1]
    return latest, latest not in have
