"""PR3 policy, arithmetic, and storage-boundary regressions."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from tmon.cli import envelope, main, render_text
from tmon.errors import TmonError
from tmon.recommend_config import load_config
from tmon.recommend_store import Store
from tmon.strategies import signal


KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 4, 10, 0, 10, tzinfo=KST)


class ConfigAndStrategyTests(unittest.TestCase):
    def test_frozen_policy_defaults_and_integer_signal_age(self):
        config = load_config()
        self.assertEqual(config['day']['maxSignalAgeSeconds'], 600)
        self.assertEqual(config['signalPolicy'], 'frozen-current-state-v2')
        self.assertEqual(config['relativeReturnWindowPolicy'], 'signal-aligned-v2')
        self.assertEqual(config['universePolicy'],
                         'ranking-union-batch-prefilter-detail-limit-v1')
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'recommend.json'
            for value in (600.9, '600', True, 0, -1, 86401):
                path.write_text(json.dumps({'day': {'maxSignalAgeSeconds': value}}))
                with self.assertRaises(TmonError):
                    load_config(path)

    def test_breakout_with_falling_sma_is_not_trend_confirmed(self):
        rows = []
        for i in range(65):
            close = D('80') if i < 40 else D('120') if i < 60 else D('100')
            rows.append({'timestamp': (NOW - timedelta(days=65 - i)).isoformat(),
                         'closePrice': close, 'highPrice': close + 1,
                         'lowPrice': close - 1, 'volume': D('200000000')})
        # Break the previous 20-day high and finish above SMA20, while the
        # current SMA20 has fallen below its prior 20-day average.
        rows[-1].update(closePrice=D('130'), highPrice=D('132'), lowPrice=D('129'))
        with self.assertRaises(TmonError) as context:
            signal(rows, [], 'swing', load_config()['swing'], NOW)
        self.assertEqual(context.exception.code, 'trend-not-confirmed')

    def test_next_bar_completion_includes_configured_delay(self):
        from tmon.recommend import Engine

        session = {
            'date': NOW.date().isoformat(),
            'start': NOW.replace(hour=9, minute=0),
            'end': NOW.replace(hour=15, minute=30),
            'auction': NOW.replace(hour=15, minute=20),
            'entryEnd': NOW.replace(hour=15),
        }
        signal_start = NOW.replace(hour=9, minute=55, second=0)
        candle_row = {'timestamp': signal_start.isoformat(),
                      'closePrice': D('100'), 'highPrice': D('101'),
                      'lowPrice': D('99'), 'volume': D('100000000')}
        engine = Engine(load_config(research='off'), 'day', 1,
                        now=lambda: NOW)
        snapshot = engine._make_signal_snapshot(
            '000001', [candle_row] * 20, [candle_row] * 6, session,
            {'signalAsOf': signal_start.isoformat(),
             'breakoutLevel': D('99'), 'invalidation': D('98'),
             'volumeRatio': D('2'), 'signalClose': D('100')})
        self.assertEqual(snapshot['signalBarEndAt'],
                         NOW.replace(hour=10, minute=0, second=0).isoformat())
        self.assertEqual(snapshot['nextBarCompletionAt'],
                         NOW.replace(hour=10, minute=5, second=5).isoformat())


class StoreBoundaryTests(unittest.TestCase):
    def test_other_store_cannot_replace_existing_run(self):
        with tempfile.TemporaryDirectory() as td:
            result = envelope('recommend')
            result['meta']['runId'] = 'same-run-id'
            result['data'] = []
            inputs = {'requests': [], 'candidates': [], 'excluded': [],
                      'notEvaluated': [], 'screened': []}
            root = Path(td).resolve()
            first = Store(root)
            first.save(result, inputs)
            record = Path(result['meta']['recordPath'])
            before = record.read_text()

            with self.assertRaises(OSError):
                Store(root).save(result, inputs)
            self.assertEqual(record.read_text(), before)

    def test_failed_owned_run_swap_restores_previous_record(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            result = envelope('recommend')
            result['meta']['runId'] = 'rollback-run'
            result['data'] = []
            inputs = {'requests': [], 'candidates': [], 'excluded': [],
                      'notEvaluated': [], 'screened': []}
            store = Store(root)
            store.save(result, inputs)
            record = Path(result['meta']['recordPath'])
            before = record.read_text()

            import tmon.recommend_store as store_module
            original_rename = store_module.os.rename
            failed = [False]

            def fail_install_once(source, destination):
                if str(destination) == str(record.parent) and not failed[0]:
                    failed[0] = True
                    raise OSError('simulated replacement failure')
                return original_rename(source, destination)

            with patch.object(store_module.os, 'rename', side_effect=fail_install_once):
                with self.assertRaises(OSError):
                    store.save(result, inputs)
            self.assertEqual(record.read_text(), before)

    def test_actual_cli_buffer_boundary_matches_persisted_empty_result(self):
        # Exercise cli.main -> run_recommend -> Engine, with no provider or
        # model call.  Delaying the first complete render crosses the row's
        # expiry, so the CLI must trim and persist the same final JSON before
        # its first stdout write.
        from tests.test_recommend_snapshot_signals import (
            SnapshotClient, research_ok,
        )
        from tmon.recommend import Engine as RealEngine

        client = SnapshotClient(wall=NOW, mono=100.0)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()

            def engine_factory(config, horizon, limit):
                return RealEngine(config, horizon, limit, client, Store(root),
                                  now=client.now, clock=client.clock,
                                  researcher=research_ok)

            original_render_text = render_text
            renders = [0]

            def delayed_render(result, json_mode):
                renders[0] += 1
                if renders[0] == 1:
                    client.wall[0] = NOW + timedelta(seconds=31)
                return original_render_text(result, json_mode)

            stdout, stderr = StringIO(), StringIO()
            with patch('tmon.recommend.Engine', side_effect=engine_factory), \
                 patch('tmon.cli.render_text', side_effect=delayed_render), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(['recommend', '--horizon', 'day', '--research', 'off', '--json'])

            displayed = json.loads(stdout.getvalue())
            record = Path(displayed['meta']['recordPath'])
            persisted = json.loads(record.read_text())
            self.assertEqual(code, 5)
            self.assertEqual(displayed, persisted)
            self.assertEqual(displayed['data'], [])
            self.assertGreaterEqual(renders[0], 2)


if __name__ == '__main__':
    unittest.main()
