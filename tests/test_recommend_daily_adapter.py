# -*- coding: utf-8 -*-
"""PR4-B adapters: exact daily windows and paginated budget checks."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from tmon.errors import TmonError
from tmon.market import history, normalize_candle, timestamp
from tmon.recommend_data import daily_data
from tmon.recommend_daily import DAILY_CALENDAR_UNVERIFIED, DAILY_GAP

from tests.recommend_calendar_fixture import NOW, calendar_for, daily_bars


CALENDAR_PATH = "/api/v1/market-calendar/KR"


def normalized_bars(count):
    return [normalize_candle(row, "KRW")[1] for row in daily_bars(NOW, count)]


def reference_for(rows):
    return {
        "status": "verified",
        "verified": True,
        "dates": [row["date"] for row in rows],
        "source": {"endpoint": CALENDAR_PATH, "fixture": True},
    }


class FixturePages:
    """Calendar plus deterministic candle pages, with an inclusive cursor."""

    def __init__(self, raw_rows, page_size=None):
        self.raw_rows = list(raw_rows)
        self.page_size = page_size or len(self.raw_rows)
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, dict(params)))
        if path == CALENDAR_PATH:
            return deepcopy(calendar_for(NOW))
        if path != "/api/v1/candles":
            raise AssertionError(path)
        before = params.get("before")
        if before is None:
            end = len(self.raw_rows)
        else:
            cursor = timestamp(before)
            end = next(index for index, row in enumerate(self.raw_rows)
                       if timestamp(row["timestamp"]) == cursor) + 1
        start = max(0, end - self.page_size)
        next_before = self.raw_rows[start]["timestamp"] if start else None
        return {"candles": list(reversed(self.raw_rows[start:end])),
                "nextBefore": next_before}

    @property
    def candle_calls(self):
        return [call for call in self.calls if call[0] == "/api/v1/candles"]


class AuthPages(FixturePages):
    def get(self, path, **params):
        if path == "/api/v1/candles":
            self.calls.append((path, dict(params)))
            raise TmonError("auth-expired", "auth", 3)
        return super().get(path, **params)


class AdapterTests(unittest.TestCase):
    def test_explicit_horizons_request_20_and_65_instead_of_legacy_120(self):
        for horizon, count in (("day", 20), ("swing", 65)):
            with self.subTest(horizon=horizon), patch("tmon.recommend_data.history") as mocked:
                rows = normalized_bars(count)
                mocked.return_value = (rows, {"requestedCount": count}, [], 0)
                result = daily_data(
                    object(), "000001", calendar_for(NOW), NOW,
                    horizon=horizon, reference=reference_for(rows))
                self.assertEqual(result, rows)
                self.assertEqual(mocked.call_args.args[2], count)
                self.assertEqual(mocked.call_args.kwargs["now"], NOW)

    def test_missing_reference_is_explicit_unverified_without_fetch(self):
        class NoCall:
            def __init__(self):
                self.calls = []

            def get(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                raise AssertionError("missing reference must fail before fetch")

        client = NoCall()
        with self.assertRaises(TmonError) as caught:
            daily_data(client, "000001", calendar_for(NOW), NOW,
                       horizon="day", reference=None)
        self.assertEqual(caught.exception.code, DAILY_CALENDAR_UNVERIFIED)
        self.assertEqual(caught.exception.details["reason"], DAILY_CALENDAR_UNVERIFIED)
        self.assertEqual(caught.exception.details["dailyValidation"]["reason"],
                         DAILY_CALENDAR_UNVERIFIED)
        self.assertEqual(client.calls, [])

    def test_verified_reference_turns_real_missing_date_into_daily_gap(self):
        complete = normalized_bars(20)
        missing = complete[:7] + complete[8:]
        with patch("tmon.recommend_data.history", return_value=(missing, {}, [], 5)):
            with self.assertRaises(TmonError) as caught:
                daily_data(object(), "000001", calendar_for(NOW), NOW,
                           horizon="day", reference=reference_for(complete))
        self.assertEqual(caught.exception.code, DAILY_GAP)
        self.assertEqual(caught.exception.details["missingDates"], [complete[7]["date"]])
        self.assertEqual(caught.exception.details["dailyValidation"]["reason"], DAILY_GAP)

    def test_legacy_daily_data_keeps_120_and_newest_stale_behavior(self):
        rows = normalized_bars(120)[:-1]
        with patch("tmon.recommend_data.history", return_value=(rows, {}, [], 5)) as mocked:
            with self.assertRaises(TmonError) as caught:
                daily_data(object(), "000001", calendar_for(NOW), NOW)
        self.assertEqual(caught.exception.code, "stale-daily")
        self.assertEqual(mocked.call_args.args[2], 120)
        self.assertNotIn("dailyValidation", getattr(caught.exception, "details", {}))

    def test_history_allows_none_budget_return_and_checks_before_each_page(self):
        client = FixturePages(daily_bars(NOW, 25), page_size=25)
        rows, _, _, code = history(
            client, "000001", 20, now=NOW, check_budget=lambda: None)
        self.assertEqual(len(rows), 20)
        self.assertEqual(code, 0)
        self.assertEqual(len(client.candle_calls), 1)

    def test_history_budget_error_stops_before_next_page(self):
        client = FixturePages(daily_bars(NOW, 25), page_size=11)
        checks = []

        def budget():
            checks.append(True)
            if len(checks) == 2:
                raise TmonError("recommend-budget", "budget", 4)

        with self.assertRaises(TmonError) as caught:
            history(client, "000001", 20, now=NOW, check_budget=budget)
        self.assertEqual(caught.exception.code, "recommend-budget")
        self.assertEqual(caught.exception.exit_code, 4)
        self.assertEqual(len(checks), 2)
        self.assertEqual(len(client.candle_calls), 1)

    def test_history_auth_error_propagates_from_page_callback_path(self):
        client = AuthPages(daily_bars(NOW, 20))
        with self.assertRaises(TmonError) as caught:
            history(client, "000001", 20, now=NOW, check_budget=lambda: None)
        self.assertEqual(caught.exception.code, "auth-expired")
        self.assertEqual(caught.exception.exit_code, 3)

    def test_history_false_budget_result_becomes_budget_error(self):
        client = FixturePages(daily_bars(NOW, 20))
        with self.assertRaises(TmonError) as caught:
            history(client, "000001", 20, now=NOW, check_budget=lambda: False)
        self.assertEqual(caught.exception.code, "recommend-budget")
        self.assertEqual(client.candle_calls, [])


if __name__ == "__main__":
    unittest.main()
