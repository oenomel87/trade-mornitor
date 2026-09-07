# -*- coding: utf-8 -*-
"""Bounded daily-history evidence helpers for recommendation snapshots.

The helpers in this module are intentionally independent of the recommendation
engine.  They prepare one auditable market-calendar chain, compare normalized
daily rows with that chain, and provide a conservative adjusted/unadjusted
window check for a later integration step.
"""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .errors import TmonError, safe_code
from .market import market_zone, timestamp
from .strategies import NoMatch


CALENDAR_PATH = "/api/v1/market-calendar/KR"
DAILY_GAP = "daily-gap"
DAILY_CALENDAR_UNVERIFIED = "daily-calendar-unverified"
ADJUSTMENT_BASIS_UNVERIFIED = "adjustment-basis-unverified"


def _iso_date(value):
    """Return an exact ISO date string, or ``None`` for malformed input."""
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.isoformat() if parsed.isoformat() == value else None


def _valid_session(day, now, completed=True):
    """Validate the regular session of a calendar business-day object."""
    if not isinstance(day, dict):
        return False
    day_date = _iso_date(day.get("date"))
    if day_date is None:
        return False
    integrated = day.get("integrated")
    regular = integrated.get("regularMarket") if isinstance(integrated, dict) else None
    if not isinstance(regular, dict):
        return False
    try:
        start = timestamp(regular["startTime"])
        end = timestamp(regular["endTime"])
    except (KeyError, TmonError, TypeError, ValueError):
        return False
    if not start < end or end - start > timedelta(days=1):
        return False
    try:
        if start.astimezone(market_zone("KRW")).date().isoformat() != day_date:
            return False
        if end.astimezone(market_zone("KRW")).date().isoformat() != day_date:
            return False
    except TmonError:
        return False
    if completed:
        try:
            return end <= now
        except TypeError:
            return False
    return True


def _source(required, today_date, fetched, *, reason=None, error_code=None):
    source = {
        "endpoint": CALENDAR_PATH,
        "country": "KR",
        "requestedDate": today_date,
        "requiredDates": required,
        "fetchedLinks": fetched,
    }
    if reason is not None:
        source["reason"] = reason
    if error_code is not None:
        source["errorCode"] = safe_code(error_code)
    return source


def _unavailable(required, today_date, fetched, reason, *, error_code=None,
                 details=None):
    result = {
        "status": "unavailable",
        "verified": False,
        "dates": [],
        "requiredDates": required,
        "reason": reason,
        "source": _source(required, today_date, fetched,
                           reason=reason, error_code=error_code),
    }
    if details:
        result.update(details)
    return result


def _budget_available(check_budget):
    """Accept both Engine.check_budget() and boolean test callbacks."""
    if check_budget is None:
        return True
    try:
        value = check_budget()
    except TmonError as error:
        if getattr(error, "exit_code", None) == 3:
            raise
        return False
    return value is not False


