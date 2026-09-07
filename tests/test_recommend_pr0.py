"""PR0 regression: screened snapshot, dedup, observations, evidence-based status."""
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.test_recommend import FakeClient, NOW, cal, candle, days, minutes, stock
from tmon.cli import envelope
from tmon.client import Transport
from tmon.errors import TmonError, is_expected_ineligible
from tmon.recommend import Engine
from tmon.recommend_config import load_config
from tmon.recommend_data import RecordingClient, relative_return_window, request_counts, signal_input_hash
from tmon.recommend_store import Store
from tmon.research import research, unavailable
from tmon.strategies import NoMatch


class MultiClient(FakeClient):
    """FakeClient variant serving N passing symbols with identical charts."""

    def __init__(self, symbols):
        super().__init__()
        self.symbols = list(symbols)

    def get(self, path, **params):
        if path.endswith('/rankings'):
            self.calls.append((path, params))
            return {'rankedAt': NOW.isoformat(), 'rankings': [
                {'rank': i + 1, 'symbol': sym, 'currency': 'KRW',
                 'price': {'lastPrice': '101.2', 'basePrice': '100', 'changeRate': '0.012'},
                 'tradingVolume': '100000000', 'tradingAmount': '10000000000'}
                for i, sym in enumerate(self.symbols)]}
        if path.endswith('/stocks'):
            self.calls.append((path, params))
            return [stock(sym) for sym in params['symbols'].split(',')]
        if path.endswith('/prices'):
            self.calls.append((path, params))
            return [{'symbol': sym, 'currency': 'KRW', 'lastPrice': self.price,
                     'timestamp': NOW.isoformat()}
                    for sym in params['symbols'].split(',')]
        return super().get(path, **params)


def run_engine(client, config=None, researcher=research, horizon='day', limit=3):
    with tempfile.TemporaryDirectory() as td:
        engine = Engine(config or load_config(research='off'), horizon, limit,
                        client, Store(Path(td).resolve()), now=lambda: NOW,
                        researcher=researcher)
        result = envelope('recommend')
        code = engine.run(result)
        return code, result, engine


class ScreenedSnapshotTests(unittest.TestCase):
    def test_all_passes_preserved_beyond_shortlist(self):
        config = load_config(research='off')
        config['universe'].update(detailLimit=5, researchLimit=1)
        code, result, engine = run_engine(MultiClient(['000001', '000002', '000003']),
                                          config=config, limit=1)
        self.assertEqual(code, 0)
        screened = engine.inputs['screened']
        self.assertEqual(len(screened), 3)
        self.assertEqual(result['meta']['screenedCount'], 3)
        self.assertEqual(result['meta']['quantitativePassCount'], 3)
        shortlist_symbols = [r['symbol'] for r in result['data']]
        self.assertEqual(len(shortlist_symbols), 1)
        outside = [r for r in screened if r['symbol'] not in shortlist_symbols]
        self.assertTrue(outside, 'screened must keep rows outside research')
        for row in screened:
            for field in ('signalId', 'signalInputHash', 'breakoutLevel', 'invalidation',
                          'volumeRatio', 'relativeReturnPct', 'relativeReturnWindow',
                          'entryReference', 'riskPerShare', 'targetRange', 'quantity',
                          'signalAsOf', 'spreadPct'):
                self.assertIn(field, row, '%s missing in screened row' % field)
            self.assertEqual(len(row['signalInputHash']), 64)
            self.assertEqual(row['signalId'], row['signalInputHash'])
            self.assertIsNotNone(row['relativeReturnWindow'])

    def test_screened_keeps_final_rejections_with_original_values(self):
        client = FakeClient()

        def change(*args, **kwargs):
            client.price = '99'  # Final pass leaves the entry band.
            return {'000001': unavailable()}, {'status': 'unavailable'}

        code, result, engine = run_engine(client, researcher=change)
        self.assertEqual(result['data'], [])
        screened = engine.inputs['screened']
        self.assertEqual(len(screened), 1)
        self.assertEqual(screened[0]['entryReference'], D('101.2'))
        self.assertEqual(screened[0]['lastPrice'], D('101.2'))
        reasons = [r['reason'] for r in engine.inputs['excluded'] if r['stage'] == 'final']
        self.assertIn('entry-invalidated', reasons)

    def test_snapshot_immutable_when_research_mutates(self):
        seen = {}

        def mutating(shortlist, *args, **kwargs):
            seen['before'] = deepcopy(shortlist[0])
            shortlist[0]['entryReference'] = D('999')
            shortlist[0]['targetRange'] = [D('1'), D('2')]
            shortlist[0]['limitations'].append('mutated by research')
            return {'000001': unavailable()}, {'status': 'unavailable'}

        _, _, engine = run_engine(FakeClient(), researcher=mutating)
        screened = engine.inputs['screened']
        self.assertEqual(screened[0]['entryReference'], seen['before']['entryReference'])
        self.assertEqual(screened[0]['targetRange'], seen['before']['targetRange'])
        self.assertNotIn('mutated by research', screened[0]['limitations'])

    def test_hash_distinguishes_input_corrections(self):
        from tmon.market import normalize_candle
        daily = [normalize_candle(r, 'KRW')[1] for r in days()]
        first = [normalize_candle(r, 'KRW')[1] for r in minutes()]
        from tmon.intraday import five_minutes
        from tests.test_recommend import START
        five = five_minutes(first, START)
        base = signal_input_hash('000001', 'day', daily, five)
        altered = deepcopy(daily)
        altered[-1] = dict(altered[-1], closePrice=altered[-1]['closePrice'] + D('1'))
        self.assertNotEqual(base, signal_input_hash('000001', 'day', altered, five))
        self.assertEqual(base, signal_input_hash('000001', 'day', list(daily), list(five)))

    def test_relative_window_shapes(self):
        from tmon.market import normalize_candle
        daily = [normalize_candle(r, 'KRW')[1] for r in days()]
        first = [normalize_candle(r, 'KRW')[1] for r in minutes()]
        window = relative_return_window('day', daily, first)
        self.assertEqual((window['interval'], window['count']), ('1m', 30))
        swing_window = relative_return_window('swing', daily, [])
        self.assertEqual((swing_window['interval'], swing_window['count']), ('1d', 5))
        self.assertIn('startDate', swing_window)


