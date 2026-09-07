# -*- coding: utf-8 -*-
"""Deterministic Korean-market calendar and daily-candle fixture.

This module is test data only.  Its business-day rules are deliberately
explicit and do not claim to reproduce an exchange calendar.  The one listed
closure, 2026-08-17, exercises a non-weekend holiday gap.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal


KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 4, 10, 0, 10, tzinfo=KST)
FIXTURE_HOLIDAYS = frozenset({"2026-08-17"})


def _as_date(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fixture datetime must be timezone aware")
        return value.astimezone(KST).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError("expected a date, datetime, or ISO date")


def _is_business(day):
    return day.weekday() < 5 and day.isoformat() not in FIXTURE_HOLIDAYS


def _step_business(day, direction):
    day += timedelta(days=direction)
    while not _is_business(day):
        day += timedelta(days=direction)
    return day


def business_days_before(now, count):
    """Return ``count`` prior fixture business dates oldest first as ISO dates."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("count must be a non-negative integer")
    current = _as_date(now)
    result = []
    for _ in range(count):
        current = _step_business(current, -1)
        result.append(current.isoformat())
    return list(reversed(result))


def _session(day):
    start = datetime.combine(day, time(9, 0), KST)
    auction = datetime.combine(day, time(15, 20), KST)
    end = datetime.combine(day, time(15, 30), KST)
    return {
        "date": day.isoformat(),
        "integrated": {
            "preMarket": None,
            "regularMarket": {
                "startTime": start.isoformat(),
                "singlePriceAuctionStartTime": auction.isoformat(),
                "endTime": end.isoformat(),
            },
            "afterMarket": None,
        },
    }


def calendar_for(value):
    """Return a coherent calendar response for a date or timezone-aware datetime."""
    requested = _as_date(value)
    today = _session(requested) if _is_business(requested) else {
        "date": requested.isoformat(), "integrated": None,
    }
    previous = _step_business(requested, -1)
    following = _step_business(requested, 1)
    return {
        "today": today,
        "previousBusinessDay": _session(previous),
        "nextBusinessDay": _session(following),
    }


def _business_days_after(now, count):
    current = _as_date(now)
    result = []
    for _ in range(count):
        current = _step_business(current, 1)
        result.append(current.isoformat())
    return result


FIXTURE_PREVIOUS_DATES = tuple(business_days_before(NOW, 150))
FIXTURE_FOLLOWING_DATES = tuple(_business_days_after(NOW, 10))


def _candle(day_text, close, *, volume="100000000", low=None, high=None):
    close_decimal = Decimal(str(close))
    low_decimal = close_decimal - Decimal("1") if low is None else Decimal(str(low))
    high_decimal = close_decimal + Decimal("1") if high is None else Decimal(str(high))
    timestamp_text = datetime.combine(_as_date(day_text), time(9), KST).isoformat()
    return {
        "timestamp": timestamp_text,
        "currency": "KRW",
        "openPrice": str(close_decimal),
        "highPrice": str(high_decimal),
        "lowPrice": str(low_decimal),
        "closePrice": str(close_decimal),
        "volume": str(volume),
    }


def daily_bars(now=NOW, count=120):
    """Return ascending business-day candles with the legacy strategy shape.

    The final candle is 102 with volume 200m.  The preceding breakout window
    is around 100, while earlier blocks are 90 and 80, preserving the existing
    SMA20/SMA60 and volume-breakout arithmetic without calendar-day fakes.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("count must be a positive integer")
    canonical_count = max(120, count)
    dates = business_days_before(now, canonical_count)
    rows = []
    for index, day_text in enumerate(dates):
        # Keep the final 120 rows identical to the legacy days() fixture even
        # when a caller asks for more history than the canonical window.
        canonical_index = index - (canonical_count - 120)
        close = ("80" if canonical_index < 70 else
                 "90" if canonical_index < 100 else "100")
        rows.append(_candle(day_text, close))
    rows[-1] = _candle(dates[-1], "102", volume="200000000", low="100", high="103")
    return rows[-count:]


def _smoke_check():
    assert len(FIXTURE_PREVIOUS_DATES) >= 150
    assert len(FIXTURE_FOLLOWING_DATES) >= 10
    assert "2026-08-17" not in FIXTURE_PREVIOUS_DATES
    assert "2026-08-14" in FIXTURE_PREVIOUS_DATES
    assert "2026-08-18" in FIXTURE_PREVIOUS_DATES
    window = FIXTURE_PREVIOUS_DATES[-65:]
    assert len(window) == 65 and list(window) == sorted(window)
    rows20 = daily_bars(NOW, 20)
    rows65 = daily_bars(NOW, 65)
    rows = daily_bars(NOW, 120)
    assert len(rows20) == 20 and len(rows65) == 65 and len(rows) == 120
    assert rows20[-1]["closePrice"] == rows65[-1]["closePrice"] == "102"
    assert rows20[-2]["closePrice"] == rows65[-2]["closePrice"] == "100"
    assert rows[-1]["timestamp"].startswith(FIXTURE_PREVIOUS_DATES[-1])
    assert rows[-1]["closePrice"] == "102"
    assert rows[-2]["closePrice"] == "100"
    return True


if __name__ == "__main__":
    _smoke_check()
