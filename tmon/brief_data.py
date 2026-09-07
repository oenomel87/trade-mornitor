"""Dated market observations for a briefing, independent of entry windows."""
from datetime import date, timedelta
from decimal import Decimal

from .errors import TmonError, invalid_data
from .market import decimal, market_zone, timestamp, quote
from .ranking import rank


def _response_received_at(client, fallback):
    """Return the receipt instant for the most recent logical API read.

    ``fallback`` keeps the standalone helpers compatible with simple test
    clients and callers that do not use ``RecordingClient``.  Briefing
    collection wraps real clients in ``RecordingClient``, whose receipt marks
    are the clock for response freshness and completed-bar evaluation.
    """
    records = getattr(client, 'records', None)
    if not isinstance(records, list) or not records:
        return fallback
    try:
        return timestamp(records[-1]['receivedAt'])
    except (KeyError, TypeError, TmonError):
        return fallback


def kst(value):
    return timestamp(value).astimezone(market_zone('KRW')).isoformat()


def calendar_context(raw, now, requested='auto'):
    today = now.astimezone(market_zone('KRW')).date().isoformat()
    try:
        if raw['today']['date'] != today:
            raise ValueError()
        previous, following = raw['previousBusinessDay']['date'], raw['nextBusinessDay']['date']
        if not date.fromisoformat(previous) < date.fromisoformat(today) < date.fromisoformat(following):
            raise ValueError()
        integrated = raw['today']['integrated']
        regular = integrated.get('regularMarket') if integrated is not None else None
        start = end = None
        if regular is None:
            phase = 'closed'
        else:
            start, end = timestamp(regular['startTime']), timestamp(regular['endTime'])
            if not start < end or (end - start) > timedelta(days=1) or start.astimezone(market_zone('KRW')).date().isoformat() != today:
                raise ValueError()
            phase = 'premarket' if now < start else 'intraday' if now < end else 'close'
        return {'phase': phase, 'session': phase if requested == 'auto' else requested,
                'date': today, 'referenceDate': today if phase in ('intraday', 'close') else previous,
                'previousBusinessDay': previous, 'nextBusinessDay': following,
                'regularStart': kst(start.isoformat()) if start else None,
                'regularEnd': kst(end.isoformat()) if end else None}
    except (KeyError, TypeError, AttributeError, ValueError):
        raise invalid_data('브리핑 기준 거래일을 시장 캘린더에서 확인할 수 없습니다.') from None


def freshness(value, context, now):
    if value is None:
        return 'unknown'
    instant = timestamp(value)
    if instant > now + timedelta(seconds=5):
        raise invalid_data('미래 시각의 시세입니다.')
    if instant.astimezone(market_zone('KRW')).date().isoformat() != context['referenceDate']:
        return 'stale'
    if context['phase'] == 'intraday' and (now - instant).total_seconds() > 180:
        return 'stale'
    return 'current-session' if context['phase'] == 'intraday' else 'dated-snapshot'


def index_prices(client, context, now):
    raw = client.get('/api/v1/market-indicators/prices', symbols='KOSPI,KOSDAQ')
    received_at = _response_received_at(client, now)
    if not isinstance(raw, list):
        raise invalid_data()
    rows, seen = [], set()
    for item in raw:
        try:
            sym = item['symbol']
            if sym not in ('KOSPI', 'KOSDAQ') or sym in seen:
                raise invalid_data()
            seen.add(sym)
            value = item.get('timestamp')
            rows.append({'symbol': sym, 'lastPrice': decimal(item['lastPrice']), 'unit': 'point',
                         'asOf': kst(value) if value else None, 'freshness': freshness(value, context, received_at),
                         'priceBasis': 'provider-quote', 'timeBasis': 'provider-quote', 'providerLastPrice': decimal(item['lastPrice']),
                         'providerAsOf': kst(value) if value else None,
                         'changePct': None, 'baseDate': None, 'basePrice': None})
        except (KeyError, TypeError):
            raise invalid_data() from None
    if seen != {'KOSPI', 'KOSDAQ'}:
        raise invalid_data('국내 지수 시세가 누락되었습니다.')
    return rows


