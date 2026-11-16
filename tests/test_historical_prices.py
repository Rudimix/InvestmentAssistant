"""Историческая цена предшествует оценке, кэш сохраняет версии и полноту запросов."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import unittest

from _support import FakeToolClient
from investment_journal.historical_prices import fx_rate, historical_prices, price_before
from investment_journal.market import MarketDataService
from investment_journal.mcp_client import ToolError
from investment_journal.models import CurrencyPair, PriceObservation, timestamp
from investment_journal.storage import JournalRepository
from test_analysis_models import NOW

FIXTURE = json.loads((Path(__file__).parent / "fixtures/history_mcp_contracts.json").read_text(encoding="utf-8"))


def candle(at, price="100", complete=True):
    return {"time": at.isoformat(), "close": {"value": str(price)}, "isComplete": complete}


class PriceTools(FakeToolClient):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)

    async def list_tools(self):
        return FIXTURE["tools"]

    async def call(self, name, arguments=None):
        self.calls.append((name, arguments))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class HistoricalPriceTests(unittest.IsolatedAsyncioTestCase):
    async def test_weekend_uses_last_completed_price_and_excludes_future_candle(self):
        friday = NOW-timedelta(days=3, hours=4)
        tools = PriceTools([{"priceCurrency": "rub", "candles": [
            candle(friday, "99.000000001"), candle(NOW, "1000"), candle(NOW-timedelta(minutes=30), "999"),
        ]}])
        batch = await historical_prices(MarketDataService(tools), "uid", NOW-timedelta(days=7), NOW, now=NOW)
        observation = price_before(batch, NOW)
        self.assertTrue(batch.complete)
        self.assertEqual(observation.close, Decimal("99.000000001"))
        self.assertEqual(observation.candle_end, friday+timedelta(hours=1))
        self.assertLessEqual(observation.candle_end, NOW)
        self.assertIsNone(price_before(batch, NOW, max_age=timedelta(days=1)))

    async def test_half_hour_cutoff_uses_only_prices_known_before_the_boundary(self):
        end = NOW+timedelta(minutes=30)
        tools = PriceTools([{"priceCurrency": "rub", "candles": [
            candle(NOW-timedelta(hours=1), "100"), candle(NOW, "999"),
        ]}])
        batch = await historical_prices(MarketDataService(tools), "uid", NOW-timedelta(days=1), end, now=end)
        self.assertEqual(price_before(batch, end).close, Decimal(100))

    async def test_missing_invalid_and_unfinished_candles_have_explicit_coverage(self):
        tools = PriceTools([{"priceCurrency": "rub", "candles": [
            candle(NOW-timedelta(hours=2), complete=False),
            {"time": "2026-11-15", "close": {"value": "100"}, "isComplete": True},
        ]}])
        batch = await historical_prices(MarketDataService(tools), "uid", NOW-timedelta(days=1), NOW, now=NOW)
        self.assertFalse(batch.complete)
        self.assertIsNone(price_before(batch, NOW))
        self.assertTrue(batch.quality)
        empty = await historical_prices(MarketDataService(PriceTools([{"priceCurrency": "rub", "candles": []}])),
                                        "uid", NOW-timedelta(days=1), NOW, now=NOW)
        self.assertTrue(empty.complete)
        self.assertIsNone(price_before(empty, NOW))

    async def test_cache_reuses_exact_query_and_keeps_corrected_price_versions(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        tools = PriceTools([{"priceCurrency": "rub", "candles": [candle(NOW-timedelta(hours=1))]},
                            {"priceCurrency": "rub", "candles": [candle(NOW-timedelta(hours=1), "101")]}])
        market = MarketDataService(tools)
        first = await historical_prices(market, "uid", NOW-timedelta(days=1), NOW, repo, now=NOW)
        again = await historical_prices(market, "uid", NOW-timedelta(days=1), NOW, repo, now=NOW+timedelta(hours=1))
        self.assertEqual(first, again)
        self.assertEqual(len(tools.calls), 1)
        changed = await historical_prices(market, "uid", NOW-timedelta(days=1), NOW, repo, refresh=True,
                                          now=NOW+timedelta(hours=2))
        self.assertEqual(changed.items[0].close, Decimal(101))
        self.assertNotEqual(changed.items[0].version, first.items[0].version)
        self.assertEqual(len(repo.get_historical_prices("uid", NOW-timedelta(hours=1), NOW+timedelta(seconds=1))), 2)

    async def test_failed_refresh_retains_previous_complete_version_with_warning(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        tools = PriceTools([{"priceCurrency": "rub", "candles": [candle(NOW-timedelta(hours=1))]}, ToolError("offline")])
        market = MarketDataService(tools)
        first = await historical_prices(market, "uid", NOW-timedelta(days=1), NOW, repo, now=NOW)
        failed = await historical_prices(market, "uid", NOW-timedelta(days=1), NOW, repo, refresh=True,
                                         now=NOW+timedelta(hours=1))
        self.assertFalse(failed.complete)
        self.assertEqual(failed.items, first.items)
        self.assertEqual(failed.retrieved_at, NOW)

    async def test_long_range_is_chunked_and_partial_failure_preserves_first_chunk(self):
        start = NOW-timedelta(days=40)
        tools = PriceTools([{"priceCurrency": "rub", "candles": [candle(start)]}, ToolError("temporary")])
        batch = await historical_prices(MarketDataService(tools), "uid", start, NOW, now=NOW)
        self.assertEqual(len(tools.calls), 2)
        self.assertFalse(batch.complete)
        self.assertEqual(len(batch.items), 1)
        for _, args in tools.calls:
            self.assertEqual(args["interval"], "CANDLE_INTERVAL_HOUR")
            self.assertNotIn("candleSourceType", args)

    async def test_actual_hourly_contract_has_aware_times(self):
        example = FIXTURE["examples"]["hourly_candles"]
        tools = PriceTools([example["response"]])
        start, end = timestamp(example["request"]["from"]), timestamp(example["request"]["to"])
        batch = await historical_prices(MarketDataService(tools), "SBER_TQBR", start, end, now=NOW)
        self.assertTrue(batch.items)
        self.assertTrue(batch.complete)
        self.assertTrue(all(item.candle_end <= end and item.candle_start.tzinfo is not None for item in batch.items))


class CurrencyRateTests(unittest.TestCase):
    def test_direct_inverse_nominal_and_invalid_currency_pair(self):
        observation = PriceObservation("usd-rub", NOW-timedelta(hours=1), NOW, Decimal(900), "rub", NOW, "v1")
        pair = CurrencyPair("usd-rub", "usd", "rub", Decimal(10))
        self.assertEqual(fx_rate(observation, pair, "usd", "rub"), Decimal(90))
        self.assertEqual(fx_rate(observation, pair, "rub", "usd"), Decimal(1)/90)
        self.assertEqual(fx_rate(None, None, "rub", "rub"), Decimal(1))
        self.assertIsNone(fx_rate(None, pair, "usd", "rub"))
        self.assertIsNone(fx_rate(replace(observation, instrument_id="share"), pair, "usd", "rub"))
        self.assertIsNone(fx_rate(replace(observation, currency="pt"), pair, "usd", "rub"))
        self.assertIsNone(fx_rate(observation, pair, "eur", "rub"))
