"""PR4-A focused tests for bounded, evidence-preserving stock prechecks."""

from datetime import datetime, timedelta, timezone
import unittest

from tmon.errors import TmonError
from tmon.recommend_data import RecordingClient
from tmon.recommend_precheck import MAX_STOCK_BATCH, precheck_universe


KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 8, 10, 0, tzinfo=KST)


def candidate(symbol, rank=1):
    return {"symbol": symbol, "preRank": 1 / (60 + rank),
            "ranks": [{"by": "amount", "duration": "1d", "rank": rank,
                       "asOf": NOW.isoformat()}]}


def stock(symbol, **changes):
    row = {
        "symbol": symbol,
        "name": "테스트보통주-" + symbol,
        "market": "KOSPI",
        "securityType": "STOCK",
        "isCommonShare": True,
        "status": "ACTIVE",
        "currency": "KRW",
        "listDate": "2020-01-02",
        "koreanMarketDetail": {
            "nxtSupported": False,
            "nxtTradingSuspended": None,
            "liquidationTrading": False,
            "krxTradingSuspended": False,
        },
    }
    row.update(changes)
    return row


class PlainStockClient:
    """Simple fake without RecordingClient's request ledger."""

    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        return self.responder(params)


class PrecheckTests(unittest.TestCase):
    def test_cap_applies_after_precheck_and_excludes_etf(self):
        rows = [candidate("ETF", 1), candidate("A", 2), candidate("B", 3)]

        def respond(params):
            return [stock(symbol, **({"securityType": "ETF", "isCommonShare": False}
                                  if symbol == "ETF" else {}))
                    for symbol in params["symbols"].split(",")]

        client = PlainStockClient(respond)
        result = precheck_universe(client, rows, "day", 2, now=lambda: NOW)

        self.assertEqual(result["universeCount"], 3)
        self.assertEqual([r["symbol"] for r in result["eligibleRows"]], ["A", "B"])
        self.assertEqual([r["symbol"] for r in result["selectedRows"]], ["A", "B"])
        self.assertEqual([r["symbol"] for r in result["preExcluded"]], ["ETF"])
        self.assertEqual(result["preExcluded"][0]["reason"], "not-common-stock")
        self.assertEqual(result["dataUnavailable"], [])
        self.assertEqual(result["notEvaluated"], [])

    def test_batches_are_split_at_provider_limit_and_order_is_preserved(self):
        rows = [candidate("S%03d" % i, i + 1) for i in range(MAX_STOCK_BATCH * 2 + 1)]

        def respond(params):
            return [stock(symbol) for symbol in params["symbols"].split(",")]

        client = PlainStockClient(respond)
        result = precheck_universe(client, rows, "day", len(rows), now=lambda: NOW)

        self.assertEqual(len(client.calls), 3)
        self.assertEqual([len(call[1]["symbols"].split(",")) for call in client.calls], [200, 200, 1])
        self.assertEqual([r["symbol"] for r in result["eligibleRows"]],
                         [r["symbol"] for r in rows])
        self.assertEqual(len(result["stockRaw"]), len(rows))

    def test_missing_and_duplicate_rows_are_data_unavailable_per_symbol(self):
        rows = [candidate("A"), candidate("B")]
        responses = [[stock("A")], [stock("A"), stock("A"), stock("B")]]

        for response, unavailable_reason in zip(responses, ("missing-symbol", "duplicate-symbol")):
            client = PlainStockClient(lambda params, response=response: response)
            result = precheck_universe(client, rows, "day", 2, now=lambda: NOW)
            self.assertEqual([r["symbol"] for r in result["eligibleRows"]],
                             ["A"] if unavailable_reason == "missing-symbol" else ["B"])
            self.assertEqual(result["dataUnavailable"][0]["symbol"],
                             "B" if unavailable_reason == "missing-symbol" else "A")
            self.assertEqual(result["dataUnavailable"][0]["reason"], unavailable_reason)
            self.assertEqual(result["preExcluded"], [])

    def test_unrequested_row_invalidates_the_whole_batch(self):
        rows = [candidate("A"), candidate("B")]
        client = PlainStockClient(lambda params: [stock("A"), stock("OUTSIDE")])
        result = precheck_universe(client, rows, "day", 2, now=lambda: NOW)

        self.assertEqual(result["eligibleRows"], [])
        self.assertEqual({r["symbol"] for r in result["dataUnavailable"]}, {"A", "B"})
        self.assertTrue(all(r["reason"] == "unrequested-symbol"
                            for r in result["dataUnavailable"]))
        self.assertEqual(result["dataUnavailable"][0]["unrequestedSymbols"], ["OUTSIDE"])

    def test_non_list_response_is_data_unavailable_without_assignment(self):
        rows = [candidate("A"), candidate("B")]
        client = PlainStockClient(lambda params: {"symbol": "A", "name": "wrong-shape"})
        result = precheck_universe(client, rows, "day", 2, now=lambda: NOW)
        self.assertEqual(result["eligibleRows"], [])
        self.assertEqual({r["symbol"] for r in result["dataUnavailable"]}, {"A", "B"})
        self.assertTrue(all(r["reason"] == "invalid-data" for r in result["dataUnavailable"]))
        self.assertEqual(result["dataUnavailable"][0]["responseType"], "dict")

    def test_condition_and_data_records_are_separate_and_listing_evidence_survives(self):
        rows = [candidate("ETF"), candidate("BROKEN"), candidate("NEW")]

        def respond(params):
            return [
                stock("ETF", securityType="ETF", isCommonShare=False),
                stock("BROKEN", koreanMarketDetail={"krxTradingSuspended": "yes",
                                                      "liquidationTrading": False,
                                                      "nxtSupported": False}),
                stock("NEW", listDate="2026-09-01"),
            ]

        result = precheck_universe(PlainStockClient(respond), rows, "day", 3,
                                   now=lambda: NOW)
        self.assertEqual([r["symbol"] for r in result["preExcluded"]], ["ETF", "NEW"])
        self.assertEqual(result["preExcluded"][0]["kind"], "condition")
        self.assertEqual(result["preExcluded"][1]["reason"], "listing-history-too-short")
        self.assertEqual(result["preExcluded"][1]["listDate"], "2026-09-01")
        self.assertEqual(result["preExcluded"][1]["requiredDailyBars"], 20)
        self.assertEqual([r["symbol"] for r in result["dataUnavailable"]], ["BROKEN"])
        self.assertEqual(result["dataUnavailable"][0]["kind"], "data")
        self.assertEqual(result["precheckDataUnavailable"], 1)

    def test_auth_is_fatal_and_does_not_become_data_unavailable(self):
        rows = [candidate("A"), candidate("B")]

        def respond(params):
            raise TmonError("auth-expired", "auth", 3)

        client = PlainStockClient(respond)
        with self.assertRaises(TmonError) as caught:
            precheck_universe(client, rows, "day", 2, now=lambda: NOW)
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertEqual(len(client.calls), 1)

    def test_budget_is_checked_before_each_batch_and_remaining_rows_are_skipped(self):
        rows = [candidate("S%03d" % i, i + 1) for i in range(201)]
        allowed = [True, False]

        def respond(params):
            return [stock(symbol) for symbol in params["symbols"].split(",")]

        client = PlainStockClient(respond)
        result = precheck_universe(client, rows, "day", 201, now=lambda: NOW,
                                   budget_remaining=lambda: allowed.pop(0))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(result["eligibleRows"]), 200)
        self.assertEqual([r["reason"] for r in result["notEvaluated"]],
                         ["not-evaluated-budget"])
        self.assertEqual(result["notEvaluated"][0]["symbol"], "S200")

    def test_engine_style_budget_check_returns_none_when_check_passes(self):
        rows = [candidate("A"), candidate("B")]
        checks = []

        def check_budget():
            checks.append(True)
            # Engine.check_budget() raises on failure and returns None on pass.
            return None

        client = PlainStockClient(lambda params: [stock(symbol)
                                                   for symbol in params["symbols"].split(",")])
        result = precheck_universe(client, rows, "day", 2, now=lambda: NOW,
                                   budget_check=check_budget)
        self.assertEqual(len(checks), 1)
        self.assertEqual([r["symbol"] for r in result["selectedRows"]], ["A", "B"])
        self.assertEqual(result["notEvaluated"], [])

    def test_received_at_is_per_batch_and_raw_mapping_is_single_symbol_reusable(self):
        rows = [candidate("A"), candidate("B"), candidate("C")]
        received = [NOW, NOW + timedelta(seconds=3)]

        def respond(params):
            return [stock(symbol) for symbol in params["symbols"].split(",")]

        # Force two requests with a low local batch limit by constructing a
        # 201-row batch and inspect the first and last batch markers.
        many = rows + [candidate("S%03d" % i, i + 4) for i in range(198)]
        client = PlainStockClient(respond)
        result = precheck_universe(client, many, "day", 201,
                                   now=lambda: received.pop(0) if received else NOW)
        self.assertEqual(len(result["batches"]), 2)
        self.assertEqual(result["receivedAtBySymbol"]["A"], NOW.isoformat())
        self.assertEqual(result["receivedAtBySymbol"]["S197"],
                         (NOW + timedelta(seconds=3)).isoformat())
        self.assertEqual(result["stockRaw"]["A"], [stock("A")])
        self.assertEqual(result["stockRaw"]["B"], [stock("B")])

    def test_recording_client_receive_marker_is_reused_without_an_extra_refresh(self):
        rows = [candidate("A"), candidate("B")]
        inner = PlainStockClient(lambda params: [stock(symbol) for symbol in params["symbols"].split(",")])
        recorder = RecordingClient(inner, now=lambda: NOW)
        result = precheck_universe(recorder, rows, "day", 2, now=lambda: NOW + timedelta(days=1))
        marker = recorder.records[-1]["receivedAt"]
        self.assertEqual(result["batches"][0]["receivedAt"], marker)
        self.assertEqual(result["receivedAtBySymbol"]["A"], marker)
        self.assertEqual(result["receivedAtBySymbol"]["B"], marker)

    def test_empty_union_does_not_call_client(self):
        client = PlainStockClient(lambda params: self.fail("no request expected"))
        result = precheck_universe(client, [], "day", 2, now=lambda: NOW)
        self.assertEqual(client.calls, [])
        self.assertEqual(result["universeCount"], 0)
        self.assertEqual(result["batches"], [])


if __name__ == "__main__":
    unittest.main()