def dated_index_fallback(client, row, context, now, warnings=None):
    """Use a separately dated completed minute close, never timestamp an undated quote."""
    raw = client.get('/api/v1/market-indicators/' + row['symbol'] + '/candles', interval='1m', count=5)
    received_at = _response_received_at(client, now)
    try:
        from .recommend_data import completed_index_minutes
        notes = []
        try:
            bars = completed_index_minutes(raw, received_at, received_at, None, 5,
                                           None, None, warnings=notes)
        finally:
            if warnings is not None:
                warnings.extend({**note, 'symbol': row['symbol']} for note in notes)
        if not bars:
            raise invalid_data('시각을 확인할 수 있는 완료 지수 분봉이 없습니다.')
        stamp = bars[-1]['timestamp']
        row.update(lastPrice=bars[-1]['closePrice'], asOf=kst(stamp),
                   freshness=freshness(stamp, context, received_at), priceBasis='completed-minute-close', timeBasis='bar-start')
    except (KeyError, TypeError):
        raise invalid_data() from None


def index_change(client, row, now):
    if row['asOf'] is None:
        raise invalid_data('지수 시각이 없어 전 거래일 대비 등락률을 계산하지 않습니다.')
    pricedate = timestamp(row['asOf']).astimezone(market_zone('KRW')).date().isoformat()
    cal = client.get('/api/v1/market-calendar/KR', date=pricedate)
    context = calendar_context(cal, timestamp(row['asOf']))
    expected = context['previousBusinessDay']
    raw = client.get('/api/v1/market-indicators/' + row['symbol'] + '/candles', interval='1d', count=10)
    received_at = _response_received_at(client, now)
    try:
        from .recommend_data import normalize_index_candle
        matches = []
        for item in raw['candles']:
            instant, candle = normalize_index_candle(item)
            if instant > received_at:
                raise invalid_data()
            if candle['date'] == expected:
                matches.append(candle)
        if len(matches) != 1 or matches[0]['closePrice'] <= 0:
            raise invalid_data('전 거래일 지수 종가를 확인할 수 없습니다.')
        base = matches[0]['closePrice']
        row.update(baseDate=expected, basePrice=base, changePct=(row['lastPrice'] / base - 1) * 100)
    except (KeyError, TypeError):
        raise invalid_data() from None


def investor_flow(client, symbol, context, now):
    raw = client.get('/api/v1/market-indicators/' + symbol + '/investor-trading',
                     interval='1d', count=1, until=context['referenceDate'])
    received_at = _response_received_at(client, now)
    try:
        records = raw['records']
        if not isinstance(records, list) or len(records) != 1:
            raise invalid_data('투자자별 매매대금이 없습니다.')
        r = records[0]
        if date.fromisoformat(r['date']) > date.fromisoformat(context['referenceDate']):
            raise invalid_data()
        if timestamp(r['updatedAt']).astimezone(market_zone('KRW')).date() < date.fromisoformat(r['date']):
            raise invalid_data()
        buys, sells, net = [], [], {}
        for who in ('individual', 'foreigner', 'institution', 'otherCorporation'):
            buy, sell = decimal(r[who]['buyAmount']), decimal(r[who]['sellAmount'])
            if buy != buy.to_integral_value() or sell != sell.to_integral_value():
                raise invalid_data()
            buys.append(buy)
            sells.append(sell)
            net[who] = buy - sell
        if sum(buys) != sum(sells):
            raise invalid_data('투자자별 매수·매도 합계가 일치하지 않습니다.')
        state = freshness(r['updatedAt'], context, received_at)
        if r['date'] != context['referenceDate']:
            state = 'stale'
        return {'symbol': symbol, 'date': r['date'], 'asOf': kst(r['updatedAt']), 'freshness': state,
                'turnoverKrw': sum(buys, Decimal(0)), 'netBuyingKrw': net,
                'provisional': r['date'] == context['date'], 'venueScope': 'KRX'}
    except (KeyError, TypeError, ValueError):
        raise invalid_data('투자자별 매매대금 응답을 해석할 수 없습니다.') from None


