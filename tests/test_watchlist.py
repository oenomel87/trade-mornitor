import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from tmon.cli import main
from tmon.errors import TmonError
from tmon.watchlist import Watchlist, config_directory, run_watchlist


def price(symbol):
    return {"symbol": symbol, "lastPrice": "100", "currency": "USD" if symbol == "AAPL" else "KRW",
            "timestamp": "2026-09-04T20:00:00+09:00"}


class WatchlistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "settings"
        self.store = Watchlist(self.directory)

    def saved_symbols(self, result):
        return [row["symbol"] for row in result[0]]

    def test_empty_list_and_quote_create_nothing(self):
        for action in ("list", "quote"):
            factory = Mock(side_effect=AssertionError("no network"))
            rows, meta, _, code = run_watchlist(self.store, action, None, factory)
            self.assertEqual(rows, [])
            self.assertEqual(meta["savedCount"], 0)
            self.assertEqual(code, 0)
            self.assertFalse(self.directory.exists())
            factory.assert_not_called()

    def test_add_order_normalization_and_persistence(self):
        result = self.store.apply("add", ["005930", "aapl", "AAPL", "005380"])
        self.assertEqual(self.saved_symbols(result), ["005930", "AAPL", "005380"])
        self.assertEqual(result[1]["addedSymbols"], ["005930", "AAPL", "005380"])
        self.assertEqual(self.saved_symbols(Watchlist(self.directory).apply()), ["005930", "AAPL", "005380"])
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)

    def test_noop_preserves_file_and_updated_at(self):
        added = self.store.apply("add", ["AAPL"])
        before = self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns
        duplicate = self.store.apply("add", ["aapl"])
        missing = self.store.apply("remove", ["005930"])
        self.assertEqual(duplicate[1]["alreadyPresentSymbols"], ["AAPL"])
        self.assertEqual(missing[1]["notFoundSymbols"], ["005930"])
        self.assertEqual(added[1]["updatedAt"], missing[1]["updatedAt"])
        self.assertEqual(before, (self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns))

    def test_remove_subset_preserves_order(self):
        self.store.apply("add", ["005930", "AAPL", "005380"])
        result = self.store.apply("remove", ["AAPL", "MSFT"])
        self.assertEqual(self.saved_symbols(result), ["005930", "005380"])
        self.assertEqual(result[1]["removedSymbols"], ["AAPL"])
        self.assertEqual(result[1]["notFoundSymbols"], ["MSFT"])
        self.store.apply("remove", ["005930", "005380"])
        self.assertEqual(self.store.apply()[0], [])

    def test_capacity_overflow_is_all_or_nothing(self):
        original = ["%06d" % i for i in range(199)]
        self.store.apply("add", original)
        before = self.store.path.read_bytes()
        with self.assertRaises(TmonError):
            self.store.apply("add", ["AAPL", "MSFT"])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.apply("add", ["AAPL"])
        self.assertEqual(self.store.apply()[1]["savedCount"], 200)
        self.assertEqual(self.store.apply("add", ["aapl"])[1]["addedSymbols"], [])

    def test_invalid_input_does_not_partially_add(self):
        for values in (["005930", "삼성전자"], ["AAPL", "bad/symbol"], [], ["A" * 65]):
            with self.subTest(values=values), self.assertRaises(TmonError):
                self.store.apply("add", values)
            self.assertFalse(self.directory.exists())

    def test_corrupt_file_or_schema_never_overwritten(self):
        self.directory.mkdir()
        malformed = ["broken", '{"version":2,"symbols":[]}',
                     '{"version":true,"symbols":[]}',
                     '{"version":1,"symbols":["aapl"],"updatedAt":"2026-09-05T00:00:00+00:00"}',
                     '{"version":1,"symbols":["AAPL","AAPL"],"updatedAt":"2026-09-05T00:00:00+00:00"}',
                     '{"version":1,"symbols":[],"updatedAt":"no-date"}']
        for raw in malformed:
            self.store.path.write_text(raw)
            for action in ("list", "add", "remove"):
                with self.subTest(raw=raw, action=action), self.assertRaises(TmonError) as caught:
                    self.store.apply(action, ["005930"])
                self.assertEqual(caught.exception.code, "invalid-watchlist")
                self.assertEqual(self.store.path.read_text(), raw)

    def test_symbolic_links_are_not_followed(self):
        self.directory.mkdir()
        other = Path(self.temp.name) / "other.json"
        other.write_text("leave this alone")
        self.store.path.symlink_to(other)
        with self.assertRaises(TmonError):
            self.store.apply("add", ["AAPL"])
        self.assertEqual(other.read_text(), "leave this alone")

    def test_lock_timeout(self):
        self.store.lock_timeout = 0
        with patch("tmon.watchlist.fcntl.flock", side_effect=BlockingIOError):
            with self.assertRaises(TmonError) as caught:
                self.store.apply("add", ["AAPL"])
        self.assertEqual(caught.exception.code, "watchlist-busy")
        self.assertFalse(self.store.path.exists())

    def test_failed_replace_preserves_old_file_and_cleans_temp(self):
        self.store.apply("add", ["005930"])
        before = self.store.path.read_bytes()
        with patch("tmon.watchlist.os.replace", side_effect=OSError):
            with self.assertRaises(TmonError):
                self.store.apply("add", ["AAPL"])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.directory.glob(".watchlist-*")), [])

    def test_quote_batch_order_partial_and_no_mutation(self):
        self.store.apply("add", ["005930", "AAPL", "005380"])
        before = self.store.path.read_bytes()
        client = Mock()
        client.get.return_value = [price("AAPL"), price("005930")]
        rows, meta, warnings, code = run_watchlist(self.store, "quote", None, lambda: client)
        self.assertEqual([row["symbol"] for row in rows], ["005930", "AAPL"])
        self.assertEqual(meta["missingSymbols"], ["005380"])
        self.assertEqual(meta["requestedCount"], 3)
        self.assertEqual(meta["returnedCount"], 2)
        self.assertEqual(warnings[0]["code"], "missing-symbols")
        self.assertEqual(code, 5)
        client.get.assert_called_once_with("/api/v1/prices", symbols="005930,AAPL,005380")
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_all_quotes_missing_remains_error_and_preserves_list(self):
        self.store.apply("add", ["MISSING"])
        client = Mock()
        client.get.return_value = []
        with self.assertRaises(TmonError) as caught:
            run_watchlist(self.store, "quote", None, lambda: client)
        self.assertEqual(caught.exception.code, "no-data")
        self.assertEqual(self.saved_symbols(self.store.apply()), ["MISSING"])

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch("tmon.watchlist.config_directory", return_value=self.directory):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_cli_local_actions_no_credentials_or_transport(self):
        with patch("tmon.cli.Auth", side_effect=AssertionError("no auth")), \
                patch("tmon.cli.Transport", side_effect=AssertionError("no network")):
            for args in (["watchlist", "--json"], ["watchlist", "quote", "--json"],
                         ["--json", "watchlist", "add", "005930", "AAPL"],
                         ["watchlist", "--json", "list"], ["watchlist", "remove", "AAPL", "--json"]):
                code, output, error = self.invoke(args)
                self.assertEqual(code, 0)
                result = json.loads(output)
                self.assertEqual(result["command"], "watchlist")
                self.assertEqual(result["meta"]["source"], "local")
                self.assertEqual(error, "")
            _, output, _ = self.invoke(["watchlist"])
            self.assertIn("005930", output)

    def test_cli_partial_quotes_keep_action_and_source(self):
        self.store.apply("add", ["AAPL", "005930"])
        client = Mock()
        client.get.return_value = [price("AAPL")]
        with patch("tmon.cli.Auth"), patch("tmon.cli.TossClient", return_value=client):
            code, output, error = self.invoke(["watchlist", "quote", "--json"])
        result = json.loads(output)
        self.assertEqual(code, 5)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["meta"]["action"], "quote")
        self.assertEqual(result["meta"]["source"], "Toss Securities Open API")
        self.assertEqual(error, "")

    def test_cli_missing_symbols_and_help_are_local(self):
        with patch("tmon.cli.Auth", side_effect=AssertionError("no auth")):
            code, output, _ = self.invoke(["watchlist", "add", "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output)["command"], "watchlist")
            for command in (["watchlist"], ["watchlist", "add"], ["watchlist", "quote"]):
                with self.assertRaises(SystemExit) as caught:
                    self.invoke(command + ["--help"])
                self.assertEqual(caught.exception.code, 0)

    def test_os_settings_paths(self):
        with patch("tmon.watchlist.sys.platform", "linux"), patch("tmon.watchlist.Path.home", return_value=Path("/home/test")):
            for configured, expected in [("/tmp/config", "/tmp/config/tmon"), ("relative", "/home/test/.config/tmon"), ("", "/home/test/.config/tmon")]:
                with patch.dict(os.environ, {"XDG_CONFIG_HOME": configured}):
                    self.assertEqual(str(config_directory()), expected)
        with patch("tmon.watchlist.sys.platform", "darwin"), patch("tmon.watchlist.Path.home", return_value=Path("/Users/test")):
            self.assertEqual(str(config_directory()), "/Users/test/Library/Application Support/tmon")


def add_worker(directory, symbol, event):
    event.wait()
    Watchlist(directory).apply("add", [symbol])


class WatchlistConcurrencyTests(unittest.TestCase):
    def test_concurrent_adds_do_not_lose_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = multiprocessing.get_context("fork")
            event = ctx.Event()
            requested = ["005930", "005380", "AAPL"]
            processes = [ctx.Process(target=add_worker, args=(directory, symbol, event)) for symbol in requested]
            for process in processes:
                process.start()
            event.set()
            for process in processes:
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    self.fail("watchlist lock did not finish")
                self.assertEqual(process.exitcode, 0)
            rows = Watchlist(directory).apply()[0]
            self.assertEqual({row["symbol"] for row in rows}, set(requested))
