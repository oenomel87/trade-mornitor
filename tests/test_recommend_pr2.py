"""PR2: frozen per-market completed index snapshot, fixed comparison window."""
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from tmon.cli import envelope
from tmon.errors import TmonError
from tmon.intraday import completed_minutes, signal_window_end
from tmon.market import timestamp
from tmon.recommend import Engine
from tmon.recommend_config import load_config
from tmon.recommend_data import (
    RecordingClient, completed_index_minutes, ensure_frozen_index,
    frozen_relative_for_candidate, normalize_index_candle,
    INDEX_COMPLETION_POLICY_VERSION, INDEX_PRICE_BASIS_DAY,
    INDEX_RETURN_CALCULATION_DAY, INDEX_RETURN_CALCULATION_SWING,
    index_cache_key,
)
from tmon.recommend_daily import common_trading_dates
from tmon.recommend_store import Store

from tests.test_recommend import FakeClient, NOW, START, cal, candle, days, minutes, stock, book

KST = timezone(timedelta(hours=9))
F = datetime(2026, 9, 4, 10, 0, 0, tzinfo=KST)
P = datetime(2026, 9, 4, 10, 0, 10, tzinfo=KST)


def index_bar(t, open_px='1000', close_px='1001'):
    # Real MarketIndicatorCandle schema: no currency, bar-start timestamp.
    from decimal import Decimal as _D
    hi = str(max(_D(open_px), _D(close_px)) + _D('5'))
    lo = str(min(_D(open_px), _D(close_px)) - _D('5'))
    return {'timestamp': t.isoformat(), 'openPrice': open_px, 'highPrice': hi,
            'lowPrice': lo, 'closePrice': close_px, 'volume': '100000'}


def valid_index_30():
    # Covers exactly [F-30m, F) = [09:30, 10:00): 1% index move.
    bars = []
    for i in range(30, 60):
        t = START + timedelta(minutes=i)
        close = '1010' if i == 59 else '1000'
        open_px = '1000'
        bars.append(index_bar(t, open_px, close))
    return bars