def common_trading_dates(client, calendar, now, needed, check_budget=None):
    """Build one verified previous-business-day chain.

    ``calendar`` is the already fetched execution-day response.  The returned
    dates are chronological and include the latest completed signal date.  A
    chain response must echo the requested date, carry a valid regular session,
    and point its ``nextBusinessDay`` back to the date from which the link was
    reached.  Weekend and holiday gaps are accepted because no weekday
    arithmetic is used.

    Network/data/budget failures return an auditable ``unavailable`` result.
    Authentication failures (exit code 3) propagate so callers cannot turn an
    authorization problem into an optional data gap.
    """
    if isinstance(needed, bool) or not isinstance(needed, int) or needed < 1:
        return _unavailable(needed, None, 0, "invalid-required-date-count")

    fetched_links = 0
    try:
        today = calendar["today"]
        previous = calendar["previousBusinessDay"]
        today_date = _iso_date(today["date"])
        signal_date = _iso_date(previous["date"])
        if today_date is None or signal_date is None:
            return _unavailable(needed, today_date, fetched_links,
                                "calendar-date-invalid")
        try:
            execution_date = now.astimezone(market_zone("KRW")).date().isoformat()
        except (AttributeError, TmonError):
            return _unavailable(needed, today_date, fetched_links,
                                "calendar-now-invalid")
        if today_date != execution_date or not signal_date < today_date:
            return _unavailable(needed, today_date, fetched_links,
                                "calendar-request-mismatch")
        if not _valid_session(today, now, completed=False):
            return _unavailable(needed, today_date, fetched_links,
                                "calendar-today-unverified")
        if not _valid_session(previous, now, completed=True):
            return _unavailable(needed, today_date, fetched_links,
                                "calendar-signal-unverified")
        # The signal day must be a genuinely completed session before its
        # daily candle can be used.  A malformed session is never inferred.
        next_day = calendar.get("nextBusinessDay")
        next_date = _iso_date(next_day.get("date")) if isinstance(next_day, dict) else None
        if next_date is None or not _valid_session(next_day, now, completed=False):
            return _unavailable(needed, today_date, fetched_links,
                                "calendar-next-unverified")
        if next_date <= today_date:
            return _unavailable(needed, today_date, fetched_links,
                                "calendar-next-not-future")
    except (KeyError, TypeError, AttributeError):
        return _unavailable(needed, None, fetched_links,
                            "calendar-response-invalid")

    dates = [signal_date]
    seen = {today_date, signal_date}
    current_date = signal_date
    expected_next = today_date
    while len(dates) < needed:
        if not _budget_available(check_budget):
            return _unavailable(needed, today_date, fetched_links,
                                "calendar-budget-exhausted",
                                details={"dates": list(reversed(dates))})
        try:
            fetched = client.get(CALENDAR_PATH, date=current_date)
            fetched_links += 1
        except TmonError as error:
            if getattr(error, "exit_code", None) == 3:
                raise
            return _unavailable(needed, today_date, fetched_links,
                                "calendar-fetch-failed",
                                error_code=getattr(error, "code", None),
                                details={"dates": list(reversed(dates))})
        except KeyboardInterrupt:
            raise
        except Exception:
            return _unavailable(needed, today_date, fetched_links,
                                "calendar-fetch-failed",
                                details={"dates": list(reversed(dates))})

        try:
            echoed = _iso_date(fetched["today"]["date"])
            if echoed != current_date:
                return _unavailable(needed, today_date, fetched_links,
                                    "calendar-request-mismatch",
                                    details={"dates": list(reversed(dates)),
                                             "requestedDate": current_date,
                                             "returnedDate": echoed})
            current_day = fetched["today"]
            previous_day = fetched["previousBusinessDay"]
            if not _valid_session(current_day, now, completed=True):
                return _unavailable(needed, today_date, fetched_links,
                                    "calendar-current-unverified",
                                    details={"dates": list(reversed(dates))})
            if not _valid_session(previous_day, now, completed=True):
                return _unavailable(needed, today_date, fetched_links,
                                    "calendar-previous-unverified",
                                    details={"dates": list(reversed(dates))})
            next_business = fetched["nextBusinessDay"]
            next_date = _iso_date(next_business["date"])
            if next_date is None or not _valid_session(next_business, now, completed=False):
                return _unavailable(needed, today_date, fetched_links,
                                    "calendar-next-unverified",
                                    details={"dates": list(reversed(dates))})
            if next_date != expected_next:
                return _unavailable(needed, today_date, fetched_links,
                                    "calendar-next-mismatch",
                                    details={"dates": list(reversed(dates)),
                                             "expectedNextDate": expected_next,
                                             "returnedNextDate": next_date})
            previous_date = _iso_date(previous_day["date"])
            if previous_date is not None and previous_date in seen:
                return _unavailable(needed, today_date, fetched_links,
                                    "calendar-chain-loop",
                                    details={"dates": list(reversed(dates)),
                                             "repeatedDate": previous_date})
            if previous_date is None or not previous_date < current_date:
                return _unavailable(needed, today_date, fetched_links,
                                    "calendar-chain-not-decreasing",
                                    details={"dates": list(reversed(dates)),
                                             "requestedDate": current_date,
                                             "returnedPreviousDate": previous_date})
        except (KeyError, TypeError, AttributeError):
            return _unavailable(needed, today_date, fetched_links,
                                "calendar-response-invalid",
                                details={"dates": list(reversed(dates))})

        dates.append(previous_date)
        seen.add(previous_date)
        expected_next = current_date
        current_date = previous_date

    chronological = list(reversed(dates))
    return {
        "status": "verified",
        "verified": True,
        "reason": None,
        "dates": chronological,
        "requiredDates": needed,
        "signalDate": signal_date,
        "source": _source(needed, today_date, fetched_links),
    }


def _reference_dates(reference, required):
    if not isinstance(reference, dict) or reference.get("status") != "verified" or \
            reference.get("verified") is not True:
        return None
    dates = reference.get("dates")
    if not isinstance(dates, list) or len(dates) != required:
        return None
    normalized = [_iso_date(value) for value in dates]
    if any(value is None for value in normalized) or normalized != sorted(set(normalized)):
        return None
    return normalized


