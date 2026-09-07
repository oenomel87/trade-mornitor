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


def signal_window_end(session_start, phase_started_at, delay=5):
    """Frozen signal window end F = S + n*300 where n=floor((P-S-D)/300.

    Returns None when n <= 0 (no completed 5-minute bar yet). Callers
    treat n < 6 as expected session-not-ready, not unexplained missing data.
    """
    try:
        delta = (phase_started_at - session_start).total_seconds() - delay
    except (TypeError, AttributeError):
        raise invalid_data('신호 구간 시각을 확인할 수 없습니다.') from None
    import math
    n = math.floor(delta / 300)
    if n <= 0:
        return None
    return session_start + timedelta(seconds=n * 300)


def filter_complete_bars(raw, received_at, phase_started_at, delay, normalize_fn,
                          session_start=None, window_start=None, window_end=None,
                          warnings=None, future_code='future-minute-bar',
                          future_message='다음 분 경계보다 먼 미래 분봉입니다.',
                          skip_code='future-minute-bar-skipped',
                          skip_message='다음 분 경계의 미래 봉을 계산에서 제외했습니다.'):
    """Shared PR1 time-filter path for stock and index 1-minute bars.

    Work-spec order shared by both legs: parse/check timestamp first and
    apply the allowed next-minute boundary exclusion using received_at,
    then normalize/validate only retained candles. A malformed
    next-boundary candle is therefore skipped with a diagnostic instead
    of failing the symbol; farther future still fails. Completion uses
    the actual phase start (barEnd + D <= phase); clipping uses the
    frozen window with no extra delay. ``normalize_fn`` validates the
    leg-specific schema (stock: currency/OHLC via normalize_candle;
    index: actual no-currency MarketIndicatorCandle schema).
    """
    if not isinstance(raw, dict) or not isinstance(raw.get('candles'), list):
        raise invalid_data()
    rows = {}
    for item in raw['candles']:
        try:
            raw_ts = item.get('timestamp') if isinstance(item, dict) else None
            t = timestamp(raw_ts)
        except Exception:
            raise invalid_data('분봉 시작 시각을 확인할 수 없습니다.') from None
        if t.second or t.microsecond:
            raise invalid_data('분봉 시작 시각이 분 경계가 아닙니다.')
        next_boundary = received_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if t > next_boundary:
            error = TmonError(future_code, future_message)
            error.details = {'evaluatedAt': received_at.isoformat(), 'barTimestamp': t.isoformat()}
            raise error
        if t > received_at:
            if warnings is not None:
                warnings.append({'code': skip_code, 'message': skip_message,
                                 'evaluatedAt': received_at.isoformat(), 'barTimestamp': t.isoformat()})
            continue
        _, row = normalize_fn(item)
        if (session_start is not None and t < session_start) or \
                t + timedelta(minutes=1, seconds=delay) > phase_started_at:
            continue
        if window_start is not None and t < window_start:
            continue
        if window_end is not None and t + timedelta(minutes=1) > window_end:
            continue
        if t in rows and rows[t] != row:
            raise invalid_data('중복 분봉 값이 다릅니다.')
        rows[t] = row
    return [rows[t] for t in sorted(rows)]


def completed_minutes(raw, now, start, delay=5, warnings=None, *,
                      received_at=None, phase_started_at=None, window_end=None):
    """Completed 1-minute bars with separated time roles (PR1).

    - ``received_at``: actual response receipt instant for future-boundary
      validation (next-minute-boundary exclusion). Defaults to ``now`` so
      existing brief calls are unchanged.
    - ``phase_started_at``: actual screening-phase start for completion
      (barEnd + D <= phaseStartedAt). Defaults to ``received_at``.
    - ``window_end``: frozen signalWindowEndAt F for clipping
      (barEnd <= F, no additional delay subtracted). None disables clipping.

    ``now`` is kept for backward compatibility and supplies defaults.
    Uses the shared filter_complete_bars path (same PR1 future-before-OHLC,
    completion and clip rules as the index leg).
    """
    received = received_at if received_at is not None else now
    phase = phase_started_at if phase_started_at is not None else received

    def _stock_normalizer(item):
        return normalize_candle(item, 'KRW')

    return filter_complete_bars(raw, received, phase, delay, _stock_normalizer,
                                session_start=start, window_start=None,
                                window_end=window_end, warnings=warnings)


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