class DedupCountsTests(unittest.TestCase):
    def test_screening_reuses_prefetch_stock_call(self):
        _, _, engine = run_engine(FakeClient())
        stocks = [r for r in engine.client.records if r['path'] == '/api/v1/stocks']
        by_phase = [r['phase'] for r in stocks]
        # Prefetch once + final refresh once; screening itself must not refetch.
        self.assertEqual(len(stocks), 2, [ (r['phase'], r['path']) for r in stocks])
        self.assertEqual(sorted(by_phase), ['final', 'prefetch'])
        meta = request_counts(engine.client.records)
        self.assertEqual(meta['byEndpoint']['/api/v1/stocks'], 2)

    def test_endpoint_vs_stock_group_counts(self):
        _, result, engine = run_engine(FakeClient())
        meta = result['meta']
        endpoint_stocks = meta['endpointCounts'].get('/api/v1/stocks', 0)
        warnings_endpoint = next(k for k in meta['endpointCounts'] if k.endswith('/warnings'))
        warnings_count = meta['endpointCounts'][warnings_endpoint]
        self.assertEqual(meta['endpointGroupCounts']['STOCK'], endpoint_stocks + warnings_count)
        phases = meta['requestPhaseCounts']
        self.assertGreaterEqual(phases.get('prefetch', 0), 1)
        self.assertGreater(phases.get('screen', 0), 0)
        self.assertGreater(phases.get('final', 0), 0)

    def test_records_preserve_retrieved_at_and_no_secrets(self):
        _, _, engine = run_engine(FakeClient())
        for record in engine.client.records:
            self.assertEqual(record['retrievedAt'], record['receivedAt'])
            for field in ('requestStartedAt', 'receivedAt', 'endpointGroup', 'attemptCount',
                          'rateLimitWaitSeconds', 'retryWaitSeconds', 'elapsedSeconds',
                          'success', 'errorCode'):
                self.assertIn(field, record)
            blob = json.dumps(record, default=str)
            for secret in ('Bearer', 'Authorization', 'TOSS_CLIENT_SECRET', 'OPENAI_API_KEY'):
                self.assertNotIn(secret, blob)