def validate_daily_dates(rows, reference, required):
    """Compare daily row dates with an authoritative common-date reference.

    No row is synthesized or filled.  A verified reference plus any missing
    or unexpected date yields ``daily-gap`` and explicit lists.  An unverified
    reference yields ``daily-calendar-unverified`` without pretending the
    observed rows are complete.
    """
    if isinstance(required, bool) or not isinstance(required, int) or required < 1:
        return {"status": "unavailable", "verified": False,
                "reason": "invalid-required-date-count", "required": required,
                "dates": [], "missingDates": [], "unexpectedDates": []}
    expected = _reference_dates(reference, required)
    if expected is None:
        source = reference.get("source") if isinstance(reference, dict) else None
        return {"status": "unavailable", "verified": False,
                "reason": DAILY_CALENDAR_UNVERIFIED, "required": required,
                "dates": [], "expectedDates": [], "missingDates": [],
                "unexpectedDates": [], "source": source}
    if not isinstance(rows, list):
        return {"status": "unavailable", "verified": False,
                "reason": "daily-data-invalid", "required": required,
                "dates": [], "expectedDates": expected,
                "missingDates": list(expected), "unexpectedDates": [],
                "duplicateDates": []}
    observed = []
    malformed = False
    for row in rows:
        if not isinstance(row, dict) or _iso_date(row.get("date")) is None:
            malformed = True
            continue
        observed.append(row["date"])
    duplicate = sorted({value for value in observed if observed.count(value) > 1})
    unique_observed = sorted(set(observed))
    missing = sorted(set(expected) - set(unique_observed))
    unexpected = sorted(set(unique_observed) - set(expected))
    if malformed:
        return {"status": "unavailable", "verified": False,
                "reason": "daily-data-invalid", "required": required,
                "dates": unique_observed, "expectedDates": expected,
                "missingDates": missing, "unexpectedDates": unexpected,
                "duplicateDates": duplicate}
    if duplicate or missing or unexpected or len(observed) != required:
        return {"status": "unavailable", "verified": False,
                "reason": DAILY_GAP, "required": required,
                "dates": unique_observed, "expectedDates": expected,
                "missingDates": missing, "unexpectedDates": unexpected,
                "duplicateDates": duplicate}
    return {"status": "verified", "verified": True, "reason": None,
            "required": required, "dates": unique_observed,
            "expectedDates": expected, "missingDates": [],
            "unexpectedDates": [], "duplicateDates": []}


def _window_count(horizon):
    return 20 if horizon == "day" else 65 if horizon == "swing" else None


def _number(value):
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError()
        return number
    except (InvalidOperation, TypeError, ValueError):
        return None


def _valid_daily_value_row(row):
    """Require the normalized OHLCV shape before comparing two sources."""
    if not isinstance(row, dict) or _iso_date(row.get("date")) is None:
        return False
    try:
        timestamp(row["timestamp"])
    except (KeyError, TmonError, TypeError, ValueError):
        return False
    prices = [_number(row.get(field)) for field in
              ("openPrice", "highPrice", "lowPrice", "closePrice")]
    volume = _number(row.get("volume"))
    if any(value is None or value <= 0 for value in prices) or volume is None:
        return False
    low, high = prices[2], prices[1]
    return low <= min(prices[0], prices[3]) <= max(prices[0], prices[3]) <= high


def _basis_error(message, details):
    error = TmonError(ADJUSTMENT_BASIS_UNVERIFIED, message)
    error.details = details
    return error


def _raw_turnover(raw_by_date, expected):
    """Recompute the strategy's raw close-times-volume average."""
    values = []
    for day in expected[-20:]:
        row = raw_by_date.get(day)
        if not isinstance(row, dict):
            return None, day
        close, volume = _number(row.get("closePrice")), _number(row.get("volume"))
        if close is None or volume is None:
            return None, day
        values.append(close * volume)
    return sum(values, Decimal(0)) / Decimal(len(values)), None


def _raw_liquidity_details(amount, threshold):
    passed = None
    if amount is not None and threshold is not None:
        passed = amount >= threshold
    return {
        "rawEstimatedAvgDailyAmount": str(amount) if amount is not None else None,
        "minimumEstimatedAvgDailyAmount": str(threshold) if threshold is not None else None,
        "rawLiquidityCheck": ("pass" if passed else "fail") if passed is not None else "unavailable",
        "turnoverPriceBasis": "provider-unadjusted",
    }


