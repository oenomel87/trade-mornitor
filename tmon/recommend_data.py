"""Strict normalization of extra read-only market endpoints."""
from datetime import timedelta
from decimal import Decimal

from .errors import TmonError, invalid_data
from .intraday import completed_minutes, five_minutes, fresh, utcnow
from .market import decimal, history, normalize_candle, quote, timestamp
from .strategies import NoMatch

VENUE_SCOPE = 'TOSS_PROVIDED'


def stock_info(raw, symbol):
    if not isinstance(raw, list):
        raise invalid_data()
    matches = [r for r in raw if isinstance(r, dict) and r.get('symbol') == symbol]
    if len(matches) != 1:
        raise invalid_data('종목 상세 응답이 누락되거나 중복되었습니다.')
    r = matches[0]
    if r.get('market') not in ('KOSPI', 'KOSDAQ') or r.get('securityType') != 'STOCK' or r.get('isCommonShare') is not True:
        raise NoMatch('not-common-stock')
    if r.get('status') != 'ACTIVE' or r.get('currency') != 'KRW':
        raise NoMatch('inactive-stock')
    k = r.get('koreanMarketDetail')
    if not isinstance(k, dict) or any(type(k.get(x)) is not bool for x in ('krxTradingSuspended', 'liquidationTrading', 'nxtSupported')):
        raise invalid_data('국내 거래 상태를 확인할 수 없습니다.')
    nxt_suspended = k.get('nxtTradingSuspended')
    if nxt_suspended is not None and type(nxt_suspended) is not bool:
        raise invalid_data('NXT 거래 상태 값이 유효하지 않습니다.')
    if k['krxTradingSuspended'] or k['liquidationTrading'] or (k['nxtSupported'] and nxt_suspended is True):
        raise NoMatch('trading-suspended')
    if not isinstance(r.get('name'), str) or not r['name'].strip():
        raise invalid_data()
    return {'symbol': symbol, 'name': r['name'], 'market': r['market'],
            # Use provider defaults without claiming a verified exchange aggregation.
            'venueScope': VENUE_SCOPE, 'venueBasis': 'provider-default',
            'nxtSupported': k['nxtSupported']}


def warnings_ok(raw):
    if not isinstance(raw, list):
        raise invalid_data()
    for row in raw:
        if not isinstance(row, dict) or not isinstance(row.get('warningType'), str):
            raise invalid_data()
        # Both exchanges and unknown warnings use the same eligibility rule.
        raise NoMatch('stock-warning-' + row['warningType'][:64])


def orderbook(raw, now, max_age):
    try:
        if raw['currency'] != 'KRW':
            raise invalid_data()
        fresh(raw['timestamp'], now, max_age)
        asks, bids = raw['asks'], raw['bids']
        if not asks or not bids:
            raise invalid_data('호가가 비어 있습니다.')
        aa = [(decimal(r['price']), decimal(r['volume'])) for r in asks]
        bb = [(decimal(r['price']), decimal(r['volume'])) for r in bids]
        if aa != sorted(aa, key=lambda p:p[0]) or bb != sorted(bb, key=lambda p:p[0], reverse=True):
            raise invalid_data('호가 정렬이 유효하지 않습니다.')
        if not 0 < bb[0][0] < aa[0][0] or aa[0][1] <= 0 or bb[0][1] <= 0:
            raise invalid_data('교차·빈 호가입니다.')
        return {'bestAsk': aa[0][0], 'bestBid': bb[0][0], 'askVolume': aa[0][1], 'timestamp': raw['timestamp']}
    except (KeyError, TypeError, IndexError):
        raise invalid_data('호가 필수 값이 누락되었습니다.') from None


class RecordingClient:
    def __init__(self, client, now=utcnow):
        self.client, self.now = client, now
        self.records = []
        self.calendar_cache = {}

    def get(self, path, **params):
        key = (path, tuple(sorted(params.items())))
        if 'market-calendar' in path and key in self.calendar_cache:
            return self.calendar_cache[key]
        value = self.client.get(path, **params)
        self.records.append({'path': path, 'params': params, 'retrievedAt': self.now().isoformat(), 'result': value})
        if 'market-calendar' in path:
            self.calendar_cache[key] = value
        return value


