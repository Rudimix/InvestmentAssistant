"""Дневная дата имеет консервативную доступность, неизменяемую версию и кэш."""

from datetime import timedelta
from unittest.mock import AsyncMock
import unittest

import _support  # noqa: F401
from investment_journal.daily_prices import daily_prices
from investment_journal.mcp_client import ToolError
from investment_journal.storage import JournalRepository
from test_analysis_models import NOW


def daily_row(day, close="100", volume="100000", complete=True):
    return {"time": day.isoformat(), "close": {"value": close}, "volume": volume, "isComplete": complete}


class DailyPriceTests(unittest.IsolatedAsyncioTestCase):
    async def test_availability_cache_and_versions(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        market = AsyncMock()
        market.report_call.return_value = {"priceCurrency": "rub", "candles": [daily_row((NOW-timedelta(days=2)).date()),
                                                                                   daily_row((NOW-timedelta(days=1)).date())]}
        first, version, quality = await daily_prices(market, "SBER", NOW-timedelta(days=7), NOW, repo)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].available_at, NOW)
        self.assertEqual(first[0].day, (NOW-timedelta(days=2)).date().isoformat())
        self.assertFalse(quality)
        second, other, _ = await daily_prices(market, "SBER", NOW-timedelta(days=7), NOW+timedelta(hours=3), repo)
        self.assertEqual((first, version), (second, other))
        market.report_call.assert_awaited_once()
        record = repo.get_historical_prices("SBER", NOW, NOW+timedelta(microseconds=1))[0]
        self.assertEqual(record["version"], version)

    async def test_bad_candle_and_partial_network_are_explicit(self):
        market = AsyncMock()
        market.report_call.return_value = {"priceCurrency": "rub", "candles": [daily_row((NOW-timedelta(days=2)).date(), "NaN")]}
        values, _, quality = await daily_prices(market, "SBER", NOW-timedelta(days=7), NOW)
        self.assertFalse(values)
        self.assertTrue(quality)
        market.report_call.side_effect = ToolError("unavailable")
        self.assertTrue((await daily_prices(market, "SBER", NOW-timedelta(days=7), NOW))[2])


if __name__ == "__main__":
    unittest.main()
