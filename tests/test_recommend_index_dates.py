# -*- coding: utf-8 -*-
"""PR4-B optional canonical trading-date checks for swing index snapshots."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from tmon.recommend_data import ensure_frozen_index


KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 4, 10, 0, 10, tzinfo=KST)
DATES = ["2026-08-27", "2026-08-28", "2026-08-31",
         "2026-09-01", "2026-09-02", "2026-09-03"]
ALT_DATES = ["2026-08-27", "2026-08-28", "2026-08-30",
             "2026-09-01", "2026-09-02", "2026-09-03"]


def index_bar(day_text, close="1000"):
    return {
        "timestamp": day_text + "T09:00:00+09:00",
        "openPrice": close,
        "highPrice": str(int(close) + 5),
        "lowPrice": str(int(close) - 5),
        "closePrice": close,
        "volume": "100000",
    }


class IndexClient:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, dict(params)))
        return {"candles": deepcopy(list(reversed(self.rows))), "nextBefore": None}


def run_snapshot(client, dates, cache=None):
    return ensure_frozen_index(
        client, "KOSPI", "swing", dates[0], dates[-1], now=NOW,
        budget_remaining=lambda: True, cache=cache,
        trading_dates=dates)


class TradingDateIndexTests(unittest.TestCase):
    def test_complete_six_date_reference_is_verified_and_audited(self):
        client = IndexClient([index_bar(day) for day in DATES])
        result = run_snapshot(client, DATES, {})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["expectedTradingDates"], DATES)
        self.assertEqual(result["observedTradingDates"], DATES)
        self.assertEqual(result["observedSpanDates"], DATES)

    def test_missing_interior_date_fails_even_when_endpoints_exist(self):
        rows = [index_bar(day) for day in DATES if day != DATES[2]]
        client = IndexClient(rows)
        result = run_snapshot(client, DATES, {})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "index-bar-missing")
        self.assertEqual(result["missingTradingDates"], [DATES[2]])
        self.assertEqual(len(client.calls), 2)  # existing bounded retry

    def test_unexpected_date_inside_reference_span_is_data_invalid(self):
        rows = [index_bar(day) for day in DATES] + [index_bar("2026-08-30")]
        result = run_snapshot(IndexClient(rows), DATES, {})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "index-data-invalid")
        self.assertEqual(result["unexpectedTradingDates"], ["2026-08-30"])
        self.assertIn("2026-08-30", result["observedTradingDates"])

    def test_bogus_calendar_window_is_unverified_without_index_fetch(self):
        client = IndexClient([index_bar(day) for day in DATES])
        bogus = DATES[:-1] + ["2026-09-04"]
        result = ensure_frozen_index(
            client, "KOSPI", "swing", DATES[0], DATES[-1], now=NOW,
            budget_remaining=lambda: True, cache={}, trading_dates=bogus)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "comparison-basis-unverified")
        self.assertEqual(client.calls, [])

    def test_two_provided_date_bases_do_not_share_cache_entry(self):
        class SwitchingClient:
            def __init__(self):
                self.calls = []

            def get(self, path, **params):
                self.calls.append((path, dict(params)))
                dates = DATES if len(self.calls) == 1 else ALT_DATES
                return {"candles": [index_bar(day) for day in dates],
                        "nextBefore": None}

        client = SwitchingClient()
        cache = {}
        first = run_snapshot(client, DATES, cache)
        second = run_snapshot(client, ALT_DATES, cache)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")
        self.assertNotEqual(first["key"], second["key"])
        self.assertEqual(len(client.calls), 2)

    def test_calls_without_reference_keep_endpoint_only_pr2_semantics(self):
        client = IndexClient([index_bar(DATES[0]), index_bar(DATES[-1])])
        result = ensure_frozen_index(
            client, "KOSPI", "swing", DATES[0], DATES[-1], now=NOW,
            budget_remaining=lambda: True, cache={})
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("expectedTradingDates", result)


if __name__ == "__main__":
    unittest.main()
