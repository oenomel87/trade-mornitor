import contextlib
from copy import deepcopy
from decimal import Decimal
import io
import json
import unittest
from unittest.mock import patch

from tmon.cli import main
from tmon.errors import TmonError
from tmon.ranking import change_percent, rank


def row(symbol="005930", position=1, currency="KRW", rate="0.0125"):
    return {"symbol": symbol, "rank": position, "currency": currency,
            "price": {"lastPrice": "10125", "basePrice": "10000", "changeRate": rate},
            "tradingVolume": "1200", "tradingAmount": "12150000"}


class FakeClient:
    def __init__(self, rows=None, ranked_at="2026-09-04T20:00:00+09:00"):
        self.response = {"rankings": [row()] if rows is None else rows, "rankedAt": ranked_at}
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        return deepcopy(self.response)


class RankingTests(unittest.TestCase):
    def test_all_six_type_mappings_and_request_options(self):
        kinds = [("amount", "market", "MARKET_TRADING_AMOUNT"),
                 ("volume", "market", "MARKET_TRADING_VOLUME"),
                 ("amount", "toss", "TOSS_SECURITIES_TRADING_AMOUNT"),
                 ("volume", "toss", "TOSS_SECURITIES_TRADING_VOLUME"),
                 ("gain", "market", "TOP_GAINERS"), ("loss", "market", "TOP_LOSERS")]
        for by, source, kind in kinds:
            with self.subTest(kind=kind):
                client = FakeClient([row("AAPL", currency="USD")])
                _, meta, _, code = rank(client, "US", by, "1w", source, 10, True)
                path, params = client.calls[0]
                self.assertEqual(path, "/api/v1/rankings")
                self.assertEqual(params, {"type": kind, "marketCountry": "US", "duration": "1w", "count": 10, "excludeInvestmentCaution": "true"})
                self.assertEqual(meta["aggregationScope"], source)
                self.assertEqual(meta["changeBasis"], "period-start" if by in ("gain", "loss") else "previous-close")
                self.assertEqual(code, 0)

    def test_signed_decimal_rate_is_percent(self):
        self.assertEqual(change_percent("0.0125"), Decimal("1.25"))
        self.assertEqual(change_percent("-0.0107"), Decimal("-1.07"))
        self.assertEqual(change_percent("0"), 0)
        self.assertEqual(change_percent("-0"), 0)
        self.assertIsNone(change_percent(None))
        for invalid in ("NaN", "-Infinity", "--1", 1.25):
            with self.assertRaises(TmonError):
                change_percent(invalid)

    def test_missing_and_null_change_rate_are_not_recomputed(self):
        first, second = row(rate=None), row("005380", 2)
        del second["price"]["changeRate"]
        first["price"]["basePrice"] = "0"
        rows, meta, warnings, code = rank(FakeClient([first, second]), limit=2)
        self.assertTrue(all(r["changePct"] is None for r in rows))
        self.assertEqual(meta["missingChangeSymbols"], ["005930", "005380"])
        self.assertEqual(warnings[0]["code"], "missing-change-rate")
        self.assertEqual(code, 0)

    def test_provider_rank_order_and_gaps_are_preserved(self):
        rows, _, _, _ = rank(FakeClient([row("005380", 4), row("005930", 1)]), limit=2)
        self.assertEqual([r["rank"] for r in rows], [1, 4])
        self.assertEqual([r["symbol"] for r in rows], ["005930", "005380"])

    def test_empty_result_and_null_rank_time(self):
        rows, meta, warnings, code = rank(FakeClient([], None))
        self.assertEqual(rows, [])
        self.assertEqual(meta["returnedCount"], 0)
        self.assertIsNone(meta["rankedAt"])
        self.assertEqual(code, 0)
        self.assertEqual(warnings, [])

    def test_fewer_rows_and_missing_time_warn_but_succeed(self):
        rows, meta, warnings, code = rank(FakeClient(ranked_at=None), limit=20)
        self.assertEqual(len(rows), 1)
        self.assertEqual(meta["requestedCount"], 20)
        self.assertEqual(code, 0)
        self.assertEqual({w["code"] for w in warnings}, {"fewer-ranking-results", "missing-ranked-at"})

    def test_bad_provider_fields_fail(self):
        cases = [dict(row(), rank=True), dict(row(), rank=0), dict(row(), currency="USD"),
                 dict(row(), symbol="005930\x1b"), dict(row(), tradingVolume="-1"), dict(row(), price=[])]
        missing = row()
        del missing["tradingAmount"]
        cases.append(missing)
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(TmonError) as caught:
                rank(FakeClient([bad]))
            self.assertEqual(caught.exception.exit_code, 5)

    def test_duplicate_symbols_and_excess_rows_fail(self):
        for rows, limit in [([row(), row(position=2)], 2), ([row(), row("005380", 2)], 1)]:
            with self.assertRaises(TmonError):
                rank(FakeClient(rows), limit=limit)

    def test_bad_timestamp_is_data_error(self):
        with self.assertRaises(TmonError):
            rank(FakeClient(ranked_at="2026-09-04T20:00:00"))

    def test_us_amount_has_no_assumed_dollar_unit(self):
        client = FakeClient([row("AAPL", currency="USD")])
        rows, _, warnings, code = rank(client, market="US", limit=1)
        self.assertEqual(rows[0]["currency"], "USD")
        self.assertEqual(rows[0]["tradingAmount"], Decimal("12150000"))
        self.assertIsNone(rows[0]["tradingAmountCurrency"])
        self.assertEqual(warnings[0]["code"], "unverified-trading-amount-currency")
        self.assertEqual(code, 0)