class ObservationTests(unittest.TestCase):
    def test_failure_record_uses_safe_code(self):
        class BoomClient:
            def get(self, path, **params):
                raise TmonError('no-data', 'SECRET-RAW-provider-detail-xyz')

        recorder = RecordingClient(BoomClient(), now=lambda: NOW, clock=lambda: 10.0)
        with self.assertRaises(TmonError):
            recorder.get('/api/v1/prices', symbols='000001')
        record = recorder.records[0]
        self.assertFalse(record['success'])
        self.assertEqual(record['errorCode'], 'no-data')
        self.assertNotIn('result', record)
        self.assertNotIn('SECRET-RAW-provider-detail-xyz', json.dumps(record, default=str))
        self.assertEqual(record['retrievedAt'], record['receivedAt'])
        self.assertEqual(record['attemptCount'], 1)

    def test_transport_retry_timing(self):
        ticks = [100.0]
        sleeps = []

        def sleeper(seconds):
            sleeps.append(seconds)
            ticks[0] += seconds

        transport = Transport(60, clock=lambda: ticks[0], sleeper=sleeper, now=lambda: NOW)
        with patch.object(transport, 'send', side_effect=[
                (500, {}, {}), (200, {}, {'result': []})]):
            transport.request('GET', '/api/v1/stocks/000001/warnings', token='fake')
        observation = transport.observations[0]
        self.assertTrue(observation['success'])
        self.assertEqual(observation['attemptCount'], 2)
        self.assertGreater(observation['retryWaitSeconds'], 0)
        self.assertGreaterEqual(observation['elapsedSeconds'], 0)
        self.assertEqual(observation['endpointGroup'], 'STOCK')
        self.assertTrue(sleeps)

    def test_transport_rate_limit_wait_and_failure(self):
        ticks = [200.0]
        transport = Transport(60, clock=lambda: ticks[0],
                              sleeper=lambda seconds: ticks.__setitem__(0, ticks[0] + seconds),
                              now=lambda: NOW)
        with patch.object(transport, 'send', side_effect=[
                (200, {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '7'}, {'result': []}),
                (200, {}, {'result': []})]):
            transport.request('GET', '/api/v1/stocks/000001/warnings', token='fake')
            transport.request('GET', '/api/v1/stocks/000001/warnings', token='fake')
        self.assertGreater(transport.observations[1]['rateLimitWaitSeconds'], 0)
        with patch.object(transport, 'send', return_value=(404, {}, {'error': {'code': 'gone'}})):
            with self.assertRaises(TmonError):
                transport.request('GET', '/api/v1/stocks/000001/warnings', token='fake')
        failure = transport.observations[-1]
        self.assertFalse(failure['success'])
        self.assertEqual(failure['errorCode'], 'gone')
        self.assertIn('requestStartedAt', failure)

    def test_refused_retry_books_zero_actual_wait(self):
        # budget=1, frozen clock, 429 with planned delay 2: pause refuses
        # before sleeping, so actual retry wait and elapsed stay 0.
        transport = Transport(1, clock=lambda: 0.0,
                              sleeper=lambda s: (_ for _ in ()).throw(
                                  AssertionError('must not sleep on refusal')),
                              now=lambda: NOW)
        with patch.object(transport, 'send', return_value=(429, {}, {})), \
             patch.object(transport, 'retry_delay', return_value=2), \
             self.assertRaises(TmonError) as caught:
            transport.request('GET', '/api/v1/stocks/000001/warnings', token='fake')
        self.assertEqual(caught.exception.code, 'retry-budget-exceeded')
        observation = transport.observations[0]
        self.assertFalse(observation['success'])
        self.assertEqual(observation['errorCode'], 'retry-budget-exceeded')
        self.assertEqual(observation['retryWaitSeconds'], 0)
        self.assertEqual(observation['elapsedSeconds'], 0)
        self.assertEqual(observation['attemptCount'], 1)

    def test_retry_wait_equals_actual_sleep(self):
        ticks, slept = [0.0], []

        def sleeper(seconds):
            slept.append(seconds)
            ticks[0] += seconds

        transport = Transport(60, clock=lambda: ticks[0], sleeper=sleeper, now=lambda: NOW)
        with patch.object(transport, 'send',
                          side_effect=[(429, {}, {}), (200, {}, {'result': []})]), \
             patch.object(transport, 'retry_delay', return_value=2):
            transport.request('GET', '/api/v1/stocks/000001/warnings', token='fake')
        observation = transport.observations[0]
        self.assertTrue(observation['success'])
        self.assertEqual(observation['attemptCount'], 2)
        self.assertEqual(observation['retryWaitSeconds'], 2)
        self.assertEqual(observation['retryWaitSeconds'], slept[0])
        self.assertEqual(observation['elapsedSeconds'], 2)

    def test_interrupted_pause_records_actual_only(self):
        ticks = [5.0]

        def sleeper(seconds):
            raise KeyboardInterrupt()

        transport = Transport(60, clock=lambda: ticks[0], sleeper=sleeper, now=lambda: NOW)
        with patch.object(transport, 'send', return_value=(429, {}, {})), \
             patch.object(transport, 'retry_delay', return_value=2), \
             self.assertRaises(KeyboardInterrupt):
            transport.request('GET', '/api/v1/stocks/000001/warnings', token='fake')
        observation = transport.observations[0]
        self.assertFalse(observation['success'])
        self.assertEqual(observation['errorCode'], 'interrupted')
        self.assertEqual(observation['retryWaitSeconds'], 0)

    def test_auth_observations_stay_separate_from_get(self):
        ticks = [0.0]

        def sleeper(seconds):
            ticks[0] += seconds

        transport = Transport(60, clock=lambda: ticks[0], sleeper=sleeper, now=lambda: NOW)

        class AuthRecoveryStub:
            def __init__(self, inner_transport):
                self.transport = inner_transport

            def get(self, path, **params):
                self.transport.request('POST', '/oauth2/token', form={'grant_type': 'x'})
                return self.transport.request('GET', path, params, 'tok-SECRET-xyz')['result']

        stub = AuthRecoveryStub(transport)
        recorder = RecordingClient(stub, now=lambda: NOW, clock=lambda: ticks[0])
        send_plan = [(200, {}, {'access_token': 't', 'token_type': 'bearer', 'expires_in': 3600}),
                     (500, {}, {}), (200, {}, {'result': ['ok']})]
        with patch.object(transport, 'send', side_effect=send_plan), \
             patch.object(transport, 'retry_delay', return_value=1):
            value = recorder.get('/api/v1/stocks/000001/warnings')
        self.assertEqual(value, ['ok'])
        record = recorder.records[0]
        # GET record holds only GET attempts (1 failed + 1 retried), not AUTH.
        self.assertEqual(record['attemptCount'], 2)
        self.assertEqual(record['endpointGroup'], 'STOCK')
        auth_obs = [o for o in transport.observations if o.get('path') == '/oauth2/token']
        self.assertEqual(len(auth_obs), 1)
        self.assertEqual(auth_obs[0]['endpointGroup'], 'AUTH')
        self.assertNotIn('tok-SECRET-xyz', json.dumps(recorder.records, default=str))

    def test_zero_attempts_preserved_for_presend_failure(self):
        class NoSendClient:
            def __init__(self):
                self.transport = Transport(60, clock=lambda: 9.0, sleeper=lambda s: None,
                                           now=lambda: NOW)
                self.transport.observations = []

            def get(self, path, **params):
                raise TmonError('unsupported-endpoint', 'blocked', 2)

        recorder = RecordingClient(NoSendClient(), now=lambda: NOW, clock=lambda: 9.0)
        with self.assertRaises(TmonError):
            recorder.get('/api/v1/orders')
        record = recorder.records[0]
        self.assertFalse(record['success'])
        self.assertEqual(record['attemptCount'], 0)
        self.assertEqual(record['errorCode'], 'unsupported-endpoint')

    def test_malicious_code_and_message_never_stored(self):
        class EvilClient:
            def get(self, path, **params):
                raise TmonError('../../EVIL CODE!!', 'raw secret TOKEN-ABC-123')

        recorder = RecordingClient(EvilClient(), now=lambda: NOW, clock=lambda: 3.0)
        with self.assertRaises(TmonError):
            recorder.get('/api/v1/prices', symbols='000001')
        record = recorder.records[0]
        self.assertEqual(record['errorCode'], 'api-error')
        blob = json.dumps(record, default=str)
        self.assertNotIn('EVIL', blob)
        self.assertNotIn('TOKEN-ABC-123', blob)

        ticks = [0.0]
        transport = Transport(60, clock=lambda: ticks[0], sleeper=lambda s: None, now=lambda: NOW)
        with patch.object(transport, 'send',
                          side_effect=TmonError('BAD CODE!!', 'provider raw exploded')):
            with self.assertRaises(TmonError):
                transport.request('GET', '/api/v1/stocks/000001/warnings', token='fake')
        observation = transport.observations[0]
        self.assertEqual(observation['errorCode'], 'api-error')
        self.assertNotIn('exploded', json.dumps(observation, default=str))