class AdapterTests(unittest.TestCase):
    def test_no_currency_validated_not_stock_normalize(self):
        instant, row = normalize_index_candle(index_bar(START))
        self.assertEqual(instant, START)
        self.assertNotIn('currency', row)
        # Optional KRW currency tolerated (older fixtures), wrong rejected.
        instant2, _ = normalize_index_candle({**index_bar(START), 'currency': 'KRW'})
        self.assertEqual(instant2, START)
        with self.assertRaises(TmonError):
            normalize_index_candle({**index_bar(START), 'currency': 'USD'})
        with self.assertRaises(TmonError):
            normalize_index_candle({**index_bar(START), 'highPrice': '1'})

    def test_key_includes_full_comparison(self):
        key = index_cache_key('KOSPI', '1m', 'a', 'b', INDEX_PRICE_BASIS_DAY,
                              INDEX_COMPLETION_POLICY_VERSION)
        self.assertEqual(key, ('KOSPI', '1m', 'a', 'b', INDEX_PRICE_BASIS_DAY,
                               'v1', INDEX_RETURN_CALCULATION_DAY))
        swing_key = index_cache_key('KOSPI', '1d', 'a', 'b', INDEX_PRICE_BASIS_DAY,
                                    INDEX_COMPLETION_POLICY_VERSION,
                                    INDEX_RETURN_CALCULATION_SWING)
        self.assertEqual(swing_key[-1], INDEX_RETURN_CALCULATION_SWING)

    def test_inprogress_excluded_and_exact_endpoints(self):
        bars = valid_index_30()
        inprog = index_bar(F, '1000', '9999')  # 10:00 start: incomplete at P
        raw = {'candles': bars + [inprog]}
        rows = completed_index_minutes(raw, P, P, START, 5, F - timedelta(minutes=30), F)
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[-1]['closePrice'], D('1010'))
        # Missing endpoint -> caller maps to bar-missing; duplicates conflict -> invalid.
        short = {'candles': bars[1:]}
        rows2 = completed_index_minutes(short, P, P, START, 5, F - timedelta(minutes=30), F)
        self.assertEqual(len(rows2), 29)
        dup = {'candles': bars + [{**bars[-1], 'closePrice': '9999'}]}
        with self.assertRaises(TmonError):
            completed_index_minutes(dup, P, P, START, 5, F - timedelta(minutes=30), F)

    def test_index_and_stock_share_completion_future_and_clip_rules(self):
        """Both legs use the same time filter while keeping schemas separate."""
        index_rows = [index_bar(START + timedelta(minutes=i)) for i in range(60)]
        stock_rows = minutes()
        late_phase = datetime(2026, 9, 4, 11, 5, 10, tzinfo=KST)
        for delay in (1, 5, 60):
            stock = completed_minutes({'candles': stock_rows}, P, START, delay,
                                      received_at=P, phase_started_at=late_phase)
            index = completed_index_minutes({'candles': index_rows}, P, late_phase,
                                            START, delay, None, None)
            self.assertEqual([r['timestamp'] for r in stock],
                             [r['timestamp'] for r in index])

            boundary = P.replace(second=0, microsecond=0) + timedelta(minutes=1)
            malformed_stock = candle(boundary)
            malformed_stock.pop('highPrice')
            malformed_index = index_bar(boundary)
            malformed_index.pop('highPrice')
            stock_warnings, index_warnings = [], []
            stock_with_skip = completed_minutes(
                {'candles': stock_rows + [malformed_stock]}, P, START, delay,
                stock_warnings, received_at=P, phase_started_at=late_phase)
            index_with_skip = completed_index_minutes(
                {'candles': index_rows + [malformed_index]}, P, late_phase,
                START, delay, None, None, index_warnings)
            self.assertEqual([r['timestamp'] for r in stock_with_skip],
                             [r['timestamp'] for r in index_with_skip])
            self.assertEqual(stock_warnings[0]['code'], 'future-minute-bar-skipped')
            self.assertEqual(index_warnings[0]['code'], 'future-minute-bar-skipped')

            distant = P.replace(second=0, microsecond=0) + timedelta(minutes=2)
            bad_stock = candle(distant)
            bad_stock.pop('highPrice')
            bad_index = index_bar(distant)
            bad_index.pop('highPrice')
            with self.assertRaises(TmonError) as stock_error:
                completed_minutes({'candles': stock_rows + [bad_stock]}, P, START,
                                   delay, received_at=P, phase_started_at=late_phase)
            with self.assertRaises(TmonError) as index_error:
                completed_index_minutes({'candles': index_rows + [bad_index]}, P,
                                        late_phase, START, delay, None, None)
            self.assertEqual(stock_error.exception.code, 'future-minute-bar')
            self.assertEqual(index_error.exception.code, 'future-minute-bar')


