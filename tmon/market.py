from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import TmonError, invalid_data

PRICE_FIELDS = ("openPrice", "highPrice", "lowPrice", "closePrice")


def symbols(values):
    result = []
    for value in values:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]*", value):
            raise TmonError("invalid-symbol", "심볼에는 영문·숫자·점·하이픈만 사용할 수 있습니다.", 2)
        value = value.upper()
        if value not in result:
            result.append(value)
    if not 1 <= len(result) <= 200:
        raise TmonError("invalid-symbol-count", "중복 제거 후 1~200개 심볼을 지정하세요.", 2)
    return result


def decimal(value):
    try:
        if not isinstance(value, str) or len(value) > 30:
            raise ValueError()
        number = Decimal(value)
        if not number.is_finite() or number < 0 or abs(number.adjusted()) > 30:
            raise ValueError()
        return number
    except (ValueError, InvalidOperation):
        raise invalid_data("유효하지 않은 가격 또는 거래량입니다.") from None


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise invalid_data("시각 또는 시간대가 유효하지 않습니다.") from None


def market_zone(currency):
    if currency not in ("KRW", "USD"):
        raise invalid_data("지원하지 않는 통화입니다.")
    try:
        return ZoneInfo("Asia/Seoul" if currency == "KRW" else "America/New_York")
    except ZoneInfoNotFoundError:
        raise TmonError("timezone-unavailable", "운영체제의 IANA 시간대 데이터가 필요합니다.", 2) from None


def quote(client, requested):
    raw = client.get("/api/v1/prices", symbols=",".join(requested))
    if not isinstance(raw, list):
        raise invalid_data()
    rows = {}
    for item in raw:
        try:
            symbol = item["symbol"]
            if symbol not in requested or symbol in rows:
                raise invalid_data("현재가 응답의 종목이 중복되거나 요청과 다릅니다.")
            market_zone(item["currency"])
            rows[symbol] = {"symbol": symbol, "lastPrice": decimal(item["lastPrice"]),
                            "currency": item["currency"], "timestamp": timestamp(item["timestamp"]).isoformat()}
        except (KeyError, TypeError):
            raise invalid_data() from None
    missing = [symbol for symbol in requested if symbol not in rows]
    if not rows:
        raise TmonError("no-data", "현재가 데이터가 없습니다.")
    warnings = [{"code": "missing-symbols", "message": "응답 누락 종목: " + ", ".join(missing)}] if missing else []
    return [rows[s] for s in requested if s in rows], {"missingSymbols": missing}, warnings, 5 if missing else 0


def calendar_cutoff(raw, country, today, now):
    """Exclude the entire current market date until session coverage is specified.

    Previous business day's sessions must also have ended; handles overnight
    sessions without hardcoding DST, exchange holidays or closing times.
    """
    try:
        if not isinstance(raw, dict) or raw["today"]["date"] != today.isoformat():
            raise ValueError()
        previous = raw["previousBusinessDay"]
        previous_date = date.fromisoformat(previous["date"])
        if previous_date >= today:
            raise ValueError()
        sessions = previous["integrated"] if country == "KR" else previous
        if not isinstance(sessions, dict):
            raise ValueError()
        names = ("preMarket", "regularMarket", "afterMarket") if country == "KR" else (
            "dayMarket", "preMarket", "regularMarket", "afterMarket")
        ends = []
        for name in names:
            session = sessions[name]
            if session is not None:
                start, end = timestamp(session["startTime"]), timestamp(session["endTime"])
                if end <= start or end - start > timedelta(days=1):
                    raise ValueError()
                ends.append(end)
        if not ends:
            raise ValueError()
        return previous_date if max(ends) < now else previous_date - timedelta(days=1)
    except (KeyError, ValueError, TypeError):
        raise invalid_data("시장 캘린더로 완료된 거래일을 판단할 수 없습니다.") from None


