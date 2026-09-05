"""Provider rankings, with explicit period and aggregation semantics."""

from decimal import localcontext
import re

from .errors import TmonError, invalid_data
from .market import decimal, timestamp

METRICS = ("amount", "volume", "gain", "loss")
DURATIONS = ("realtime", "1d", "1w", "1mo", "3mo", "6mo", "1y")
SOURCES = ("market", "toss")
METRIC_LABELS = {"amount": "거래대금 상위", "volume": "거래량 상위", "gain": "상승률 상위", "loss": "하락률 상위"}


def validate_rank(market, by, duration, source, limit):
    if market not in ("KR", "US") or by not in METRICS or duration not in DURATIONS or source not in SOURCES:
        raise TmonError("invalid-ranking-options", "지원하지 않는 랭킹 옵션입니다.", 2)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise TmonError("invalid-limit", "랭킹 --limit는 1~100이어야 합니다.", 2)
    if by in ("gain", "loss"):
        if source == "toss":
            raise TmonError("unsupported-ranking-source", "상승률·하락률 랭킹은 --source market만 지원합니다.", 2)
        if duration == "realtime":
            raise TmonError("unsupported-ranking-duration", "상승률·하락률 랭킹에는 --duration 1d 이상을 사용하세요.", 2)


def ranking_type(by, source):
    if by in ("gain", "loss"):
        return "TOP_GAINERS" if by == "gain" else "TOP_LOSERS"
    prefix = "MARKET" if source == "market" else "TOSS_SECURITIES"
    return prefix + ("_TRADING_AMOUNT" if by == "amount" else "_TRADING_VOLUME")


def change_percent(value):
    if value is None:
        return None
    with localcontext() as context:
        context.prec = 50
        if isinstance(value, str) and value.startswith("-"):
            return -decimal(value[1:]) * 100
        return decimal(value) * 100


def rank(client, market="KR", by="amount", duration="1d", source="market", limit=20, exclude_caution=False):
    validate_rank(market, by, duration, source, limit)
    kind = ranking_type(by, source)
    raw = client.get("/api/v1/rankings", type=kind, marketCountry=market, duration=duration,
                     count=limit, excludeInvestmentCaution="true" if exclude_caution else "false")
    if not isinstance(raw, dict) or not isinstance(raw.get("rankings"), list):
        raise invalid_data("랭킹 목록 형식이 올바르지 않습니다.")
    if len(raw["rankings"]) > limit:
        raise invalid_data("랭킹 응답이 요청한 개수를 초과했습니다.")
    ranked_at = timestamp(raw["rankedAt"]).isoformat() if raw.get("rankedAt") is not None else None
    rows, seen = [], set()
    for item in raw["rankings"]:
        try:
            if not isinstance(item, dict) or type(item["rank"]) is not int or item["rank"] < 1:
                raise invalid_data("유효하지 않은 랭킹 순위입니다.")
            symbol = item["symbol"]
            if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,63}", symbol):
                raise invalid_data("유효하지 않은 랭킹 종목코드입니다.")
            symbol = symbol.upper()
            if symbol in seen:
                raise invalid_data("랭킹 응답에 중복 종목코드가 있습니다.")
            seen.add(symbol)
            currency = item["currency"]
            if currency != ("KRW" if market == "KR" else "USD"):
                raise invalid_data("랭킹의 통화가 요청한 시장과 다릅니다.")
            price = item["price"]
            if not isinstance(price, dict):
                raise invalid_data("랭킹 가격 형식이 올바르지 않습니다.")
            row = {"rank": item["rank"], "symbol": symbol, "currency": currency,
                   "lastPrice": decimal(price["lastPrice"]), "basePrice": decimal(price["basePrice"]),
                   "changePct": change_percent(price.get("changeRate")),
                   "tradingVolume": decimal(item["tradingVolume"]), "tradingAmount": decimal(item["tradingAmount"]),
                   "tradingAmountCurrency": "KRW" if market == "KR" else None}
            rows.append(row)
        except (KeyError, TypeError):
            raise invalid_data("랭킹 응답의 필수 필드를 확인할 수 없습니다.") from None
    rows.sort(key=lambda row: row["rank"])
    warnings = []
    if rows and market == "US":
        # Live US amounts do not reconcile with USD prices/volume. The spec
        # does not explicitly define their currency; preserve raw values.
        warnings.append({"code": "unverified-trading-amount-currency", "message":
                         "미국 거래대금의 통화는 확인되지 않았습니다. AMOUNT는 API 원본 값이며 달러로 환산·표기하지 않습니다."})
    if rows and len(rows) < limit:
        warnings.append({"code": "fewer-ranking-results", "message": "%d개 요청 중 %d개가 반환되었습니다. 공급자 응답만 표시합니다." % (limit, len(rows))})
    if rows and ranked_at is None:
        warnings.append({"code": "missing-ranked-at", "message": "공급자가 랭킹 집계 시각을 제공하지 않았습니다."})
    missing_rates = [row["symbol"] for row in rows if row["changePct"] is None]
    if missing_rates:
        warnings.append({"code": "missing-change-rate", "message": "등락률 미제공 종목: " + ", ".join(missing_rates)})
    meta = {"market": market, "by": by, "duration": duration, "rankingType": kind,
            "aggregationScope": source, "changeBasis": "period-start" if by in ("gain", "loss") else "previous-close",
            "excludeInvestmentCaution": exclude_caution, "requestedCount": limit, "returnedCount": len(rows),
            "rankedAt": ranked_at, "missingChangeSymbols": missing_rates}
    return rows, meta, warnings, 0
