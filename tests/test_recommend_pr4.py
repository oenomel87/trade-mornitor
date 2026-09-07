"""PR4-A Engine integration: batch prechecks and coverage accounting."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from tests.test_recommend import FakeClient, NOW, stock
from tmon.cli import envelope
from tmon.errors import TmonError
from tmon.recommend import Engine
from tmon.recommend_config import load_config
from tmon.recommend_store import Store


class BatchEngineClient(FakeClient):
    """Fake provider that returns every requested stock row by symbol."""

    def __init__(self, symbols, *, etf=(), kdaq=(), clock=None, auth=False):
        super().__init__()
        self.symbols = list(symbols)
        self.etf = set(etf)
        self.kdaq = set(kdaq)
        self.fake_clock = clock
        self.auth = auth
        self.stock_batches = []
        self.indicator_markets = []

    def get(self, path, **params):
        if path.endswith('/rankings'):
            # For the large-union case, make each ranking endpoint contribute
            # a distinct slice so the union really exceeds one 200-row stock
            # batch.  Small fixtures retain the same ordered rows everywhere.
            self.calls.append((path, params))
            offsets = {
                'MARKET_TRADING_AMOUNT': 0,
                'MARKET_TRADING_VOLUME': 50,
                'TOP_GAINERS': 101,
            }
            offset = offsets.get(params.get('type'), 0) if len(self.symbols) > 100 else 0
            ranked_symbols = self.symbols[offset:offset + params['count']]
            return {'rankedAt': NOW.isoformat(), 'rankings': [
                {'rank': i + 1, 'symbol': symbol, 'currency': 'KRW',
                 'price': {'lastPrice': '101.2', 'basePrice': '100', 'changeRate': '0.012'},
                 'tradingVolume': '100000000', 'tradingAmount': '10000000000'}
                for i, symbol in enumerate(ranked_symbols)
            ]}
        if path == '/api/v1/stocks':
            requested = params['symbols'].split(',')
            self.stock_batches.append(requested)
            if self.auth:
                raise TmonError('auth-failure', 'auth', 3)
            if self.fake_clock is not None:
                self.fake_clock[0] = 81.0
            return [dict(stock(symbol),
                         market='KOSDAQ' if symbol in self.kdaq else 'KOSPI',
                         securityType='ETF' if symbol in self.etf else 'STOCK',
                         isCommonShare=False if symbol in self.etf else True)
                    for symbol in requested]
        if 'market-indicators' in path:
            self.indicator_markets.append(path.split('/')[4])
        if path.endswith('/prices'):
            self.calls.append((path, params))
            return [{'symbol': symbol, 'currency': 'KRW', 'lastPrice': self.price,
                     'timestamp': NOW.isoformat()}
                    for symbol in params['symbols'].split(',')]
        return super().get(path, **params)


def run_engine(client, config, *, limit=1, clock=None):
    with tempfile.TemporaryDirectory() as directory:
        engine = Engine(config, 'day', limit, client,
                        Store(Path(directory).resolve()), now=lambda: NOW,
                        clock=(lambda: clock[0]) if clock is not None else __import__('time').monotonic,
                        researcher=lambda rows, *args, **kwargs:
                        ({row['symbol']: {} for row in rows}, {'status': 'ok'}))
        result = envelope('recommend')
        code = engine.run(result)
        return code, result, engine


class PrecheckEngineTests(unittest.TestCase):
    def test_cap_is_after_prefilter_and_selected_rows_are_screened(self):
        config = load_config(research='off')
        config['universe'].update(detailLimit=2, researchLimit=1)
        client = BatchEngineClient(['ETF', 'A', 'B'], etf=['ETF'])
        code, result, engine = run_engine(client, config)

        self.assertEqual(code, 0)
        self.assertEqual(result['meta']['universeCount'], 3)
        self.assertEqual(result['meta']['preExcludedCount'], 1)
        self.assertEqual(result['meta']['precheckDataUnavailable'], 0)
        self.assertEqual(result['meta']['detailSelectedCount'], 2)
        self.assertEqual(result['meta']['screenedCount'], 2)
        self.assertEqual([row['symbol'] for row in result['data']], ['A'])
        self.assertEqual([row['symbol'] for row in engine.inputs['screened']], ['A', 'B'])
        self.assertEqual(client.stock_batches[0],
                         ['ETF', 'A', 'B'])
        self.assertEqual(result['meta']['universePolicy'],
                         'ranking-union-batch-prefilter-detail-limit-v1')
        self.assertEqual(result['data'][0]['universePolicy'],
                         'ranking-union-batch-prefilter-detail-limit-v1')
        screen = result['meta']['evaluationSummary']['screening']
        self.assertEqual(screen['evaluatedCount'], 3)
        self.assertEqual(screen['detailEvaluatedCount'], 2)
        self.assertEqual(screen['conditionExcludedCount'], 1)
        self.assertEqual(screen['dataUnavailableCount'], 0)

    def test_large_union_batches_and_index_work_follow_selected_market_only(self):
        # Three 100-row ranking responses form a 201-symbol union.  Only the
        # first detail target reaches charts/current state, while stock
        # precheck still covers the complete union in 200+1 chunks.
        symbols = ['S%03d' % i for i in range(201)]
        client = BatchEngineClient(symbols, kdaq=['S051'])
        config = load_config(research='off')
        config['universe'].update(detailLimit=1, researchLimit=1, rankCount=100)
        code, result, engine = run_engine(client, config)
        self.assertEqual(code, 0)
        self.assertEqual([len(batch) for batch in client.stock_batches[:2]], [200, 1])
        self.assertEqual(result['meta']['universeCount'], 201)
        self.assertEqual(result['meta']['detailSelectedCount'], 1)
        self.assertEqual(result['meta']['screenedCount'], 1)
        self.assertEqual([row['symbol'] for row in engine.inputs['screened']], ['S050'])
        self.assertEqual(engine.stock_raw['S051'][0]['market'], 'KOSDAQ')
        self.assertNotIn('KOSDAQ', client.indicator_markets)
        self.assertEqual(len([r for r in engine.client.records
                              if r['path'] == '/api/v1/stocks' and r['phase'] == 'prefetch']), 2)
        self.assertEqual(len([r for r in engine.client.records
                              if r['path'] == '/api/v1/stocks' and r['phase'] == 'screen']), 0)

    def test_precheck_data_failure_is_separate_and_cannot_be_no_match(self):
        class BrokenClient(BatchEngineClient):
            def get(self, path, **params):
                if path == '/api/v1/stocks':
                    self.stock_batches.append(params['symbols'].split(','))
                    return {'wrong': 'shape'}
                return super().get(path, **params)

        config = load_config(research='off')
        code, result, engine = run_engine(BrokenClient(['A']), config)
        self.assertEqual(code, 5)
        self.assertEqual(result['status'], 'error')
        self.assertNotEqual(result['meta']['outcomeReason'], 'no-match')
        self.assertEqual(result['meta']['preExcludedCount'], 0)
        self.assertEqual(result['meta']['precheckDataUnavailable'], 1)
        self.assertEqual(result['meta']['candidateDataUnavailableCount'], 1)
        self.assertEqual(result['meta']['evaluationSummary']['screening']['dataUnavailableCount'], 1)
        self.assertEqual(engine.inputs['excluded'], [])

    def test_auth_failure_is_fatal_before_screening(self):
        client = BatchEngineClient(['A', 'B'], auth=True)
        config = load_config(research='off')
        code, result, engine = run_engine(client, config)
        self.assertEqual(code, 3)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(engine.inputs['screened'], [])
        self.assertEqual([r['path'] for r in engine.client.records
                          if r['path'].endswith('/candles')], [])

    def test_budget_stop_skips_remaining_batches_and_preserves_partial_status(self):
        clock = [0.0]
        symbols = ['S%03d' % i for i in range(201)]
        client = BatchEngineClient(symbols, clock=clock)
        config = load_config(research='off')
        code, result, engine = run_engine(client, config, clock=clock)
        self.assertEqual(code, 5)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(client.stock_batches), 1)
        self.assertTrue(any(row['reason'] == 'not-evaluated-budget'
                            for row in result['meta']['notEvaluated']))
        self.assertEqual(result['meta']['precheckDataUnavailable'], 0)
        self.assertNotEqual(result['meta']['outcomeReason'], 'no-match')

    def test_error_details_cannot_override_exclusion_classification(self):
        config = load_config(research='off')
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(config, 'day', 1, BatchEngineClient(['A']),
                            Store(Path(directory).resolve()), now=lambda: NOW)
            error = TmonError('adjustment-basis-unverified', 'data')
            error.details = {'reason': 'window-values-differ', 'symbol': 'WRONG',
                             'kind': 'condition', 'stage': 'wrong'}
            engine.reject('A', error, 'screen')
        record = engine.inputs['excluded'][0]
        self.assertEqual({record['symbol'], record['reason'], record['kind'], record['stage']},
                         {'A', 'adjustment-basis-unverified', 'data', 'screen'})
        self.assertEqual(record['details']['reason'], 'window-values-differ')


if __name__ == '__main__':
    unittest.main()