def normalize_candle(item, currency):
    try:
        if item["currency"] != currency:
            raise invalid_data("캔들의 통화가 일관되지 않습니다.")
        instant = timestamp(item["timestamp"])
        row = {"timestamp": instant.isoformat(),
               "date": instant.astimezone(market_zone(currency)).date().isoformat(), "currency": currency}
        for field in PRICE_FIELDS + ("volume",):
            row[field] = decimal(item[field])
        if not (row["lowPrice"] <= min(row["openPrice"], row["closePrice"]) <=
                max(row["openPrice"], row["closePrice"]) <= row["highPrice"]):
            raise invalid_data("일봉 OHLC 가격 범위가 일관되지 않습니다.")
        return instant, row
    except (KeyError, TypeError):
        raise invalid_data() from None


def history(client, symbol, count, adjusted=True, now=None):
    now = now or datetime.now(timezone.utc)
    candles, cursor, seen_cursors = {}, None, set()
    currency, cutoff = None, None
    warnings = [{"code": "conservative-daily-cutoff", "message":
                 "일봉 확정 시점이 명시되지 않아 시장 현지 당일 봉을 제외하고 이전 영업일까지 사용합니다."}]
    excluded = set()
    pages = 0
    for _ in range(3):
        params = {"symbol": symbol, "interval": "1d", "count": min(200, count + 2),
                  "adjusted": "true" if adjusted else "false"}
        if cursor is not None:
            params["before"] = cursor
        raw = client.get("/api/v1/candles", **params)
        pages += 1
        if not isinstance(raw, dict) or not isinstance(raw.get("candles"), list) or "nextBefore" not in raw:
            raise invalid_data()
        records = raw["candles"]
        if not records:
            break
        if currency is None:
            if not isinstance(records[0], dict):
                raise invalid_data()
            currency = records[0].get("currency")
            zone = market_zone(currency)
            country = "KR" if currency == "KRW" else "US"
            today = now.astimezone(zone).date()
            calendar = client.get("/api/v1/market-calendar/" + country, date=today.isoformat())
            cutoff = calendar_cutoff(calendar, country, today, now)
        for item in records:
            instant, row = normalize_candle(item, currency)
            if cursor is not None and instant > timestamp(cursor):
                raise invalid_data("캔들 페이지가 요청한 경계를 벗어났습니다.")
            if instant in candles and candles[instant] != row:
                raise invalid_data("같은 시각의 일봉 값이 서로 다릅니다.")
            candles[instant] = row
            if row["date"] > cutoff.isoformat():
                excluded.add(row["date"])
        complete = [row for _, row in sorted(candles.items()) if row["date"] <= cutoff.isoformat()]
        if len({r["date"] for r in complete}) != len(complete):
            raise invalid_data("하나의 거래일에 서로 다른 일봉 시각이 있습니다.")
        if len(complete) >= count:
            break
        next_cursor = raw["nextBefore"]
        if next_cursor is None:
            break
        next_time = timestamp(next_cursor)
        if next_time in seen_cursors or (cursor is not None and next_time >= timestamp(cursor)):
            warnings.append({"code": "pagination-stalled", "message": "과거 조회 경계가 진행되지 않아 추가 조회를 중단했습니다."})
            break
        seen_cursors.add(next_time)
        cursor = next_cursor
    complete = [row for _, row in sorted(candles.items()) if cutoff and row["date"] <= cutoff.isoformat()][-count:]
    if not complete:
        raise TmonError("no-complete-candles", "조회 범위에 완료된 일봉이 없습니다.")
    short = len(complete) < count
    if short:
        warnings.append({"code": "insufficient-history", "message": "%d봉 요청 중 %d봉을 확보했습니다." % (count, len(complete))})
    meta = {"symbol": symbol, "currency": currency, "adjusted": adjusted,
            "interval": "1d", "requestedCount": count, "usedCount": len(complete),
            "fromDate": complete[0]["date"], "toDate": complete[-1]["date"],
            "dataAsOf": complete[-1]["timestamp"], "completionCutoffDate": cutoff.isoformat(),
            "completionPolicy": "previous-market-business-day", "excludedDates": sorted(excluded), "pages": pages}
    return complete, meta, warnings, 5 if short else 0
