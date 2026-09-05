import contextlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from tmon.analysis import analyze
from tmon.auth import Auth, cache_directory
from tmon.cli import main, serialize
from tmon.client import TossClient, Transport
from tmon.errors import TmonError
from tmon.market import calendar_cutoff, history, normalize_candle, quote, symbols, timestamp

ENV = {"TOSS_CLIENT_ID": "test-client", "TOSS_CLIENT_SECRET": "test-secret"}


def candle(day, close="100", currency="KRW", volume="10"):
    hour = "00" if currency == "KRW" else "13"
    return {"timestamp": "%sT%s:00:00+09:00" % (day, hour), "openPrice": close,
            "highPrice": close, "lowPrice": close, "closePrice": close,
            "volume": volume, "currency": currency}


def calendar(country="KR", today="2026-09-05", previous="2026-09-04"):
    if country == "KR":
        sessions = {"preMarket": None, "regularMarket": None,
                    "afterMarket": {"startTime": previous + "T15:30:00+09:00", "endTime": previous + "T20:00:00+09:00"}}
        prev = {"date": previous, "integrated": sessions}
    else:
        tomorrow = (datetime.fromisoformat(previous) + timedelta(days=1)).date().isoformat()
        prev = {"date": previous, "dayMarket": None, "preMarket": None, "regularMarket": None,
                "afterMarket": {"startTime": tomorrow + "T05:00:00+09:00", "endTime": tomorrow + "T08:50:00+09:00"}}
    return {"today": {"date": today}, "previousBusinessDay": prev}


class FakeClient:
    def __init__(self, pages, cal=None):
        self.pages, self.cal, self.calls = iter(pages), cal or calendar(), []

    def get(self, path, **params):
        self.calls.append((path, params))
        return self.cal if "calendar" in path else next(self.pages)


class AnalysisTests(unittest.TestCase):
    def rows(self, closes, volumes=None):
        volumes = volumes or [10] * len(closes)
        return [{"closePrice": Decimal(str(c)), "volume": Decimal(str(v))} for c, v in zip(closes, volumes)]

    def test_sma_volume_and_change(self):
        data = analyze(self.rows(range(1, 61), [10] * 59 + [30]))
        values = data["indicators"]
        self.assertEqual(values["sma20"], Decimal("50.5"))
        self.assertEqual(values["sma60"], Decimal("30.5"))
        self.assertEqual(values["volumeRatio"], 3)
        self.assertAlmostEqual(float(values["closeChangePct"]), 100 / 59)
        self.assertEqual(values["rsi14"], 100)

    def test_rsi_wilder_update(self):
        closes = [100 + i % 2 for i in range(15)] + [101]
        self.assertAlmostEqual(float(analyze(self.rows(closes))["indicators"]["rsi14"]), 53.5714285714)

    def test_rsi_flat_and_falling(self):
        self.assertEqual(analyze(self.rows([10] * 20))["indicators"]["rsi14"], 50)
        self.assertEqual(analyze(self.rows(range(20, 0, -1)))["indicators"]["rsi14"], 0)

    def test_short_and_zero_data(self):
        short = analyze(self.rows([10]))
        self.assertTrue(all(value is None for value in short["indicators"].values()))
        zero = analyze(self.rows([0] * 60, [0] * 60))
        for key in ("closeChangePct", "sma20GapPct", "volumeRatio"):
            self.assertIsNone(zero["indicators"][key])
            self.assertIn("분모", zero["unavailable"][key])

    def test_decimal_output(self):
        self.assertEqual(serialize(Decimal("1234000")), "1234000")
        self.assertEqual(serialize(Decimal("-0.00000000001")), "0")
        self.assertEqual(serialize(Decimal("1.234567895")), "1.2345679")


