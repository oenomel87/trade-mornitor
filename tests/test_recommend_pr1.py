"""PR1: actual phaseStartedAt, frozen signal window, separated time roles."""
import unittest
from datetime import datetime, timedelta, timezone

from tmon.errors import TmonError
from tmon.intraday import completed_minutes, five_minutes, signal_window_end
from tmon.market import timestamp

KST = timezone(timedelta(hours=9))
START = datetime(2026, 9, 4, 9, 0, 0, tzinfo=KST)
NOW = datetime(2026, 9, 4, 10, 0, 10, tzinfo=KST)


def candle(t, close='100'):
    return {'timestamp': t.isoformat(), 'currency': 'KRW', 'openPrice': close,
            'highPrice': '101', 'lowPrice': '99', 'closePrice': close, 'volume': '100000'}


def minutes_range(start, count):
    return [candle(start + timedelta(minutes=i)) for i in range(count)]


def extended_minutes():
    # 09:00..09:59 breakout fixture plus a higher 10:00..10:04 bucket so a
    # 10:05 wall stays source-fresh while screening (F=10:00) still uses the
    # 09:55 bucket and final (F=10:05) can pass on the 10:00 bucket.
    from tests.test_recommend import START as _START, minutes as _minutes
    base = _minutes()
    extra = []
    for i in range(60, 65):
        t = _START + timedelta(minutes=i)
        extra.append({'timestamp': t.isoformat(), 'currency': 'KRW',
                      'openPrice': '101.5', 'highPrice': '102.1',
                      'lowPrice': '101.4', 'closePrice': '102', 'volume': '200000'})
    return base + extra


class Inner:
    def __init__(self, candles):
        self.candles = candles
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        return {'candles': list(self.candles)}


def recording(candles, received):
    from tmon.recommend_data import RecordingClient
    return RecordingClient(Inner(candles), now=lambda: received)


def sess():
    return {'start': START}


def settings(delay=5, minute_seconds=120):
    return {'barCompletionDelaySeconds': delay, 'minuteSeconds': minute_seconds}


