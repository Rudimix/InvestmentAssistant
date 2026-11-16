"""Фактические исполнения, направления количества и исторические границы операций."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import unittest

import _support  # noqa: F401
from investment_journal.market import MarketDataService
from investment_journal.models import PortfolioOperation, data_json
from investment_journal.portfolio_sources import fetch_operations, operation_from
from test_portfolio_sources import NOW, SourceTools


def buy_operation():
    return {"id": "buy", "date": NOW.isoformat(), "type": "OPERATION_TYPE_BUY", "state": "OPERATION_STATE_EXECUTED",
            "ticker": "TEST", "classCode": "TQBR", "quantity": "10", "quantityDone": "3",
            "payment": {"value": "-301", "currency": "rub"}, "price": {"value": "100", "currency": "rub"},
            "commission": {"value": "-1", "currency": "rub"},
            "tradesInfo": {"trades": [
                {"num": "first", "date": NOW.isoformat(), "quantity": "1", "price": {"value": "100", "currency": "rub"}},
                {"num": "second", "date": (NOW+timedelta(hours=1)).isoformat(), "quantity": "2",
                 "price": {"value": "100", "currency": "rub"}},
            ]}, "childOperations": [{"id": "fee", "payment": {"value": "-1", "currency": "rub"}}]}


class HistoricalOperationParsingTests(unittest.TestCase):
    def test_execution_times_actual_quantity_commission_and_round_trip(self):
        item = operation_from(buy_operation())
        self.assertEqual(item.instrument_id, "TEST_TQBR")
        self.assertEqual((item.quantity, item.requested_quantity, item.quantity_change), (Decimal(3), Decimal(10), Decimal(3)))
        self.assertTrue(item.execution_complete)
        self.assertEqual(item.executed_at, NOW+timedelta(hours=1))
        self.assertEqual(sum(fill.quantity for fill in item.trades), Decimal(3))
        self.assertEqual((item.commission, item.commission_currency), (Decimal(-1), "rub"))
        self.assertEqual(item.child_operations[0]["id"], "fee")
        self.assertEqual(PortfolioOperation.from_dict(json.loads(data_json(item))), item)

    def test_signed_quantities_progress_and_cancellation(self):
        for name, expected in (("SELL", -3), ("INPUT_SECURITIES", 3), ("OUTPUT_SECURITIES", -3)):
            with self.subTest(name=name):
                self.assertEqual(operation_from(buy_operation() | {"type": "OPERATION_TYPE_"+name}).quantity_change, expected)
        partial = operation_from(buy_operation() | {"state": "OPERATION_STATE_PROGRESS"})
        self.assertEqual(partial.quantity_change, 3)
        self.assertTrue(partial.execution_complete)
        cancelled = operation_from(buy_operation() | {"state": "OPERATION_STATE_CANCELED"})
        self.assertEqual(cancelled.quantity_change, 0)
        unknown = buy_operation() | {"type": "OPERATION_TYPE_OTHER", "corporateAction": {"kind": "split", "factor": "2"}}
        self.assertEqual(operation_from(unknown).corporate_effect, {"kind": "split", "factor": "2"})
        self.assertIsNone(operation_from(unknown).quantity_change)

    def test_duplicate_conflicting_invalid_and_incomplete_fills(self):
        raw = buy_operation()
        raw["tradesInfo"]["trades"].append(deepcopy(raw["tradesInfo"]["trades"][0]))
        self.assertEqual(len(operation_from(raw).trades), 2)
        raw["tradesInfo"]["trades"][-1]["quantity"] = "9"
        self.assertFalse(operation_from(raw).execution_complete)
        self.assertTrue(operation_from(raw).quality)
        raw["tradesInfo"]["trades"][-1]["date"] = "invalid"
        self.assertFalse(operation_from(raw).execution_complete)
        missing = buy_operation() | {"tradesInfo": {}}
        self.assertFalse(operation_from(missing).execution_complete)
        self.assertIsNone(operation_from(missing).executed_at)
        self.assertIsNone(operation_from({"id": "old", "date": NOW.isoformat(), "state": "OPERATION_STATE_PROGRESS",
                                         "type": "OPERATION_TYPE_BUY", "quantity": "10"}).quantity)

    def test_live_shape_preserves_child_effects_and_trade_details(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/history_mcp_contracts.json").read_text(encoding="utf-8"))
        raw = fixture["examples"]["buy_operation"]["response"]["items"][0]
        item = operation_from(raw)
        self.assertTrue(item.trades)
        self.assertTrue(item.child_operations)
        self.assertEqual(item.instrument_id, "sample-ticker_TQBR")


class HistoricalOperationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_includes_start_excludes_end_and_keeps_execution_inside(self):
        first = buy_operation()
        second = buy_operation() | {"id": "at-end", "date": (NOW+timedelta(days=1)).isoformat(), "tradesInfo": {}}
        root_before = buy_operation() | {"id": "root-before", "date": (NOW-timedelta(days=1)).isoformat()}
        tools = SourceTools({"invest_get_operations": [{"items": [first, second, root_before], "hasNext": False}]})
        result = await fetch_operations(MarketDataService(tools, "account"), NOW, NOW+timedelta(days=1), "closed_open")
        self.assertTrue(result.complete)
        self.assertEqual({item.id for item in result.items}, {"buy", "root-before"})
        self.assertLess(tools.calls[0][1]["from"], NOW.isoformat())

    async def test_changed_state_across_pages_and_missing_next_page(self):
        first = buy_operation()
        tools = SourceTools({"invest_get_operations": [
            {"items": [first], "hasNext": True, "nextCursor": "next"},
            {"items": [first | {"state": "OPERATION_STATE_CANCELED"}], "hasNext": True},
        ]})
        result = await fetch_operations(MarketDataService(tools, "account"), NOW, NOW+timedelta(days=1), "closed_open")
        self.assertFalse(result.complete)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].state, "OPERATION_STATE_CANCELED")