class MarketTests(unittest.TestCase):
    now = datetime(2026, 9, 5, 10, tzinfo=timezone.utc)

    def test_symbols_and_ordered_quotes(self):
        self.assertEqual(symbols(["005930", "aapl", "AAPL"]), ["005930", "AAPL"])
        client = FakeClient([[{"symbol": s, "lastPrice": "10", "currency": "USD", "timestamp": self.now.isoformat()}
                              for s in ["MSFT", "AAPL"]]])
        rows, _, _, code = quote(client, ["AAPL", "MSFT", "NVDA"])
        self.assertEqual([r["symbol"] for r in rows], ["AAPL", "MSFT"])
        self.assertEqual(code, 5)

    def test_excludes_current_day_and_paginates(self):
        client = FakeClient([
            {"candles": [candle("2026-09-05"), candle("2026-09-04")], "nextBefore": "2026-09-04T00:00:00+09:00"},
            {"candles": [candle("2026-09-04"), candle("2026-09-03")], "nextBefore": None}])
        rows, meta, _, code = history(client, "005930", 2, now=self.now)
        self.assertEqual([r["date"] for r in rows], ["2026-09-03", "2026-09-04"])
        self.assertEqual(meta["excludedDates"], ["2026-09-05"])
        self.assertEqual(meta["pages"], 2)
        self.assertEqual(code, 0)
        self.assertEqual(client.calls[-1][1]["before"], "2026-09-04T00:00:00+09:00")

    def test_conflicting_duplicate_is_error(self):
        client = FakeClient([{"candles": [candle("2026-09-04"), candle("2026-09-04", "101")], "nextBefore": None}])
        with self.assertRaises(TmonError):
            history(client, "005930", 2, now=self.now)

    def test_invalid_ohlc_and_naive_timestamp(self):
        bad = candle("2026-09-04")
        bad["highPrice"] = "99"
        for row in (bad, dict(candle("2026-09-04"), timestamp="2026-09-04T00:00:00"),
                    dict(candle("2026-09-04"), volume="NaN")):
            with self.subTest(row=row), self.assertRaises(TmonError):
                history(FakeClient([{"candles": [row], "nextBefore": None}]), "005930", 1, now=self.now)

    def test_us_date_mapping_and_explicit_calendar_date(self):
        client = FakeClient([{"candles": [candle("2026-09-04", currency="USD")], "nextBefore": None}], calendar("US"))
        rows, meta, _, _ = history(client, "AAPL", 1, now=self.now)
        self.assertEqual(rows[0]["date"], "2026-09-04")
        self.assertEqual(meta["currency"], "USD")
        self.assertEqual(client.calls[1][1]["date"], "2026-09-05")

    def test_market_day_excluded_even_after_close(self):
        cal = calendar("KR", "2026-09-04", "2026-09-03")
        now = timestamp("2026-09-04T23:59:00+09:00")
        self.assertEqual(str(calendar_cutoff(cal, "KR", now.date(), now)), "2026-09-03")

    def test_overnight_session_not_complete(self):
        cal = calendar("US")
        # Conservative cutoff also guards a previous session ending unusually late.
        cal["previousBusinessDay"]["afterMarket"]["endTime"] = "2026-09-05T15:00:00+09:00"
        now = timestamp("2026-09-05T14:00:00+09:00")
        self.assertEqual(str(calendar_cutoff(cal, "US", now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date(), now)), "2026-09-03")

    def test_bad_calendar_fails_closed(self):
        with self.assertRaises(TmonError):
            calendar_cutoff({}, "US", self.now.date(), self.now)

    def test_us_winter_local_date(self):
        row = candle("2026-01-05", currency="USD")
        row["timestamp"] = "2026-01-05T14:00:00+09:00"
        _, normalized = normalize_candle(row, "USD")
        self.assertEqual(normalized["date"], "2026-01-05")

    def test_pagination_stalls_and_returns_partial(self):
        cursor = "2026-09-04T00:00:00+09:00"
        page = {"candles": [candle("2026-09-04")], "nextBefore": cursor}
        client = FakeClient([page, page])
        _, meta, warnings, code = history(client, "005930", 20, now=self.now)
        self.assertEqual(meta["pages"], 2)
        self.assertEqual(code, 5)
        self.assertIn("pagination-stalled", [w["code"] for w in warnings])

    def test_page_limit(self):
        pages = [{"candles": [candle("2026-09-%02d" % day)],
                  "nextBefore": "2026-09-%02dT00:00:00+09:00" % (day - 1)} for day in [4, 3, 2]]
        _, meta, _, code = history(FakeClient(pages), "005930", 200, now=self.now)
        self.assertEqual(meta["pages"], 3)
        self.assertEqual(code, 5)


