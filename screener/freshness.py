"""Offline NYSE session dates and output freshness (no provider requests)."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
PROVIDER_GRACE = dt.timedelta(hours=1)
# Exceptional exchange closures cannot be inferred from recurring holidays.
EXTRA_CLOSURES = {dt.date(2025, 1, 9), dt.date(2018, 12, 5),
                  dt.date(2012, 10, 29), dt.date(2012, 10, 30)}


def observed(day):
    return day + dt.timedelta(days=1 if day.weekday() == 6 else -1 if day.weekday() == 5 else 0)


def nth_weekday(year, month, weekday, n):
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    value = h + l - 7 * m + 114
    return dt.date(year, value // 31, value % 31 + 1)


def is_session(day):
    year = day.year
    # NYSE does not observe Saturday New Year's Day on the preceding Friday.
    new_year = dt.date(year, 1, 1)
    holidays = {new_year if new_year.weekday() != 6 else new_year + dt.timedelta(days=1),
                nth_weekday(year, 1, 0, 3), nth_weekday(year, 2, 0, 3),
                easter(year) - dt.timedelta(days=2),
                nth_weekday(year, 6, 0, 1) - dt.timedelta(days=7),
                observed(dt.date(year, 7, 4)), nth_weekday(year, 9, 0, 1),
                nth_weekday(year, 11, 3, 4), observed(dt.date(year, 12, 25))}
    if year >= 2022:
        holidays.add(observed(dt.date(year, 6, 19)))
    return day.weekday() < 5 and day not in holidays and day not in EXTRA_CLOSURES


def close_hour(day):
    thanksgiving = nth_weekday(day.year, 11, 3, 4)
    if (day == thanksgiving + dt.timedelta(days=1)
            or (day.month, day.day) == (12, 24)
            or ((day.month, day.day) == (7, 3) and day.weekday() < 4)):
        return 13
    return 16


def latest_session(now=None):
    """Latest session whose close + provider grace has passed, as YYYY-MM-DD."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("freshness clock must be timezone aware")
    local = now.astimezone(NY)
    day = local.date()
    ready = dt.datetime.combine(day, dt.time(close_hour(day)), NY) + PROVIDER_GRACE
    if local < ready:
        day -= dt.timedelta(days=1)
    while not is_session(day):
        day -= dt.timedelta(days=1)
    return day.isoformat()


def last_bar(data):
    dates = (data or {}).get("dates") or []
    return str(dates[-1]) if dates else None


def coverage(target, dates):
    """Count exact-date coverage; future dates are never treated as current."""
    dates = list(dates)
    return {"expected_session": target, "total": len(dates),
            "current": sum(d == target for d in dates)}
