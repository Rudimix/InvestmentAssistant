"""Рыночные данные через инструменты официального T-Invest MCP."""

import unittest

from _support import FakeToolClient
from investment_journal.market import MarketDataService, parse_portfolio, pick_instrument, quotation
from investment_journal.mcp_client import ToolError


class QuotationTests(unittest.TestCase):
    def test_quotation(self):
        self.assertEqual(quotation({"value": "294.35"}), 294.35)
        self.assertEqual(quotation({"value": "1", "currency": "rub"}), 1.0)
        self.assertEqual(quotation({"units": "12", "nano": 500000000}), 12.5)
        self.assertEqual(quotation({"units": "-3", "nano": -250000000}), -3.25)
        self.assertEqual(quotation({}), 0.0)
        self.assertEqual(quotation(None), 0.0)
        self.assertEqual(quotation("7.5"), 7.5)


class PickInstrumentTests(unittest.TestCase):
    def test_prefers_main_board(self):
        found = [{"ticker": "SBER", "classCode": "SPEQ"}, {"ticker": "SBERP", "classCode": "TQBR"},
                 {"ticker": "SBER", "classCode": "TQBR"}]
        self.assertEqual(pick_instrument("SBER", found), "SBER_TQBR")

    def test_unknown_board_is_still_used(self):
        self.assertEqual(pick_instrument("XYZ", [{"ticker": "xyz", "classCode": "ABC"}]), "xyz_ABC")

    def test_no_exact_match(self):
        with self.assertRaisesRegex(ToolError, "не найден"):
            pick_instrument("SBER", [{"ticker": "SBERP", "classCode": "TQBR"}])


class MarketDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tools = FakeToolClient()
        self.market = MarketDataService(self.tools)

    async def test_last_price_resolves_ticker_once(self):
        self.assertEqual(await self.market.last_price("siz6"), 90000.0)
        self.assertEqual(await self.market.last_price("SiZ6"), 90000.0)
        names = [c[0] for c in self.tools.calls]
        self.assertEqual(names.count("invest_find_instrument"), 1)
        self.assertEqual(self.tools.calls[-1], ("invest_get_market_values", {
            "instrumentId": ["SiZ6_SPBFUT"], "values": ["INSTRUMENT_VALUE_LAST_PRICE"]}))

    async def test_instrument_id_passthrough(self):
        self.assertEqual(await self.market.instrument_id("sber_tqbr"), "SBER_TQBR")
        uid = "e6123145-9665-43e0-8413-cd61b8aa9b13"
        self.assertEqual(await self.market.instrument_id(uid), uid)
        self.assertEqual(self.tools.calls, [])

    async def test_unknown_ticker(self):
        with self.assertRaisesRegex(ToolError, "не найден"):
            await self.market.last_price("NOPE")

    async def test_zero_or_missing_price_is_error(self):
        self.tools.prices["ZERO"] = 0
        with self.assertRaisesRegex(ToolError, "Нет цены"):
            await self.market.last_price("ZERO_TQBR")
        with self.assertRaisesRegex(ToolError, "Нет цены"):
            await self.market.last_price("MISSING_TQBR")

    async def test_candles_are_parsed(self):
        candles = await self.market.candles("SiZ6", "1d", 6)
        self.assertEqual(len(candles), 6)
        self.assertEqual((candles[0].close, candles[-1].close, candles[-1].volume, candles[0].time),
                         (100, 108, 60, "2026-11-01"))
        name, args = self.tools.calls[-1]
        self.assertEqual((name, args["instrumentId"], args["interval"], args["limit"]),
                         ("invest_get_candles", "SiZ6_SPBFUT", "CANDLE_INTERVAL_DAY", 6))
        self.assertIn("to", args)

    async def test_unknown_interval_rejected(self):
        with self.assertRaises(ValueError):
            await self.market.candles("SiZ6", "2d")

    async def test_portfolio_uses_first_open_account(self):
        self.tools.accounts.insert(0, {"id": "bank-1", "type": "ACCOUNT_TYPE_BANK"})
        p = await self.market.portfolio()
        self.assertEqual((p.total, p.cash, p.unrealized_pnl), (125000.5, 20000.0, 2000.0))
        self.assertEqual(len(p.positions), 1)
        pos = p.positions[0]
        self.assertEqual(
            (pos.ticker, pos.instrument_type, pos.quantity, pos.average_price, pos.current_price, pos.expected_yield),
            ("SiZ6", "futures", 2.0, 90000.0, 91000.5, 2000.0),
        )
        self.assertEqual(self.tools.calls[-1][1]["accountId"], "2000000001")

    def test_sandbox_portfolio_with_only_rubles(self):
        """Реальный ответ песочницы (FULL): рубли — позицией RUB000UTSTOM, пустых полей нет."""
        data = {
            "totalAmountCurrencies": {"currency": "rub", "value": "100000"},
            "totalAmountPortfolio": {"currency": "rub", "value": "100000"},
            "totalAmountShares": {"currency": "rub", "value": "0"},
            "expectedYield": {"value": "0"},
            "positions": [{"quantity": {"value": "100000"}, "averagePositionPrice": {"currency": "rub", "value": "1"},
                           "blockedLots": {"value": "0"}, "ticker": "RUB000UTSTOM", "classCode": "CETS"}],
        }
        p = parse_portfolio(data)
        self.assertEqual((p.total, p.cash, p.unrealized_pnl, p.positions), (100000.0, 100000.0, 0.0, []))
        self.assertEqual(parse_portfolio({"totalAmountPortfolio": {"value": "5"}, "positions": None}).positions, [])

    async def test_configured_account_is_used(self):
        self.tools.accounts.append({"id": "42"})
        await MarketDataService(self.tools, account_id="42").portfolio()
        self.assertNotIn("invest_list_accounts", [c[0] for c in self.tools.calls])

    async def test_no_accounts(self):
        self.tools.accounts.clear()
        with self.assertRaisesRegex(ToolError, "Нет открытых"):
            await self.market.portfolio()


if __name__ == "__main__":
    unittest.main()