class FrozenSnapshotTests(unittest.TestCase):
    def test_missing_endpoint_bounded_retry(self):
        calls = [0]

        class RetryClient:
            def get(self, path, **params):
                calls[0] += 1
                if calls[0] == 1:
                    return {'candles': valid_index_30()[1:], 'nextBefore': None}
                return {'candles': valid_index_30(), 'nextBefore': None}
        cache = {}
        result = ensure_frozen_index(RetryClient(), 'KOSPI', 'day',
                                     F - timedelta(minutes=30), F,
                                     session_start=START, phase_started_at=P,
                                     delay=5, now=P, budget_remaining=lambda: True,
                                     cache=cache)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(calls[0], 2)
        # Success cached: second call hits cache with no extra fetch.
        result2 = ensure_frozen_index(RetryClient(), 'KOSPI', 'day',
                                      F - timedelta(minutes=30), F,
                                      session_start=START, phase_started_at=P,
                                      delay=5, now=P, budget_remaining=lambda: True,
                                      cache=cache)
        self.assertEqual(result2['startValue'], result['startValue'])

    def test_failed_fetch_bounded_and_unavailable_cached(self):
        calls = [0]

        class FailClient:
            def get(self, path, **params):
                calls[0] += 1
                raise TmonError('network-error', 'down', 4)
        cache = {}
        result = ensure_frozen_index(FailClient(), 'KOSPI', 'day',
                                     F - timedelta(minutes=30), F,
                                     session_start=START, phase_started_at=P,
                                     delay=5, now=P, budget_remaining=lambda: True,
                                     cache=cache)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reason'], 'index-fetch-failed')
        self.assertEqual(calls[0], 2)
        before = calls[0]
        result2 = ensure_frozen_index(FailClient(), 'KOSPI', 'day',
                                      F - timedelta(minutes=30), F,
                                      session_start=START, phase_started_at=P,
                                      delay=5, now=P, budget_remaining=lambda: True,
                                      cache=cache)
        self.assertEqual(result2['reason'], 'index-fetch-failed')
        self.assertEqual(calls[0], before)

    def test_zero_budget_does_not_probe_index(self):
        calls = []

        class ZeroBudgetClient:
            def get(self, path, **params):
                calls.append((path, params))
                raise AssertionError('zero-budget request')

        result = ensure_frozen_index(
            ZeroBudgetClient(), 'KOSPI', 'day', F - timedelta(minutes=30), F,
            session_start=START, phase_started_at=P, delay=5, now=P,
            budget_remaining=lambda: False, cache={})
        self.assertEqual(calls, [])
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reason'], 'index-bar-missing')

    def test_budget_exhaustion_stops_paging_and_retry(self):
        calls, budget = [], [True]

        class ExhaustingClient:
            def get(self, path, **params):
                calls.append((path, params))
                budget[0] = False
                return {'candles': valid_index_30()[1:], 'nextBefore':
                        (F - timedelta(hours=1)).isoformat()}

        result = ensure_frozen_index(
            ExhaustingClient(), 'KOSPI', 'day', F - timedelta(minutes=30), F,
            session_start=START, phase_started_at=P, delay=5, now=P,
            budget_remaining=lambda: budget[0], cache={})
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['reason'], 'index-bar-missing')

    def test_page_two_covers_window_without_page_three(self):
        calls = []
        required = valid_index_30()

        class PagedClient:
            def get(self, path, **params):
                calls.append((path, params))
                if len(calls) == 1:
                    return {'candles': required[:10], 'nextBefore':
                            (F - timedelta(hours=1)).isoformat()}
                return {'candles': required, 'nextBefore':
                        (F - timedelta(hours=2)).isoformat()}

        result = ensure_frozen_index(
            PagedClient(), 'KOSPI', 'day', F - timedelta(minutes=30), F,
            session_start=START, phase_started_at=P, delay=5, now=P,
            budget_remaining=lambda: True, cache={})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(calls), 2)
        self.assertNotIn((F - timedelta(hours=2)).isoformat(),
                         [params.get('before') for _, params in calls])

    def test_unavailable_error_code_is_safe(self):
        class UnsafeErrorClient:
            def get(self, path, **params):
                raise TmonError('provider secret/with spaces', 'do not persist', 4)

        result = ensure_frozen_index(
            UnsafeErrorClient(), 'KOSPI', 'day', F - timedelta(minutes=30), F,
            session_start=START, phase_started_at=P, delay=5, now=P,
            budget_remaining=lambda: True, cache={})
        self.assertEqual(result['reason'], 'index-fetch-failed')
        self.assertEqual(result['errorCode'], 'api-error')

    def test_inprogress_value_does_not_leak_across_complete_windows(self):
        first_start, first_end = F - timedelta(minutes=30), F
        second_start, second_end = F - timedelta(minutes=29), F + timedelta(minutes=1)
        first_bars = [index_bar(START + timedelta(minutes=i), '1000', '1000')
                      for i in range(30, 60)] + [index_bar(F, '1000', '100')]
        second_bars = [index_bar(START + timedelta(minutes=i), '1000',
                                  '101' if i == 60 else '1000')
                       for i in range(31, 61)]
        responses = [first_bars, second_bars]

        class SuccessiveClient:
            def __init__(self):
                self.calls = 0

            def get(self, path, **params):
                bars = responses[self.calls]
                self.calls += 1
                return {'candles': bars, 'nextBefore': None}

        client, cache = SuccessiveClient(), {}
        first = ensure_frozen_index(
            client, 'KOSPI', 'day', first_start, first_end,
            session_start=START, phase_started_at=F + timedelta(seconds=5),
            delay=5, now=F + timedelta(seconds=5),
            budget_remaining=lambda: True, cache=cache)
        second = ensure_frozen_index(
            client, 'KOSPI', 'day', second_start, second_end,
            session_start=START, phase_started_at=F + timedelta(minutes=1, seconds=10),
            delay=5, now=F + timedelta(minutes=1, seconds=10),
            budget_remaining=lambda: True, cache=cache)
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(first['endValue'], D('1000'))
        self.assertEqual(second['status'], 'ok')
        self.assertEqual(second['endValue'], D('101'))
        self.assertNotEqual(first['key'], second['key'])
        self.assertEqual(client.calls, 2)

    def test_auth_propagates(self):
        class AuthClient:
            def get(self, path, **params):
                raise TmonError('auth-expired-test', 'auth', 3)
        with self.assertRaises(TmonError):
            ensure_frozen_index(AuthClient(), 'KOSPI', 'day',
                                F - timedelta(minutes=30), F,
                                session_start=START, phase_started_at=P,
                                delay=5, now=P, budget_remaining=lambda: True, cache={})



