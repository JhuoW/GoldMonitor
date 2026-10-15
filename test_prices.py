"""Deterministic tests of units, timestamp honesty, and independent feed failures."""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError

from gold_monitor.prices import (
    FX_URL, GOLD_URL, SWISSQUOTE_GOLD_URL, PriceClient, PriceDataError,
    _request_json, _retry_after_seconds, parse_fx, parse_gold, parse_swissquote_gold,
)


class PriceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 15, 7, 0, tzinfo=timezone.utc)
        self.calls = []
        self.gold = {
            "symbol": "XAU", "currency": "USD", "price": "3110.34768",
            "updatedAt": self.now.isoformat(),
        }
        self.fx = {
            "result": "success", "base_code": "USD", "rates": {"CNY": "7.0"},
            "time_last_update_unix": int(self.now.timestamp()),
            "time_next_update_unix": int((self.now + timedelta(days=1)).timestamp()),
        }
        self.fail_gold = self.fail_fx = False
        self.fx_error = None
        self.client = PriceClient(source="goldapi", fetch_json=self.fetch, clock=lambda: self.now)

    def fetch(self, url):
        self.calls.append(url)
        if url == FX_URL and self.fx_error is not None:
            raise self.fx_error
        if (url == GOLD_URL and self.fail_gold) or (url == FX_URL and self.fail_fx):
            raise PriceDataError("test timeout")
        return self.gold if url == GOLD_URL else self.fx

    def test_exact_troy_ounce_conversion(self):
        snapshot = self.client.refresh()
        self.assertEqual(snapshot.usd_per_oz, Decimal("3110.34768"))
        self.assertEqual(snapshot.cny_per_gram, Decimal("700"))
        self.assertFalse(snapshot.gold_stale)
        self.assertFalse(snapshot.fx_stale)
        self.assertEqual(snapshot.errors, ())

    def test_gold_and_fx_have_independent_cache_schedules(self):
        self.client.refresh()
        self.now += timedelta(seconds=29)
        self.client.refresh()
        self.assertEqual(self.calls, [GOLD_URL, FX_URL])
        self.now += timedelta(seconds=1)
        self.client.refresh()
        self.assertEqual(self.calls, [GOLD_URL, FX_URL, GOLD_URL])
        self.now += timedelta(days=1)
        self.client.refresh()
        self.assertEqual(self.calls.count(FX_URL), 2)

    def test_fx_outage_does_not_hide_live_usd_price(self):
        self.fail_fx = True
        snapshot = self.client.refresh()
        self.assertIsNotNone(snapshot.usd_per_oz)
        self.assertIsNone(snapshot.cny_per_gram)
        self.assertEqual(len(snapshot.errors), 1)
        self.now += timedelta(seconds=30)
        self.client.refresh()
        self.assertEqual(self.calls.count(FX_URL), 1)

    def test_failed_refresh_keeps_original_quote_time_and_reports_error(self):
        first = self.client.refresh()
        self.fail_gold = True
        self.now += timedelta(minutes=5)
        failed = self.client.refresh()
        self.assertEqual(failed.gold, first.gold)
        self.assertTrue(failed.gold_stale)
        self.assertEqual(len(failed.errors), 1)
        self.assertIsNotNone(failed.cny_per_gram)

    def test_fx_recovers_one_minute_after_transient_startup_failure(self):
        self.fail_fx = True
        self.client.refresh()
        self.fail_fx = False
        self.now += timedelta(seconds=59)
        self.assertIsNone(self.client.refresh().cny_per_gram)
        self.assertEqual(self.calls.count(FX_URL), 1)
        self.now += timedelta(seconds=1)
        recovered = self.client.refresh()
        self.assertEqual(recovered.cny_per_gram, Decimal("700"))
        self.assertEqual(recovered.errors, ())
        self.assertEqual(self.calls.count(FX_URL), 2)
        self.now += timedelta(hours=2)
        self.client.refresh()
        self.assertEqual(self.calls.count(FX_URL), 2)

    def test_http_429_maps_to_typed_twenty_minute_cooldown(self):
        error = HTTPError(FX_URL, 429, "Too Many Requests", {}, None)
        with patch("gold_monitor.prices.urlopen", side_effect=error):
            with self.assertRaises(PriceDataError) as caught:
                _request_json(FX_URL, timeout=8)
        self.assertEqual(caught.exception.retry_after_seconds, 1200)

    def test_fx_rate_limit_is_not_retried_until_twenty_minutes(self):
        self.fx_error = PriceDataError("rate limited", retry_after_seconds=1200)
        self.client.refresh()
        self.fx_error = None
        self.now += timedelta(seconds=1199)
        self.assertIsNone(self.client.refresh().cny_per_gram)
        self.assertEqual(self.calls.count(FX_URL), 1)
        self.now += timedelta(seconds=1)
        recovered = self.client.refresh()
        self.assertEqual(recovered.cny_per_gram, Decimal("700"))
        self.assertEqual(recovered.errors, ())
        self.assertEqual(self.calls.count(FX_URL), 2)

    def test_old_provider_timestamp_is_stale_even_on_success(self):
        self.gold["updatedAt"] = (self.now - timedelta(hours=12)).isoformat()
        snapshot = self.client.refresh()
        self.assertTrue(snapshot.gold_stale)
        self.assertEqual(snapshot.errors, ())

    def test_ui_can_recompute_age_without_another_network_request(self):
        snapshot = self.client.refresh()
        self.assertFalse(snapshot.is_gold_stale(self.now))
        self.assertTrue(snapshot.is_gold_stale(self.now + timedelta(minutes=4)))
        self.assertTrue(snapshot.is_fx_stale(self.now + timedelta(days=3)))
        self.assertEqual(len(self.calls), 2)

    def test_no_demo_values_on_first_connection_failure(self):
        self.fail_gold = self.fail_fx = True
        snapshot = self.client.refresh()
        self.assertIsNone(snapshot.usd_per_oz)
        self.assertIsNone(snapshot.cny_per_gram)
        self.assertTrue(snapshot.gold_stale)
        self.assertTrue(snapshot.fx_stale)
        self.assertEqual(len(snapshot.errors), 2)

    def test_recovery_clears_error_and_replaces_old_quote(self):
        self.fail_gold = True
        self.client.refresh()
        self.fail_gold = False
        self.now += timedelta(seconds=30)
        self.gold["updatedAt"] = self.now.isoformat()
        self.gold["price"] = "4000"
        recovered = self.client.refresh()
        self.assertEqual(recovered.usd_per_oz, Decimal("4000"))
        self.assertEqual(recovered.errors, ())

    def test_invalid_prices_are_rejected(self):
        for bad in (None, True, 0, -1, "NaN", "Infinity", "1e100000", "oops"):
            with self.subTest(bad=bad), self.assertRaises(PriceDataError):
                parse_gold({**self.gold, "price": bad}, self.now)

    def test_wrong_asset_currency_and_bad_dates_are_rejected(self):
        for patch in (
            {"symbol": "XAG"}, {"currency": "CNY"},
            {"updatedAt": "2026-10-15T07:00:00"},
            {"updatedAt": (self.now + timedelta(hours=1)).isoformat()},
        ):
            with self.subTest(patch=patch), self.assertRaises(PriceDataError):
                parse_gold({**self.gold, **patch}, self.now)
        for patch in (
            {"base_code": "EUR"}, {"result": "error"}, {"rates": {}},
            {"time_last_update_unix": True},
            {"time_next_update_unix": self.fx["time_last_update_unix"] - 1},
        ):
            with self.subTest(patch=patch), self.assertRaises(PriceDataError):
                parse_fx({**self.fx, **patch}, self.now)


class SwissquoteTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 15, 7, 0, tzinfo=timezone.utc)
        self.calls = []
        self.quote = [{
            "topo": {"platform": "AT", "server": "AT"},
            "spreadProfilePrices": [{"spreadProfile": "standard", "bid": "3999.9", "ask": "4000.3"}],
            "ts": int(self.now.timestamp() * 1000),
        }]
        self.fx = {
            "result": "success", "base_code": "USD", "rates": {"CNY": "7.0"},
            "time_last_update_unix": int(self.now.timestamp()),
            "time_next_update_unix": int((self.now + timedelta(days=1)).timestamp()),
        }
        self.failure = None
        self.client = PriceClient(fetch_json=self.fetch, clock=lambda: self.now)

    def fetch(self, url):
        self.calls.append(url)
        if url == SWISSQUOTE_GOLD_URL:
            if self.failure:
                raise self.failure
            return self.quote
        if url == FX_URL:
            return self.fx
        raise AssertionError(f"Unexpected endpoint: {url}")

    def test_uses_stable_bank_book_midpoint_and_original_timestamp(self):
        self.quote.insert(0, {
            "topo": {"platform": "Other", "server": "Live"},
            "spreadProfilePrices": [{"spreadProfile": "standard", "bid": "1", "ask": "2"}],
            "ts": int(self.now.timestamp() * 1000),
        })
        result = self.client.refresh()
        self.assertEqual(result.usd_per_oz, Decimal("4000.1"))
        self.assertEqual(result.gold.bid, Decimal("3999.9"))
        self.assertEqual(result.gold.ask, Decimal("4000.3"))
        self.assertEqual(result.gold.quoted_at, self.now)
        self.assertEqual(result.gold.source_name, "Swissquote")
        self.assertEqual(self.client.minimum_interval_seconds, 1)
        self.assertEqual(self.client.source, "swissquote")

    def test_one_second_fetches_actual_new_quote_but_keeps_daily_fx_cached(self):
        self.client.refresh()
        self.now += timedelta(milliseconds=900)
        self.client.refresh()
        self.assertEqual(self.calls.count(SWISSQUOTE_GOLD_URL), 1)
        self.now += timedelta(milliseconds=100)
        self.quote[0]["ts"] = int(self.now.timestamp() * 1000)
        self.quote[0]["spreadProfilePrices"][0]["bid"] = "4000"
        self.assertEqual(self.client.refresh().usd_per_oz, Decimal("4000.15"))
        self.assertEqual(self.calls.count(SWISSQUOTE_GOLD_URL), 2)
        self.assertEqual(self.calls.count(FX_URL), 1)

    def test_same_server_quote_does_not_get_a_new_quote_timestamp(self):
        first = self.client.refresh()
        self.now += timedelta(minutes=4)
        second = self.client.refresh()
        self.assertEqual(second.gold.quoted_at, first.gold.quoted_at)
        self.assertTrue(second.gold_stale)

    def test_realtime_source_warns_when_server_quote_stops_for_twenty_seconds(self):
        snapshot = self.client.refresh()
        self.assertFalse(snapshot.gold.is_stale(self.now + timedelta(seconds=20)))
        self.assertTrue(snapshot.gold.is_stale(self.now + timedelta(seconds=21)))

    def test_exponential_failure_backoff_retains_last_price_and_recovers(self):
        first = self.client.refresh()
        self.failure = PriceDataError("connection unavailable")
        self.now += timedelta(seconds=1)
        failed = self.client.refresh()
        self.assertEqual(failed.gold, first.gold)
        self.assertTrue(failed.errors)
        for wait in (2, 4, 8, 16, 32, 60, 60):
            count = self.calls.count(SWISSQUOTE_GOLD_URL)
            self.now += timedelta(seconds=wait - 0.1)
            self.client.refresh()
            self.assertEqual(self.calls.count(SWISSQUOTE_GOLD_URL), count)
            self.now += timedelta(seconds=0.1)
            self.client.refresh()
            self.assertEqual(self.calls.count(SWISSQUOTE_GOLD_URL), count + 1)
        self.failure = None
        self.now += timedelta(seconds=60)
        self.quote[0]["ts"] = int(self.now.timestamp() * 1000)
        self.assertEqual(self.client.refresh().errors, ())
        self.now += timedelta(seconds=1)
        count = self.calls.count(SWISSQUOTE_GOLD_URL)
        self.client.refresh()
        self.assertEqual(self.calls.count(SWISSQUOTE_GOLD_URL), count + 1)

    def test_old_response_cannot_replace_newer_quote(self):
        first = self.client.refresh()
        self.now += timedelta(seconds=1)
        self.quote[0]["ts"] -= 10000
        second = self.client.refresh()
        self.assertEqual(second.gold, first.gold)
        self.assertTrue(second.errors)

    def test_rate_limit_retry_after_is_honored(self):
        self.failure = PriceDataError("throttled", retry_after_seconds=120)
        self.client.refresh()
        self.now += timedelta(seconds=119)
        self.client.refresh()
        self.assertEqual(self.calls.count(SWISSQUOTE_GOLD_URL), 1)
        self.now += timedelta(seconds=1)
        self.client.refresh()
        self.assertEqual(self.calls.count(SWISSQUOTE_GOLD_URL), 2)

    def test_http_rate_limit_parses_seconds_and_date(self):
        error = HTTPError(SWISSQUOTE_GOLD_URL, 429, "Too Many Requests", {"Retry-After": "90"}, None)
        with patch("gold_monitor.prices.urlopen", side_effect=error):
            with self.assertRaises(PriceDataError) as caught:
                _request_json(SWISSQUOTE_GOLD_URL, timeout=8)
        self.assertEqual(caught.exception.retry_after_seconds, 90)
        self.assertEqual(_retry_after_seconds("Thu, 15 Oct 2026 07:02:00 GMT", self.now), 120)
        self.assertIsNone(_retry_after_seconds("bad", self.now))

    def test_missing_or_bad_bank_quotes_are_rejected(self):
        for bad in ([], {}, [None], [{"topo": {"platform": "AT", "server": "Wrong"}}]):
            with self.subTest(bad=bad), self.assertRaises(PriceDataError):
                parse_swissquote_gold(bad, self.now)
        for bad in (None, True, 0, -1, "NaN", "Infinity"):
            self.quote[0]["spreadProfilePrices"][0]["bid"] = bad
            with self.subTest(bad=bad), self.assertRaises(PriceDataError):
                parse_swissquote_gold(self.quote, self.now)
        self.quote[0]["spreadProfilePrices"][0]["bid"] = "5000"
        with self.assertRaises(PriceDataError):
            parse_swissquote_gold(self.quote, self.now)

    def test_bad_bank_timestamps_are_rejected(self):
        for stamp in (True, None, "bad", -1, int((self.now + timedelta(hours=1)).timestamp() * 1000)):
            self.quote[0]["ts"] = stamp
            with self.subTest(stamp=stamp), self.assertRaises(PriceDataError):
                parse_swissquote_gold(self.quote, self.now)

    def test_explicit_legacy_source_keeps_thirty_second_minimum(self):
        legacy = PriceClient(source="goldapi")
        self.assertEqual(legacy.minimum_interval_seconds, 30)
        with self.assertRaises(ValueError):
            PriceClient(source="unknown")


if __name__ == "__main__":
    unittest.main()
