"""Релевантность, устранение повторов и покрытие AI-контекста."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import unittest

import _support  # noqa: F401
from investment_journal.analytics import portfolio_structure
from investment_journal.market import snapshot_from
from investment_journal.models import EventBatch, InstrumentEvent, SnapshotPosition, data_json
from investment_journal.portfolio_events import event_window, report_context, select_events
from test_portfolio_market import NOW, portfolio_response


class EventSelectionTests(unittest.TestCase):
    def test_id_url_issuer_and_closed_positions(self):
        current = SnapshotPosition("current", "CURRENT", "share", Decimal(1), "rub", asset_id="issuer")
        closed = SnapshotPosition("closed", "CLOSED", "share", Decimal(1), "rub")
        first = InstrumentEvent("first", "news", ["issuer"], "A", "source", "https://example.com/a?utm_source=test",
                                NOW-timedelta(hours=1))
        duplicate = replace(first, id="second", instrument_ids=["closed"], url="https://example.com/a")
        repeated = replace(first, instrument_ids=["current"])
        unrelated = replace(first, id="unrelated", url="https://example.com/b", instrument_ids=["unrelated"])
        result = select_events(EventBatch([first, duplicate, repeated, unrelated]), [current, closed], NOW-timedelta(days=7), NOW)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(set(result.items[0].instrument_ids), {"issuer", "current", "closed"})
        self.assertEqual(result.found, 1)
        self.assertEqual(len(first.instrument_ids), 1)

    def test_news_outside_period_excluded_calendar_is_date_based(self):
        position = SnapshotPosition("p", "P", "share", Decimal(1), "rub")
        old = InstrumentEvent("old", "news", ["p"], "Old", "source", published_at=NOW-timedelta(days=30))
        calendar = InstrumentEvent("report", "report_calendar", ["p"], "Report", "source", event_at=NOW)
        result = select_events(EventBatch([old, calendar]), [position], NOW+timedelta(hours=1), NOW+timedelta(hours=2))
        self.assertEqual([item.id for item in result.items], ["report"])


class ReportContextTests(unittest.TestCase):
    def test_positions_without_valuation_are_included_and_marked(self):
        snapshot = snapshot_from(portfolio_response(), 1, "account", NOW)
        snapshot.positions = [replace(item, value=None) for item in snapshot.positions[:2]] + snapshot.positions[2:]
        context = report_context(portfolio_structure(snapshot), EventBatch())
        self.assertEqual(context["coverage"]["positions_included"], 3)
        self.assertEqual(context["coverage"]["positions_valued"], 1)
        self.assertEqual([p["valuation_available"] for p in context["positions"]].count(False), 2)
        self.assertNotIn("position:share:value", context["metrics"])

    def test_initial_window_and_actual_baseline_period(self):
        analysis = portfolio_structure(snapshot_from(portfolio_response(), 1, "account", NOW))
        self.assertEqual(event_window(analysis), (NOW-timedelta(days=7), NOW))
        analysis.baseline = replace(analysis.snapshot, id="before", observed_at=NOW-timedelta(hours=3))
        self.assertEqual(event_window(analysis), (NOW-timedelta(hours=3), NOW))

    def test_context_limits_source_status_and_missing_attribution(self):
        analysis = portfolio_structure(snapshot_from(portfolio_response(), 1, "account", NOW))
        first = InstrumentEvent("one", "news", ["share"], "News", "source", content="X"*10000)
        batch = EventBatch([first, replace(first, id="two")], {"news:SHARE": "partial", "reports:share": "unavailable"}, found=2)
        context = report_context(analysis, batch, limit=3000)
        self.assertLessEqual(len(data_json(context)), 3000)
        self.assertEqual(context["coverage"]["events_found"], 2)
        self.assertLess(context["coverage"]["events_included"], 2)
        self.assertEqual(context["source_states"]["reports:share"], "unavailable")
        if context["events"]:
            self.assertTrue(context["events"][0]["content_truncated"])
            self.assertIsNone(context["events"][0]["url"])
        empty = report_context(analysis, EventBatch([], {"news:SHARE": "complete"}))
        self.assertEqual(empty["source_states"]["news:SHARE"], "complete")
        self.assertEqual(empty["coverage"]["events_found"], 0)