class RankingCLITests(unittest.TestCase):
    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_invalid_combinations_do_not_authenticate(self):
        options = [["--by", "gain", "--duration", "realtime"], ["--by", "loss", "--source", "toss"],
                   ["--limit", "0"], ["--limit", "101"], ["--market", "ALL"], ["--duration", "2d"]]
        with patch("tmon.cli.Auth", side_effect=AssertionError("no auth")), patch("tmon.cli.Transport", side_effect=AssertionError("no network")):
            for flags in options:
                code, out, err = self.invoke(["rank"] + flags + ["--json"])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)["status"], "error")
                self.assertEqual(json.loads(out)["command"], "rank")
                self.assertEqual(err, "")

    def test_defaults_and_json_contract(self):
        client = FakeClient()
        with patch("tmon.cli.Auth"), patch("tmon.cli.TossClient", return_value=client):
            code, out, err = self.invoke(["rank", "--json"])
        result = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"][0]["changePct"], "1.25")
        self.assertEqual(result["meta"]["market"], "KR")
        self.assertEqual(result["meta"]["duration"], "1d")
        self.assertEqual(result["meta"]["aggregationScope"], "market")
        self.assertEqual(result["meta"]["requestedCount"], 20)

    def test_table_labels_period_and_scope(self):
        client = FakeClient([row("AAPL", currency="USD", rate="-0.1")])
        with patch("tmon.cli.Auth"), patch("tmon.cli.TossClient", return_value=client):
            code, out, _ = self.invoke(["rank", "--market", "us", "--by", "loss", "--duration", "1w"])
            self.assertEqual(code, 0)
            self.assertIn("기간 시작 대비", out)
            self.assertIn("-10", out)
            self.assertIn("USD", out)
            _, out, _ = self.invoke(["rank", "--market", "US", "--by", "volume", "--source", "toss", "--duration", "1w"])
            self.assertIn("토스증권 체결", out)
            self.assertIn("전일 대비", out)
            self.assertIn("랭킹 집계 시각", out)

    def test_empty_table_is_success(self):
        with patch("tmon.cli.Auth"), patch("tmon.cli.TossClient", return_value=FakeClient([], None)):
            code, out, _ = self.invoke(["rank"])
            self.assertEqual(code, 0)
            self.assertIn("집계된 랭킹이 없습니다", out)

    def test_help_without_network(self):
        with patch("tmon.cli.Auth", side_effect=AssertionError("no auth")):
            with self.assertRaises(SystemExit) as caught:
                self.invoke(["rank", "--help"])
            self.assertEqual(caught.exception.code, 0)