class ClassificationTests(unittest.TestCase):
    def test_expected_listing_stays_ok(self):
        client = FakeClient()
        original = client.get

        def get(path, **params):
            raw = original(path, **params)
            if path.endswith('/stocks'):
                raw[0]['listDate'] = (NOW - timedelta(days=3)).date().isoformat()
            return raw

        client.get = get
        code, result, engine = run_engine(client)
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['meta']['outcomeReason'], 'no-match')
        self.assertEqual(result['meta']['expectedIneligibleCount'], 1)
        self.assertEqual(result['meta']['candidateDataUnavailableCount'], 0)
        self.assertEqual(result['meta']['executionStatus'], 'completed')
        self.assertEqual(result['meta']['coverageStatus'], 'complete')

    def test_generic_short_history_is_unavailable_not_expected(self):
        client = FakeClient()
        original = client.get

        def get(path, **params):
            raw = original(path, **params)
            if path.endswith('/candles') and params.get('interval') == '1d':
                raw['candles'] = [days()[-1]]
            return raw

        client.get = get
        code, result, _ = run_engine(client)
        self.assertEqual(code, 5)
        self.assertNotEqual(result['meta']['outcomeReason'], 'no-match')
        self.assertEqual(result['meta']['expectedIneligibleCount'], 0)
        self.assertEqual(result['meta']['candidateDataUnavailableCount'], 1)

    def test_stale_daily_incomplete_and_zero_baseline_stay_unavailable(self):
        # Rejection behavior (not set membership): each generic failure
        # classifies as data, never as expected ineligibility.
        with tempfile.TemporaryDirectory() as td:
            engine = Engine(load_config(research='off'), 'day', 3, FakeClient(),
                            Store(Path(td).resolve()), now=lambda: NOW)
            for code_name in ('insufficient-history', 'stale-daily', 'incomplete-bars',
                              'zero-volume-baseline'):
                error = TmonError(code_name, 'probe')
                self.assertFalse(is_expected_ineligible(error))
                engine.reject('000001', error, 'screen')
        excluded = engine.inputs['excluded']
        self.assertTrue(all(r['kind'] == 'data' for r in excluded))
        self.assertTrue(all(not r['expectedIneligible'] for r in excluded))
        self.assertEqual(engine.expected_count(), 0)
        recent = NoMatch('listing-history-too-short')
        recent.details = {'listDate': '2026-09-03', 'requiredDailyBars': 20, 'maxPossibleDailyBars': 1}
        self.assertTrue(is_expected_ineligible(recent))
        bare = NoMatch('listing-history-too-short')
        self.assertFalse(is_expected_ineligible(bare))

    def test_all_unknown_cannot_be_no_match(self):
        client = FakeClient()
        client.stale = True  # every candidate hits stale quote data
        code, result, _ = run_engine(client)
        self.assertEqual(code, 5)
        self.assertNotEqual(result['meta']['outcomeReason'], 'no-match')
        self.assertGreater(result['meta']['candidateDataUnavailableCount'], 0)
        self.assertEqual(result['meta']['coverageStatus'], 'partial')

    def test_repeated_data_exclusions_count_one_symbol(self):
        # Distinct-symbol semantics: two failure events for one candidate count
        # once in candidateDataUnavailableCount while excludedCounts keeps both.
        with tempfile.TemporaryDirectory() as td:
            engine = Engine(load_config(research='off'), 'day', 3, FakeClient(),
                            Store(Path(td).resolve()), now=lambda: NOW)
            engine.reject('000001', TmonError('stale-daily', 'probe'), 'screen')
            engine.reject('000001', TmonError('no-data', 'probe'), 'final')
            counts = dict(__import__('collections').Counter(
                r['reason'] for r in engine.inputs['excluded']))
        self.assertEqual(engine.unavailable_count(), 1)
        self.assertEqual(counts, {'stale-daily': 1, 'no-data': 1})


