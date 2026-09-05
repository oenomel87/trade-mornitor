"""Completed bars, market sessions and freshness checks."""
from datetime import datetime, timedelta, timezone

from .errors import TmonError, invalid_data
from .market import market_zone, normalize_candle, timestamp


def utcnow():
    return datetime.now(timezone.utc)


def fresh(value, now, seconds):
    instant = timestamp(value)
    age = (now - instant).total_seconds()
    if age < -5 or age > seconds:
        raise TmonError('stale-data', '시세·봉·랭킹이 신선도 기준을 충족하지 않습니다.')
    return instant


def session(raw, now, horizon):
    try:
        today = raw['today']
        if today['date'] != now.astimezone(market_zone('KRW')).date().isoformat():
            raise invalid_data('현재 거래일의 시장 캘린더가 아닙니다.')
        integrated = today['integrated']
        regular = integrated.get('regularMarket') if integrated is not None else None
        if regular is None:
            return None, 'market-closed'
        start, end = timestamp(regular['startTime']), timestamp(regular['endTime'])
        auction_raw = regular.get('singlePriceAuctionStartTime')
        if auction_raw is None:
            raise TmonError('unverified-session', 'KRX 연속매매 종료 시각을 확인할 수 없습니다.')
        auction = timestamp(auction_raw)
        if not start < auction < end or (end - start) > timedelta(days=1):
            raise invalid_data('정규장 세션 범위가 유효하지 않습니다.')
        if start.astimezone(market_zone('KRW')).date().isoformat() != today['date']:
            raise invalid_data()
        entry_end = min(auction, end - timedelta(minutes=30)) if horizon == 'day' else auction
        result = {'start': start, 'end': end, 'auction': auction, 'entryEnd': entry_end,
                  'exitBy': end - timedelta(minutes=10), 'date': today['date']}
        if not start <= now < end:
            return result, 'market-closed'
        if not start + timedelta(minutes=30) <= now < entry_end:
            return result, 'outside-entry-window'
        return result, None
    except (KeyError, TypeError, AttributeError):
        raise invalid_data('시장 세션을 해석할 수 없습니다.') from None


def completed_minutes(raw, now, start, delay=5):
    if not isinstance(raw, dict) or not isinstance(raw.get('candles'), list):
        raise invalid_data()
    rows = {}
    for item in raw['candles']:
        t, row = normalize_candle(item, 'KRW')
        if t.second or t.microsecond:
            raise invalid_data('분봉 시작 시각이 분 경계가 아닙니다.')
        if t > now + timedelta(seconds=5):
            raise invalid_data('미래 분봉입니다.')
        if t < start or t + timedelta(minutes=1, seconds=delay) > now:
            continue
        if t in rows and rows[t] != row:
            raise invalid_data('중복 분봉 값이 다릅니다.')
        rows[t] = row
    return [rows[t] for t in sorted(rows)]


def five_minutes(rows, start):
    groups = {}
    for row in rows:
        t = timestamp(row['timestamp'])
        bucket = int((t - start).total_seconds()) // 300
        groups.setdefault(bucket, {})[t] = row
    output = []
    for bucket, values in sorted(groups.items()):
        t = start + timedelta(minutes=5 * bucket)
        times = [t + timedelta(minutes=i) for i in range(5)]
        if set(values) != set(times):
            continue
        bars = [values[x] for x in times]
        output.append({'timestamp': t.isoformat(), 'openPrice': bars[0]['openPrice'],
                       'closePrice': bars[-1]['closePrice'], 'highPrice': max(r['highPrice'] for r in bars),
                       'lowPrice': min(r['lowPrice'] for r in bars), 'volume': sum(r['volume'] for r in bars)})
    return output


def require_contiguous(rows, minutes):
    if any(timestamp(b['timestamp']) - timestamp(a['timestamp']) != timedelta(minutes=minutes)
           for a, b in zip(rows, rows[1:])):
        raise TmonError('incomplete-bars', '신호 계산 구간에 봉 누락이 있습니다.')
