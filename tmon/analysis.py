"""Deterministic Decimal calculations on ascending, completed daily candles."""

from decimal import Decimal, localcontext


def analyze(candles):
    with localcontext() as context:
        context.prec = 50
        return _analyze(candles)


def _analyze(candles):
    closes = [row["closePrice"] for row in candles]
    values, unavailable = {}, {}

    def unavailable_value(name, reason):
        values[name] = None
        unavailable[name] = reason

    def ratio(name, numerator, denominator, pct=False):
        if denominator == 0:
            unavailable_value(name, "분모가 0입니다.")
        else:
            values[name] = (numerator / denominator - 1) * 100 if pct else numerator / denominator

    if len(closes) >= 2:
        ratio("closeChangePct", closes[-1], closes[-2], True)
    else:
        unavailable_value("closeChangePct", "2봉 이상 필요합니다.")
    for period in (20, 60):
        name, gap = "sma%d" % period, "sma%dGapPct" % period
        if len(closes) >= period:
            values[name] = sum(closes[-period:]) / period
            ratio(gap, closes[-1], values[name], True)
        else:
            for field in (name, gap):
                unavailable_value(field, "%d봉 이상 필요합니다." % period)
    if len(closes) < 15:
        unavailable_value("rsi14", "15봉 이상 필요합니다.")
    else:
        differences = [b - a for a, b in zip(closes, closes[1:])]
        gain = sum(max(d, Decimal(0)) for d in differences[:14]) / 14
        loss = sum(max(-d, Decimal(0)) for d in differences[:14]) / 14
        for change in differences[14:]:
            gain = (gain * 13 + max(change, Decimal(0))) / 14
            loss = (loss * 13 + max(-change, Decimal(0))) / 14
        values["rsi14"] = (Decimal(50) if gain == loss == 0 else Decimal(100) if loss == 0
                           else 100 - 100 / (1 + gain / loss))
    if len(candles) >= 21:
        ratio("volumeRatio", candles[-1]["volume"], sum(r["volume"] for r in candles[-21:-1]) / 20)
    else:
        unavailable_value("volumeRatio", "21봉 이상 필요합니다.")
    facts = []
    for period in (20, 60):
        sma = values["sma%d" % period]
        if sma is not None:
            position = "위에 있습니다" if closes[-1] > sma else "아래에 있습니다" if closes[-1] < sma else "같습니다"
            facts.append("종가가 SMA%d와 %s." % (period, position) if closes[-1] == sma
                         else "종가가 SMA%d %s." % (period, position))
    return {"lastClose": closes[-1], "lastVolume": candles[-1]["volume"],
            "indicators": values, "unavailable": unavailable, "facts": facts}
