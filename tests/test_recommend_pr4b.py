"""PR4-B integration: shared calendar windows and conservative raw checks."""

from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest

from tmon.cli import envelope
from tmon.recommend import Engine
from tmon.recommend_config import config_hash, load_config
from tmon.recommend_data import benchmark_snapshot_hash
from tmon.recommend_store import Store

from tests.test_recommend import FakeClient, NOW, stock
from tests.test_recommend_pr4 import BatchEngineClient
from tests.test_recommend_snapshot_signals import SnapshotClient


class PR4BIntegrationTests(unittest.TestCase):
    def run_engine(self, client, horizon='day', config=None, limit=3):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(config or load_config(research='off'), horizon, limit, client,
                            Store(Path(directory).resolve()), now=getattr(client, 'now', lambda: NOW),
                            clock=getattr(client, 'clock', __import__('time').monotonic),
                            researcher=lambda rows, *args, **kwargs:
                            ({row['symbol']: {} for row in rows}, {'status': 'ok'}))
            result = envelope('recommend')
            code = engine.run(result)
        return code, result, engine

    def test_day_and_swing_request_exact_windows_and_record_verification(self):
        for horizon, expected in (('day', 20), ('swing', 65)):
            with self.subTest(horizon=horizon):
                client = SnapshotClient(horizon=horizon)
                code, result, engine = self.run_engine(client, horizon)
                self.assertEqual(code, 0)
                self.assertTrue(result['data'])
                self.assertEqual(result['meta']['tradingDateReference']['requiredDates'], expected)
                daily_requests = [record for record in engine.client.records
                                  if record['path'].endswith('/candles') and
                                  record['phase'] == 'screen' and
                                  record['params'].get('interval') == '1d' and
                                  record['params'].get('adjusted') == 'true']
                self.assertEqual(len(daily_requests), 1)
                self.assertEqual(daily_requests[0]['params']['count'], expected + 2)
                self.assertEqual(result['data'][0]['finalVerification']['status'], 'verified')
                self.assertEqual(result['data'][0]['finalVerification']['requiredDailyBars'], expected)
                self.assertIn('adjustmentChecks', result['meta'])

    def test_unknown_calendar_basis_is_explicit_and_no_weekday_fallback(self):
        class UnknownCalendar(SnapshotClient):
            def get(self, path, **params):
                value = super().get(path, **params)
                if path.endswith('/market-calendar/KR') and \
                        params.get('date') == NOW.date().isoformat():
                    value['previousBusinessDay']['date'] = '2026-09-05'
                return value

        code, result, engine = self.run_engine(UnknownCalendar())
        self.assertEqual(code, 5)
        self.assertEqual(result['meta']['excludedCounts']['daily-calendar-unverified'], 1)
        self.assertEqual(result['meta']['tradingDateReference']['verified'], False)
        self.assertEqual([r['params'].get('interval') for r in engine.client.records
                          if r['path'] == '/api/v1/candles'], [])

    def test_verified_trading_dates_make_holiday_listing_exclusion_expected(self):
        class ListedClient(BatchEngineClient):
            def __init__(self):
                super().__init__(['A', 'B'])

            def get(self, path, **params):
                value = super().get(path, **params)
                if path == '/api/v1/stocks':
                    for row in value:
                        row['listDate'] = '2026-08-14' if row['symbol'] == 'A' else '2020-01-01'
                return value

        config = load_config(research='off')
        config['universe'].update(detailLimit=1, researchLimit=1)
        code, result, engine = self.run_engine(ListedClient(), config=config, limit=1)
        self.assertEqual(code, 0)
        self.assertEqual(result['meta']['expectedIneligibleCount'], 1)
        self.assertEqual(result['meta']['preExcluded'][0]['symbol'], 'A')
        self.assertEqual(result['meta']['preExcluded'][0]['reason'],
                         'listing-history-too-short')
        self.assertEqual(result['meta']['screenedCount'], 1)
        self.assertEqual(result['data'][0]['symbol'], 'B')

    def test_stock_window_gap_is_not_filled(self):
        class MissingStockDay(SnapshotClient):
            def get(self, path, **params):
                value = super().get(path, **params)
                if path == '/api/v1/candles' and params.get('interval') == '1d':
                    value['candles'].pop(0)  # remove the newest expected day
                return value

        code, result, engine = self.run_engine(MissingStockDay())
        self.assertEqual(code, 5)
        self.assertEqual(result['meta']['excludedCounts']['daily-gap'], 1)
        self.assertEqual(result['meta']['dailyChecks']['000001']['reason'], 'daily-gap')
        self.assertEqual([record for record in engine.client.records
                          if record['path'] == '/api/v1/candles' and
                          record['params'].get('interval') == '1m'], [])

    def test_index_window_gap_keeps_index_leg_unavailable(self):
        class MissingIndexDay(SnapshotClient):
            def __init__(self):
                super().__init__(horizon='swing')

            def get(self, path, **params):
                value = super().get(path, **params)
                if 'market-indicators' in path:
                    value['candles'] = [row for row in value['candles']
                                        if not row['timestamp'].startswith('2026-08-28')]
                return value

        code, result, engine = self.run_engine(MissingIndexDay(), 'swing')
        self.assertEqual(code, 0)
        self.assertTrue(result['data'])
        self.assertEqual(engine.inputs['screened'][0]['relativeReturnReason'],
                         'index-bar-missing')
        self.assertEqual(result['data'][0]['finalVerification']['status'], 'verified')

    def test_researcher_config_mutation_cannot_change_engine_policy(self):
        config = load_config(research='off')
        expected_hash = config_hash(config)

        def mutating_research(rows, horizon, callback_config, *_args, **_kwargs):
            callback_config['capital'] = '999999999'
            callback_config[horizon]['minEstimatedAvgDailyAmount'] = '1'
            return ({row['symbol']: {} for row in rows}, {'status': 'ok'})

        client = SnapshotClient()
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(config, 'day', 1, client, Store(Path(directory).resolve()),
                            now=client.now, clock=client.clock,
                            researcher=mutating_research)
            result = envelope('recommend')
            self.assertEqual(engine.run(result), 0)
        self.assertTrue(result['data'])
        self.assertIsNone(result['data'][0]['quantity'])
        self.assertEqual(str(result['data'][0]['finalVerification']['minimumEstimatedAvgDailyAmount']),
                         config['day']['minEstimatedAvgDailyAmount'])
        self.assertEqual(result['meta']['configHash'], expected_hash)
        self.assertEqual(result['meta']['effectiveConfig']['capital'], config['capital'])
        self.assertEqual(result['meta']['effectiveConfig']['day']['minEstimatedAvgDailyAmount'],
                         config['day']['minEstimatedAvgDailyAmount'])

    def test_benchmark_hash_binds_comparison_window_and_ignores_receive_time(self):
        base = {'market': 'KOSPI', 'interval': '1d',
                'comparisonStartAt': '2026-08-27',
                'comparisonEndAt': '2026-09-03',
                'expectedTradingDates': ['2026-08-27', '2026-08-28',
                                         '2026-08-31', '2026-09-01',
                                         '2026-09-02', '2026-09-03'],
                'startValue': '1000', 'endValue': '1010',
                'priceBasis': 'provider-index-points',
                'stockPriceBasis': 'adjusted',
                'returnCalculation': 'close-to-close',
                'completionPolicyVersion': 'v1', 'receivedAt': '10:00'}
        later = {**base, 'receivedAt': '10:01'}
        shifted = {**base, 'expectedTradingDates': [*base['expectedTradingDates'][:2],
                                                     '2026-08-29',
                                                     *base['expectedTradingDates'][3:]]}
        self.assertEqual(benchmark_snapshot_hash(base), benchmark_snapshot_hash(later))
        self.assertNotEqual(benchmark_snapshot_hash(base), benchmark_snapshot_hash(shifted))

    def test_adjusted_price_mismatch_is_not_rescued_by_equal_volume(self):
        class PriceMismatch(SnapshotClient):
            def get(self, path, **params):
                value = super().get(path, **params)
                if path.endswith('/candles') and params.get('adjusted') == 'false':
                    value['candles'][0]['closePrice'] = '101'
                return value

        code, result, engine = self.run_engine(PriceMismatch())
        self.assertEqual(code, 5)
        self.assertEqual(result['data'], [])
        self.assertEqual(result['meta']['excludedCounts']['adjustment-basis-unverified'], 1)
        self.assertEqual(engine.inputs['adjustmentChecks'][0]['reason'],
                         'adjustment-basis-unverified')
        self.assertEqual(len(engine.inputs['screened']), 1)
        self.assertIn('rawEstimatedAvgDailyAmount', engine.inputs['adjustmentChecks'][0])

    def test_raw_low_amount_is_condition_after_successful_screen(self):
        class LowRaw(SnapshotClient):
            def get(self, path, **params):
                value = super().get(path, **params)
                if path.endswith('/candles') and params.get('adjusted') == 'false':
                    for row in value['candles']:
                        row['volume'] = '1'
                return value

        code, result, engine = self.run_engine(LowRaw())
        self.assertEqual(code, 5)
        self.assertEqual(result['data'], [])
        self.assertEqual(result['meta']['excludedCounts']['adjustment-basis-unverified'], 1)
        self.assertEqual(engine.inputs['adjustmentChecks'][0]['rawLiquidityCheck'], 'fail')

    def test_top_five_is_fixed_and_does_not_refill_after_raw_rejection(self):
        symbols = ['S%03d' % index for index in range(1, 7)]

        class MismatchOne(BatchEngineClient):
            def __init__(self):
                super().__init__(symbols)

            def get(self, path, **params):
                value = super().get(path, **params)
                if (path.endswith('/candles') and params.get('adjusted') == 'false' and
                        params.get('symbol') == 'S001'):
                    value['candles'][0]['closePrice'] = '101'
                return value

        config = load_config(research='off')
        config['universe'].update(detailLimit=6, researchLimit=5)
        code, result, engine = self.run_engine(MismatchOne(), config=config, limit=5)
        self.assertEqual(code, 5)
        self.assertEqual(len(engine.inputs['screened']), 6)
        self.assertEqual(len(engine.inputs['adjustmentChecks']), 5)
        raw_symbols = [record['params'].get('symbol') for record in engine.client.records
                       if record['path'].endswith('/candles') and
                       record['params'].get('adjusted') == 'false']
        self.assertNotIn('S006', raw_symbols)
        self.assertEqual(result['meta']['excludedCounts']['adjustment-basis-unverified'], 1)


if __name__ == '__main__':
    unittest.main()