def collect(client, context, now, symbols, warnings):
    data = {'indices': [], 'investorTrading': [], 'rankings': [], 'watchlist': []}
    def attempt(label, action):
        try:
            return action()
        except TmonError as e:
            warning = {'code': e.code, 'message': label + ': ' + e.message}
            details = getattr(e, 'details', {})
            if isinstance(details, dict):
                warning.update({key: value for key, value in details.items()
                                if key not in ('code', 'message')})
            warnings.append(warning)
        return None

    data['indices'] = attempt('국내 지수', lambda: index_prices(client, context, now)) or []
    for row in data['indices']:
        if row['asOf'] is None:
            attempt(row['symbol'] + ' 완료 분봉', lambda: dated_index_fallback(client, row, context, now, warnings))
        attempt(row['symbol'] + ' 등락률', lambda: index_change(client, row, now))
    for sym in ('KOSPI', 'KOSDAQ'):
        row = attempt(sym + ' 수급', lambda: investor_flow(client, sym, context, now))
        if row:
            data['investorTrading'].append(row)
    for metric in ('amount', 'gain', 'loss'):
        ranked = attempt(metric + ' 랭킹', lambda: rank(client, 'KR', metric, '1d', 'market', 5))
        if ranked:
            rows, meta, notes, _ = ranked
            warnings.extend(notes)
            stamp = meta['rankedAt']
            received_at = _response_received_at(client, now)
            state = attempt(metric + ' 집계 시각', lambda: freshness(stamp, context, received_at))
            if state is None:
                continue
            data['rankings'].append({'by': metric, 'asOf': kst(stamp) if stamp else None,
                                     'freshness': state, 'changeBasis': meta['changeBasis'],
                                     'duration': '1d', 'venueScope': 'TOSS_PROVIDED', 'rows': rows})
    if symbols:
        quoted = attempt('관심종목 현재가', lambda: quote(client, symbols))
        if quoted:
            rows, _, notes, _ = quoted
            warnings.extend(notes)
            received_at = _response_received_at(client, now)
            for row in rows:
                # US watchlist quotes retain their own market date and never inherit KR freshness.
                row['timestamp'] = kst(row['timestamp'])
                row['freshness'] = (attempt(row['symbol'] + ' 시각', lambda: freshness(row['timestamp'], context, received_at))
                                    if row['currency'] == 'KRW' else 'dated-snapshot') or 'unknown'
            data['watchlist'] = rows
    all_symbols = list(dict.fromkeys(symbols + [r['symbol'] for g in data['rankings'] for r in g['rows']]))
    names = {}
    if all_symbols:
        def details():
            raw = client.get('/api/v1/stocks', symbols=','.join(all_symbols))
            if not isinstance(raw, list):
                raise invalid_data()
            result = {}
            for r in raw:
                if not isinstance(r, dict) or r.get('symbol') not in all_symbols or not isinstance(r.get('name'), str):
                    raise invalid_data()
                result[r['symbol']] = {'symbol': r['symbol'], 'name': r['name'], 'market': r.get('market')}
            return result
        names = attempt('종목명', details) or {}
    for row in data['watchlist'] + [r for g in data['rankings'] for r in g['rows']]:
        row['name'] = names.get(row['symbol'], {}).get('name')
    for row in data['indices'] + data['investorTrading'] + data['rankings'] + data['watchlist']:
        if row['freshness'] in ('stale', 'unknown'):
            warnings.append({'code': 'brief-stale-data', 'message': (row.get('symbol') or row.get('by')) + ': 시각 미확인 또는 오래된 자료입니다.'})
    return data, [names.get(s, {'symbol': s, 'name': None, 'market': None}) for s in symbols]
