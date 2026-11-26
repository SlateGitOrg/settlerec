"""Business-day arithmetic on the bank holiday calendar.

The SQL pipeline does its own business-day arithmetic from ``calendar.csv``; this module
mirrors it in Python for the data generator and for tests that need an independent answer.
"""

from __future__ import annotations

import datetime as dt

# 2026 US federal bank holidays (Independence Day falls on a Saturday, so the Friday is observed).
HOLIDAYS_2026: tuple[tuple[dt.date, str], ...] = (
    (dt.date(2026, 1, 1), "New Year's Day"),
    (dt.date(2026, 1, 19), "Martin Luther King Jr. Day"),
    (dt.date(2026, 2, 16), "Washington's Birthday (Presidents Day)"),
    (dt.date(2026, 5, 25), "Memorial Day"),
    (dt.date(2026, 6, 19), "Juneteenth"),
    (dt.date(2026, 7, 3), "Independence Day (observed)"),
    (dt.date(2026, 9, 7), "Labor Day"),
    (dt.date(2026, 10, 12), "Columbus Day"),
    (dt.date(2026, 11, 11), "Veterans Day"),
    (dt.date(2026, 11, 26), "Thanksgiving Day"),
    (dt.date(2026, 12, 25), "Christmas Day"),
)


class BusinessCalendar:
    """Weekends plus a set of holidays are non-business days."""

    def __init__(self, holidays: set[dt.date] | None = None) -> None:
        self.holidays = holidays if holidays is not None else {d for d, _ in HOLIDAYS_2026}

    def is_business_day(self, day: dt.date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def add(self, day: dt.date, n: int) -> dt.date:
        """The date ``n`` business days after ``day``, which need not be a business day itself."""
        current = day
        remaining = n
        while remaining > 0:
            current += dt.timedelta(days=1)
            if self.is_business_day(current):
                remaining -= 1
        return current

    def between(self, start: dt.date, end: dt.date) -> int:
        """Business days in the half-open interval (start, end]."""
        count = 0
        current = start
        while current < end:
            current += dt.timedelta(days=1)
            if self.is_business_day(current):
                count += 1
        return count
