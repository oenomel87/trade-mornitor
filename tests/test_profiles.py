import contextlib
import json
import multiprocessing
from pathlib import Path
import stat
import unicodedata
import unittest
from unittest.mock import Mock, patch

from tmon.errors import TmonError
from tmon.watchlist import Watchlist, run_watchlist
from . import test_watchlist as helpers


class ProfileTests(unittest.TestCase):
    setUp = helpers.WatchlistTests.setUp
    invoke = helpers.WatchlistTests.invoke

    def legacy(self):
        self.directory.mkdir()
        raw = b'{"version":1,"symbols":["005930","AAPL"],"updatedAt":"2026-09-05T00:00:00+00:00"}\n'
        self.store.path.write_bytes(raw)
        return raw

    def fails(self, code, operation):
        before = self.store.path.read_bytes() if self.store.path.exists() else None
        with self.assertRaises(TmonError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.store.path.read_bytes() if self.store.path.exists() else None, before)

    def test_virtual_default_does_not_create_files(self):
        rows, meta, _, _ = self.store.profile()
        self.assertEqual(rows, [{"name": "default", "symbolCount": 0, "active": True, "updatedAt": None}])
        self.assertEqual(meta["activeProfile"], "default")
        self.assertFalse(self.directory.exists())
        self.store.profile("use", "default")
        self.assertFalse(self.store.path.exists())

    def test_profiles_isolate_symbols_and_selection_persists(self):
        self.store.profile("create", "국내주식")
        self.store.profile("create", "미국주식")
        self.assertEqual(self.store.profile()[1]["activeProfile"], "default")
        self.store.profile("use", "국내주식")
        self.store.apply("add", ["005930", "AAPL"])
        self.store.apply("add", ["AAPL", "MSFT"], "미국주식")
        self.store.apply("remove", ["AAPL"], "국내주식")
        reloaded = Watchlist(self.directory)
        rows, meta, _, _ = reloaded.apply()
        self.assertEqual(rows, [{"symbol": "005930"}])
        self.assertEqual(meta["profile"], "국내주식")
        self.assertEqual(meta["profileSource"], "active")
        rows, meta, _, _ = reloaded.apply(profile="미국주식")
        self.assertEqual([r["symbol"] for r in rows], ["AAPL", "MSFT"])
        self.assertEqual(meta["activeProfile"], "국내주식")
        self.assertEqual(meta["profileSource"], "explicit")
        self.assertEqual(json.loads(self.store.path.read_text())["version"], 2)

    def test_names_normalization_validation_and_collisions(self):
        decomposed = unicodedata.normalize("NFD", "국내")
        self.store.profile("create", decomposed)
        self.assertEqual(self.store.apply(profile=decomposed)[1]["profile"], "국내")
        self.fails("profile-exists", lambda: self.store.profile("create", "국내"))
        for name in ("", "a b", "../bad", "x/y", "a" * 33, "💰", "bad\n"):
            self.fails("invalid-profile-name", lambda: self.store.profile("create", name))
        self.store.profile("create", "US")
        self.store.profile("create", "us")
        self.assertEqual([row["name"] for row in self.store.profile()[0]], ["default", "US", "us", "국내"])

    def test_missing_profile_does_not_fallback_or_create(self):
        for action in ("list", "quote", "add", "remove"):
            self.fails("profile-not-found", lambda: self.store.apply(action, ["AAPL"], "typo"))
        for action in ("use", "rename", "delete"):
            self.fails("profile-not-found", lambda: self.store.profile(action, "typo", "new"))
        self.assertFalse(self.store.path.exists())

    def test_rename_updates_active_and_preserves_order(self):
        self.store.profile("create", "old")
        self.store.profile("use", "old")
        self.store.apply("add", ["AAPL", "005930"])
        self.store.profile("rename", "old", "new")
        self.assertEqual(self.store.apply()[1]["profile"], "new")
        self.assertEqual([r["symbol"] for r in self.store.apply()[0]], ["AAPL", "005930"])
        self.fails("profile-exists", lambda: self.store.profile("rename", "new", "default"))
        self.fails("reserved-profile", lambda: self.store.profile("rename", "default", "other"))

    def test_delete_requires_nonactive_and_explicit_force_when_nonempty(self):
        self.store.profile("create", "US")
        self.store.profile("create", "empty")
        self.store.profile("delete", "empty")
        self.store.apply("add", ["AAPL"], "US")
        self.fails("profile-not-empty", lambda: self.store.profile("delete", "US"))
        self.store.profile("use", "US")
        self.fails("active-profile", lambda: self.store.profile("delete", "US", force=True))
        self.fails("reserved-profile", lambda: self.store.profile("delete", "default", force=True))
        self.store.profile("use", "default")
        self.store.profile("delete", "US", force=True)
        self.assertEqual(len(self.store.profile()[0]), 1)

    def test_noops_preserve_bytes_time_and_profile_timestamp_on_use(self):
        self.store.profile("create", "US")
        profile_time = self.store.apply(profile="US")[1]["updatedAt"]
        self.store.profile("use", "US")
        self.assertEqual(self.store.apply()[1]["updatedAt"], profile_time)
        before = self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns
        self.store.profile("use", "US")
        self.store.profile("rename", "US", "US")
        self.store.apply("remove", ["AAPL"])
        self.assertEqual((self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns), before)

    def test_capacity_is_per_profile(self):
        requested = ["%06d" % i for i in range(200)]
        self.store.apply("add", requested)
        self.store.profile("create", "other")
        self.store.apply("add", requested, "other")
        self.fails("watchlist-full", lambda: self.store.apply("add", ["AAPL"], "other"))
        self.assertEqual([r["symbolCount"] for r in self.store.profile()[0]], [200, 200])

    def test_legacy_reads_and_noops_do_not_migrate(self):
        raw = self.legacy()
        self.assertEqual(self.store.apply()[1]["profile"], "default")
        self.store.profile()
        self.store.profile("use", "default")
        self.store.apply("add", ["AAPL"])
        self.assertEqual(self.store.path.read_bytes(), raw)
        self.assertEqual(list(self.directory.glob("*.bak")), [])

    def test_migration_preserves_exact_backup_and_legacy_timestamp(self):
        raw = self.legacy()
        result = self.store.profile("create", "US")
        backup = Path(result[1]["migration"]["backupFile"])
        self.assertEqual(backup.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(self.store.apply()[1]["updatedAt"], "2026-09-05T00:00:00+00:00")
        self.assertEqual(self.store.apply()[1]["savedCount"], 2)
        self.assertNotIn("migration", self.store.profile("use", "US")[1])
        self.assertEqual(len(list(self.directory.glob("*.bak"))), 1)

    def test_failed_migration_preserves_original_and_unique_backup(self):
        raw = self.legacy()
        with patch.object(self.store, "_backup", side_effect=OSError):
            self.fails("watchlist-unavailable", lambda: self.store.profile("create", "US"))
        with patch("tmon.watchlist.os.replace", side_effect=OSError):
            self.fails("watchlist-unavailable", lambda: self.store.profile("create", "US"))
        backups = list(self.directory.glob("*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), raw)
        self.store.profile("create", "US")
        self.assertEqual(len(list(self.directory.glob("*.bak"))), 2)

    def test_corrupt_v2_and_duplicate_keys_are_preserved(self):
        self.store.profile("create", "US")
        valid = self.store.path.read_text()
        mutations = [lambda d: d.update(activeProfile="absent"),
                     lambda d: d["profiles"].pop("default"),
                     lambda d: d["profiles"]["US"].update(symbols=["AAPL", "AAPL"]),
                     lambda d: d["profiles"].update({"../bad": d["profiles"]["US"]}),
                     lambda d: d.update(updatedAt=None),
                     lambda d: d["profiles"]["US"].update(updatedAt=None)]
        for mutate in mutations:
            data = json.loads(valid)
            mutate(data)
            self.store.path.write_text(json.dumps(data))
            self.fails("invalid-watchlist", lambda: self.store.profile("create", "other"))
        self.store.path.write_text(valid.replace('"version": 2', '"version": 2, "version": 2'))
        self.fails("invalid-watchlist", lambda: self.store.apply())

    def test_size_limit_rejects_read_and_write_without_damage(self):
        self.store.profile("create", "US")
        with patch("tmon.watchlist.MAX_BYTES", self.store.path.stat().st_size + 5):
            self.fails("watchlist-too-large", lambda: self.store.profile("create", "extra"))
        with patch("tmon.watchlist.MAX_BYTES", 10):
            self.fails("watchlist-too-large", lambda: self.store.profile())

    def test_quote_snapshot_survives_selection_change_and_releases_lock(self):
        self.store.profile("create", "US")
        self.store.apply("add", ["AAPL"], "US")
        self.store.profile("use", "US")
        other = Watchlist(self.directory, lock_timeout=0)
        client = Mock()
        def get(*args, **kwargs):
            other.profile("use", "default")
            other.apply("add", ["MSFT"], "US")
            return [helpers.price("AAPL")]
        client.get.side_effect = get
        rows, meta, _, code = run_watchlist(self.store, "quote", None, lambda: client)
        self.assertEqual(code, 0)
        self.assertEqual(meta["profile"], "US")
        self.assertEqual(meta["activeProfile"], "US")
        self.assertEqual(meta["requestedCount"], 1)
        self.assertEqual(rows[0]["symbol"], "AAPL")
        self.assertEqual(other.profile()[1]["activeProfile"], "default")

    def test_cli_option_positions_local_workflow_and_table(self):
        with patch("tmon.cli.Transport", side_effect=AssertionError("no network")):
            commands = [["--json", "profile", "create", "국내"],
                        ["profile", "use", "국내", "--json"],
                        ["watchlist", "--profile", "국내", "add", "005930", "--json"],
                        ["watchlist", "list", "--profile", "국내", "--json"],
                        ["watchlist", "--json", "--profile", "default"],
                        ["profile", "--json", "rename", "국내", "한국"],
                        ["profile", "--json"]]
            for args in commands:
                code, output, error = self.invoke(args)
                self.assertEqual(code, 0, output)
                self.assertEqual(json.loads(output)["meta"]["source"], "local")
                self.assertEqual(error, "")
            _, output, _ = self.invoke(["profile"])
            self.assertIn("한국", output)
            _, output, _ = self.invoke(["watchlist"])
            self.assertIn("프로필: 한국", output)
            code, output, _ = self.invoke(["watchlist", "quote", "--profile", "default", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["data"], [])

    def test_cli_api_failure_keeps_resolved_profile(self):
        self.store.profile("create", "US")
        self.store.apply("add", ["AAPL"], "US")
        client = Mock()
        client.get.return_value = []
        with patch("tmon.cli.Auth"), patch("tmon.cli.TossClient", return_value=client):
            code, output, _ = self.invoke(["watchlist", "quote", "--profile", "US", "--json"])
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(output)["meta"]["profile"], "US")

    def test_cli_delete_force_and_argument_validation(self):
        self.store.profile("create", "US")
        self.store.apply("add", ["AAPL"], "US")
        code, output, _ = self.invoke(["profile", "delete", "US", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["error"]["code"], "profile-not-empty")
        code, _, _ = self.invoke(["profile", "delete", "US", "--force", "--json"])
        self.assertEqual(code, 0)
        for args in (["profile", "create"], ["profile", "rename", "US"], ["quote", "AAPL", "--profile", "default"]):
            code, _, _ = self.invoke(list(args) + ["--json"])
            self.assertEqual(code, 2)

    def test_selection_is_resolved_after_write_lock(self):
        self.store.profile("create", "US")
        original = self.store.locked
        @contextlib.contextmanager
        def intervening_lock():
            Watchlist(self.directory).profile("use", "US")
            with original():
                yield
        with patch.object(self.store, "locked", intervening_lock):
            self.store.apply("add", ["AAPL"])
        self.assertEqual(self.store.apply(profile="default")[0], [])
        self.assertEqual(self.store.apply(profile="US")[0], [{"symbol": "AAPL"}])


def profile_worker(directory, name, event):
    event.wait()
    store = Watchlist(directory)
    store.profile("create", name)
    store.apply("add", ["AAPL"], name)


class ProfileConcurrencyTests(unittest.TestCase):
    def test_concurrent_profile_creation_and_adds(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ctx = multiprocessing.get_context("fork")
            event = ctx.Event()
            processes = [ctx.Process(target=profile_worker, args=(directory, name, event)) for name in ("KR", "US", "tech")]
            try:
                for process in processes:
                    process.start()
                event.set()
                for process in processes:
                    process.join(5)
                    self.assertFalse(process.is_alive())
                    self.assertEqual(process.exitcode, 0)
                rows = Watchlist(directory).profile()[0]
                self.assertEqual({r["name"]: r["symbolCount"] for r in rows}, {"default": 0, "KR": 1, "US": 1, "tech": 1})
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join()
