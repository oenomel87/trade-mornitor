"""Strict normalization of extra read-only market endpoints."""
import copy
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal

from .client import endpoint_group
from .errors import TmonError, invalid_data, safe_code
from .intraday import completed_minutes, five_minutes, fresh, signal_window_end, utcnow
from .market import decimal, history, market_zone, normalize_candle, quote, timestamp
from .recommend_daily import DAILY_CALENDAR_UNVERIFIED, DAILY_GAP, validate_daily_dates
from .strategies import NoMatch

VENUE_SCOPE = 'TOSS_PROVIDED'


def stock_eligibility(raw, symbol, horizon, now, *, trading_dates=None):
    stock_info(raw, symbol)
    listing = next(r for r in raw if isinstance(r, dict) and r.get('symbol') == symbol).get('listDate')
    if listing is None:
        return
    try:
        listed = date.fromisoformat(listing)
        if listed.isoformat() != listing:
            raise ValueError()
    except (TypeError, ValueError):
        raise invalid_data('상장일 형식을 확인할 수 없습니다.') from None
    from .market import market_zone
    today = now.astimezone(market_zone('KRW')).date()
    if listed > today:
        raise invalid_data('상장일이 현재 거래일보다 미래입니다.')
    needed = 20 if horizon == 'day' else 65
    required_dates = None
    if isinstance(trading_dates, (list, tuple)) and len(trading_dates) == needed:
        try:
            parsed_dates = [date.fromisoformat(value) for value in trading_dates]
            if all(parsed.isoformat() == value for parsed, value in zip(parsed_dates, trading_dates)) and \
                    parsed_dates == sorted(set(parsed_dates)):
                required_dates = list(trading_dates)
        except (TypeError, ValueError):
            required_dates = None
    # A verified common trading-date reference is stronger than a calendar-day
    # upper bound: it provides the exact completed sessions that the strategy
    # must consume. Do not infer a short trading history from sparse candles.
    if required_dates is not None:
        maximum = sum(value >= listing for value in required_dates)
        if maximum < needed:
            error = NoMatch('listing-history-too-short', '상장 초기로 완료 일봉 %d개 요건을 충족할 수 없습니다.' % needed)
            error.details = {'listDate': listing, 'requiredDailyBars': needed,
                             'maxPossibleDailyBars': maximum,
                             'tradingDateBasis': 'verified-common-calendar',
                             'expectedTradingDates': list(required_dates)}
            raise error
        return
    # Calendar days are an upper bound on completed trading days when no
    # verified session chain was supplied (legacy standalone behavior).
    maximum = (today - listed).days
    if maximum < needed:
        error = NoMatch('listing-history-too-short', '상장 초기로 완료 일봉 %d개 요건을 충족할 수 없습니다.' % needed)
        error.details = {'listDate': listing, 'requiredDailyBars': needed,
                         'maxPossibleDailyBars': maximum}
        raise error


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
    """Audit ledger for recommend reads with PR0 timing/group observations.

    Each record preserves the legacy ``retrievedAt`` (== ``receivedAt``) and
    adds ``requestStartedAt``, ``endpointGroup``, ``attemptCount``,
    ``rateLimitWaitSeconds``, ``retryWaitSeconds``, ``elapsedSeconds``,
    ``success`` and a safe ``errorCode`` (None on success). Failures are also
    recorded with a normalized code; raw messages, headers, bodies, tokens
    and secrets are never stored. ``phase`` labels the pipeline stage
    (ranking/prefetch/screen/final/...) to separate prefetch from
    screening/final counts.
    """

    def __init__(self, client, now=utcnow, clock=None):
        import time as _time
        self.client, self.now = client, now
        self._clock = clock if clock is not None else _time.monotonic
        self.records = []
        self.calendar_cache = {}
        self.phase = None

    def _transport_marks(self):
        inner = getattr(self.client, 'transport', None)
        observations = getattr(inner, 'observations', None)
        if isinstance(observations, list):
            return inner, len(observations)
        return None, 0

    def get(self, path, **params):
        key = (path, tuple(sorted(params.items())))
        if 'market-calendar' in path and key in self.calendar_cache:
            return self.calendar_cache[key]
        phase = self.phase
        group = endpoint_group(path)
        start_wall = self.now()
        start_mono = self._clock()
        transport, mark = self._transport_marks()
        try:
            value = self.client.get(path, **params)
        except TmonError as error:
            self.records.append(self._build(path, params, phase, group, start_wall,
                                            start_mono, transport, mark, False,
                                            getattr(error, 'code', None), None))
            raise
        except KeyboardInterrupt:
            self.records.append(self._build(path, params, phase, group, start_wall,
                                            start_mono, transport, mark, False, 'interrupted', None))
            raise
        except Exception:
            self.records.append(self._build(path, params, phase, group, start_wall,
                                            start_mono, transport, mark, False, 'internal-error', None))
            raise
        record = self._build(path, params, phase, group, start_wall,
                             start_mono, transport, mark, True, None, value)
        self.records.append(record)
        if 'market-calendar' in path:
            self.calendar_cache[key] = value
        return value

    def _build(self, path, params, phase, group, start_wall, start_mono,
               transport, mark, success, error_code, value):
        end_wall = self.now()
        end_mono = self._clock()
        if transport is None:
            # Test doubles perform one logical call with no transport underneath.
            attempts, rate_wait, retry_wait = 1, 0.0, 0.0
            nested = []
        else:
            # Attribute only observations for this path. AUTH token POSTs issued
            # inside TossClient.get stay separate in Transport.observations and
            # are never folded into the GET record. Zero is preserved when no
            # send happened (e.g. endpoint rejected before any attempt), so one
            # record always equals one logical request while attemptCount holds
            # actual transport attempts.
            fresh = transport.observations[mark:]
            matching = [o for o in fresh if o.get('path') == path]
            attempts = sum(o.get('attemptCount', 0) for o in matching)
            rate_wait = round(sum(o.get('rateLimitWaitSeconds', 0.0) for o in matching), 3)
            retry_wait = round(sum(o.get('retryWaitSeconds', 0.0) for o in matching), 3)
            # Persist ALL actual transport work for this call (including AUTH)
            # as a whitelist deep-copy, so inputs.json keeps the full ledger
            # without headers, bodies, tokens or auth params.
            nested = [whitelisted_observation(o) for o in fresh]
        elapsed = round(max(0.0, end_mono - start_mono), 3)
        record = {'path': path, 'params': dict(params), 'phase': phase,
                  'requestStartedAt': start_wall.isoformat(),
                  'receivedAt': end_wall.isoformat(),
                  'retrievedAt': end_wall.isoformat(),
                  'endpointGroup': group, 'attemptCount': attempts,
                  'rateLimitWaitSeconds': rate_wait, 'retryWaitSeconds': retry_wait,
                  'elapsedSeconds': elapsed, 'success': success,
                  'errorCode': safe_code(error_code) if not success else None,
                  'transportRequests': nested}
        if success:
            try:
                record['result'] = copy.deepcopy(value)
            except Exception:
                record['result'] = value
        return record