class WindowTests(unittest.TestCase):
    def test_formula_and_none(self):
        f = signal_window_end(START, NOW, 5)
        self.assertEqual(f, START + timedelta(seconds=12 * 300))
        self.assertEqual(f.isoformat(), datetime(2026, 9, 4, 10, 0, 0, tzinfo=KST).isoformat())
        # n <= 0 -> no completed bar
        self.assertIsNone(signal_window_end(START, START + timedelta(seconds=5), 5))
        self.assertIsNone(signal_window_end(START, START, 5))

    def test_n_less_than_6_detectable(self):
        # P=09:30:04, D=5 -> delta=1799 -> n=5 -> F=09:25 (only 5 bars possible)
        p = START + timedelta(minutes=30, seconds=4)
        f = signal_window_end(START, p, 5)
        self.assertEqual(f, START + timedelta(minutes=25))
        n = int((f - START).total_seconds() // 300)
        self.assertLess(n, 6)

    def test_100010_with_d5_includes_0959(self):
        raw = {'candles': minutes_range(START, 60)}
        f = signal_window_end(START, NOW, 5)
        rows = completed_minutes(raw, NOW, START, 5, received_at=NOW,
                                 phase_started_at=NOW, window_end=f)
        self.assertEqual(timestamp(rows[-1]['timestamp']), NOW.replace(hour=9, minute=59, second=0))
        five = five_minutes(rows, START)
        self.assertEqual(timestamp(five[-1]['timestamp']) + timedelta(minutes=5), f)

    def test_delay_boundaries_before_exact_after(self):
        for delay in (1, 5, 60):
            with self.subTest(delay=delay):
                f = datetime(2026, 9, 4, 10, 0, 0, tzinfo=KST)
                raw = {'candles': minutes_range(START, 60)}
                received = datetime(2026, 9, 4, 10, 0, 10, tzinfo=KST)
                exact = f + timedelta(seconds=delay)
                # exact: barEnd + D <= phase -> included
                rows = completed_minutes(raw, exact, START, delay, received_at=received,
                                         phase_started_at=exact, window_end=f)
                self.assertEqual(timestamp(rows[-1]['timestamp']).replace(tzinfo=KST).hour, 9)
                self.assertEqual(timestamp(rows[-1]['timestamp']), f - timedelta(minutes=1))
                # before: one second early -> 09:59 excluded
                before = exact - timedelta(seconds=1)
                rows = completed_minutes(raw, before, START, delay, received_at=received,
                                         phase_started_at=before, window_end=f)
                self.assertEqual(timestamp(rows[-1]['timestamp']), f - timedelta(minutes=2))
                # after: one second late -> still included, no double delay
                after = exact + timedelta(seconds=1)
                rows = completed_minutes(raw, after, START, delay, received_at=received,
                                         phase_started_at=after, window_end=f)
                self.assertEqual(timestamp(rows[-1]['timestamp']), f - timedelta(minutes=1))

    def test_receive_spanning_minute_boundary(self):
        # Same raw + same phase, different receive instants -> different future decisions.
        phase = datetime(2026, 9, 4, 10, 0, 10, tzinfo=KST)
        f = datetime(2026, 9, 4, 10, 0, 0, tzinfo=KST)
        bar_1001 = candle(datetime(2026, 9, 4, 10, 1, 0, tzinfo=KST))
        raw = {'candles': minutes_range(START, 60) + [bar_1001]}
        early_received = datetime(2026, 9, 4, 10, 0, 10, tzinfo=KST)
        warnings = []
        rows = completed_minutes(raw, phase, START, 5, warnings,
                                 received_at=early_received,
                                 phase_started_at=phase, window_end=None)
        self.assertEqual(len(rows), 60)
        self.assertEqual(warnings[0]['code'], 'future-minute-bar-skipped')
        # Received after the boundary: 10:01 bar is no longer "future", it is
        # just an incomplete bar for this phase (excluded silently by completion).
        late_received = datetime(2026, 9, 4, 10, 1, 5, tzinfo=KST)
        warnings = []
        rows = completed_minutes(raw, phase, START, 5, warnings,
                                 received_at=late_received,
                                 phase_started_at=phase, window_end=None)
        self.assertEqual(len(rows), 60)
        self.assertEqual(warnings, [])
        # Distant future still errors regardless of phase.
        far = candle(datetime(2026, 9, 4, 10, 3, 0, tzinfo=KST))
        with self.assertRaises(TmonError) as caught:
            completed_minutes({'candles': minutes_range(START, 60) + [far]}, phase, START, 5,
                              received_at=late_received, phase_started_at=phase)
        self.assertEqual(caught.exception.code, 'future-minute-bar')

    def test_backward_compatible_defaults_match_brief(self):
        raw = {'candles': minutes_range(START, 60)}
        a = completed_minutes(raw, NOW, START, 5)
        b = completed_minutes(raw, NOW, START, 5, received_at=NOW,
                              phase_started_at=NOW, window_end=None)
        self.assertEqual(a, b)


class MinuteDataTests(unittest.TestCase):
    def test_source_freshness_split_from_frozen_phase(self):
        # Reproduced bug: P=10:00:10, receivedAt=10:02:10, F=10:00. A normal
        # source already contains the completed 10:01-start minute
        # (10:02:05<=10:02:10). Freshness against receivedAt must pass on the
        # source bar while the calculation set stays clipped to frozen F
        # (latest 09:59 minute). Uses advancing wall+monotonic clocks and an
        # actual RecordingClient.
        from tmon.recommend_data import RecordingClient, minute_data
        p = datetime(2026, 9, 4, 10, 0, 10, tzinfo=KST)
        f = signal_window_end(START, p, 5)
        self.assertEqual(f, datetime(2026, 9, 4, 10, 0, 0, tzinfo=KST))
        wall = [p]
        mono = [100.0]

        def wall_now():
            return wall[0]

        def mono_clock():
            return mono[0]

        class AdvancingInner:
            def __init__(self, candles):
                self.candles = candles

            def get(self, path, **params):
                # Simulate ~120s network/queue delay between phase start and
                # candle receipt; both clocks advance coherently.
                wall[0] = wall[0] + timedelta(seconds=120)
                mono[0] = mono[0] + 120.0
                return {'candles': list(self.candles)}

        candles = minutes_range(START, 62)  # 09:00..10:01 inclusive
        client = RecordingClient(AdvancingInner(candles), now=wall_now, clock=mono_clock)
        rows, five = minute_data(client, '000001', sess(), p, settings(),
                                 warnings=[], phase_started_at=p,
                                 signal_window_end_at=f)
        received = timestamp(client.records[-1]['receivedAt'])
        self.assertEqual(received, datetime(2026, 9, 4, 10, 2, 10, tzinfo=KST))
        self.assertEqual(timestamp(rows[-1]['timestamp']),
                         datetime(2026, 9, 4, 9, 59, 0, tzinfo=KST))
        self.assertEqual(timestamp(five[-1]['timestamp']) + timedelta(minutes=5), f)
        # Truly stale source still fails: same source, but received 8 minutes
        # after the latest source bar end (age 480s > 120s).
        wall2 = [p]
        mono2 = [200.0]

        class StaleInner:
            def get(self, path, **params):
                wall2[0] = wall2[0] + timedelta(seconds=600)
                mono2[0] = mono2[0] + 600.0
                return {'candles': list(candles)}

        stale = RecordingClient(StaleInner(), now=lambda: wall2[0], clock=lambda: mono2[0])
        with self.assertRaises(TmonError) as caught:
            minute_data(stale, '000001', sess(), p, settings(),
                        warnings=[], phase_started_at=p, signal_window_end_at=f)
        self.assertEqual(caught.exception.code, 'stale-data')

    def test_malformed_next_boundary_skipped_far_future_fails(self):
        # Order: parse/check timestamp + next-boundary exclusion using
        # receivedAt first, then normalize retained candles. A malformed
        # next-boundary candle is skipped with a diagnostic; farther future
        # (even malformed) still fails.
        received = datetime(2026, 9, 4, 10, 0, 10, tzinfo=KST)
        phase = received
        f = datetime(2026, 9, 4, 10, 0, 0, tzinfo=KST)
        baseline = completed_minutes({'candles': minutes_range(START, 60)}, phase,
                                     START, 5, received_at=received,
                                     phase_started_at=phase, window_end=f)
        # Invalid currency at the immediate next boundary (10:01) is skipped.
        bad_currency = {'timestamp': datetime(2026, 9, 4, 10, 1, 0, tzinfo=KST).isoformat(),
                        'currency': 'USD', 'openPrice': '100', 'highPrice': '101',
                        'lowPrice': '99', 'closePrice': '100', 'volume': '100000'}
        warnings = []
        rows = completed_minutes({'candles': minutes_range(START, 60) + [bad_currency]},
                                 phase, START, 5, warnings, received_at=received,
                                 phase_started_at=phase, window_end=f)
        self.assertEqual(rows, baseline)
        self.assertEqual(warnings[0]['code'], 'future-minute-bar-skipped')
        # Invalid OHLC at the immediate next boundary is also skipped.
        bad_ohlc = {'timestamp': datetime(2026, 9, 4, 10, 1, 0, tzinfo=KST).isoformat(),
                    'currency': 'KRW', 'openPrice': '100', 'highPrice': '90',
                    'lowPrice': '99', 'closePrice': '100', 'volume': '100000'}
        warnings = []
        rows = completed_minutes({'candles': minutes_range(START, 60) + [bad_ohlc]},
                                 phase, START, 5, warnings, received_at=received,
                                 phase_started_at=phase, window_end=f)
        self.assertEqual(rows, baseline)
        self.assertEqual(warnings[0]['code'], 'future-minute-bar-skipped')
        # Farther future malformed candle still fails.
        far_bad = {'timestamp': datetime(2026, 9, 4, 10, 3, 0, tzinfo=KST).isoformat(),
                   'currency': 'USD', 'openPrice': '100', 'highPrice': '101',
                   'lowPrice': '99', 'closePrice': '100', 'volume': '100000'}
        with self.assertRaises(TmonError):
            completed_minutes({'candles': minutes_range(START, 60) + [far_bad]},
                              phase, START, 5, received_at=received,
                              phase_started_at=phase, window_end=f)

    def test_missing_required_latest_has_no_fallback(self):
        from tmon.recommend_data import minute_data
        # Drop 09:59; older complete 5m blocks still exist but F=10:00 is required.
        candles = minutes_range(START, 59)
        received = NOW
        client = recording(candles, received)
        f = signal_window_end(START, NOW, 5)
        with self.assertRaises(TmonError) as caught:
            minute_data(client, '000001', sess(), NOW, settings(),
                        warnings=[], phase_started_at=NOW, signal_window_end_at=f)
        self.assertEqual(caught.exception.code, 'incomplete-bars')
        self.assertEqual(caught.exception.details['signalWindowEndAt'], f.isoformat())

    def test_fresh_source_with_older_fixed_window(self):
        from tmon.recommend_data import minute_data
        # Source latest end 10:00 is fresh at receive 10:00:10, but frozen
        # window is older (09:55). Freshness must pass on the source bar,
        # not fail on the clipped bar (age would be 315s > 120s).
        candles = minutes_range(START, 60)
        received = NOW
        client = recording(candles, received)
        old_f = datetime(2026, 9, 4, 9, 55, 0, tzinfo=KST)
        rows, five = minute_data(client, '000001', sess(), NOW, settings(),
                                 warnings=[], phase_started_at=NOW,
                                 signal_window_end_at=old_f)
        self.assertEqual(timestamp(rows[-1]['timestamp']), old_f - timedelta(minutes=1))
        self.assertEqual(timestamp(five[-1]['timestamp']) + timedelta(minutes=5), old_f)


class EnginePhaseTests(unittest.TestCase):
    def test_frozen_phase_shared_across_candidates_with_advancing_clock(self):
        # Two eligible symbols, coherent progressing wall+monotonic clocks
        # advanced by fake API calls across a 5-minute boundary (start near
        # 10:04:59), fresh quotes/books, valid index fixture. Both SCREENED
        # rows must share the same frozen required F regardless of later
        # evaluation time. Focus is screened records (PR3 freeze not yet).
        from tests.test_recommend import cal, candle, days, minutes, stock, book
        from tmon.cli import envelope
        from tmon.recommend import Engine
        from tmon.recommend_config import load_config
        from tmon.recommend_store import Store
        import tempfile
        from pathlib import Path

        # Leave room for the shared 19-link calendar preparation.
        start_wall = datetime(2026, 9, 4, 10, 4, 35, tzinfo=KST)
        wall = [start_wall]
        mono = [500.0]

        def wall_now():
            return wall[0]

        def mono_clock():
            return mono[0]

        symbols = ['000001', '000002']

        class TwoSymbolClient:
            def __init__(self):
                self.calls = []

            def get(self, path, **params):
                # Every logical API call advances both clocks coherently.
                wall[0] = wall[0] + timedelta(seconds=1)
                mono[0] = mono[0] + 1.0
                self.calls.append((path, params))
                now = wall[0]
                if path.endswith('/market-calendar/KR'):
                    requested = params.get('date')
                    if requested:
                        requested = datetime.fromisoformat(requested).replace(hour=10, tzinfo=KST)
                    return cal(requested or now)
                if path.endswith('/rankings'):
                    return {'rankedAt': now.isoformat(), 'rankings': [
                        {'rank': i + 1, 'symbol': sym, 'currency': 'KRW',
                         'price': {'lastPrice': '101.2', 'basePrice': '100', 'changeRate': '0.012'},
                         'tradingVolume': '100000000', 'tradingAmount': '10000000000'}
                        for i, sym in enumerate(symbols)]}
                if path == '/api/v1/stocks':
                    return [stock(sym) for sym in params['symbols'].split(',')]
                if path.endswith('/warnings'):
                    return []
                if path.endswith('/prices'):
                    req = params['symbols'].split(',')
                    # Late (final) phase needs a price inside the higher
                    # 10:00-bucket breakout band; early screening uses 101.2.
                    px = '102.0' if now >= datetime(2026, 9, 4, 10, 5, 5, tzinfo=KST) else '101.2'
                    return [{'symbol': s, 'currency': 'KRW', 'lastPrice': px,
                             'timestamp': now.isoformat()} for s in req]
                if path.endswith('/orderbook'):
                    b = book(now)
                    if now >= datetime(2026, 9, 4, 10, 5, 5, tzinfo=KST):
                        b['asks'][0]['price'] = '102.0'
                        b['bids'][0]['price'] = '101.9'
                    return b
                if path.endswith('/price-limits'):
                    return {'timestamp': now.isoformat(), 'currency': 'KRW',
                            'upperLimitPrice': '130'}
                if 'market-indicators' in path and path.endswith('/candles'):
                    # Valid index fixture aligned to the stock minute bars.
                    idx = []
                    for row in extended_minutes():
                        t = row['timestamp']
                        idx.append({'timestamp': t, 'currency': 'KRW',
                                    'openPrice': '1000', 'highPrice': '1002',
                                    'lowPrice': '999', 'closePrice': '1001',
                                    'volume': '100000'})
                    return {'candles': idx}
                if path.endswith('/candles'):
                    if params.get('interval') == '1d':
                        return {'candles': list(reversed(days())), 'nextBefore': None}
                    return {'candles': list(reversed(extended_minutes())), 'nextBefore': None}
                raise AssertionError(path)

        with tempfile.TemporaryDirectory() as td:
            engine = Engine(load_config(research='off'), 'day', 3, TwoSymbolClient(),
                            Store(Path(td).resolve()), now=wall_now, clock=mono_clock)
            result = envelope('recommend')
            code = engine.run(result)
            self.assertEqual(code, 0)
            self.assertEqual(result['meta']['outcomeReason'], 'recommended')
            screened = engine.inputs['screened']
            self.assertEqual(len(screened), 2)
            self.assertEqual({r['symbol'] for r in screened}, set(symbols))
            frozen_f = result['meta']['signalWindowEndAt']
            phase_p = timestamp(result['meta']['phaseStartedAt'])
            # Phase started near 10:04:59 (before the 10:05 roll); frozen F is 10:00.
            self.assertEqual(frozen_f, datetime(2026, 9, 4, 10, 0, 0, tzinfo=KST).isoformat())
            self.assertLess(phase_p, datetime(2026, 9, 4, 10, 5, 5, tzinfo=KST))
            # Wall advanced across the 5-minute boundary during screening/final.
            self.assertGreater(wall[0], datetime(2026, 9, 4, 10, 5, 5, tzinfo=KST))
            asofs = {r['signalAsOf'] for r in screened}
            self.assertEqual(len(asofs), 1)
            self.assertEqual(next(iter(asofs)),
                             datetime(2026, 9, 4, 9, 55, 0, tzinfo=KST).isoformat())
            windows = {(r['relativeReturnWindow']['start'], r['relativeReturnWindow']['end'])
                       for r in screened}
            self.assertEqual(len(windows), 1)
            for row in screened:
                self.assertEqual(len(row['signalInputHash']), 64)
                self.assertEqual(row['signalId'], row['signalInputHash'])
                self.assertIsNotNone(row['relativeReturnPct'])

    def test_delayed_nonround_session_start_boundary_formula(self):
        # Actual non-round/delayed session start: F stays anchored at S.
        s = datetime(2026, 9, 4, 9, 1, 37, tzinfo=KST)
        p = datetime(2026, 9, 4, 10, 0, 10, tzinfo=KST)
        import math
        n = math.floor(((p - s).total_seconds() - 5) / 300)
        f = signal_window_end(s, p, 5)
        self.assertEqual(f, s + timedelta(seconds=n * 300))
        # Second is preserved from the delayed start (not wall-aligned).
        self.assertEqual((f.second, f.microsecond), (37, 0))
        # Frozen window stays anchored at the delayed start across phases.
        later = p + timedelta(seconds=200)
        f2 = signal_window_end(s, later, 5)
        self.assertGreaterEqual(f2, f)
        self.assertEqual((f2 - s).total_seconds() % 300, 0)

    def test_early_session_reports_session_not_ready_not_data(self):
        from tests.test_recommend import FakeClient
        from tmon.cli import envelope
        from tmon.recommend import Engine
        from tmon.recommend_config import load_config
        from tmon.recommend_store import Store
        import tempfile
        from pathlib import Path

        early = START + timedelta(minutes=30, seconds=4)  # 09:30:04, n=5

        class EarlyClient(FakeClient):
            def get(self, path, **params):
                raw = super().get(path, **params)
                if path.endswith('/rankings'):
                    raw['rankedAt'] = early.isoformat()
                return raw

        with tempfile.TemporaryDirectory() as td:
            engine = Engine(load_config(research='off'), 'day', 3, EarlyClient(),
                            Store(Path(td).resolve()), now=lambda: early)
            result = envelope('recommend')
            code = engine.run(result)
            self.assertEqual(result['meta']['outcomeReason'], 'no-match')
            self.assertIn('session-not-ready', result['meta']['excludedCounts'])
            self.assertEqual(result['meta']['candidateDataUnavailableCount'], 0)
            # Expected evaluation unavailability: evidence attached, counted
            # as expected (not a normal condition failure nor data defect).
            screen = result['meta']['evaluationSummary']['screening']
            self.assertEqual(screen['dataUnavailableCount'], 0)
            self.assertEqual(screen['conditionExcludedCount'], 0)
            self.assertGreater(screen['expectedUnavailabilityCount'], 0)
            self.assertGreaterEqual(result['meta']['expectedIneligibleCount'], 1)
            entry = result['meta']['excluded'][0]
            for field in ('sessionStart', 'phaseStartedAt', 'requiredCompletedBars', 'completedBars'):
                self.assertIn(field, entry)
            self.assertEqual(entry['requiredCompletedBars'], 6)
            self.assertLess(entry['completedBars'], 6)

    def test_swing_stores_phase_started_at(self):
        from tests.test_recommend import FakeClient
        from tmon.cli import envelope
        from tmon.recommend import Engine
        from tmon.recommend_config import load_config
        from tmon.recommend_store import Store
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            engine = Engine(load_config(research='off'), 'swing', 3, FakeClient(),
                            Store(Path(td).resolve()), now=lambda: NOW)
            result = envelope('recommend')
            code = engine.run(result)
            self.assertEqual(code, 0)
            self.assertIn('phaseStartedAt', result['meta'])
            self.assertIsNone(result['meta']['signalWindowEndAt'])


if __name__ == '__main__':
    unittest.main()
