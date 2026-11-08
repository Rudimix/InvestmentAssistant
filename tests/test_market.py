"""Рыночные данные поверх MCP."""

import unittest

from _support import FakeToolClient, mcp_settings
from investment_journal.market import MarketDataService
from investment_journal.mcp_client import ToolError


class MarketDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tools = FakeToolClient()
        self.market = MarketDataService(self.tools, mcp_settings())

    async def test_last_price_uppercases_ticker(self):
        self.assertEqual(await self.market.last_price("siz6"), 90000.0)
        self.assertEqual(self.tools.calls[-1], ("get_last_price", {"ticker": "SIZ6"}))

    async def test_zero_price_is_error(self):
        self.tools.prices["ZERO"] = 0
        with self.assertRaises(ToolError):
            await self.market.last_price("ZERO")

    async def test_candles_are_parsed(self):
        candles = await self.market.candles("SiZ6", "1d", 6)
        self.assertEqual(len(candles), 6)
        self.assertEqual((candles[0].close, candles[-1].close, candles[-1].volume), (100, 108, 60))
        self.assertEqual(self.tools.calls[-1][1], {"ticker": "SIZ6", "interval": "1d", "bars": 6})

    async def test_unknown_interval_rejected(self):
        with self.assertRaises(ValueError):
            await self.market.candles("SiZ6", "2d")

    async def test_portfolio_parses_quotations_and_skips_empty_currency(self):
        p = await self.market.portfolio()
        self.assertEqual((p.total, p.cash, p.unrealized_pnl), (125000.5, 20000.0, 1500.25))
        self.assertEqual(len(p.positions), 1)
        pos = p.positions[0]
        self.assertEqual(
            (pos.ticker, pos.instrument_type, pos.quantity, pos.average_price, pos.current_price, pos.expected_yield),
            ("SiZ6", "futures", 2.0, 90000.0, 91000.5, 2000.0),
        )

    async def test_portfolio_accepts_plain_numbers_and_missing_ticker(self):
        async def call(name, arguments=None):
            return {"total_portfolio": 1, "raw": {"positions": [
                {"figi": "BBG000", "instrumentType": "share", "quantity": 3,
                 "averagePositionPrice": 10, "currentPrice": 12, "expectedYield": 6},
            ]}}
        self.tools.call = call
        pos = (await self.market.portfolio()).positions[0]
        self.assertEqual((pos.ticker, pos.quantity, pos.average_price, pos.current_price, pos.expected_yield),
                         ("BBG000", 3.0, 10.0, 12.0, 6.0))


if __name__ == "__main__":
    unittest.main()