def daily_data(client, symbol, today, now, *, horizon=None, reference=None,
               check_budget=None):
    """Load the strategy's exact daily window.

    Calls without ``horizon`` retain the legacy 120-bar/newest-date behavior.
    An explicit horizon is the snapshot path: it requires a verified common
    trading-date reference and validates every returned date against it.
    """
    if horizon is None:
        history_kwargs = {'now': now}
        if check_budget is not None:
            history_kwargs['check_budget'] = check_budget
        rows, meta, _, _ = history(client, symbol, 120, **history_kwargs)
        if rows[-1]['date'] != today['previousBusinessDay']['date']:
            raise TmonError('stale-daily', '직전 완료 거래일의 일봉이 없습니다.')
        return rows

    required = 20 if horizon == 'day' else 65 if horizon == 'swing' else None
    if required is None:
        error = TmonError('invalid-horizon', '일봉 horizon을 확인할 수 없습니다.')
        error.details = {'horizon': horizon}
        raise error

    # Do not make a provider call when the shared calendar basis is absent.
    reference_check = validate_daily_dates([], reference, required)
    if reference_check.get('reason') == DAILY_CALENDAR_UNVERIFIED:
        error = TmonError(DAILY_CALENDAR_UNVERIFIED,
                          '공통 거래일 기준을 확인할 수 없어 일봉을 평가할 수 없습니다.')
        error.details = {'horizon': horizon, 'requiredDailyBars': required,
                         'dailyValidation': reference_check,
                         'reason': DAILY_CALENDAR_UNVERIFIED}
        raise error

    history_kwargs = {'now': now}
    if check_budget is not None:
        history_kwargs['check_budget'] = check_budget
    rows, meta, _, _ = history(client, symbol, required, **history_kwargs)
    validation = validate_daily_dates(rows, reference, required)
    if validation.get('status') != 'verified':
        reason = validation.get('reason') or DAILY_GAP
        error = TmonError(reason, '필수 일봉 거래일 창을 확인할 수 없습니다.')
        error.details = {'horizon': horizon, 'requiredDailyBars': required,
                         'dailyValidation': validation, 'reason': reason}
        for field in ('expectedDates', 'dates', 'missingDates',
                      'unexpectedDates', 'duplicateDates'):
            if field in validation:
                error.details[field] = validation[field]
        raise error
    return rows


def minute_data(client, symbol, session, now, settings, warnings=None, *,
                phase_started_at=None, signal_window_end_at=None):
    """Day 1-minute bars with PR1 phase semantics.

    - Future-boundary validation uses the actual response ``receivedAt``.
    - Completion uses the actual ``phaseStartedAt`` (barEnd + D <= phase).
    - Clipping uses the frozen ``signalWindowEndAt`` F (barEnd <= F).
    - Source freshness is checked on the latest usable bar BEFORE clipping
      to the frozen window, using the actual receive instant.
    - The required latest F 5-minute bar must be present; never fall back
      to an older complete block.

    ``now`` is the legacy evaluation instant and supplies defaults so
    existing callers without phase arguments keep working.
    """
    raw = client.get('/api/v1/candles', symbol=symbol, interval='1m', count=125, adjusted='true')
    retrieved_at = client.records[-1]['retrievedAt'] if isinstance(client, RecordingClient) else None
    try:
        received_at = timestamp(retrieved_at) if retrieved_at else now
    except Exception:
        received_at = now
    phase = phase_started_at if phase_started_at is not None else now
    window_end = signal_window_end_at
    diagnostics = []
    try:
        # Split source availability from the calculation set. Source
        # completion uses the actual receipt instant (barEnd + D <=
        # receivedAt) and freshness is checked against receivedAt, so a
        # normally arrived source that already contains the next completed
        # bar is not judged stale merely because the frozen phase P is
        # older. The calculation set is completed at P and clipped to F.
        source_usable = completed_minutes(raw, now, session['start'], settings['barCompletionDelaySeconds'],
                                          diagnostics, received_at=received_at,
                                          phase_started_at=received_at, window_end=None)
    except TmonError as error:
        error.details = {**getattr(error, 'details', {}), 'evaluatedAt': phase.isoformat(),
                         'receivedAt': received_at.isoformat() if hasattr(received_at, 'isoformat') else received_at,
                         'retrievedAt': retrieved_at}
        raise
    finally:
        if warnings is not None:
            warnings.extend({**item, 'symbol': symbol, 'retrievedAt': retrieved_at} for item in diagnostics)
    if not source_usable:
        raise TmonError('insufficient-minute-bars', '완료 분봉이 없습니다.')
    # Freshness on the latest source-usable bar before clipping.
    fresh((timestamp(source_usable[-1]['timestamp']) + timedelta(minutes=1)).isoformat(),
          received_at, settings['minuteSeconds'])
    # Calculation set: same helper (single pipeline), completed at P, clipped to F.
    try:
        usable = completed_minutes(raw, now, session['start'], settings['barCompletionDelaySeconds'],
                                   None, received_at=received_at,
                                   phase_started_at=phase, window_end=None)
    except TmonError as error:
        error.details = {**getattr(error, 'details', {}), 'evaluatedAt': phase.isoformat(),
                         'receivedAt': received_at.isoformat() if hasattr(received_at, 'isoformat') else received_at,
                         'retrievedAt': retrieved_at}
        raise
    if not usable:
        raise TmonError('insufficient-minute-bars', '완료 분봉이 없습니다.')
    if window_end is not None:
        rows = [r for r in usable if timestamp(r['timestamp']) + timedelta(minutes=1) <= window_end]
    else:
        rows = usable
    if not rows:
        raise TmonError('insufficient-minute-bars', '완료 분봉이 없습니다.')
    five = five_minutes(rows, session['start'])
    if window_end is not None:
        # Required latest F bar must be present; no fallback to older blocks.
        if not five or timestamp(five[-1]['timestamp']) + timedelta(minutes=5) != window_end:
            error = TmonError('incomplete-bars', '최신 완료 5분봉이 없습니다.')
            error.details = {'evaluatedAt': phase.isoformat(),
                             'receivedAt': received_at.isoformat() if hasattr(received_at, 'isoformat') else received_at,
                             'retrievedAt': retrieved_at,
                             'signalWindowEndAt': window_end.isoformat()}
            raise error
    return rows, five


