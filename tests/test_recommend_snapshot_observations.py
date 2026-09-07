"""Independent PR3 contracts for observation timestamps and output storage."""

import json
import os
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tmon.cli import envelope, serialize_result
from tmon.recommend import Engine
from tmon.recommend_config import load_config
from tmon.recommend_data import RecordingClient, current_data
from tmon.recommend_store import Store
from tmon.market import timestamp

from tests.test_recommend import NOW
from tests.test_recommend_snapshot_signals import SnapshotClient, research_ok


class ObservationAndStorageContractTests(unittest.TestCase):
    def config(self):
        config = load_config(research='off')
        config['budget'].update(totalSeconds=1000, dataSeconds=200,
                                researchSeconds=500, finalSeconds=200)
        return config

    def test_observation_times_are_sequential_and_drive_final_expiry(self):
        # Direct current_data contract: each state endpoint is received at a
        # different wall-clock instant from one advancing fake provider.
        direct_inner = SnapshotClient(wall=NOW, step=5.0)
        direct = RecordingClient(direct_inner, now=direct_inner.now,
                                 clock=direct_inner.clock)
        settings = self.config()['freshness']
        _, _, _, _, _, observations = current_data(
            direct, '000001', settings, now=direct_inner.now,
            return_observations=True)
        stock_received = next(record['receivedAt'] for record in direct.records
                               if record['path'] == '/api/v1/stocks')
        warning_received = next(record['receivedAt'] for record in direct.records
                                if record['path'].endswith('/warnings'))
        self.assertEqual(observations['stockCheckedAt'], timestamp(stock_received))
        self.assertEqual(observations['warningsCheckedAt'], timestamp(warning_received))
        self.assertNotEqual(observations['stockCheckedAt'], observations['warningsCheckedAt'])
        self.assertLess(observations['stockCheckedAt'], observations['warningsCheckedAt'])

        # Integrated row contract: make state TTL the first limiting clock,
        # so using one later state timestamp for both endpoints is observable.
        config = self.config()
        config['freshness'].update(quoteSeconds=300, resultSeconds=300,
                                   stateSeconds=10, minuteSeconds=300,
                                   rankingSeconds=300)
        client = SnapshotClient(wall=NOW, mono=100.0, step=1.0)
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(config, 'day', 3, client,
                            Store(Path(directory).resolve()), now=client.now,
                            clock=client.clock, researcher=research_ok)
            result = envelope('recommend')
            self.assertEqual(engine.run(result), 0)
        self.assertTrue(result['data'])
        row = result['data'][0]
        stock_at = timestamp(row['stockCheckedAt'])
        warning_at = timestamp(row['warningsCheckedAt'])
        final_stock = next(record for record in engine.client.records
                           if record['phase'] == 'final' and record['path'] == '/api/v1/stocks')
        final_warning = next(record for record in engine.client.records
                             if record['phase'] == 'final' and record['path'].endswith('/warnings'))
        self.assertEqual(row['stockCheckedAt'], final_stock['receivedAt'])
        self.assertEqual(row['warningsCheckedAt'], final_warning['receivedAt'])
        self.assertLess(stock_at, warning_at)
        self.assertEqual(row['stateAsOf'], row['warningsCheckedAt'])
        self.assertEqual(timestamp(row['expiresAt']),
                         stock_at + timedelta(seconds=config['freshness']['stateSeconds']))
        self.assertLess(timestamp(row['expiresAt']),
                         warning_at + timedelta(seconds=config['freshness']['stateSeconds']))

    def test_store_failed_owned_swap_preserves_first_bytes_and_foreign_store(self):
        result = envelope('recommend')
        result['meta']['runId'] = 'owned-run'
        result['data'] = []
        inputs = {'requests': [], 'candidates': [], 'excluded': [],
                  'notEvaluated': [], 'screened': []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owner = Store(root)
            owner.save(result, inputs)
            record = Path(result['meta']['recordPath'])
            first_bytes = record.read_bytes()
            destination = record.parent
            real_rename = os.rename

            def fail_only_pending_swap(source, target):
                source_path, target_path = Path(source), Path(target)
                if source_path.name.startswith('.pending-') and target_path == destination:
                    raise OSError('injected pending swap failure')
                return real_rename(source, target)

            # The narrow side effect delegates setup, backup, and recovery
            # renames; only pending -> destination is rejected.
            with patch('tmon.recommend_store.os.rename',
                       side_effect=fail_only_pending_swap):
                with self.assertRaises(OSError):
                    owner.save(result, inputs)
            self.assertEqual(record.read_bytes(), first_bytes)

            # A different Store instance cannot claim the existing run.
            with self.assertRaises(OSError):
                Store(root).save(result, inputs)
            self.assertEqual(record.read_bytes(), first_bytes)

    def test_slow_store_trims_expired_output_and_persists_normalized_result(self):
        class SlowStore(Store):
            def __init__(self, root, wall):
                super().__init__(root)
                self.wall = wall
                self.save_count = 0
                self.first_bytes = None
                self.screened_before = None

            def save(self, result, inputs):
                if self.screened_before is None:
                    self.screened_before = deepcopy(inputs['screened'])
                super().save(result, inputs)
                self.save_count += 1
                record = Path(result['meta']['recordPath'])
                if self.first_bytes is None:
                    self.first_bytes = record.read_bytes()
                # Advance only after the actual fsync and rename.
                self.wall[0] += timedelta(seconds=20)

        client = SnapshotClient(wall=NOW, mono=100.0)
        with tempfile.TemporaryDirectory() as directory:
            wall = client.wall
            store = SlowStore(Path(directory).resolve(), wall)
            engine = Engine(self.config(), 'day', 3, client, store,
                            now=client.now, clock=client.clock,
                            researcher=research_ok)
            result = envelope('recommend')
            code = engine.run(result)
            record = Path(result['meta']['recordPath'])
            first_payload = json.loads(store.first_bytes)
            final_payload = json.loads(record.read_text())
            normalized = serialize_result(result)

        self.assertEqual(code, 5)
        self.assertGreaterEqual(store.save_count, 2)
        self.assertTrue(first_payload['data'])
        self.assertEqual(result['data'], [])
        self.assertEqual(final_payload['data'], [])
        self.assertEqual(final_payload, normalized)
        self.assertEqual(engine.inputs['screened'], store.screened_before)
        self.assertIn('expired-candidate', result['meta']['excludedCounts'])


if __name__ == '__main__':
    unittest.main()
