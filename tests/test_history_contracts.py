"""Проверенные формы ответов MCP для исторического анализа."""

import json
from pathlib import Path
import unittest

import _support  # noqa: F401
from investment_journal.models import decimal_value


FIXTURE = json.loads((Path(__file__).parent / "fixtures/history_mcp_contracts.json").read_text(encoding="utf-8"))


class HistoryContractTests(unittest.TestCase):
    def test_candles_have_explicit_range_and_date_only_daily_times(self):
        schemas = {tool["name"]: tool["input_schema"] for tool in FIXTURE["tools"]}
        candle = schemas["invest_get_candles"]
        self.assertTrue({"from", "to", "limit"}.issubset(candle["properties"]))
        self.assertIn("to", candle["required"])
        sample = FIXTURE["examples"]["candles"]["response"]
        self.assertEqual(sample["priceCurrency"], "rub")
        self.assertTrue(all(len(item["time"]) == 10 for item in sample["candles"]))
        self.assertTrue(all(decimal_value(item["close"]) > 0 for item in sample["candles"]))

    def test_operations_do_not_guarantee_instrument_uid_or_trade_times(self):
        sample = FIXTURE["examples"]["operations"]
        self.assertTrue(sample["anonymized"])
        page = sample["response"]
        self.assertTrue(page["hasNext"])
        self.assertTrue(page["nextCursor"])
        item = page["items"][0]
        self.assertTrue({"ticker", "classCode", "quantityDone", "date", "state"}.issubset(item))
        self.assertNotIn("instrumentUid", item)
        self.assertNotIn("tradesInfo", item)

    def test_share_access_flags_and_available_corporate_events(self):
        share = FIXTURE["examples"]["share"]["response"]["instrument"]
        self.assertTrue({"apiTradeAvailableFlag", "forQualInvestorFlag", "forIisFlag", "lot"}.issubset(share))
        self.assertIn("events", FIXTURE["examples"]["bond_events"]["response"])
        names = FIXTURE["availability"]["tool_names"]
        self.assertIn("invest_get_bond_events", names)
        self.assertIn("invest_get_share_dividends", names)
        self.assertFalse(any("split" in name for name in names))
