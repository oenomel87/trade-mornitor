"""PR3 contract tests for frozen recommendation signals and final revalidation."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tmon.cli import envelope
from tmon.recommend import Engine
from tmon.recommend_config import load_config
from tmon.recommend_store import Store
from tmon.strategies import signal as strategy_signal

from tests.test_recommend import FakeClient, NOW, START, book, cal, candle, days, minutes, stock


KST = timezone(timedelta(hours=9))


def extended_minutes():
    """Keep the source fresh while the frozen phase stays at 10:00."""
    rows = minutes()
    for i in range(60, 65):
        t = START + timedelta(minutes=i)
        rows.append(candle(t, '102', '101.4', '102.1', '200000', op='101.5'))
    return rows


def index_minutes():
    """A valid 1m MarketIndicatorCandle source for the PR2 comparison range."""
    rows = []
    for i in range(65):
        t = START + timedelta(minutes=i)
        close = '1010' if i == 59 else '1000'
        rows.append({'timestamp': t.isoformat(), 'openPrice': '1000',
                     'highPrice': '1015', 'lowPrice': '995',
                     'closePrice': close, 'volume': '100000'})
    return rows


def index_daily():
    """A valid daily index source covering the swing calendar chain."""
    rows = []
    for row in days():
        rows.append({key: value for key, value in row.items() if key != 'currency'})
    return rows


def research_ok(candidates, *_args, **_kwargs):
    """Return a valid enough research result without starting a worker."""
    return {
        candidate['symbol']: {
            'researchStatus': 'verified',
            'summary': '검증된 조사 결과',
            'catalysts': [],
            'counterEvidence': [],
            'upcomingEvents': [],
            'sources': [],
        }
        for candidate in candidates
    }, {'status': 'ok'}


class SnapshotClient(FakeClient):
    """FakeClient-shaped provider with controllable observations and clocks."""

    def __init__(self, wall=None, mono=None, *, step=0.0, horizon='day',
                 price='101.2', bid='101.1', extended=False):
        super().__init__()
        self.wall = [wall or NOW]
        self.mono = [mono if mono is not None else 100.0]
        self.step = step
        self.horizon = horizon
        self.price = price
        self.bid = bid
        self.extended = extended
        self.daily_rows = days()
        self.index_rows = index_minutes() if horizon == 'day' else index_daily()

    def now(self):
        return self.wall[0]

    def clock(self):
        return self.mono[0]

    def get(self, path, **params):
        if self.step:
            self.wall[0] += timedelta(seconds=self.step)
            self.mono[0] += self.step
        self.calls.append((path, params))
        now = self.wall[0]

        if path.endswith('/market-calendar/KR'):
            requested = params.get('date')
            if requested:
                requested_at = datetime.fromisoformat(requested).replace(hour=10, tzinfo=KST)
                return cal(requested_at)
            return cal(now)
        if path.endswith('/rankings'):
            return {'rankedAt': now.isoformat(), 'rankings': [
                {'rank': 1, 'symbol': '000001', 'currency': 'KRW',
                 'price': {'lastPrice': self.price, 'basePrice': '100',
                           'changeRate': '0.012'},
                 'tradingVolume': '100000000',
                 'tradingAmount': '10000000000'}]}
        if path.endswith('/warnings'):
            return list(self.warning)
        if path.endswith('/stocks'):
            return [stock(sym, nxt=self.nxt) for sym in params['symbols'].split(',')]
        if path.endswith('/prices'):
            return [{'symbol': '000001', 'currency': 'KRW', 'lastPrice': self.price,
                     'timestamp': now.isoformat()}]
        if path.endswith('/orderbook'):
            current = book(now)
            current['asks'][0]['price'] = self.price
            current['bids'][0]['price'] = self.bid
            return current
        if path.endswith('/price-limits'):
            return {'timestamp': now.isoformat(), 'currency': 'KRW',
                    'upperLimitPrice': '130'}
        if 'market-indicators' in path:
            return {'candles': deepcopy(self.index_rows), 'nextBefore': None}
        if path.endswith('/candles'):
            rows = self.daily_rows if params.get('interval') == '1d' else (
                extended_minutes() if self.extended else minutes())
            return {'candles': list(reversed(deepcopy(rows))), 'nextBefore': None}
        raise AssertionError(path)


class SnapshotSignalContractTests(unittest.TestCase):
    def config(self, *, capital=None):
        config = load_config(research='off', capital=capital)
        # Keep the wall-clock age scenarios independent of the monotonic
        # pipeline budget and any incidental test-double request latency.
        config['budget'].update(totalSeconds=1000, dataSeconds=200,
                                researchSeconds=500, finalSeconds=200)
        return config

    def run_engine(self, client, *, horizon='day', config=None, researcher=research_ok):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(config or self.config(), horizon, 3, client,
                            Store(Path(directory).resolve()), now=client.now,
                            clock=client.clock, researcher=researcher)
            result = envelope('recommend')
            code = engine.run(result)
        return code, result, engine

    def test_research_crossing_five_minute_boundary_preserves_snapshot(self):
        client = SnapshotClient(
            wall=datetime(2026, 9, 4, 10, 4, 55, tzinfo=KST),
            mono=500.0, step=0.25, extended=True)
        original_wall = client.wall[0]

        def crossing_research(candidates, *_args, **_kwargs):
            # Move both clocks over the next five-minute wall boundary while
            # leaving enough monotonic budget for final revalidation.
            client.wall[0] = original_wall + timedelta(seconds=15)
            client.mono[0] += 15.0
            return research_ok(candidates)

        with patch('tmon.recommend.signal', wraps=strategy_signal) as signal_call:
            code, result, engine = self.run_engine(
                client, config=self.config(), researcher=crossing_research)

        self.assertEqual(code, 0)
        self.assertTrue(result['data'])
        self.assertEqual(signal_call.call_count, 1)
        screened = engine.inputs['screened'][0]
        final = result['data'][0]
        for field in ('signalId', 'signalInputHash', 'signalAsOf',
                      'signalBarStartAt', 'signalBarEndAt', 'signalClose',
                      'breakoutLevel', 'invalidation', 'volumeRatio',
                      'relativeReturnPct', 'relativeReturnWindow',
                      'priceAdjustmentPolicy'):
            self.assertEqual(final[field], screened[field], field)
        self.assertEqual(final['signalSnapshot'], screened['signalSnapshot'])
        self.assertGreater(client.wall[0], datetime(2026, 9, 4, 10, 5, 0, tzinfo=KST))
        final_records = [record for record in engine.client.records
                         if record['phase'] == 'final']
        # PR4-B verifies the unadjusted daily window immediately before the
        # final quote/state reads; it does not refetch the signal chart.
        raw_records = [record for record in final_records
                        if record['path'].endswith('/candles') and
                        record['params'].get('adjusted') == 'false']
        self.assertEqual(len(raw_records), 1)
        final_prices = [record for record in final_records
                        if record['path'].endswith('/prices')]
        self.assertTrue(final_prices)
        self.assertLess(engine.client.records.index(raw_records[0]),
                        engine.client.records.index(final_prices[0]))
        self.assertFalse(any('market-indicators' in record['path']
                             for record in final_records))
        index_records = [record for record in engine.client.records
                         if 'market-indicators' in record['path']]
        self.assertEqual(len(index_records), 1)
        self.assertEqual(index_records[0]['phase'], 'screen')

    def test_day_signal_age_boundary_uses_actual_final_wall_time(self):
        for age, valid in ((599, True), (600, False), (601, False)):
            with self.subTest(age=age):
                client = SnapshotClient(wall=NOW, mono=100.0)
                final_wall = datetime(2026, 9, 4, 10, 0, tzinfo=KST) + timedelta(seconds=age)

                def delayed_research(candidates, *_args, **_kwargs):
                    client.wall[0] = final_wall
                    return research_ok(candidates)

                code, result, engine = self.run_engine(
                    client, config=self.config(), researcher=delayed_research)
                final_prices = [record for record in engine.client.records
                                if record['phase'] == 'final' and record['path'].endswith('/prices')]
                if valid:
                    self.assertTrue(final_prices)
                    self.assertEqual(final_prices[-1]['result'][0]['timestamp'],
                                     final_wall.isoformat())
                    self.assertEqual(code, 0)
                    self.assertTrue(result['data'])
                    self.assertNotIn('signal-stale', result['meta']['excludedCounts'])
                    self.assertEqual(result['data'][0]['signalAgeSeconds'], age)
                else:
                    # An expired signal is rejected before raw-window and
                    # quote verification, so no final price request is made.
                    self.assertFalse(final_prices)
                    self.assertEqual(code, 0)
                    self.assertEqual(result['data'], [])
                    self.assertEqual(result['meta']['excludedCounts']['signal-stale'], 1)

    def test_final_observations_recompute_entry_fields_without_changing_screen(self):
        client = SnapshotClient(wall=NOW, price='101.2', bid='101.1')

        def changed_observations(candidates, *_args, **_kwargs):
            client.price = '101.95'
            client.bid = '101.85'
            return research_ok(candidates)

        code, result, engine = self.run_engine(
            client, config=self.config(capital='10050'), researcher=changed_observations)
        self.assertEqual(code, 0)
        self.assertTrue(result['data'])
        screened = engine.inputs['screened'][0]
        final = result['data'][0]
        self.assertEqual(screened['entryReference'], D('101.2'))
        self.assertEqual(screened['bestBid'], D('101.1'))
        self.assertEqual(final['entryReference'], D('101.95'))
        self.assertEqual(final['bestBid'], D('101.85'))
        for field in ('riskPerShare', 'riskPct', 'targetRange', 'quantity', 'spreadPct'):
            self.assertNotEqual(final[field], screened[field], field)
        for field in ('signalId', 'signalInputHash', 'breakoutLevel',
                      'invalidation', 'volumeRatio', 'relativeReturnPct',
                      'relativeReturnWindow'):
            self.assertEqual(final[field], screened[field], field)
        self.assertEqual(final['signalSnapshot'], screened['signalSnapshot'])

    def test_nested_research_mutation_cannot_change_authoritative_shortlist(self):
        client = SnapshotClient(wall=NOW)
        original_symbol = '000001'

        def mutating_research(candidates, *_args, **_kwargs):
            candidate = candidates[0]
            candidate['symbol'] = '999999'
            candidate['invalidation'] = D('1')
            candidate['signalSnapshot']['signal']['invalidation'] = D('1')
            candidate['relativeReturnWindow']['comparisonStartAt'] = 'mutated'
            candidate['relativeReturnWindow']['start'] = 'mutated'
            # The callback must still return research under the original key.
            return {original_symbol: {'researchStatus': 'verified',
                                      'summary': '원 종목 조사 결과'}}, {'status': 'ok'}

        code, result, engine = self.run_engine(
            client, config=self.config(), researcher=mutating_research)
        self.assertEqual(code, 0)
        self.assertTrue(result['data'])
        self.assertEqual([row['symbol'] for row in engine.inputs['screened']], [original_symbol])
        row = result['data'][0]
        self.assertEqual(row['symbol'], original_symbol)
        self.assertEqual(row['research']['summary'], '원 종목 조사 결과')
        self.assertEqual(row['invalidation'], engine.inputs['screened'][0]['invalidation'])
        self.assertEqual(row['signalSnapshot']['signal']['invalidation'],
                         engine.inputs['screened'][0]['signalSnapshot']['signal']['invalidation'])
        self.assertEqual(row['relativeReturnWindow'],
                         engine.inputs['screened'][0]['relativeReturnWindow'])

    def test_signal_id_ignores_runtime_inputs_but_tracks_signal_inputs_and_policy(self):
        def screen_id(client, config=None):
            _, _, engine = self.run_engine(client, config=config)
            return engine.inputs['screened'][0]['signalId']

        baseline = screen_id(SnapshotClient())
        runtime_config = self.config(capital='10050')
        runtime_config['maxLossPct'] = '3'
        runtime_config['day']['maxSpreadPct'] = '0.50'
        runtime_config['day']['maxBreakoutGapPct'] = '1.50'
        self.assertEqual(screen_id(SnapshotClient(), runtime_config), baseline)
        self.assertEqual(screen_id(SnapshotClient(price='101.95', bid='101.85')), baseline)

        corrected = SnapshotClient()
        corrected.daily_rows[-1]['closePrice'] = '102.1'
        self.assertNotEqual(screen_id(corrected), baseline)

        spelling = SnapshotClient()
        spelling.daily_rows[-1]['lowPrice'] = '100.0'
        self.assertEqual(screen_id(spelling), baseline)

        with patch('tmon.recommend.SIGNAL_PRICE_ADJUSTMENT_POLICY', 'raw'):
            self.assertNotEqual(screen_id(SnapshotClient()), baseline)

    def test_swing_previous_business_day_signal_survives_more_than_600_seconds(self):
        client = SnapshotClient(wall=NOW, mono=100.0, horizon='swing')
        final_wall = NOW + timedelta(seconds=601)

        def delayed_research(candidates, *_args, **_kwargs):
            client.wall[0] = final_wall
            return research_ok(candidates)

        code, result, engine = self.run_engine(
            client, horizon='swing', config=self.config(), researcher=delayed_research)
        self.assertEqual(code, 0)
        self.assertTrue(result['data'])
        screened = engine.inputs['screened'][0]
        final = result['data'][0]
        self.assertEqual(screened['signalSessionDate'], '2026-09-03')
        self.assertIsNone(screened['signalBarEndAt'])
        self.assertIsNone(final['signalBarEndAt'])
        self.assertIsNone(final['signalAgeSeconds'])
        self.assertEqual(final['signalId'], screened['signalId'])
        self.assertEqual(final['relativeReturnWindow'], screened['relativeReturnWindow'])
        self.assertNotIn('signal-stale', result['meta']['excludedCounts'])


if __name__ == '__main__':
    unittest.main()
