"""Адаптеры реальных контрактов и полнота постраничных данных."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from decimal import Decimal
import unittest

from _support import FakeToolClient
from investment_journal.market import MarketDataService
from investment_journal.mcp_client import ToolError
from investment_journal.models import SnapshotPosition
from investment_journal.portfolio_sources import fetch_events
from investment_journal.portfolio_sources import fetch_operations

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)
FIXTURE = json.loads((Path(__file__).parent / "fixtures/portfolio_mcp_contracts.json").read_text(encoding="utf-8"))


def operation(number="op", state="OPERATION_STATE_EXECUTED"):
    return {"id": number, "date": NOW.isoformat(), "type": "OPERATION_TYPE_INPUT", "state": state,
            "payment": {"value": "10000.000000001", "currency": "rub"}}


class SourceTools(FakeToolClient):
    def __init__(self, responses):
        super().__init__()
        self.responses = deepcopy(responses)

    async def list_tools(self):
        return FIXTURE["tools"]

    async def call(self, name, arguments=None):
        if name not in self.responses:
            return await super().call(name, arguments)
        self.calls.append((name, arguments or {}))
        response = self.responses[name].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    async def test_pages_deduplicate_and_keep_changed_state(self):
        tools = SourceTools({"invest_get_operations": [
            {"hasNext": True, "nextCursor": "next", "items": [operation()]},
            {"hasNext": False, "items": [operation(state="OPERATION_STATE_CANCELED"), operation("other")]},
        ]})
        batch = await fetch_operations(MarketDataService(tools, "account"), NOW - timedelta(days=1), NOW)
        self.assertTrue(batch.complete)
        self.assertEqual(len(batch.items), 2)
        self.assertEqual(batch.items[0].state, "OPERATION_STATE_CANCELED")
        self.assertEqual(str(batch.items[0].payment), "10000.000000001")
        self.assertEqual(tools.calls[1][1]["cursor"], "next")
        self.assertEqual(tools.calls[0][1]["responseView"], ["FULL"])

    async def test_partial_page_error_preserves_observed_operations(self):
        tools = SourceTools({"invest_get_operations": [
            {"hasNext": True, "nextCursor": "next", "items": [operation()]}, ToolError("unavailable"),
        ]})
        batch = await fetch_operations(MarketDataService(tools, "account"), NOW - timedelta(days=1), NOW)
        self.assertFalse(batch.complete)
        self.assertEqual(len(batch.items), 1)
        self.assertTrue(batch.quality)

    async def test_missing_cursor_invalid_item_and_period_boundary(self):
        before = operation("before") | {"date": (NOW - timedelta(days=1)).isoformat()}
        tools = SourceTools({"invest_get_operations": [{"hasNext": True, "items": [before, {}, operation()]}]})
        batch = await fetch_operations(MarketDataService(tools, "account"), NOW - timedelta(days=1), NOW)
        self.assertFalse(batch.complete)
        self.assertEqual([item.id for item in batch.items], ["op"])


class EventSourceTests(unittest.IsolatedAsyncioTestCase):
    def position(self):
        return SnapshotPosition("sber", "SBER", "share", Decimal(1), "rub",
                                instrument_id="e6123145-9665-43e0-8413-cd61b8aa9b13")

    async def test_public_response_shapes_keep_calendar_and_missing_links(self):
        tools = SourceTools({"invest_get_news": [FIXTURE["news"]],
                             "invest_get_instrument_reports": [FIXTURE["reports"]]})
        batch = await fetch_events(MarketDataService(tools, "account"), [self.position()],
                                   datetime(2026, 1, 1, tzinfo=timezone.utc), NOW)
        self.assertEqual(batch.items[0].kind, "news")
        self.assertIn(self.position().instrument_id, batch.items[0].instrument_ids)
        self.assertIsNotNone(batch.items[0].published_at)
        self.assertTrue(all(item.kind == "report_calendar" for item in batch.items[1:]))
        self.assertIsNone(batch.items[0].url)
        self.assertTrue(batch.quality)
        self.assertNotIn("from", tools.calls[0][1])

    async def test_content_and_link_preserved_and_source_failure_visible(self):
        report = {"reportDate": "2026-11-15", "periodYear": 2026, "content": "Доступные показатели",
                  "url": "https://example.com/report", "publishedAt": "2026-11-15T12:00:00Z"}
        tools = SourceTools({"invest_get_news": [ToolError("unavailable")],
                             "invest_get_instrument_reports": [{"events": [report]}]})
        batch = await fetch_events(MarketDataService(tools, "account"), [self.position()], NOW-timedelta(days=7), NOW)
        self.assertEqual(batch.sources["news:SBER"], "unavailable")
        self.assertEqual(batch.items[0].kind, "report_content")
        self.assertEqual(batch.items[0].url, report["url"])
