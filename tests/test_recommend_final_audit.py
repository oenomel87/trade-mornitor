"""Focused regressions for the final recommendation persistence boundary."""

import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tmon.cli import envelope
from tmon.recommend import Engine, finalize_cli_result
from tmon.recommend_config import load_config
from tmon.recommend_store import Store

from tests.test_recommend_snapshot_signals import (
    NOW, SnapshotClient, research_ok,
)


def empty_inputs():
    return {
        "requests": [], "candidates": [], "excluded": [],
        "preExcluded": [], "precheckBatches": [], "notEvaluated": [],
        "screened": [],
    }


def test_config():
    config = load_config(research="off")
    config["budget"].update(
        totalSeconds=1000, dataSeconds=200,
        researchSeconds=500, finalSeconds=200,
    )
    return config


class FinalAuditStoreTests(unittest.TestCase):
    def test_cleanup_failure_after_swap_keeps_new_record_and_private_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = Store(root)
            old = envelope("recommend")
            old["meta"].update(runId="cleanup-run", marker="old")
            old["data"] = []
            inputs = empty_inputs()
            store.save(old, inputs)

            new = deepcopy(old)
            new["meta"]["marker"] = "new"
            real_rmtree = shutil.rmtree

            def fail_previous_cleanup(path, *args, **kwargs):
                if Path(path).name.startswith(".previous-"):
                    raise OSError("injected backup cleanup failure")
                return real_rmtree(path, *args, **kwargs)

            with patch("tmon.recommend_store.shutil.rmtree",
                       side_effect=fail_previous_cleanup):
                store.save(new, inputs)

            destination = root / "runs" / "cleanup-run"
            self.assertEqual(
                json.loads((destination / "result.json").read_text())["meta"]["marker"],
                "new",
            )
            backups = list((root / "runs").glob(".previous-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(
                json.loads((backups[0] / "result.json").read_text())["meta"]["marker"],
                "old",
            )

    def test_cli_force_empty_fallback_records_every_dropped_row(self):
        client = SnapshotClient(wall=NOW, mono=100.0)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory).resolve())
            engine = Engine(
                test_config(), "day", 3, client, store,
                now=client.now, clock=client.clock, researcher=research_ok,
            )
            result = envelope("recommend")
            self.assertEqual(engine.run(result), 0)
            dropped = deepcopy(result["data"])
            self.assertTrue(dropped)

            changed, code = finalize_cli_result(result, engine, force_empty=True)

            self.assertTrue(changed)
            self.assertEqual(code, 5)
            self.assertEqual(result["data"], [])
            output_exclusions = [
                row for row in engine.inputs["excluded"]
                if row.get("stage") == "output"
            ]
            self.assertEqual(len(output_exclusions), len(dropped))
            self.assertEqual(
                [row["reason"] for row in output_exclusions],
                ["expired-candidate"] * len(dropped),
            )
            self.assertEqual(
                result["meta"]["excludedCounts"]["expired-candidate"],
                len(dropped),
            )
            self.assertEqual(
                len(result["meta"]["evaluationSummary"]["laterExclusions"]),
                len(dropped),
            )
            record = Path(result["meta"]["recordPath"])
            persisted = json.loads(record.read_text())
            self.assertEqual(persisted["data"], [])
            self.assertEqual(
                persisted["meta"]["excludedCounts"],
                result["meta"]["excludedCounts"],
            )

    def test_expiry_retry_reuses_verification_without_refetching_raw_daily(self):
        client = SnapshotClient(wall=NOW, mono=100.0)

        class RetryEngine(Engine):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._final_evaluations = 0

            def evaluate(self, *args, **kwargs):
                row = super().evaluate(*args, **kwargs)
                if kwargs.get("final"):
                    self._final_evaluations += 1
                    if self._final_evaluations == 1:
                        # Make the first final row stale only after its
                        # verification has completed. Retry must reuse that
                        # verification and refresh only current state.
                        client.wall[0] = NOW + timedelta(seconds=20)
                return row

        with tempfile.TemporaryDirectory() as directory:
            engine = RetryEngine(
                test_config(), "day", 3, client,
                Store(Path(directory).resolve()),
                now=client.now, clock=client.clock, researcher=research_ok,
            )
            result = envelope("recommend")
            self.assertEqual(engine.run(result), 0)

        self.assertTrue(result["data"])
        screened = engine.inputs["screened"][0]
        final = result["data"][0]
        self.assertEqual(final["signalId"], screened["signalId"])
        self.assertEqual(final["signalSnapshot"], screened["signalSnapshot"])
        self.assertEqual(final["finalVerification"]["status"], "verified")
        self.assertEqual(
            final["finalVerification"]["rawEstimatedAvgDailyAmount"],
            final["rawEstimatedAvgDailyAmount"],
        )
        raw_daily = [
            record for record in engine.client.records
            if record["phase"] == "final"
            and record["path"].endswith("/candles")
            and record["params"].get("adjusted") == "false"
        ]
        self.assertEqual(len(raw_daily), 1)
        final_prices = [
            record for record in engine.client.records
            if record["phase"] == "final"
            and record["path"].endswith("/prices")
        ]
        self.assertEqual(len(final_prices), 2)


if __name__ == "__main__":
    unittest.main()