def _received_at(client, path, fallback):
    """Return the receipt time for the most recent logical endpoint call."""
    try:
        if isinstance(client, RecordingClient):
            for record in reversed(client.records):
                if record.get('path') == path and record.get('success'):
                    return timestamp(record['receivedAt'])
    except (TmonError, KeyError, TypeError, ValueError):
        pass
    return fallback


def current_data(client, symbol, settings, now=utcnow, preloaded_stock=None,
                 preloaded_stock_checked_at=None, return_observations=False):
    # PR0: screening may reuse the eligibility /stocks response to remove one
    # duplicate STOCK call per candidate. Final verification must pass
    # preloaded_stock=None so stock and warnings are always refreshed.
    if preloaded_stock is None:
        stock = stock_info(client.get('/api/v1/stocks', symbols=symbol), symbol)
        stock_checked_at = _received_at(client, '/api/v1/stocks', now())
    else:
        stock = stock_info(preloaded_stock, symbol)
        stock_checked_at = preloaded_stock_checked_at or now()
    warning_path = '/api/v1/stocks/' + symbol + '/warnings'
    warnings_ok(client.get(warning_path))
    warnings_checked_at = _received_at(client, warning_path, now())
    # stateAsOf historically meant the point after the stock/warning state
    # check. Preserve that meaning while retaining both component times.
    state_time = warnings_checked_at
    prices, _, _, _ = quote(client, [symbol])
    price = prices[0]
    quote_checked_at = _received_at(client, '/api/v1/prices', now())
    if price['currency'] != 'KRW':
        raise invalid_data()
    fresh(price['timestamp'], quote_checked_at, settings['quoteSeconds'])
    book_path = '/api/v1/orderbook'
    book_raw = client.get(book_path, symbol=symbol)
    book_checked_at = _received_at(client, book_path, now())
    book = orderbook(book_raw, book_checked_at, settings['quoteSeconds'])
    limits_path = '/api/v1/price-limits'
    limits = client.get(limits_path, symbol=symbol)
    limits_checked_at = _received_at(client, limits_path, now())
    if not isinstance(limits, dict) or limits.get('currency') != 'KRW':
        raise invalid_data()
    upper = decimal(limits['upperLimitPrice']) if limits.get('upperLimitPrice') is not None else None
    # The timestamp of price limits may legitimately be a start-of-session timestamp.
    from .market import market_zone
    if (not limits.get('timestamp') or
        timestamp(limits['timestamp']).astimezone(market_zone('KRW')).date() != limits_checked_at.astimezone(market_zone('KRW')).date() or
        timestamp(limits['timestamp']) > limits_checked_at + timedelta(seconds=5)):
        raise invalid_data('당일 상하한가를 확인할 수 없습니다.')
    if not return_observations:
        return stock, price, book, upper, state_time
    observations = {'stockCheckedAt': stock_checked_at,
                    'warningsCheckedAt': warnings_checked_at,
                    'quoteCheckedAt': quote_checked_at,
                    'orderbookCheckedAt': book_checked_at,
                    'limitsCheckedAt': limits_checked_at,
                    'stateAsOf': state_time}
    return stock, price, book, upper, state_time, observations


INDEX_COMPLETION_POLICY_VERSION = 'v1'
# Actual provider index price basis: provider-supplied index points with no
# currency and no adjusted option (MarketIndicatorCandle schema). The
# open-to-close / close-to-close formulas are return-calculation methods,
# not price adjustment or index basis, and are recorded separately.
INDEX_PRICE_BASIS = 'provider-index-points'
INDEX_PRICE_BASIS_DAY = INDEX_PRICE_BASIS
INDEX_PRICE_BASIS_SWING = INDEX_PRICE_BASIS
INDEX_RETURN_CALCULATION_DAY = 'first-open-to-last-close'
INDEX_RETURN_CALCULATION_SWING = 'close-to-close'
# Stock leg uses provider adjusted candles (adjusted=true).
STOCK_PRICE_BASIS = 'adjusted'