class FatalSnapshotTests(unittest.TestCase):
    def test_auth_failure_preserves_earlier_pass_on_disk(self):
        # PR0 contract: an ALREADY screened pass is preserved when a LATER
        # candidate fails on daily/minute/screen API. The failure happens
        # during screening (after the first screen pass), not during the
        # precheck phase.
        class AuthDeathClient(MultiClient):
            def get(self, path, **params):
                if path.endswith('/candles') and params.get('symbol') == '000002':
                    raise TmonError('auth-expired-test', 'auth boom', 3)
                return super().get(path, **params)

        with tempfile.TemporaryDirectory() as td:
            engine = Engine(load_config(research='off'), 'day', 3,
                            AuthDeathClient(['000001', '000002']),
                            Store(Path(td).resolve()), now=lambda: NOW)
            result = envelope('recommend')
            code = engine.run(result)
            self.assertEqual(code, 3)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(len(engine.inputs['screened']), 1)
            self.assertEqual(engine.inputs['screened'][0]['symbol'], '000001')
            self.assertEqual(result['meta']['quantitativePassCount'], 1)
            self.assertEqual(result['meta']['screenedCount'], 1)
            persisted = json.loads((Path(result['meta']['recordPath']).parent / 'inputs.json').read_text())
            self.assertEqual(len(persisted['screened']), 1)
            self.assertEqual(persisted['screened'][0]['symbol'], '000001')
            self.assertIn('signalInputHash', persisted['screened'][0])

    def test_interrupt_preserves_earlier_passes(self):
        def abort(*args, **kwargs):
            raise KeyboardInterrupt()

        with tempfile.TemporaryDirectory() as td:
            engine = Engine(load_config(research='off'), 'day', 3, FakeClient(),
                            Store(Path(td).resolve()), now=lambda: NOW, researcher=abort)
            result = envelope('recommend')
            code = engine.run(result)
            self.assertEqual(code, 130)
            self.assertEqual(len(engine.inputs['screened']), 1)
            self.assertEqual(result['meta']['quantitativePassCount'], 1)

    def test_precheck_auth_failure_propagates_without_chart_calls(self):
        # Fatal auth during precheck happens before any screening, so no
        # screened row exists yet. It must propagate immediately with no
        # subsequent chart/current-data calls and no deferred control flow.
        class PrecheckAuthDeathClient(MultiClient):
            def get(self, path, **params):
                if path.endswith('/stocks') and '000002' in params.get('symbols', '').split(','):
                    raise TmonError('auth-expired-test', 'auth boom', 3)
                return super().get(path, **params)

        with tempfile.TemporaryDirectory() as td:
            engine = Engine(load_config(research='off'), 'day', 3,
                            PrecheckAuthDeathClient(['000001', '000002']),
                            Store(Path(td).resolve()), now=lambda: NOW)
            result = envelope('recommend')
            code = engine.run(result)
            self.assertEqual(code, 3)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(len(engine.inputs['screened']), 0)
            self.assertEqual(result['meta']['quantitativePassCount'], 0)
            self.assertEqual(result['meta']['screenedCount'], 0)
            chart_calls = [c for c in engine.client.records if c['path'] == '/api/v1/candles']
            self.assertEqual(chart_calls, [])
            # No current-data calls after the fatal precheck either.
            for forbidden in ('/api/v1/prices', '/api/v1/orderbook', '/api/v1/price-limits'):
                self.assertEqual([c for c in engine.client.records if c['path'] == forbidden], [])