def make_engine_client(index_bars=None, index_error=None, wall=None, mono=None,
                       price='101.2'):
    from tests.test_recommend import cal as _cal

    class Client:
        def __init__(self):
            self.calls = []
            self.index_calls = 0
            self.price = price

        def get(self, path, **params):
            if wall is not None:
                wall[0] = wall[0] + timedelta(seconds=1)
                mono[0] = mono[0] + 1.0
            self.calls.append((path, params))
            now = wall[0] if wall is not None else NOW
            if path.endswith('/market-calendar/KR'):
                requested = params.get('date')
                if requested:
                    requested = datetime.fromisoformat(requested).replace(hour=10, tzinfo=KST)
                return _cal(requested or now)
            if path.endswith('/rankings'):
                return {'rankedAt': now.isoformat(), 'rankings': [
                    {'rank': 1, 'symbol': '000001', 'currency': 'KRW',
                     'price': {'lastPrice': '101.2', 'basePrice': '100', 'changeRate': '0.012'},
                     'tradingVolume': '100000000', 'tradingAmount': '10000000000'},
                    {'rank': 2, 'symbol': '000002', 'currency': 'KRW',
                     'price': {'lastPrice': '101.2', 'basePrice': '100', 'changeRate': '0.012'},
                     'tradingVolume': '100000000', 'tradingAmount': '10000000000'}]}
            if path == '/api/v1/stocks':
                sym = params['symbols']
                if ',' in sym:
                    return [stock(s) for s in sym.split(',')]
                return [stock(sym)]
            if path.endswith('/warnings'):
                return []
            if path.endswith('/prices'):
                return [{'symbol': s, 'currency': 'KRW', 'lastPrice': self.price,
                         'timestamp': now.isoformat()} for s in params['symbols'].split(',')]
            if path.endswith('/orderbook'):
                current = book(now)
                current['asks'][0]['price'] = self.price
                current['bids'][0]['price'] = str(D(self.price) - D('0.1'))
                return current
            if path.endswith('/price-limits'):
                return {'timestamp': now.isoformat(), 'currency': 'KRW', 'upperLimitPrice': '130'}
            if 'market-indicators' in path:
                self.index_calls += 1
                if index_error is not None:
                    raise index_error
                return {'candles': list(index_bars), 'nextBefore': None}
            if path.endswith('/candles'):
                if params.get('interval') == '1d':
                    return {'candles': list(reversed(days())), 'nextBefore': None}
                try:
                    from tests.test_recommend_pr1 import extended_minutes as _ext
                    return {'candles': list(reversed(_ext())), 'nextBefore': None}
                except Exception:
                    return {'candles': list(reversed(minutes())), 'nextBefore': None}
            raise AssertionError(path)
    return Client()