def normalize_index_candle(item):
    """Validate the real MarketIndicatorCandle schema (no currency required).

    Real schema: timestamp (bar start) + openPrice/highPrice/lowPrice/
    closePrice/volume with no currency and no adjusted option. An optional
    currency, when present (older fixtures), must be KRW and is ignored;
    the stock normalize_candle currency requirement is never applied here.
    """
    from .market import market_zone
    try:
        if not isinstance(item, dict):
            raise ValueError()
        instant = timestamp(item.get('timestamp'))
        if 'currency' in item and item['currency'] is not None:
            if item['currency'] != 'KRW':
                raise invalid_data('지수 봉의 통화가 유효하지 않습니다.')
        row = {'timestamp': instant.isoformat(),
               'date': instant.astimezone(market_zone('KRW')).date().isoformat()}
        for field in ('openPrice', 'highPrice', 'lowPrice', 'closePrice', 'volume'):
            row[field] = decimal(item[field])
        if not (row['lowPrice'] <= min(row['openPrice'], row['closePrice']) <=
                max(row['openPrice'], row['closePrice']) <= row['highPrice']):
            raise invalid_data('지수 봉 OHLC 범위가 일관되지 않습니다.')
        return instant, row
    except TmonError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise invalid_data('지수 봉 형식을 확인할 수 없습니다.') from None


def completed_index_minutes(raw, received_at, phase_started_at, session_start,
                            delay, window_start, window_end, warnings=None):
    """Shared time filtering for index 1m bars via intraday.filter_complete_bars.

    Same PR1 future-before-OHLC/completion/clip behavior as the stock leg;
    index normalization validates its actual no-currency schema (never the
    stock currency rule). Exact endpoint coverage is checked by the caller.
    """
    from .intraday import filter_complete_bars
    if not isinstance(raw, dict) or not isinstance(raw.get('candles'), list):
        raise invalid_data('지수 봉 응답 형식을 확인할 수 없습니다.')
    try:
        return filter_complete_bars(
            raw, received_at, phase_started_at, delay, normalize_index_candle,
            session_start=session_start, window_start=window_start,
            window_end=window_end, warnings=warnings,
            future_message='다음 분 경계보다 먼 미래 지수 봉입니다.',
            skip_message='다음 분 경계의 미래 지수 봉을 계산에서 제외했습니다.')
    except TmonError as error:
        if error.code == 'invalid-data' and '분봉 시작 시각' in (error.message or ''):
            raise invalid_data('지수 봉 시작 시각을 확인할 수 없습니다.') from None
        raise


def normalize_index_daily(raw):
    """Validate index daily candles into date->row (no currency required)."""
    if not isinstance(raw, dict) or not isinstance(raw.get('candles'), list):
        raise invalid_data('지수 일봉 응답 형식을 확인할 수 없습니다.')
    mapped = {}
    for item in raw['candles']:
        _, row = normalize_index_candle(item)
        d = row['date']
        if d in mapped and mapped[d] != row:
            raise invalid_data('중복 지수 일봉 값이 다릅니다.')
        # Same trading date with different bar starts is also a conflict.
        if d in mapped and mapped[d]['timestamp'] != row['timestamp']:
            raise invalid_data('하나의 거래일에 서로 다른 지수 일봉 시각이 있습니다.')
        mapped[d] = row
    return mapped


def _return_calculation(horizon):
    return INDEX_RETURN_CALCULATION_DAY if horizon == 'day' else INDEX_RETURN_CALCULATION_SWING


def window_basis_fields(horizon):
    """Explicit comparison basis: provider index points + adjusted stock leg.

    ``priceBasis`` is the actual provider index price basis (never the
    return formula); ``returnCalculation`` records the formula
    (first-open-to-last-close / close-to-close); ``stockPriceBasis``
    records the adjusted stock leg. No volume-adjustment verification is
    claimed (PR4-B pending).
    """
    return {'priceBasis': INDEX_PRICE_BASIS,
            'stockPriceBasis': STOCK_PRICE_BASIS,
            'returnCalculation': _return_calculation(horizon),
            'completionPolicyVersion': INDEX_COMPLETION_POLICY_VERSION}


def index_cache_key(market, interval, comparison_start_iso, comparison_end_iso,
                    price_basis, completion_policy_version,
                    return_calculation=None, trading_dates=None):
    # Full comparison key includes the return-calculation method alongside
    # the provider price basis. Older 6-arg callers default to the
    # interval-implied method for compatibility.
    if return_calculation is None:
        return_calculation = (INDEX_RETURN_CALCULATION_DAY if interval == '1m'
                              else INDEX_RETURN_CALCULATION_SWING)
    key = (market, interval, comparison_start_iso, comparison_end_iso,
           price_basis, completion_policy_version, return_calculation)
    if trading_dates is not None:
        # Preserve the old key for calls without a calendar basis.  Once an
        # explicit basis is supplied, two different date lists must never
        # share a frozen index snapshot merely because endpoints match.
        key += ('trading-dates', tuple(trading_dates))
    return key


def _recording_received_at(client, fallback):
    try:
        if isinstance(client, RecordingClient) and client.records:
            return timestamp(client.records[-1]['retrievedAt'])
    except Exception:
        pass
    return fallback


def _is_auth_error(error):
    return isinstance(error, TmonError) and getattr(error, 'exit_code', None) == 3


