# -*- coding: utf-8 -*-
"""Independent PR4-B daily-history evidence helper tests."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import unittest

from tmon.errors import TmonError
from tmon.recommend import Engine
from tmon.recommend_daily import (
    ADJUSTMENT_BASIS_UNVERIFIED,
    CALENDAR_PATH,
    DAILY_CALENDAR_UNVERIFIED,
    DAILY_GAP,
    common_trading_dates,
    validate_daily_dates,
    verify_adjustment_basis,
)
from tmon.recommend_data import RecordingClient
from tmon.strategies import NoMatch


KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 7, 10, 0, 0, tzinfo=KST)


def business_day(day_text):
    day = datetime.fromisoformat(day_text).replace(tzinfo=KST)
    return {
        "date": day_text,
        "integrated": {
            "preMarket": None,
            "afterMarket": None,
            "regularMarket": {
                "startTime": day.replace(hour=9).isoformat(),
                "endTime": day.replace(hour=15, minute=30).isoformat(),
            },
        },
    }


def calendar_for(today_text, previous_text, next_text):
    return {
        "today": business_day(today_text),
        "previousBusinessDay": business_day(previous_text),
        "nextBusinessDay": business_day(next_text),
    }


# The Friday-to-Monday transition and the Friday-to-Monday transition later in
# the chain prove that the implementation accepts exchange gaps without using
# weekday arithmetic.  The map is intentionally independent of candle rows.
NEWEST_TO_OLDEST = [
    "2026-09-07", "2026-09-04", "2026-09-03", "2026-09-02",
    "2026-09-01", "2026-08-31", "2026-08-28",
]


def calendar_map():
    result = {}
    for i, current in enumerate(NEWEST_TO_OLDEST):
        previous = NEWEST_TO_OLDEST[i + 1] if i + 1 < len(NEWEST_TO_OLDEST) else "2026-08-27"
        next_text = NEWEST_TO_OLDEST[i - 1] if i else "2026-09-08"
        result[current] = calendar_for(current, previous, next_text)
    return result


class CalendarClient:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        if path != CALENDAR_PATH:
            raise AssertionError(path)
        return deepcopy(self.values[params["date"]])


def reference(dates, status="verified"):
    return {
        "status": status,
        "verified": status == "verified",
        "dates": list(dates) if status == "verified" else [],
        "source": {"endpoint": CALENDAR_PATH, "requiredDates": len(dates)},
    }


def daily_row(day_text, close="10", volume="1000", offset=0):
    stamp = datetime.fromisoformat(day_text).replace(hour=0, tzinfo=KST)
    value = D(close)
    return {
        "date": day_text,
        "timestamp": (stamp + timedelta(seconds=offset)).isoformat(),
        "openPrice": str(value),
        "highPrice": str(value + D("1")),
        "lowPrice": str(value - D("1")),
        "closePrice": str(value),
        "volume": str(volume),
    }


def rows_for(dates, close="10", volume="1000"):
    return [daily_row(day_text, close=close, volume=volume)
            for day_text in dates]


class CommonTradingDateTests(unittest.TestCase):
    def test_verified_chain_accepts_exchange_gaps_and_records_source(self):
        client = CalendarClient(calendar_map())
        result = common_trading_dates(
            client, calendar_map()["2026-09-07"], NOW, 6,
            check_budget=lambda: True)
        self.assertEqual(result["status"], "verified")
        self.assertIsNone(result["reason"])
        self.assertEqual(result["dates"], list(reversed(NEWEST_TO_OLDEST[1:7])))
        self.assertEqual(result["source"]["endpoint"], CALENDAR_PATH)
        self.assertEqual(result["source"]["fetchedLinks"], 5)
        self.assertEqual([params["date"] for _, params in client.calls], NEWEST_TO_OLDEST[1:6])

    def test_engine_style_budget_callback_returning_none_is_allowed(self):
        engine = Engine.__new__(Engine)
        engine.phase_end = 10.0
        engine.clock = lambda: 1.0
        client = CalendarClient(calendar_map())
        result = common_trading_dates(
            client, calendar_map()["2026-09-07"], NOW, 3,
            check_budget=engine.check_budget)
        self.assertEqual(result["status"], "verified")

    def test_recording_client_cache_prevents_repeat_chain_fetches(self):
        inner = CalendarClient(calendar_map())
        client = RecordingClient(inner, now=lambda: NOW, clock=lambda: 1.0)
        initial = calendar_map()["2026-09-07"]
        first = common_trading_dates(client, initial, NOW, 4, check_budget=lambda: True)
        second = common_trading_dates(client, initial, NOW, 4, check_budget=lambda: True)
        self.assertEqual(first["dates"], second["dates"])
        self.assertEqual(len(inner.calls), 3)
        self.assertEqual(len(client.records), 3)

    def test_request_echo_mismatch_is_unavailable(self):
        values = calendar_map()
        values["2026-09-04"]["today"]["date"] = "2026-09-03"
        client = CalendarClient(values)
        result = common_trading_dates(client, values["2026-09-07"], NOW, 3)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "calendar-request-mismatch")
        self.assertEqual(result["requestedDate"], "2026-09-04")
        self.assertEqual(result["returnedDate"], "2026-09-03")

    def test_next_business_day_mismatch_is_unavailable(self):
        values = calendar_map()
        values["2026-09-04"]["nextBusinessDay"] = business_day("2026-09-08")
        client = CalendarClient(values)
        result = common_trading_dates(client, values["2026-09-07"], NOW, 3)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "calendar-next-mismatch")
        self.assertEqual(result["expectedNextDate"], "2026-09-07")

    def test_next_business_day_without_a_valid_session_is_unavailable(self):
        values = calendar_map()
        values["2026-09-04"]["nextBusinessDay"]["integrated"] = None
        client = CalendarClient(values)
        result = common_trading_dates(client, values["2026-09-07"], NOW, 3)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "calendar-next-unverified")

    def test_non_decreasing_chain_is_unavailable(self):
        values = calendar_map()
        values["2026-09-04"]["previousBusinessDay"] = business_day("2026-09-05")
        client = CalendarClient(values)
        result = common_trading_dates(client, values["2026-09-07"], NOW, 3)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "calendar-chain-not-decreasing")

    def test_repeated_date_is_an_explicit_chain_loop(self):
        values = calendar_map()
        values["2026-09-03"]["previousBusinessDay"] = business_day("2026-09-04")
        client = CalendarClient(values)
        result = common_trading_dates(client, values["2026-09-07"], NOW, 4)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "calendar-chain-loop")
        self.assertEqual(result["repeatedDate"], "2026-09-04")

    def test_future_ending_session_is_not_completed(self):
        values = calendar_map()
        values["2026-09-04"]["today"]["integrated"]["regularMarket"]["endTime"] = \
            "2026-09-07T11:00:00+09:00"
        client = CalendarClient(values)
        result = common_trading_dates(client, values["2026-09-07"], NOW, 3)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "calendar-current-unverified")

    def test_budget_is_checked_before_each_link(self):
        client = CalendarClient(calendar_map())
        checks = []

        def budget():
            checks.append(True)
            return False

        result = common_trading_dates(
            client, calendar_map()["2026-09-07"], NOW, 3,
            check_budget=budget)
        self.assertEqual(result["reason"], "calendar-budget-exhausted")
        self.assertEqual(len(checks), 1)
        self.assertEqual(client.calls, [])
        self.assertEqual(result["dates"], ["2026-09-04"])

    def test_authentication_failure_propagates(self):
        class AuthClient(CalendarClient):
            def get(self, path, **params):
                raise TmonError("auth-expired", "auth", 3)

        with self.assertRaises(TmonError) as caught:
            common_trading_dates(
                AuthClient(calendar_map()), calendar_map()["2026-09-07"],
                NOW, 3)
        self.assertEqual(caught.exception.exit_code, 3)

    def test_malformed_or_incomplete_session_is_unavailable(self):
        values = calendar_map()
        values["2026-09-04"]["previousBusinessDay"]["integrated"]["regularMarket"]["endTime"] = \
            "2026-09-02T15:30:00+09:00"
        client = CalendarClient(values)
        result = common_trading_dates(client, values["2026-09-07"], NOW, 3)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "calendar-previous-unverified")


class DailyDateValidationTests(unittest.TestCase):
    def test_valid_dates_are_verified_without_synthesis(self):
        dates = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
        rows = rows_for(dates)
        result = validate_daily_dates(rows, reference(dates), 4)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["dates"], dates)
        self.assertEqual(result["missingDates"], [])
        self.assertEqual(result["unexpectedDates"], [])

    def test_missing_interior_and_unexpected_older_date_are_explicit(self):
        dates = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
        rows = rows_for([dates[0], dates[2], dates[3], "2026-08-31"])
        result = validate_daily_dates(rows, reference(dates), 4)
        self.assertEqual(result["reason"], DAILY_GAP)
        self.assertEqual(result["missingDates"], ["2026-09-02"])
        self.assertEqual(result["unexpectedDates"], ["2026-08-31"])
        self.assertEqual(len(rows), 4)

    def test_same_missing_date_in_stock_and_index_is_still_a_stock_gap(self):
        dates = ["2026-09-01", "2026-09-02", "2026-09-03"]
        stock = rows_for([dates[0], dates[2]])
        index = rows_for([dates[0], dates[2]])
        stock_result = validate_daily_dates(stock, reference(dates), 3)
        index_result = validate_daily_dates(index, reference(dates), 3)
        self.assertEqual(stock_result["reason"], DAILY_GAP)
        self.assertEqual(index_result["reason"], DAILY_GAP)
        self.assertEqual(stock_result["missingDates"], [dates[1]])
        self.assertEqual(index_result["missingDates"], [dates[1]])

    def test_unverified_calendar_never_becomes_a_gap_or_complete(self):
        dates = ["2026-09-01", "2026-09-02"]
        result = validate_daily_dates(rows_for(dates), reference(dates, "unavailable"), 2)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], DAILY_CALENDAR_UNVERIFIED)
        self.assertEqual(result["expectedDates"], [])

    def test_duplicate_or_malformed_dates_are_not_filled(self):
        dates = ["2026-09-01", "2026-09-02"]
        rows = [daily_row(dates[0]), daily_row(dates[0]), {"closePrice": "1"}]
        result = validate_daily_dates(rows, reference(dates), 2)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "daily-data-invalid")
        self.assertEqual(result["duplicateDates"], [dates[0]])
        self.assertEqual(len(rows), 3)

    def test_non_list_rows_are_data_invalid(self):
        dates = ["2026-09-01", "2026-09-02"]
        result = validate_daily_dates(tuple(rows_for(dates)), reference(dates), 2)
        self.assertEqual(result["reason"], "daily-data-invalid")
        self.assertEqual(result["missingDates"], dates)


class AdjustmentBasisTests(unittest.TestCase):
    def test_day_requires_exact_20_and_recomputes_raw_turnover(self):
        dates = [(date(2026, 8, 1) + timedelta(days=i)).isoformat() for i in range(20)]
        result = verify_adjustment_basis(
            rows_for(dates), rows_for(dates), "day", reference(dates), D("10000"))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["requiredDailyBars"], 20)
        self.assertEqual(result["rawEstimatedAvgDailyAmount"], D("10000"))
        self.assertEqual(result["turnoverPriceBasis"], "provider-unadjusted")
        self.assertIn("관찰상 동일", result["explanation"])
        self.assertIn("보장을 의미하지 않습니다", result["explanation"])

    def test_swing_requires_exact_65_even_though_sma_uses_60(self):
        dates = [(date(2026, 6, 1) + timedelta(days=i)).isoformat() for i in range(65)]
        reference_data = reference(dates)
        result = verify_adjustment_basis(
            rows_for(dates), rows_for(dates), "swing", reference_data, D("10000"))
        self.assertEqual(result["requiredDailyBars"], 65)
        with self.assertRaises(TmonError) as caught:
            verify_adjustment_basis(
                rows_for(dates[:-1]), rows_for(dates[:-1]), "swing",
                reference_data, D("10000"))
        self.assertEqual(caught.exception.code, ADJUSTMENT_BASIS_UNVERIFIED)

    def test_any_ohlc_divergence_rejects_even_when_volume_matches(self):
        dates = [(date(2026, 8, 1) + timedelta(days=i)).isoformat() for i in range(20)]
        for field in ("openPrice", "highPrice", "lowPrice", "closePrice"):
            with self.subTest(field=field):
                adjusted = rows_for(dates)
                raw = rows_for(dates)
                raw[7][field] = {
                    "openPrice": "10.5", "highPrice": "12",
                    "lowPrice": "8", "closePrice": "10.5",
                }[field]
                with self.assertRaises(TmonError) as caught:
                    verify_adjustment_basis(
                        adjusted, raw, "day", reference(dates), D("10000"))
                self.assertEqual(caught.exception.code, ADJUSTMENT_BASIS_UNVERIFIED)
                self.assertEqual(caught.exception.details["reason"], "window-values-differ")
                expected_amount = "10025.0" if field == "closePrice" else "10000"
                self.assertEqual(caught.exception.details["rawEstimatedAvgDailyAmount"], expected_amount)
                self.assertEqual(caught.exception.details["minimumEstimatedAvgDailyAmount"], "10000")
                self.assertEqual(caught.exception.details["rawLiquidityCheck"], "pass")

    def test_volume_divergence_rejects_without_provider_guarantee(self):
        dates = [(date(2026, 8, 1) + timedelta(days=i)).isoformat() for i in range(20)]
        adjusted = rows_for(dates)
        raw = rows_for(dates)
        raw[7]["volume"] = "2000"
        with self.assertRaises(TmonError) as caught:
            verify_adjustment_basis(
                adjusted, raw, "day", reference(dates), D("10000"))
        self.assertEqual(caught.exception.code, ADJUSTMENT_BASIS_UNVERIFIED)
        self.assertEqual(caught.exception.details["reason"], "window-values-differ")

    def test_invalid_zero_or_crossed_ohlc_is_not_verified(self):
        dates = [(date(2026, 8, 1) + timedelta(days=i)).isoformat() for i in range(20)]
        for field, value in (("openPrice", "0"), ("highPrice", "8")):
            with self.subTest(field=field):
                adjusted = rows_for(dates)
                raw = rows_for(dates)
                raw[7][field] = value
                with self.assertRaises(TmonError) as caught:
                    verify_adjustment_basis(
                        adjusted, raw, "day", reference(dates), D("10000"))
                self.assertEqual(caught.exception.code, ADJUSTMENT_BASIS_UNVERIFIED)
                self.assertEqual(caught.exception.details["reason"], "daily-data-invalid")

    def test_low_raw_turnover_is_a_condition_failure(self):
        dates = [(date(2026, 8, 1) + timedelta(days=i)).isoformat() for i in range(20)]
        adjusted = rows_for(dates, volume="1")
        raw = rows_for(dates, volume="1")
        with self.assertRaises(NoMatch) as caught:
            verify_adjustment_basis(
                adjusted, raw, "day", reference(dates), D("10000"))
        self.assertEqual(caught.exception.code, "low-liquidity")
        self.assertEqual(caught.exception.details["turnoverPriceBasis"], "provider-unadjusted")
        self.assertEqual(caught.exception.details["rawLiquidityCheck"], "fail")

    def test_unverified_reference_is_basis_failure(self):
        dates = [(date(2026, 8, 1) + timedelta(days=i)).isoformat() for i in range(20)]
        with self.assertRaises(TmonError) as caught:
            verify_adjustment_basis(
                rows_for(dates), rows_for(dates), "day",
                reference(dates, "unavailable"), D("10000"))
        self.assertEqual(caught.exception.code, ADJUSTMENT_BASIS_UNVERIFIED)
        self.assertEqual(caught.exception.details["reason"], DAILY_CALENDAR_UNVERIFIED)


if __name__ == "__main__":
    unittest.main()