def daily_data(client, symbol, today, now):
    rows, meta, _, _ = history(client, symbol, 120, now=now)
    if rows[-1]['date'] != today['previousBusinessDay']['date']:
        raise TmonError('stale-daily', '직전 완료 거래일의 일봉이 없습니다.')
    return rows


def minute_data(client, symbol, session, now, settings, warnings=None):
    raw = client.get('/api/v1/candles', symbol=symbol, interval='1m', count=125, adjusted='true')
    retrieved_at = client.records[-1]['retrievedAt'] if isinstance(client, RecordingClient) else None
    diagnostics = []
    try:
        rows = completed_minutes(raw, now, session['start'], settings['barCompletionDelaySeconds'], diagnostics)
    except TmonError as error:
        error.details = {**getattr(error, 'details', {}), 'evaluatedAt': now.isoformat(),
                         'retrievedAt': retrieved_at}
        raise
    finally:
        if warnings is not None:
            warnings.extend({**item, 'symbol': symbol, 'retrievedAt': retrieved_at} for item in diagnostics)
    if not rows:
        raise TmonError('insufficient-minute-bars', '완료 분봉이 없습니다.')
    fresh((timestamp(rows[-1]['timestamp']) + timedelta(minutes=1)).isoformat(), now, settings['minuteSeconds'])
    return rows, five_minutes(rows, session['start'])


def current_data(client, symbol, settings, now=utcnow):
    stock = stock_info(client.get('/api/v1/stocks', symbols=symbol), symbol)
    warnings_ok(client.get('/api/v1/stocks/' + symbol + '/warnings'))
    state_time = now()
    prices, _, _, _ = quote(client, [symbol])
    price = prices[0]
    if price['currency'] != 'KRW':
        raise invalid_data()
    fresh(price['timestamp'], now(), settings['quoteSeconds'])
    book = orderbook(client.get('/api/v1/orderbook', symbol=symbol), now(), settings['quoteSeconds'])
    limits = client.get('/api/v1/price-limits', symbol=symbol)
    if not isinstance(limits, dict) or limits.get('currency') != 'KRW':
        raise invalid_data()
    upper = decimal(limits['upperLimitPrice']) if limits.get('upperLimitPrice') is not None else None
    # The timestamp of price limits may legitimately be a start-of-session timestamp.
    from .market import market_zone
    if (not limits.get('timestamp') or
        timestamp(limits['timestamp']).astimezone(market_zone('KRW')).date() != state_time.astimezone(market_zone('KRW')).date() or
        timestamp(limits['timestamp']) > state_time + timedelta(seconds=5)):
        raise invalid_data('당일 상하한가를 확인할 수 없습니다.')
    return stock, price, book, upper, state_time


def relative_return(client, market, horizon, daily, minutes, now, cache):
    """Optional market-relative return, aligned by exact endpoint timestamps/dates."""
    interval = '1m' if horizon == 'day' else '1d'
    key = (market, interval)
    if key not in cache:
        try:
            raw = client.get('/api/v1/market-indicators/' + market + '/candles', interval=interval, count=125)
            cache[key] = [normalize_candle({**r, 'currency':'KRW'}, 'KRW')[1] for r in raw['candles']]
        except (TmonError, KeyError, TypeError):
            cache[key] = []
    index = cache[key]
    if horizon == 'day':
        bars = minutes[-30:]
        if len(bars) < 30:
            return None
        from .intraday import require_contiguous
        try:
            require_contiguous(bars, 1)
        except TmonError:
            return None
        mapped = {timestamp(r['timestamp']): r for r in index}
        a, b = mapped.get(timestamp(bars[0]['timestamp'])), mapped.get(timestamp(bars[-1]['timestamp']))
        if a is None or b is None:
            return None
        start, end, istart, iend = bars[0]['openPrice'], bars[-1]['closePrice'], a['openPrice'], b['closePrice']
    else:
        mapped = {r['date']: r for r in index}
        a, b = mapped.get(daily[-6]['date']), mapped.get(daily[-1]['date'])
        if a is None or b is None:
            return None
        start, end, istart, iend = daily[-6]['closePrice'], daily[-1]['closePrice'], a['closePrice'], b['closePrice']
    return ((end / start - 1) - (iend / istart - 1)) * 100 if start > 0 and istart > 0 else None