def _index_window_covered(combined, horizon, comparison_start, comparison_end,
                          trading_dates=None):
    """True once the required window endpoints are present in fetched pages."""
    try:
        if horizon == 'day' and comparison_start is not None and comparison_end is not None:
            have = {timestamp(c['timestamp']) for c in combined
                    if isinstance(c, dict) and c.get('timestamp')}
            return comparison_start in have and \
                (comparison_end - timedelta(minutes=1)) in have
        if horizon == 'swing' and comparison_start is not None and comparison_end is not None:
            have_dates = {timestamp(c['timestamp']).astimezone(market_zone('KRW')).date().isoformat()
                          for c in combined if isinstance(c, dict) and c.get('timestamp')}
            if trading_dates is not None:
                return set(trading_dates).issubset(have_dates)
            return comparison_start in have_dates and comparison_end in have_dates
    except (TmonError, TypeError, AttributeError, ValueError):
        return False
    return False


def _fetch_index_pages(client, market, interval, comparison_start=None,
                       comparison_end=None, horizon=None, budget_remaining=None,
                       trading_dates=None):
    """Fetch only the required index range using supported before/nextBefore.

    One page of count<=200 covers the 30-minute day window and the
    5-trade-day swing endpoints; further pages are fetched only while the
    required window stays uncovered (at most 3 pages total). The monotonic
    budget is checked before EACH request; exhaustion stops pagination so
    no unneeded pages or zero-budget probes are issued. A missing
    nextBefore ends pagination as a single terminal page. Auth errors
    propagate; other failures raise. Returns the combined raw payload.
    """
    path = '/api/v1/market-indicators/' + market + '/candles'
    combined, cursor = [], None

    def _budget_ok():
        if budget_remaining is None:
            return True
        try:
            return bool(budget_remaining())
        except TmonError:
            return False

    for _ in range(3):
        if not _budget_ok():
            break
        params = {'interval': interval, 'count': 200}
        if cursor is not None:
            params['before'] = cursor
        raw = client.get(path, **params)
        if not isinstance(raw, dict) or not isinstance(raw.get('candles'), list):
            raise invalid_data('지수 봉 응답 형식을 확인할 수 없습니다.')
        combined.extend(raw['candles'])
        if _index_window_covered(combined, horizon, comparison_start, comparison_end,
                                 trading_dates):
            break
        nxt = raw.get('nextBefore', None)
        if nxt is None:
            break
        try:
            next_time = timestamp(nxt)
        except Exception:
            raise invalid_data('지수 페이지 경계를 확인할 수 없습니다.') from None
        if cursor is not None and next_time >= timestamp(cursor):
            break
        cursor = nxt
    return {'candles': combined}


def _canonical_swing_dates(trading_dates, comparison_start, comparison_end):
    """Validate an explicit six-date reference without inferring weekdays."""
    if not isinstance(trading_dates, (list, tuple)) or len(trading_dates) != 6:
        return None
    normalized = []
    for value in trading_dates:
        if not isinstance(value, str):
            return None
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        if parsed.isoformat() != value:
            return None
        normalized.append(value)
    if normalized != sorted(set(normalized)):
        return None
    start_date = comparison_start if isinstance(comparison_start, str) else None
    end_date = comparison_end if isinstance(comparison_end, str) else None
    try:
        if start_date is None:
            start_date = comparison_start.isoformat()
        if end_date is None:
            end_date = comparison_end.isoformat()
    except AttributeError:
        return None
    if normalized[0] != start_date or normalized[-1] != end_date:
        return None
    return normalized


def _swing_date_audit(expected, observed):
    if expected is None:
        return {}
    observed = sorted(observed)
    return {'expectedTradingDates': list(expected),
            'observedTradingDates': observed,
            'observedSpanDates': [d for d in observed
                                  if expected[0] <= d <= expected[-1]]}