class RankingCoverageTests(unittest.TestCase):
    def test_partial_ranking_marks_coverage_partial(self):
        class GappyRankingClient(FakeClient):
            def get(self, path, **params):
                if path.endswith('/rankings') and params.get('type') == 'TOP_GAINERS':
                    raise TmonError('network-error', 'ranking down', 4)
                return super().get(path, **params)

        code, result, _ = run_engine(GappyRankingClient())
        # Old contract preserved: partial status with exit code 5.
        self.assertEqual(code, 5)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(any(w['code'] == 'ranking-unavailable' for w in result['warnings']))
        self.assertEqual(result['meta']['coverageStatus'], 'partial')

    def test_research_only_unavailable_keeps_coverage_complete(self):
        def fail(*args, **kwargs):
            return {'000001': unavailable()}, {'status': 'unavailable'}

        code, result, _ = run_engine(FakeClient(), researcher=fail)
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['meta']['coverageStatus'], 'complete')
        self.assertEqual(result['meta']['candidateDataUnavailableCount'], 0)


class PersistedTransportLedgerTests(unittest.TestCase):
    def test_inputs_json_keeps_auth_and_get_work(self):
        ticks = [0.0]
        transport = Transport(180, clock=lambda: ticks[0],
                              sleeper=lambda s: ticks.__setitem__(0, ticks[0] + s),
                              now=lambda: NOW)
        fallback = FakeClient()

        class RoutedTransportClient:
            def __init__(self, inner_transport, inner_fallback):
                self.transport = inner_transport
                self._fallback = inner_fallback

            def get(self, path, **params):
                if path.endswith('/warnings'):
                    self.transport.request('POST', '/oauth2/token', form={'grant_type': 'x'})
                    return self.transport.request('GET', path, params, 'tok-SECRET-xyz')['result']
                return self._fallback.get(path, **params)

        get_calls = [0]

        def send(method, path, headers, body=None):
            if method == 'POST':
                return (200, {}, {'ok': 1})
            get_calls[0] += 1
            if get_calls[0] % 2 == 1:
                return (500, {}, {})
            return (200, {}, {'result': []})

        with tempfile.TemporaryDirectory() as td:
            engine = Engine(load_config(research='off'), 'day', 3,
                            RoutedTransportClient(transport, fallback),
                            Store(Path(td).resolve()), now=lambda: NOW,
                            clock=lambda: ticks[0])
            result = envelope('recommend')
            with patch.object(transport, 'send', side_effect=send):
                code = engine.run(result)
            self.assertEqual(code, 0)
            persisted = json.loads((Path(result['meta']['recordPath']).parent / 'inputs.json').read_text())
        warnings_records = [r for r in persisted['requests'] if r['path'].endswith('/warnings')]
        self.assertEqual(len(warnings_records), 2)  # screening + final state refresh
        for record in warnings_records:
            self.assertTrue(record['success'])
            self.assertEqual(record['attemptCount'], 2)  # target GET attempts only
            self.assertEqual(record['endpointGroup'], 'STOCK')
            nested = record['transportRequests']
            self.assertEqual(len(nested), 2)
            by_path = {n['path']: n for n in nested}
            self.assertEqual(by_path['/oauth2/token']['endpointGroup'], 'AUTH')
            self.assertEqual(by_path['/oauth2/token']['attemptCount'], 1)
            self.assertEqual(by_path[record['path']]['attemptCount'], 2)
            blob = json.dumps(record, default=str)
            self.assertNotIn('tok-SECRET-xyz', blob)
            self.assertNotIn('grant_type', blob)
            self.assertNotIn('Authorization', blob)
        meta = result['meta']
        # Logical aggregates stay GET-only for compatibility...
        self.assertNotIn('/oauth2/token', meta['endpointCounts'])
        self.assertNotIn('AUTH', meta['endpointGroupCounts'])
        # ...while actual aggregates include AUTH work: 2 AUTH attempts and
        # Both state checks are logical GET calls; AUTH transport work is
        # counted separately from the compatibility endpoint aggregates.
        self.assertEqual(meta['actualAttemptsByEndpoint']['/oauth2/token'], 2)
        self.assertEqual(meta['actualAttemptsByEndpoint']['/api/v1/stocks/000001/warnings'], 4)
        self.assertEqual(meta['actualAttemptsByGroup']['AUTH'], 2)

    def test_auth_failure_before_get_persists_auth_entry(self):
        transport = Transport(60, clock=lambda: 0.0, sleeper=lambda s: None, now=lambda: NOW)

        class AuthFailStub:
            def __init__(self, inner_transport):
                self.transport = inner_transport

            def get(self, path, **params):
                self.transport.request('POST', '/oauth2/token', form={'grant_type': 'x'})
                return self.transport.request('GET', path, params, 'tok')['result']

        recorder = RecordingClient(AuthFailStub(transport), now=lambda: NOW, clock=lambda: 0.0)
        with patch.object(transport, 'send',
                          side_effect=TmonError('network-error', 'down', 4, True)):
            with self.assertRaises(TmonError):
                recorder.get('/api/v1/stocks/000001/warnings')
        record = recorder.records[0]
        self.assertFalse(record['success'])
        self.assertEqual(record['attemptCount'], 0)  # no GET send happened
        self.assertEqual(record['errorCode'], 'network-error')
        self.assertNotIn('result', record)
        self.assertEqual(len(record['transportRequests']), 1)
        self.assertEqual(record['transportRequests'][0]['path'], '/oauth2/token')
        self.assertFalse(record['transportRequests'][0]['success'])
        counts = request_counts(recorder.records)
        self.assertEqual(counts['byEndpoint'], {'/api/v1/stocks/000001/warnings': 1})
        self.assertEqual(counts['actualAttemptsByEndpoint'], {'/oauth2/token': 1})

    def test_interrupted_retry_persists_safe_record(self):
        ticks = [10.0]

        def sleeper(seconds):
            ticks[0] += 0.25
            raise KeyboardInterrupt()

        transport = Transport(60, clock=lambda: ticks[0], sleeper=sleeper, now=lambda: NOW)

        class OneShotStub:
            def __init__(self, inner_transport):
                self.transport = inner_transport

            def get(self, path, **params):
                return self.transport.request('GET', path, params, 'tok')['result']

        recorder = RecordingClient(OneShotStub(transport), now=lambda: NOW, clock=lambda: ticks[0])
        with patch.object(transport, 'send', return_value=(500, {}, {})), \
             patch.object(transport, 'retry_delay', return_value=5), \
             self.assertRaises(KeyboardInterrupt):
            recorder.get('/api/v1/stocks/000001/warnings')
        # Exactly one transport observation with the actual 0.25 wait.
        self.assertEqual(len(transport.observations), 1)
        observation = transport.observations[0]
        self.assertFalse(observation['success'])
        self.assertEqual(observation['errorCode'], 'interrupted')
        self.assertEqual(observation['retryWaitSeconds'], 0.25)
        # Exactly one persisted safe logical record, no fabricated market data.
        self.assertEqual(len(recorder.records), 1)
        record = recorder.records[0]
        self.assertFalse(record['success'])
        self.assertEqual(record['errorCode'], 'interrupted')
        self.assertNotIn('result', record)
        self.assertEqual(len(record['transportRequests']), 1)
        self.assertEqual(record['transportRequests'][0]['retryWaitSeconds'], 0.25)

    def test_engine_interrupt_persists_nested_observations(self):
        ticks = [0.0]

        def sleeper(seconds):
            ticks[0] += 0.25
            raise KeyboardInterrupt()

        transport = Transport(180, clock=lambda: ticks[0], sleeper=sleeper, now=lambda: NOW)
        fallback = FakeClient()

        class InterruptStub:
            def __init__(self, inner_transport, inner_fallback):
                self.transport = inner_transport
                self._fallback = inner_fallback

            def get(self, path, **params):
                if path.endswith('/rankings'):
                    return self.transport.request('GET', path, params, 'tok')['result']
                return self._fallback.get(path, **params)

        with tempfile.TemporaryDirectory() as td:
            engine = Engine(load_config(research='off'), 'day', 3,
                            InterruptStub(transport, fallback),
                            Store(Path(td).resolve()), now=lambda: NOW,
                            clock=lambda: ticks[0])
            result = envelope('recommend')
            with patch.object(transport, 'send', return_value=(500, {}, {})):
                code = engine.run(result)
            self.assertEqual(code, 130)
            interrupted = [r for r in engine.client.records if not r['success']]
            self.assertEqual(len(interrupted), 1)
            record = interrupted[0]
            self.assertEqual(record['errorCode'], 'interrupted')
            self.assertNotIn('result', record)
            self.assertEqual(len(record['transportRequests']), 1)
            self.assertEqual(record['transportRequests'][0]['retryWaitSeconds'], 0.25)


if __name__ == '__main__':
    unittest.main()