class StubTransport(Transport):
    def __init__(self, responses):
        super().__init__()
        self.responses, self.calls, self.waits = iter(responses), [], []

    def send(self, method, path, headers, body=None):
        self.calls.append((method, path))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result

    def pause(self, seconds):
        self.waits.append(seconds)


class TransportTests(unittest.TestCase):
    def test_429_then_success_and_url_encoding(self):
        transport = StubTransport([(429, {"retry-after": "2"}, {}), (200, {}, {"result": []})])
        transport.request("GET", "/api/v1/candles", {"before": "2026-09-04T00:00:00+09:00"})
        self.assertGreaterEqual(transport.waits[0], 2)
        self.assertIn("%2B09%3A00", transport.calls[0][1])

    def test_bounded_retries_and_no_post_retry(self):
        for method, path, attempts in [("GET", "/api/v1/prices", 3), ("POST", "/oauth2/token", 1)]:
            transport = StubTransport([(503, {}, {})] * 5)
            with self.assertRaises(TmonError):
                transport.request(method, path)
            self.assertEqual(len(transport.calls), attempts)

    def test_no_retry_for_403_and_safe_message(self):
        transport = StubTransport([(403, {}, {"error": {"code": "forbidden", "message": "test-secret"}})])
        with self.assertRaises(TmonError) as caught:
            transport.request("GET", "/api/v1/prices")
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertEqual(len(transport.calls), 1)

    def test_rate_headers_and_remaining_budget(self):
        transport = StubTransport([(200, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"}, {}), (200, {}, {})])
        transport.request("GET", "/api/v1/prices")
        transport.request("GET", "/api/v1/prices")
        self.assertGreater(transport.waits[0], 0)
        with self.assertRaises(TmonError):
            Transport(budget=0.01).pause(2)

    def test_trading_endpoints_rejected(self):
        for method in ("GET", "POST"):
            with self.assertRaises(TmonError):
                Transport().request(method, "/api/v1/orders")

    def test_one_auth_recovery(self):
        class FakeAuth:
            def __init__(self):
                self.calls = []
            def token(self, rejected=None):
                self.calls.append(rejected)
                return "first" if rejected is None else "second"
        transport = StubTransport([(401, {}, {"error": {"code": "expired-token"}}), (200, {}, {"result": []})])
        auth = FakeAuth()
        self.assertEqual(TossClient(auth, transport).get("/api/v1/prices"), [])
        self.assertEqual(auth.calls, [None, "first"])


class IssueTransport(Transport):
    def __init__(self, counter=None):
        super().__init__()
        self.count = 0
        self.counter = counter

    def request(self, *args, **kwargs):
        self.count += 1
        if self.counter is not None:
            with self.counter.get_lock():
                self.counter.value += 1
        time.sleep(0.03)
        return {"access_token": "fake-token-%d" % self.count, "expires_in": 86400, "token_type": "Bearer"}


def auth_worker(directory, counter, event):
    event.wait()
    Auth(IssueTransport(counter), directory).token()


@patch.dict(os.environ, ENV)
class AuthTests(unittest.TestCase):
    def test_cache_reuse_permissions_and_rejected_token(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = IssueTransport()
            auth = Auth(transport, directory)
            token = auth.token()
            self.assertEqual(Auth(transport, directory).token(), token)
            self.assertEqual(transport.count, 1)
            self.assertEqual(stat.S_IMODE(auth.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(Path(directory).stat().st_mode), 0o700)
            self.assertNotEqual(Auth(transport, directory).token(rejected=token), token)
            self.assertEqual(transport.count, 2)

    def test_other_process_token_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = IssueTransport()
            first = Auth(transport, directory)
            token = first.token()
            other = Auth(transport, directory).token(rejected=token)
            self.assertEqual(first.token(rejected=token), other)
            self.assertEqual(transport.count, 2)

    def test_expiry_refresh_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = IssueTransport()
            auth = Auth(transport, directory)
            auth.token()
            auth.path.write_text(json.dumps({"access_token": "expired", "expires_at": 0}))
            Auth(transport, directory).token()
            self.assertEqual(transport.count, 2)
            auth.path.unlink()
            auth.path.symlink_to(Path(directory) / "somewhere")
            with self.assertRaises(TmonError):
                Auth(transport, directory).token()
            self.assertEqual(transport.count, 2)

    def test_cross_process_issuance_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = multiprocessing.get_context("fork")
            count, event = ctx.Value("i", 0), ctx.Event()
            processes = [ctx.Process(target=auth_worker, args=(directory, count, event)) for _ in range(3)]
            for process in processes:
                process.start()
            event.set()
            for process in processes:
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    self.fail("lock did not complete")
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(count.value, 1)


class CLITests(unittest.TestCase):
    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_input_validation_before_auth(self):
        with patch("tmon.cli.Auth", side_effect=AssertionError("must not authenticate")):
            for args in (["history", "AAPL", "--count", "0", "--json"], ["quote", "bad/symbol", "--json"],
                         ["--json", "unknown"], ["quote", "--json"]):
                code, stdout, stderr = self.invoke(args)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(stdout)["status"], "error")
                self.assertEqual(stderr, "")

    def test_doctor_without_network_or_secret_output(self):
        with tempfile.TemporaryDirectory() as directory, patch("tmon.auth.cache_directory", return_value=Path(directory)), \
                patch.dict(os.environ, ENV), patch("tmon.cli.Transport", side_effect=AssertionError("no network")):
            code, stdout, _ = self.invoke(["doctor", "--json"])
            self.assertEqual(code, 0)
            data = json.loads(stdout)
            self.assertEqual(data["data"]["remote"], "미확인")
            self.assertNotIn("test-secret", stdout)

    def test_missing_credentials(self):
        with patch.dict(os.environ, {"TOSS_CLIENT_ID": "", "TOSS_CLIENT_SECRET": ""}):
            code, stdout, _ = self.invoke(["quote", "AAPL", "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stdout)["error"]["code"], "missing-credentials")

    def test_doctor_failure_keeps_check_details(self):
        with patch.dict(os.environ, {"TOSS_CLIENT_ID": "", "TOSS_CLIENT_SECRET": ""}):
            code, stdout, _ = self.invoke(["doctor", "--json"])
            data = json.loads(stdout)
            self.assertEqual(code, 2)
            self.assertIsNone(data["data"])
            self.assertEqual(data["meta"]["checks"]["TOSS_CLIENT_ID"], "누락")

    def test_short_analysis_exit_contract(self):
        for count, expected_code in [(20, 0), (1, 5)]:
            rows = [{"closePrice": Decimal(10), "volume": Decimal(10)} for _ in range(count)]
            with patch("tmon.cli.Auth"), patch("tmon.cli.TossClient"), \
                    patch("tmon.cli.history", return_value=(rows, {"usedCount": count}, [], 5)):
                code, stdout, _ = self.invoke(["analyze", "AAPL", "--json"])
                self.assertEqual(code, expected_code)
                self.assertEqual(json.loads(stdout)["status"], "partial")

    def test_help_never_authenticates(self):
        with patch("tmon.cli.Auth", side_effect=AssertionError("no auth")):
            for command in ([], ["quote"], ["history"], ["analyze"], ["doctor"]):
                with self.assertRaises(SystemExit) as caught:
                    self.invoke(command + ["--help"])
                self.assertEqual(caught.exception.code, 0)

    def test_json_and_table_have_same_values(self):
        raw = [{"symbol": "005930", "lastPrice": "257000", "currency": "KRW", "timestamp": "2026-09-04T20:00:00+09:00"}]
        with patch("tmon.cli.Auth"), patch("tmon.cli.TossClient") as client:
            client.return_value.get.return_value = raw
            code, output, stderr = self.invoke(["--json", "quote", "005930"])
            self.assertEqual(code, 0)
            value = json.loads(output)["data"][0]["lastPrice"]
            _, table_output, _ = self.invoke(["quote", "005930", "--no-color"])
            self.assertIn(value, table_output)
            self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