def ensure_frozen_index(client, market, horizon, comparison_start, comparison_end,
                        session_start=None, phase_started_at=None, delay=5,
                        now=None, budget_remaining=None, cache=None, *,
                        trading_dates=None):
    """Per-market frozen index snapshot with max one retry (PR2).

    Fetched once per screening phase BEFORE candidates; success and
    unavailable results alike are cached under the full key. A missing
    required range or a failed fetch is retried at most once within the
    existing monotonic budget. Authentication errors propagate and are
    never recorded as unavailable relative return.
    Returns a frozen dict with status ok/unavailable, reason, values and key.
    """
    interval = '1m' if horizon == 'day' else '1d'
    price_basis = INDEX_PRICE_BASIS
    return_calc = _return_calculation(horizon)
    basis = window_basis_fields(horizon)
    start_iso = comparison_start.isoformat() if hasattr(comparison_start, 'isoformat') else str(comparison_start)
    end_iso = comparison_end.isoformat() if hasattr(comparison_end, 'isoformat') else str(comparison_end)
    expected_trading_dates = None
    if trading_dates is not None:
        if horizon != 'swing':
            return {'status': 'unavailable', 'reason': 'comparison-basis-unverified',
                    'key': None, 'market': market, 'interval': interval,
                    'comparisonStartAt': start_iso, 'comparisonEndAt': end_iso,
                    **basis, 'expectedTradingDates': [],
                    'observedTradingDates': []}
        expected_trading_dates = _canonical_swing_dates(
            trading_dates, comparison_start, comparison_end)
        if expected_trading_dates is None:
            return {'status': 'unavailable', 'reason': 'comparison-basis-unverified',
                    'key': None, 'market': market, 'interval': interval,
                    'comparisonStartAt': start_iso, 'comparisonEndAt': end_iso,
                    **basis, 'expectedTradingDates': [],
                    'observedTradingDates': []}
    key = index_cache_key(market, interval, start_iso, end_iso, price_basis,
                          INDEX_COMPLETION_POLICY_VERSION, return_calc,
                          expected_trading_dates)
    if cache is not None and key in cache:
        return cache[key]
    empty_date_audit = _swing_date_audit(expected_trading_dates, [])
    if phase_started_at is None:
        phase_started_at = now if now is not None else utcnow()
    if now is None:
        now = phase_started_at
    received_fallback = phase_started_at if phase_started_at is not None else now

    def _budget_ok():
        if budget_remaining is None:
            return True
        try:
            return bool(budget_remaining())
        except TmonError:
            return False

    def _attempt():
        combined = _fetch_index_pages(client, market, interval, comparison_start,
                                      comparison_end, horizon, budget_remaining,
                                      expected_trading_dates)
        received_at = _recording_received_at(client, received_fallback if received_fallback is not None else utcnow())
        if horizon == 'day':
            bars = completed_index_minutes(combined, received_at, phase_started_at,
                                           session_start, delay, comparison_start,
                                           comparison_end)
            # Exact endpoint + full 30-bar contiguity check.
            if len(bars) != 30 or timestamp(bars[0]['timestamp']) != comparison_start or \
                    timestamp(bars[-1]['timestamp']) + timedelta(minutes=1) != comparison_end:
                error = TmonError('index-bar-missing', '지수 비교 구간의 봉이 부족합니다.')
                error.details = {'market': market, 'comparisonStartAt': start_iso,
                                 'comparisonEndAt': end_iso, 'indexBars': len(bars)}
                raise error
            from .intraday import require_contiguous
            try:
                require_contiguous(bars, 1)
            except TmonError as error:
                missing = TmonError('index-bar-missing', '지수 비교 구간에 봉 누락이 있습니다.')
                missing.details = {'market': market, 'comparisonStartAt': start_iso,
                                   'comparisonEndAt': end_iso}
                raise missing from None
            return {'status': 'ok', 'reason': None, 'key': key, 'market': market,
                    'interval': interval, 'comparisonStartAt': start_iso,
                    'comparisonEndAt': end_iso, **basis,
                    'startValue': bars[0]['openPrice'], 'endValue': bars[-1]['closePrice'],
                    'barCount': 30}
        # Swing: endpoint closes on common calendar dates. When a canonical
        # six-date reference is supplied, every date in its span is required;
        # index candles are never used to infer the reference calendar.
        mapped = normalize_index_daily(combined)
        date_audit = _swing_date_audit(expected_trading_dates, mapped)
        # Exclude not-yet-completed future daily bars beyond the signal date.
        end_date = end_iso
        start_date = start_iso
        if expected_trading_dates is not None:
            observed_span = set(date_audit['observedSpanDates'])
            expected_set = set(expected_trading_dates)
            unexpected = sorted(observed_span - expected_set)
            missing = sorted(expected_set - observed_span)
            if unexpected:
                error = TmonError('index-data-invalid',
                                  '지수 비교 거래일 창에 예상하지 않은 날짜가 있습니다.')
                error.details = {**date_audit,
                                 'unexpectedTradingDates': unexpected,
                                 'missingTradingDates': missing}
                raise error
            if missing:
                error = TmonError('index-bar-missing',
                                  '지수 비교 거래일 창에 봉 누락이 있습니다.')
                error.details = {**date_audit,
                                 'unexpectedTradingDates': unexpected,
                                 'missingTradingDates': missing}
                raise error
        for d in [d for d in list(mapped) if d > end_date]:
            del mapped[d]
        if start_date not in mapped or end_date not in mapped:
            error = TmonError('index-bar-missing', '지수 비교 거래일의 종가가 없습니다.')
            error.details = {'market': market, 'comparisonStartAt': start_iso,
                             'comparisonEndAt': end_iso, **date_audit}
            raise error
        return {'status': 'ok', 'reason': None, 'key': key, 'market': market,
                'interval': interval, 'comparisonStartAt': start_iso,
                'comparisonEndAt': end_iso, **basis,
                'startValue': mapped[start_date]['closePrice'],
                'endValue': mapped[end_date]['closePrice'], 'barCount': len(mapped),
                **date_audit}

    attempts = 0
    while attempts < 2:
        try:
            result = _attempt()
            if cache is not None:
                cache[key] = result
            return result
        except TmonError as error:
            if _is_auth_error(error):
                raise
            if error.code in ('invalid-data', 'future-minute-bar', 'index-data-invalid'):
                reason = 'index-data-invalid'
                result = {'status': 'unavailable', 'reason': reason, 'key': key,
                          'market': market, 'interval': interval,
                          'comparisonStartAt': start_iso, 'comparisonEndAt': end_iso,
                          **basis, **empty_date_audit,
                          **getattr(error, 'details', {})}
                if cache is not None:
                    cache[key] = result
                return result
            attempts += 1
            if attempts >= 2 or not _budget_ok():
                reason = 'index-bar-missing' if getattr(error, 'code', '') == 'index-bar-missing' \
                    else 'index-fetch-failed'
                result = {'status': 'unavailable', 'reason': reason, 'key': key,
                          'market': market, 'interval': interval,
                          'comparisonStartAt': start_iso, 'comparisonEndAt': end_iso,
                          **basis, **empty_date_audit,
                          'errorCode': safe_code(getattr(error, 'code', None)),
                          **getattr(error, 'details', {})}
                if cache is not None:
                    cache[key] = result
                return result
            # Exactly one bounded retry within the monotonic budget.
        except (KeyError, TypeError, ValueError) as error:
            attempts += 1
            if attempts >= 2 or not _budget_ok():
                result = {'status': 'unavailable', 'reason': 'index-data-invalid',
                          'key': key, 'market': market, 'interval': interval,
                          'comparisonStartAt': start_iso, 'comparisonEndAt': end_iso,
                          **basis}
                if cache is not None:
                    cache[key] = result
                return result
    result = {'status': 'unavailable', 'reason': 'index-fetch-failed', 'key': key,
                          'market': market, 'interval': interval,
                          'comparisonStartAt': start_iso, 'comparisonEndAt': end_iso,
                          **basis, **empty_date_audit}
    if cache is not None:
        cache[key] = result
    return result


