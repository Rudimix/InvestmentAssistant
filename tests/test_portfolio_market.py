"""Контракты снимков и доступности MCP-источников."""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import unittest

from _support import FakeToolClient, value
from investment_journal.market import MarketDataService, snapshot_from
from investment_journal.analytics import portfolio_structure
from investment_journal.mcp_client import ToolError

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


def portfolio_response():
    return {
        "accountId": "account", "totalAmountPortfolio": {"value": "240", "currency": "rub"},
        "totalAmountCurrencies": {"value": "20", "currency": "rub"},
        "totalAmountShares": {"value": "200", "currency": "rub"},
        "totalAmountBonds": {"value": "20", "currency": "rub"},
        "positions": [
            {"instrumentUid": "share", "ticker": "SHARE", "instrumentType": "share", "quantity": value(2),
             "currentPrice": {"value": "100", "currency": "rub"}},
            {"instrumentUid": "bond", "ticker": "BOND", "instrumentType": "bond", "quantity": value(1),
             "currentPrice": {"value": "19", "currency": "rub"}, "currentNkd": {"value": "1", "currency": "rub"}},
            {"figi": "RUB000UTSTOM", "quantity": value(20)},
        ],
    }


class SnapshotTests(unittest.TestCase):
    def test_values_currency_identifiers_and_cash(self):
        result = snapshot_from(portfolio_response(), 1, "account", NOW)
        self.assertEqual((result.total, result.cash, result.currency), (Decimal(240), Decimal(20), "rub"))
        self.assertEqual([p.value for p in result.positions], [Decimal(200), Decimal(20), Decimal(20)])
        self.assertEqual(result.positions[0].instrument_id, "share")
        self.assertEqual(result.observed_at, NOW)

    def test_bond_points_and_missing_derivative_valuation(self):
        raw = portfolio_response()
        raw["positions"][1]["currentPrice"] = {"value": "95", "currency": "pt"}
        raw["positions"].append({"instrumentUid": "future", "ticker": "FUT", "instrumentType": "futures",
                                 "quantity": value(1), "currentPrice": {"value": "100", "currency": "pt"}})
        result = snapshot_from(raw, 1, "account", NOW, {"bond": {"nominal": {"value": "20", "currency": "rub"}}})
        self.assertEqual(result.positions[1].value, Decimal(20))
        self.assertIsNone(result.positions[-1].value)
        self.assertTrue(result.positions[-1].quality)

    def test_foreign_currency_without_rate_and_invalid_core(self):
        raw = portfolio_response()
        raw["positions"][0]["currentPrice"]["currency"] = "usd"
        self.assertIsNone(snapshot_from(raw, 1, "account", NOW).positions[0].value)
        for changes in ({"accountId": "other"}, {"totalAmountPortfolio": {}}, {"positions": None}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, KeyError)):
                snapshot_from(raw | changes, 1, "account", NOW)


class ToolAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_tool_is_detected_without_call(self):
        tools = FakeToolClient()
        service = MarketDataService(tools)
        self.assertFalse(await service.supports_tool("invest_get_news"))
        self.assertEqual(tools.calls, [])

    async def test_configured_allowlist_is_respected(self):
        service = MarketDataService(FakeToolClient(), allowed_tools=("invest_get_market_values",))
        self.assertFalse(await service.supports_tool("invest_get_portfolio"))

    async def test_recorded_operations_news_and_calendar_contracts(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/portfolio_mcp_contracts.json").read_text(encoding="utf-8"))
        schemas = {tool["name"]: tool["input_schema"] for tool in fixture["tools"]}
        self.assertIn("cursor", schemas["invest_get_operations"]["properties"])
        self.assertIn("PAYMENTS", schemas["invest_get_operations"]["properties"]["responseView"]["items"]["enum"])
        self.assertEqual(schemas["invest_get_instrument_reports"]["required"], ["instrumentId"])
        self.assertNotIn("from", schemas["invest_get_news"]["properties"])


class DetailedPortfolioTools(FakeToolClient):
    """Форма реального FULL-ответа: только ticker/classCode, без цен, типов и UID."""

    def __init__(self, unavailable=None):
        super().__init__()
        self.unavailable = unavailable

    async def list_tools(self):
        fields = {
            "invest_find_instrument": ["query", "responseView"],
            "invest_get_market_values": ["instrumentId", "values"],
            "invest_get_bond": ["instrumentId", "idType", "responseView"],
        }
        return [{"name": name, "input_schema": {"properties": {key: {} for key in keys}}}
                for name, keys in fields.items()]

    async def call(self, name, arguments=None):
        if name == self.unavailable:
            raise ToolError("Additional source unavailable")
        return await super().call(name, arguments)

    def _invest_get_portfolio(self, args):
        return {
            "totalAmountPortfolio": {"value": "1170", "currency": "rub"},
            "totalAmountShares": {"value": "200", "currency": "rub"},
            "totalAmountBonds": {"value": "950", "currency": "rub"},
            "totalAmountCurrencies": {"value": "20", "currency": "rub"},
            "positions": [
                {"ticker": "SHARE", "classCode": "TQBR", "quantity": value(2),
                 "averagePositionPrice": {"value": "90", "currency": "rub"}},
                {"ticker": "BOND", "classCode": "TQOB", "quantity": value(1),
                 "averagePositionPrice": {"value": "900", "currency": "rub"}},
                {"ticker": "RUB000UTSTOM", "classCode": "CETS", "quantity": value(20)},
            ],
        }

    def _invest_find_instrument(self, args):
        ticker = args["query"]
        kind, code = ("SHARE", "TQBR") if ticker == "SHARE" else ("BOND", "TQOB")
        return {"instruments": [
            {"ticker": ticker, "classCode": "WRONG", "uid": "wrong", "instrumentKind": "INSTRUMENT_TYPE_FUTURES"},
            {"ticker": ticker, "classCode": code, "uid": ticker + "-uid", "positionUid": ticker + "-position",
             "instrumentKind": "INSTRUMENT_TYPE_" + kind},
        ]}

    def _invest_get_market_values(self, args):
        return {"instruments": [
            {"ticker": "BOND", "classCode": "TQOB", "priceCurrency": "pt.",
             "values": [{"type": "INSTRUMENT_VALUE_LAST_PRICE", "value": value(94), "time": NOW.isoformat()}]},
            {"ticker": "SHARE", "classCode": "WRONG", "priceCurrency": "rub",
             "values": [{"type": "INSTRUMENT_VALUE_LAST_PRICE", "value": value(999)}]},
            {"ticker": "SHARE", "classCode": "TQBR", "priceCurrency": "rub",
             "values": [{"type": "INSTRUMENT_VALUE_LAST_PRICE", "value": value(100), "time": NOW.isoformat()}]},
        ]}

    def _invest_get_bond(self, args):
        return {"instrument": {"nominal": {"value": "1000", "currency": "rub"},
                               "aciValue": {"value": "10", "currency": "rub"}}}


class DetailedPortfolioTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_response_enriches_ids_quotes_bond_points_and_accrued_interest(self):
        tools = DetailedPortfolioTools()
        snapshot = await MarketDataService(tools, "account").snapshot(1, NOW)
        self.assertEqual([p.value for p in snapshot.positions], [Decimal(200), Decimal(950), Decimal(20)])
        self.assertEqual([p.instrument_type for p in snapshot.positions], ["share", "bond", "currency"])
        self.assertEqual(snapshot.positions[0].key, "SHARE-position")
        self.assertEqual(snapshot.positions[0].aliases, ["SHARE_TQBR"])
        self.assertEqual(snapshot.positions[1].price, Decimal(940))
        self.assertEqual(snapshot.positions[0].source_time, NOW.isoformat())
        self.assertEqual(portfolio_structure(snapshot).metrics["valuation_gap"], 0)
        quotes = [args for name, args in tools.calls if name == "invest_get_market_values"]
        self.assertEqual(len(quotes), 1)
        self.assertEqual(set(quotes[0]["instrumentId"]), {"SHARE-uid", "BOND-uid"})

    async def test_portfolio_command_uses_enriched_monetary_prices(self):
        portfolio = await MarketDataService(DetailedPortfolioTools(), "account").portfolio()
        self.assertEqual([p.current_price for p in portfolio.positions], [100, 940])
        self.assertEqual([p.instrument_type for p in portfolio.positions], ["share", "bond"])

    async def test_additional_source_failure_preserves_all_positions_without_fabricating_values(self):
        for source in ("invest_find_instrument", "invest_get_market_values", "invest_get_bond"):
            with self.subTest(source=source):
                snapshot = await MarketDataService(DetailedPortfolioTools(source), "account").snapshot(1, NOW)
                self.assertEqual(len(snapshot.positions), 3)
                self.assertIsNone(snapshot.positions[1].value)
                self.assertEqual(snapshot.positions[2].value, Decimal(20))