class EngineFrozenTests(unittest.TestCase):
    def test_valid_index_advancing_clock_frozen_and_final_preserved(self):
        # Leave room for the shared 19-link calendar preparation.
        wall = [datetime(2026, 9, 4, 10, 4, 35, tzinfo=KST)]
        mono = [500.0]
        # Valid 30-bar index + one in-progress 10:00 bar (close 9999, must be ignored).
        bars = valid_index_30() + [index_bar(F, '1000', '9999')]
        client = make_engine_client(index_bars=bars, wall=wall, mono=mono, price='102')
        with tempfile.TemporaryDirectory() as td:
            engine = Engine(load_config(research='off'), 'day', 3, client,
                            Store(Path(td).resolve()),
                            now=lambda: wall[0], clock=lambda: mono[0])
            result = envelope('recommend')
            code = engine.run(result)
            self.assertEqual(code, 0)
            screened = engine.inputs['screened']
            self.assertEqual(len(screened), 2)
            # One frozen window for all same-market candidates.
            windows = {(r['relativeReturnWindow']['comparisonStartAt'],
                        r['relativeReturnWindow']['comparisonEndAt']) for r in screened}
            self.assertEqual(len(windows), 1)
            start_at, end_at = next(iter(windows))
            self.assertEqual(end_at, result['meta']['signalWindowEndAt'])
            self.assertEqual(timestamp(end_at) - timestamp(start_at), timedelta(minutes=30))
            for row in screened:
                w = row['relativeReturnWindow']
                # Legacy meanings preserved: start/end are bar starts.
                self.assertIn('start', w)
                self.assertIn('end', w)
                self.assertIn('comparisonStartAt', w)
                self.assertIn('comparisonEndAt', w)
                self.assertEqual(w['comparisonStartAt'], w['start'])
                self.assertEqual(timestamp(w['comparisonEndAt']) - timestamp(w['end']),
                                 timedelta(minutes=1))
                self.assertEqual(w['priceBasis'], 'provider-index-points')
                self.assertEqual(w['stockPriceBasis'], 'adjusted')
                self.assertEqual(w['returnCalculation'], 'first-open-to-last-close')
                self.assertIsNone(row.get('relativeReturnReason'))
                self.assertIsNotNone(row['relativeReturnPct'])
            # Stock 1.2% - index 1% = 0.2pp (in-progress 9999 must not leak in).
            self.assertAlmostEqual(float(screened[0]['relativeReturnPct']), 0.2, places=6)
            # Frozen index fetched once before candidates (not per candidate),
            # and final preserves screening values with no final-phase refetch.
            index_records = [r for r in engine.client.records if 'market-indicators' in r['path']]
            self.assertEqual(len(index_records), 1)
            self.assertEqual(index_records[0]['phase'], 'screen')
            self.assertTrue(result['data'])
            for row in result['data']:
                self.assertAlmostEqual(float(row['relativeReturnPct']),
                                       float(screened[0]['relativeReturnPct']), places=9)
                self.assertEqual(row['relativeReturnWindow'], screened[0]['relativeReturnWindow'])
            self.assertIn('relativeReturnCoverage', result['meta'])
            self.assertEqual(result['meta']['relativeReturnCoverage']['ok'], 2)
            self.assertEqual(result['meta']['relativeReturnBasis']['priceBasis'],
                             'provider-index-points')
            self.assertEqual(result['meta']['relativeReturnBasis']['stockPriceBasis'],
                             'adjusted')
            self.assertEqual(result['meta']['relativeReturnBasis']['returnCalculation'],
                             'first-open-to-last-close')

    def test_candidate_order_does_not_change_frozen_relative_values(self):
        """Candidate evaluation order cannot change a shared market snapshot."""
        def execute(order):
            wall = [datetime(2026, 9, 4, 10, 4, 35, tzinfo=KST)]
            mono = [500.0]
            client = make_engine_client(index_bars=valid_index_30(), wall=wall,
                                        mono=mono, price='102')
            original = client.get

            def get(path, **params):
                if path.endswith('/rankings'):
                    client.calls.append((path, params))
                    return {'rankedAt': wall[0].isoformat(), 'rankings': [
                        {'rank': rank, 'symbol': symbol, 'currency': 'KRW',
                         'price': {'lastPrice': '101.2', 'basePrice': '100',
                                   'changeRate': '0.012'},
                         'tradingVolume': '100000000',
                         'tradingAmount': '10000000000'}
                        for rank, symbol in enumerate(order, 1)]}
                return original(path, **params)

            client.get = get
            with tempfile.TemporaryDirectory() as td:
                engine = Engine(load_config(research='off'), 'day', 3, client,
                                Store(Path(td).resolve()), now=lambda: wall[0],
                                clock=lambda: mono[0])
                result = envelope('recommend')
                self.assertEqual(engine.run(result), 0)
                return client, engine, result

        first_client, first_engine, first_result = execute(['000001', '000002'])
        second_client, second_engine, second_result = execute(['000002', '000001'])
        self.assertEqual(first_client.index_calls, second_client.index_calls)
        self.assertEqual(first_client.index_calls, 1)
        self.assertEqual([r['symbol'] for r in first_engine.inputs['screened']],
                         ['000001', '000002'])
        self.assertEqual([r['symbol'] for r in second_engine.inputs['screened']],
                         ['000002', '000001'])
        expected = D('0.2')
        first = {r['symbol']: r for r in first_engine.inputs['screened']}
        second = {r['symbol']: r for r in second_engine.inputs['screened']}
        self.assertEqual(first.keys(), second.keys())
        for symbol in first:
            self.assertIsNone(first[symbol]['relativeReturnReason'])
            self.assertIsNone(second[symbol]['relativeReturnReason'])
            self.assertAlmostEqual(float(first[symbol]['relativeReturnPct']),
                                   float(expected), places=6)
            self.assertAlmostEqual(float(second[symbol]['relativeReturnPct']),
                                   float(expected), places=6)
            self.assertEqual(first[symbol]['relativeReturnWindow'],
                             second[symbol]['relativeReturnWindow'])
        self.assertTrue(first_result['data'])
        self.assertTrue(second_result['data'])
        self.assertFalse(any('market-indicators' in r['path'] and r['phase'] == 'final'
                             for r in first_engine.client.records + second_engine.client.records))

    def test_swing_common_calendar_dates_use_independent_holiday_chain(self):
        # Explicit chain: Thu 8/27, Fri 8/28, Mon 8/31, Tue 9/1, Wed 9/2,
        # Thu 9/3. Weekend dates are intentionally absent from the expected
        # result and are never inferred from index candles.
        expected_dates = ['2026-08-27', '2026-08-28', '2026-08-31',
                          '2026-09-01', '2026-09-02', '2026-09-03']
        previous = dict(zip(expected_dates[1:], expected_dates[:-1]))
        previous['2026-09-04'] = expected_dates[-1]
        following = {'2026-09-03': '2026-09-04', '2026-09-04': '2026-09-07'}

        def calendar_for(requested):
            def day(date_text):
                d = datetime.fromisoformat(date_text).replace(hour=10, tzinfo=KST)
                return {'date': date_text, 'integrated': {'regularMarket': {
                    'startTime': d.replace(hour=9).isoformat(),
                    'singlePriceAuctionStartTime': d.replace(hour=15, minute=20).isoformat(),
                    'endTime': d.replace(hour=15, minute=30).isoformat()}}}

            today = day(requested)
            prev = day(previous[requested]) if requested in previous else None
            nxt_date = following.get(requested)
            if nxt_date is None:
                candidates = [d for d in expected_dates if d > requested]
                nxt_date = candidates[0] if candidates else '2026-09-07'
            return {'today': today, 'previousBusinessDay': prev,
                    'nextBusinessDay': day(nxt_date)}

        class CalendarChainClient(FakeClient):
            def get(self, path, **params):
                if path.endswith('/market-calendar/KR'):
                    self.calls.append((path, params))
                    return calendar_for(params.get('date') or NOW.date().isoformat())
                return super().get(path, **params)

        client = CalendarChainClient()
        with tempfile.TemporaryDirectory() as td:
            engine = Engine(load_config(research='off'), 'swing', 3, client,
                            Store(Path(td).resolve()), now=lambda: NOW)
            engine.phase(engine.started + 100)
            actual = common_trading_dates(
                engine.client, calendar_for(NOW.date().isoformat()), NOW, 6,
                check_budget=engine.check_budget)['dates']
        self.assertEqual(actual, expected_dates)

    def test_swing_calendar_wrong_mapping_is_unverified_and_auth_propagates(self):
        expected_dates = ['2026-08-27', '2026-08-28', '2026-08-31',
                          '2026-09-01', '2026-09-02', '2026-09-03']
        previous = dict(zip(expected_dates[1:], expected_dates[:-1]))
        previous['2026-09-04'] = expected_dates[-1]

        def calendar_for(requested):
            def day(date_text):
                d = datetime.fromisoformat(date_text).replace(hour=10, tzinfo=KST)
                return {'date': date_text, 'integrated': {'regularMarket': {
                    'startTime': d.replace(hour=9).isoformat(),
                    'singlePriceAuctionStartTime': d.replace(hour=15, minute=20).isoformat(),
                    'endTime': d.replace(hour=15, minute=30).isoformat()}}}
            return {'today': day(requested),
                    'previousBusinessDay': day(previous.get(requested, '2026-09-07')),
                    'nextBusinessDay': day('2026-09-07')}

        class WrongMappingClient(FakeClient):
            def get(self, path, **params):
                if path.endswith('/market-calendar/KR'):
                    requested = params.get('date') or NOW.date().isoformat()
                    value = calendar_for(requested)
                    if requested != NOW.date().isoformat():
                        value['today']['date'] = '2000-01-01'
                    return value
                return super().get(path, **params)

        class AuthClient(WrongMappingClient):
            def get(self, path, **params):
                if path.endswith('/market-calendar/KR') and params.get('date'):
                    raise TmonError('calendar-auth', 'auth', 3)
                return super().get(path, **params)

        for client in (WrongMappingClient(), AuthClient()):
            with tempfile.TemporaryDirectory() as td:
                engine = Engine(load_config(research='off'), 'swing', 3, client,
                                Store(Path(td).resolve()), now=lambda: NOW)
                engine.phase(engine.started + 100)
                initial = calendar_for(NOW.date().isoformat())
                if isinstance(client, AuthClient):
                    with self.assertRaises(TmonError) as caught:
                        common_trading_dates(
                            engine.client, initial, NOW, 6,
                            check_budget=engine.check_budget)
                    self.assertEqual(caught.exception.exit_code, 3)
                else:
                    self.assertFalse(common_trading_dates(
                        engine.client, initial, NOW, 6,
                        check_budget=engine.check_budget)['verified'])


if __name__ == '__main__':
    unittest.main()