def verify_adjustment_basis(adjusted_rows, raw_rows, horizon, reference,
                            min_amount):
    """Conservatively verify an adjusted/raw daily window.

    The complete used window must be identical in date, timestamp, OHLC and
    volume.  A matching volume with adjusted prices is deliberately
    insufficient: the provider contract does not document corporate-action
    volume units.  Success means only that this observed window was equal; it
    is not a provider-wide adjustment guarantee.

    The returned amount uses the unadjusted close and volume for the most
    recent 20 dates.  A low amount raises the existing ``NoMatch`` condition.
    Basis or shape failures raise ``adjustment-basis-unverified``.
    """
    required = _window_count(horizon)
    if required is None:
        raise _basis_error("조정 기준을 확인할 수 없는 horizon입니다.",
                           {"horizon": horizon, "reason": "invalid-horizon"})
    expected = _reference_dates(reference, required)
    base_details = {
        "horizon": horizon,
        "requiredDailyBars": required,
        "priceBasis": "provider-adjusted",
        "rawPriceBasis": "provider-unadjusted",
        "volumeBasis": "observed-equal-window",
    }
    if expected is None:
        raise _basis_error("공통 거래일 기준을 확인할 수 없습니다.",
                           {**base_details, "reason": DAILY_CALENDAR_UNVERIFIED})
    adjusted_check = validate_daily_dates(adjusted_rows, reference, required)
    raw_check = validate_daily_dates(raw_rows, reference, required)
    if adjusted_check.get("status") != "verified" or raw_check.get("status") != "verified":
        raise _basis_error("조정·비조정 일봉의 거래일 창을 확인할 수 없습니다.",
                           {**base_details, "reason": DAILY_GAP,
                            "adjusted": adjusted_check, "raw": raw_check})
    if not isinstance(adjusted_rows, list) or not isinstance(raw_rows, list):
        raise _basis_error("조정·비조정 일봉 형식을 확인할 수 없습니다.",
                           {**base_details, "reason": "daily-data-invalid"})

    for label, rows in (("adjusted", adjusted_rows), ("raw", raw_rows)):
        for index, row in enumerate(rows):
            if not _valid_daily_value_row(row):
                raise _basis_error(
                    "조정·비조정 일봉 OHLCV 형식을 확인할 수 없습니다.",
                    {**base_details, "reason": "daily-data-invalid",
                     "source": label, "rowIndex": index,
                     "date": row.get("date") if isinstance(row, dict) else None})

    adjusted_by_date = {row["date"]: row for row in adjusted_rows}
    raw_by_date = {row["date"]: row for row in raw_rows}
    amount, amount_error_date = _raw_turnover(raw_by_date, expected)
    threshold = _number(min_amount)
    liquidity_details = _raw_liquidity_details(amount, threshold)
    divergences = []
    for day in expected:
        a, r = adjusted_by_date.get(day), raw_by_date.get(day)
        if not isinstance(a, dict) or not isinstance(r, dict):
            divergences.append({"date": day, "field": "row", "kind": "missing"})
            continue
        for field in ("timestamp", "openPrice", "highPrice", "lowPrice",
                      "closePrice", "volume"):
            av, rv = a.get(field), r.get(field)
            if field == "timestamp":
                equal = av == rv
            else:
                an, rn = _number(av), _number(rv)
                equal = an is not None and rn is not None and an == rn
            if not equal:
                divergences.append({"date": day, "field": field,
                                    "adjusted": str(av), "raw": str(rv)})
    if divergences:
        raise _basis_error(
            "조정·비조정 사용 창의 가격 또는 거래량 기준이 일치하지 않습니다.",
            {**base_details, "reason": "window-values-differ",
             "divergences": divergences,
             **liquidity_details,
             "explanation": "거래량만 같아도 가격 조정이 있으면 기업행사 전후 단위를 입증할 수 없어 제외했습니다."})

    if amount is None:
        raise _basis_error("비수정 거래대금 계산값을 확인할 수 없습니다.",
                           {**base_details, "reason": "raw-window-invalid",
                            "date": amount_error_date, **liquidity_details})
    if threshold is None:
        raise _basis_error("최소 거래대금 기준을 확인할 수 없습니다.",
                           {**base_details, "reason": "minimum-amount-invalid",
                            **liquidity_details})
    if amount < threshold:
        error = NoMatch("low-liquidity", "비수정 일봉 기준 유동성 요건을 충족하지 않습니다.")
        error.details = {**base_details, **liquidity_details,
                         "explanation": "최근 20개 비수정 종가×거래량 평균이 기준보다 낮습니다."}
        raise error
    return {
        "status": "verified",
        "verified": True,
        "horizon": horizon,
        "requiredDailyBars": required,
        "dates": expected,
        "rawEstimatedAvgDailyAmount": amount,
        "minimumEstimatedAvgDailyAmount": threshold,
        "rawLiquidityCheck": "pass",
        "turnoverPriceBasis": "provider-unadjusted",
        "signalPriceBasis": "provider-adjusted",
        "volumeBasis": "observed-equal-window",
        "explanation": "해당 사용 창의 조정·비조정 OHLCV가 관찰상 동일합니다. 이는 이 창의 일관성만 확인하며 공급자의 기업행사 전후 거래량 보장을 의미하지 않습니다.",
    }