def frozen_relative_for_candidate(horizon, daily, minutes, frozen_window, frozen_index):
    """Pure relative return on the frozen comparison window (PR2).

    Returns (pct_or_None, reason_or_None, window_dict). Reasons:
    index-fetch-failed/index-bar-missing/index-data-invalid (frozen),
    stock-bars-noncontiguous, comparison-basis-unverified. Never filters by
    positivity; None sorts behind known values via sort_key.
    """
    if frozen_window is None or frozen_index is None:
        return None, 'comparison-basis-unverified', relative_return_window(horizon, daily, minutes)
    try:
        if horizon == 'day':
            start_iso, end_iso = frozen_window['comparisonStartAt'], frozen_window['comparisonEndAt']
            cstart, cend = timestamp(start_iso), timestamp(end_iso)
            # Stock leg must cover the exact frozen [F-30m, F) with 30 contiguous bars.
            in_window = [r for r in minutes
                         if timestamp(r['timestamp']) >= cstart and
                         timestamp(r['timestamp']) + timedelta(minutes=1) <= cend]
            window = {'interval': '1m', 'count': 30,
                      'comparisonStartAt': start_iso, 'comparisonEndAt': end_iso,
                      **window_basis_fields('day')}
            if in_window:
                window.update({'start': in_window[0]['timestamp'],
                               'end': in_window[-1]['timestamp'],
                               'startOpen': str(in_window[0]['openPrice']),
                               'endClose': str(in_window[-1]['closePrice'])})
            else:
                legacy = relative_return_window(horizon, daily, minutes)
                if legacy is not None:
                    window.update({k: legacy[k] for k in ('start', 'end', 'startOpen', 'endClose')
                                   if k in legacy})
            if len(in_window) != 30 or timestamp(in_window[0]['timestamp']) != cstart or \
                    timestamp(in_window[-1]['timestamp']) + timedelta(minutes=1) != cend:
                return None, 'stock-bars-noncontiguous', window
            from .intraday import require_contiguous
            try:
                require_contiguous(in_window, 1)
            except TmonError:
                return None, 'stock-bars-noncontiguous', window
            if frozen_index.get('status') != 'ok':
                return None, frozen_index.get('reason') or 'index-fetch-failed', window
            start, end = in_window[0]['openPrice'], in_window[-1]['closePrice']
            istart, iend = frozen_index['startValue'], frozen_index['endValue']
            if start <= 0 or istart <= 0:
                return None, 'comparison-basis-unverified', window
            return ((end / start - 1) - (iend / istart - 1)) * 100, None, window
        # Swing: endpoint closes on the common six-date window.
        start_date, end_date = frozen_window['comparisonStartAt'], frozen_window['comparisonEndAt']
        smap = {r['date']: r for r in daily}
        window = {'interval': '1d', 'count': 5,
                  'comparisonStartAt': start_date, 'comparisonEndAt': end_date,
                  **window_basis_fields('swing')}
        if start_date in smap and end_date in smap:
            window.update({'startDate': start_date, 'endDate': end_date,
                           'startClose': str(smap[start_date]['closePrice']),
                           'endClose': str(smap[end_date]['closePrice'])})
        else:
            legacy = relative_return_window(horizon, daily, minutes)
            if legacy is not None:
                window.update({k: legacy[k] for k in ('startDate', 'endDate', 'startClose', 'endClose')
                               if k in legacy})
            else:
                window.update({'startDate': start_date, 'endDate': end_date})
            # Stock endpoint absent: noncontiguous/gap, not a verified basis.
            if start_date not in smap or end_date not in smap:
                # If the frozen calendar basis itself failed, it would be None
                # already; here the stock leg is missing the required date.
                return None, 'stock-bars-noncontiguous', window
        if frozen_index.get('status') != 'ok':
            return None, frozen_index.get('reason') or 'index-fetch-failed', window
        start, end = smap[start_date]['closePrice'], smap[end_date]['closePrice']
        istart, iend = frozen_index['startValue'], frozen_index['endValue']
        if start <= 0 or istart <= 0:
            return None, 'comparison-basis-unverified', window
        return ((end / start - 1) - (iend / istart - 1)) * 100, None, window
    except (KeyError, TypeError, IndexError, AttributeError):
        return None, 'comparison-basis-unverified', relative_return_window(horizon, daily, minutes)


def relative_return(client, market, horizon, daily, minutes, now, cache):
    """Legacy per-call wrapper kept for compatibility; Engine uses frozen snapshot.

    Derives the attempted frozen window from the stock leg itself, then
    delegates to ensure_frozen_index/frozen_relative_for_candidate so the
    index adapter, key and reasons stay identical. Auth errors propagate.
    """
    try:
        if horizon == 'day':
            bars = minutes[-30:]
            if len(bars) < 30:
                return None
            cstart, cend = timestamp(bars[0]['timestamp']), \
                timestamp(bars[-1]['timestamp']) + timedelta(minutes=1)
            window = {'comparisonStartAt': cstart.isoformat(),
                      'comparisonEndAt': cend.isoformat()}
            frozen = ensure_frozen_index(client, market, horizon, cstart, cend,
                                         session_start=None,
                                         phase_started_at=now, delay=5,
                                         now=now, budget_remaining=None,
                                         cache=cache)
            pct, _, _ = frozen_relative_for_candidate(horizon, daily, minutes, window, frozen)
            return pct
        if len(daily) < 6:
            return None
        window = {'comparisonStartAt': daily[-6]['date'],
                  'comparisonEndAt': daily[-1]['date']}
        frozen = ensure_frozen_index(client, market, horizon,
                                     daily[-6]['date'], daily[-1]['date'],
                                     now=now, budget_remaining=None, cache=cache)
        pct, _, _ = frozen_relative_for_candidate(horizon, daily, minutes, window, frozen)
        return pct
    except TmonError:
        # Auth (exit 3) is re-raised inside ensure_frozen_index; other
        # TmonErrors here mean the stock leg itself is unusable.
        raise
    except (KeyError, TypeError, IndexError, AttributeError, ValueError):
        return None


