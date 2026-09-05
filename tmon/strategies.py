"""Deterministic breakout-v1. No I/O, model judgement or probability scores."""
from decimal import Decimal, localcontext
from datetime import timedelta

from .errors import TmonError
from .market import timestamp
from .intraday import require_contiguous

D = Decimal
VERSION = 'breakout-v1'


class NoMatch(TmonError):
    def __init__(self, code):
        super().__init__(code, '전략 조건을 충족하지 않습니다.')


def mean(values):
    return sum(values) / len(values)


def signal(daily, five, horizon, settings, now):
    with localcontext() as ctx:
        ctx.prec = 50
        needed = 20 if horizon == 'day' else 65
        if len(daily) < needed:
            raise TmonError('insufficient-history', '필수 일봉이 부족합니다.')
        estimated = mean([r['closePrice'] * r['volume'] for r in daily[-20:]])
        if estimated < D(settings['minEstimatedAvgDailyAmount']):
            raise NoMatch('low-liquidity')
        if horizon == 'day':
            if len(five) < 6:
                raise TmonError('insufficient-minute-bars', '완료 5분봉 6개가 필요합니다.')
            window = five[-6:]
            require_contiguous(window, 5)
            b, prev = window[-1], window[:-1]
            # Last completed 5m bar must not be silently replaced by an older complete block.
            if (now - timestamp(b['timestamp']) - timedelta(minutes=5)).total_seconds() >= 305:
                raise TmonError('incomplete-bars', '최신 완료 5분봉이 없습니다.')
        else:
            b, prev = daily[-1], daily[-21:-1]
            sma20 = mean([r['closePrice'] for r in daily[-20:]])
            sma60 = mean([r['closePrice'] for r in daily[-60:]])
            old20 = mean([r['closePrice'] for r in daily[-25:-5]])
            if not b['closePrice'] > sma20 > sma60 or not sma20 > old20:
                raise NoMatch('trend-not-confirmed')
        level = max(r['highPrice'] for r in prev)
        avg = mean([r['volume'] for r in prev])
        if avg <= 0:
            raise TmonError('zero-volume-baseline', '거래량 비교 기준이 0입니다.')
        vr = b['volume'] / avg
        if b['closePrice'] <= level or vr < D(settings['minVolumeRatio']):
            raise NoMatch('no-volume-breakout')
        stop = min(b['lowPrice'], prev[-1]['lowPrice'])
        return {'breakoutLevel': level, 'volumeRatio': vr, 'invalidation': stop,
                'estimatedAvgDailyAmount': estimated, 'signalAsOf': b['timestamp'],
                'relativeReturnPct': None,
                'quantitativeReasons': ['완료 봉이 이전 구간 고가를 돌파함', '직전 구간 대비 거래량 배수 충족']}


def entry(sig, book, last_price, upper, horizon, config):
    with localcontext() as ctx:
        ctx.prec = 50
        e, bid = book['bestAsk'], book['bestBid']
        spread = (e - bid) / ((e + bid) / 2) * 100
        setting = config[horizon]
        if spread > D(setting['maxSpreadPct']):
            raise NoMatch('wide-spread')
        b, s = sig['breakoutLevel'], sig['invalidation']
        ceiling = b * (1 + D(setting['maxBreakoutGapPct']) / 100)
        if not 0 < s < b < e <= ceiling or not b < last_price <= ceiling:
            raise NoMatch('entry-invalidated')
        if upper is None or e >= upper:
            raise NoMatch('price-limit')
        r = e - s
        risk = r / e * 100
        if config['maxLossPct'] is not None and risk > D(config['maxLossPct']):
            raise NoMatch('loss-limit')
        target_low, target_high = e + D('1.5') * r, e + 2 * r
        if horizon == 'day':
            if upper < target_low:
                raise NoMatch('target-above-price-limit')
            target_high = min(upper, target_high)
        quantity = None
        if config['capital'] is not None:
            quantity = int(D(config['capital']) // e)
            if quantity == 0:
                raise NoMatch('capital-below-one-share')
            if quantity > book['askVolume']:
                raise NoMatch('insufficient-visible-depth')
        return {**sig, 'entryReference': e, 'entryRange': {'lowerExclusive': b, 'upperInclusive': ceiling},
                'lastPrice': last_price, 'bestAsk': e, 'bestBid': bid, 'spreadPct': spread,
                'riskPct': risk, 'riskPerShare': r, 'targetRange': [target_low, target_high],
                'targetBasis': 'risk-multiple', 'targetRewardRatios': [(target_low-e)/r, (target_high-e)/r],
                'quantity': D(quantity) if quantity is not None else None,
                'estimatedPriceRiskKrw': quantity * r if quantity is not None else None,
                'sizeCheck': 'passed' if quantity is not None else 'not-configured',
                'holdingSessions': [1, 1] if horizon == 'day' else [2, 5]}


def sort_key(row):
    relative = row.get('relativeReturnPct')
    return (relative is None, -relative if relative is not None else D(0),
            -row['volumeRatio'], row['spreadPct'], row['symbol'])
