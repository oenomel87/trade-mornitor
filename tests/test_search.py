import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tmon.cli import main
from tmon.client import Transport
from tmon.errors import TmonError
from tmon.search import Catalogue, search, selected_markets


def stock(symbol, name, kind="STOCK"):
    return {"symbol": symbol, "name": name, "securityType": kind, "isCommonShare": True}


STOCKS = [stock("005930", "삼성전자"), stock("005935", "삼성전자우"), stock("005380", "현대차"),
          stock("069500", "KODEX 200", "ETF"), stock("AAPL", "애플")]


class FakeClient:
    def __init__(self, rows=None, error=None):
        self.rows = STOCKS if rows is None else rows
        self.error, self.calls = error, []

    def get(self, path, **params):
        self.calls.append((path, params))
        if self.error:
            raise self.error
        return self.rows


class SearchTests(unittest.TestCase):
    now = datetime(2026, 9, 5, 9, tzinfo=timezone.utc)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.client = FakeClient()
        self.factory = Mock(return_value=self.client)

    def catalogue(self, now=None):
        return Catalogue(self.factory, Transport(), self.directory, self.now if now is None else now)

    def test_korean_code_alias_and_unicode(self):
        for query, first in [("삼성 전자", "005930"), ("005930", "005930"), ("현대자동차", "005380"),
                             ("APPLE", "AAPL"), ("ａａｐｌ", "AAPL"), ("hyundai motor", "005380")]:
            with self.subTest(query=query):
                rows, _, _, code = search(self.catalogue(), query, "KOSPI")
                self.assertEqual(rows[0]["symbol"], first)
                self.assertEqual(code, 0)
        self.assertEqual(len(self.client.calls), 1)

    def test_complete_prefix_substring_order_and_limit(self):
        self.client.rows = [stock("000003", "삼성전자우"), stock("000001", "삼성전자"), stock("000002", "XX삼성전자")]
        rows, meta, _, code = search(self.catalogue(), "삼성전자", "KOSPI", 2)
        self.assertEqual([row["symbol"] for row in rows], ["000001", "000003"])
        self.assertEqual(meta["totalMatches"], 3)
        self.assertEqual(meta["returnedCount"], 2)
        self.assertTrue(meta["truncated"])
        self.assertEqual(code, 0)

    def test_empty_matches_are_success(self):
        rows, meta, _, code = search(self.catalogue(), "존재하지않는이름", "KOSPI")
        self.assertEqual(rows, [])
        self.assertEqual(meta["totalMatches"], 0)
        self.assertEqual(code, 0)

    def test_market_scope_and_active_request(self):
        _, meta, _, _ = search(self.catalogue(), "삼성", "KR")
        self.assertEqual([call[1]["market"] for call in self.client.calls], ["KOSPI", "KOSDAQ", "KR_ETC"])
        self.assertTrue(all(call[1]["status"] == "ACTIVE" for call in self.client.calls))
        self.assertEqual(len(meta["catalogue"]), 3)
        self.assertEqual(selected_markets("US"), ("NYSE", "NASDAQ", "AMEX", "US_ETC"))

    def test_fresh_cache_needs_no_client(self):
        search(self.catalogue(), "삼성", "KOSPI")
        self.factory.side_effect = AssertionError("cached search must not initialize authentication")
        rows, meta, _, _ = search(self.catalogue(), "삼성", "KOSPI")
        self.assertTrue(rows)
        self.assertTrue(meta["catalogue"][0]["cacheHit"])

    def test_ttl_boundary_and_force_refresh(self):
        search(self.catalogue(), "삼성", "KOSPI")
        search(self.catalogue(self.now + timedelta(hours=24) - timedelta(microseconds=1)), "삼성", "KOSPI")
        self.assertEqual(len(self.client.calls), 1)
        search(self.catalogue(self.now + timedelta(hours=24)), "삼성", "KOSPI")
        self.assertEqual(len(self.client.calls), 2)
        search(self.catalogue(self.now + timedelta(hours=24)), "삼성", "KOSPI", refresh=True)
        self.assertEqual(len(self.client.calls), 3)

    def test_corrupt_and_wrong_version_are_refetched(self):
        for content in ["{bad-json", '{"version":99}', '{"version":1,"market":"KOSPI"}']:
            (self.directory / "KOSPI.json").write_text(content)
            rows, meta, _, _ = search(self.catalogue(), "삼성", "KOSPI")
            self.assertTrue(rows)
            self.assertFalse(meta["catalogue"][0]["cacheHit"])

    def test_stale_fallback_is_explicit(self):
        search(self.catalogue(), "삼성", "KOSPI")
        self.client.error = TmonError("network-error", "test failure", 4, True)
        rows, meta, warnings, code = search(self.catalogue(self.now + timedelta(days=2)), "삼성", "KOSPI")
        self.assertTrue(rows)
        self.assertEqual(code, 5)
        self.assertTrue(meta["catalogue"][0]["stale"])
        self.assertEqual(warnings[0]["code"], "stale-catalogue")
        self.assertEqual(meta["catalogue"][0]["fetchedAt"], self.now.isoformat())

    def test_refresh_and_auth_errors_do_not_fallback(self):
        search(self.catalogue(), "삼성", "KOSPI")
        original = (self.directory / "KOSPI.json").read_bytes()
        for error, refresh in [(TmonError("network-error", "failed", 4), True),
                               (TmonError("forbidden", "failed", 3), False)]:
            self.client.error = error
            with self.assertRaises(TmonError):
                search(self.catalogue(self.now + timedelta(days=2)), "삼성", "KOSPI", refresh=refresh)
        self.assertEqual((self.directory / "KOSPI.json").read_bytes(), original)

    def test_missing_market_fails_instead_of_incomplete_search(self):
        search(self.catalogue(), "삼성", "KOSPI")
        self.client.error = TmonError("network-error", "test failure", 4)
        with self.assertRaises(TmonError):
            search(self.catalogue(), "삼성", "KR")

    def test_invalid_response_does_not_replace_cache(self):
        search(self.catalogue(), "삼성", "KOSPI")
        original = (self.directory / "KOSPI.json").read_bytes()
        for rows in [[stock("005930", "bad\x1b[31m")], [stock("005930", "삼성전자")] * 2, [{"symbol": "missing-fields"}]]:
            self.client.rows = rows
            with self.assertRaises(TmonError):
                search(self.catalogue(), "삼성", "KOSPI", refresh=True)
        self.assertEqual((self.directory / "KOSPI.json").read_bytes(), original)

    def test_provider_whitespace_in_name_is_normalized(self):
        self.client.rows = [stock("PNFP-C", "피너클  파이낸셜 파트너스 우선주 C(6.750%)\t")]
        rows, _, _, code = search(self.catalogue(), "피너클 파이낸셜", "NYSE")
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["name"], "피너클 파이낸셜 파트너스 우선주 C(6.750%)")

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_cli_offline_json_and_empty_table(self):
        search(self.catalogue(), "삼성", "KOSPI")
        with patch("tmon.search.cache_directory", return_value=self.directory.parent), \
                patch("tmon.cli.Catalogue", side_effect=lambda factory, transport: Catalogue(factory, transport, self.directory, self.now)), \
                patch("tmon.cli.Auth", side_effect=AssertionError("must not authenticate")), \
                patch.dict(os.environ, {"TOSS_CLIENT_ID": "", "TOSS_CLIENT_SECRET": ""}):
            code, output, stderr = self.invoke(["search", "현대자동차", "--market", "kospi", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["data"][0]["symbol"], "005380")
            self.assertEqual(stderr, "")
            code, output, _ = self.invoke(["search", "없는이름", "--market", "KOSPI"])
            self.assertEqual(code, 0)
            self.assertIn("검색 결과가 없습니다", output)

    def test_invalid_search_inputs_are_local(self):
        with patch("tmon.cli.Catalogue", side_effect=AssertionError("no cache access")):
            for args in [["search", "  "], ["search", "삼성", "--limit", "0"],
                         ["search", "삼성", "--market", "invalid"], ["search", "\x1b"]]:
                code, output, _ = self.invoke(args + ["--json"])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output)["status"], "error")

    def test_stock_group_is_paced(self):
        transport = Transport()
        with patch.object(transport, "send", return_value=(200, {}, {"result": []})), patch.object(transport, "pause") as pause:
            transport.request("GET", "/api/v1/stocks/all", {"market": "KOSPI"})
            transport.request("GET", "/api/v1/stocks/all", {"market": "KOSDAQ"})
            self.assertGreater(pause.call_args.args[0], 0.5)


class CountingClient:
    def __init__(self, count):
        self.count = count

    def get(self, *args, **kwargs):
        with self.count.get_lock():
            self.count.value += 1
        time.sleep(0.08)
        return STOCKS


def search_worker(directory, count, event):
    event.wait()
    client = CountingClient(count)
    search(Catalogue(lambda: client, Transport(), directory), "삼성", "KOSPI")


class SearchConcurrencyTests(unittest.TestCase):
    def test_one_refresh_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = multiprocessing.get_context("fork")
            count, event = ctx.Value("i", 0), ctx.Event()
            processes = [ctx.Process(target=search_worker, args=(directory, count, event)) for _ in range(3)]
            for process in processes:
                process.start()
            event.set()
            for process in processes:
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    self.fail("catalogue refresh lock did not finish")
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(count.value, 1)