def _canon(value):
    if isinstance(value, Decimal):
        # Provider formatting differences (100 vs 100.0) are the same
        # numeric input; actual value corrections still change the digest.
        if value == 0:
            return '0'
        return format(value.normalize(), 'f')
    if isinstance(value, dict):
        return {k: _canon(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    return value


def signal_input_hash(symbol, horizon, daily, five, settings=None,
                      price_adjustment_policy='adjusted=true'):
    """Stable SHA-256 over the normalized signal inputs (PR0 identifier).

    Covers symbol, horizon and the breakout-relevant OHLCV tails so input
    corrections or price-basis changes produce a different identifier even
    for the same symbol and bar timestamp.
    """
    if horizon == 'day':
        daily_tail = daily[-20:] if len(daily) >= 20 else list(daily)
        five_tail = five[-6:] if len(five) >= 6 else list(five)
    else:
        daily_tail = daily[-65:] if len(daily) >= 65 else list(daily)
        five_tail = []
    signal_settings = {}
    if isinstance(settings, dict):
        signal_settings = {key: settings[key] for key in
                           ('minEstimatedAvgDailyAmount', 'minVolumeRatio')
                           if key in settings}
    payload = {'symbol': symbol, 'horizon': horizon, 'strategyVersion': 'breakout-v1',
               'priceAdjustmentPolicy': price_adjustment_policy,
               'signalSettings': _canon(signal_settings),
               'daily': _canon(daily_tail), 'five': _canon(five_tail)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def benchmark_snapshot_hash(snapshot):
    """Stable identity for frozen benchmark values, excluding observations."""
    if not isinstance(snapshot, dict):
        return None
    payload = {key: snapshot.get(key) for key in
               ('market', 'interval', 'comparisonStartAt', 'comparisonEndAt',
                'expectedTradingDates', 'startValue', 'endValue',
                'priceBasis', 'stockPriceBasis', 'returnCalculation',
                'completionPolicyVersion')}
    if payload.get('startValue') is None or payload.get('endValue') is None:
        return None
    return hashlib.sha256(json.dumps(_canon(payload), sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def relative_return_window(horizon, daily, minutes):
    """Original relative-comparison window for the screened snapshot (PR0).

    Returned even when the index leg is unavailable so the attempted
    comparison range stays auditable. None only when the stock leg itself
    is too short to define a window.

    PR2 addition: explicit comparisonStartAt/comparisonEndAt. Legacy
    start/end (day: bar starts; swing: dates) meanings are preserved;
    comparisonEndAt for day is the exclusive minute end (last bar start +1m).
    """
    try:
        if horizon == 'day':
            bars = minutes[-30:]
            if len(bars) < 30:
                return None
            end_exclusive = (timestamp(bars[-1]['timestamp']) + timedelta(minutes=1)).isoformat()
            return {'interval': '1m', 'count': 30,
                    'start': bars[0]['timestamp'], 'end': bars[-1]['timestamp'],
                    'startOpen': str(bars[0]['openPrice']),
                    'endClose': str(bars[-1]['closePrice']),
                    'comparisonStartAt': bars[0]['timestamp'],
                    'comparisonEndAt': end_exclusive,
                    **window_basis_fields('day')}
        if len(daily) < 6:
            return None
        return {'interval': '1d', 'count': 5,
                'startDate': daily[-6]['date'], 'endDate': daily[-1]['date'],
                'startClose': str(daily[-6]['closePrice']),
                'endClose': str(daily[-1]['closePrice']),
                'comparisonStartAt': daily[-6]['date'],
                'comparisonEndAt': daily[-1]['date'],
                **window_basis_fields('swing')}
    except (KeyError, TypeError, IndexError):
        return None


def request_counts(records):
    """PR0 aggregates: logical calls plus actual transport attempts.

    byEndpoint/byGroup/byPhase count logical GET calls (one record each, kept
    for compatibility). actualAttemptsByEndpoint/actualAttemptsByGroup sum real
    transport attempts from the nested per-call ledgers, including AUTH work
    that never appears as a logical record.
    """
    from collections import Counter
    by_endpoint, by_group, by_phase = Counter(), Counter(), Counter()
    actual_endpoint, actual_group = Counter(), Counter()
    for record in records:
        by_endpoint[record.get('path')] += 1
        by_group[record.get('endpointGroup')] += 1
        by_phase[record.get('phase')] += 1
        for nested in record.get('transportRequests', []):
            actual_endpoint[nested.get('path')] += nested.get('attemptCount', 0)
            actual_group[nested.get('endpointGroup')] += nested.get('attemptCount', 0)
    return {'byEndpoint': dict(by_endpoint), 'byGroup': dict(by_group),
            'byPhase': dict(by_phase),
            'actualAttemptsByEndpoint': dict(actual_endpoint),
            'actualAttemptsByGroup': dict(actual_group)}


# Transport observations never hold headers, bodies, tokens or auth params,
# but the whitelist below pins exactly which fields may persist anyway.
_TRANSPORT_OBSERVATION_FIELDS = ('requestStartedAt', 'receivedAt', 'endpointGroup',
                                 'path', 'attemptCount', 'rateLimitWaitSeconds',
                                 'retryWaitSeconds', 'elapsedSeconds', 'success',
                                 'errorCode')


def whitelisted_observation(observation):
    """Independent safe copy of one transport observation for persistence."""
    try:
        return {field: copy.deepcopy(observation.get(field))
                for field in _TRANSPORT_OBSERVATION_FIELDS}
    except Exception:
        return {field: observation.get(field) for field in _TRANSPORT_OBSERVATION_FIELDS}
