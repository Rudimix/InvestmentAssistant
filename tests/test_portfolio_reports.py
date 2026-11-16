"""Подготовка, форматирование и восстановление доставки отчёта."""

import asyncio
from datetime import timedelta
from html.parser import HTMLParser
import json
from types import SimpleNamespace
import unittest

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from _support import TEST_TOKEN, make_connections
from investment_journal.analytics import portfolio_structure
from investment_journal.models import EventBatch, InstrumentEvent
from investment_journal.portfolio_events import report_context
from investment_journal.portfolio_formatting import portfolio_report_parts, text_units
from investment_journal.portfolio_reports import PortfolioReportService
from investment_journal.market import snapshot_from
from test_portfolio_market import NOW, portfolio_response
from test_portfolio_sources import SourceTools


def report_tools(count=2, long_news=False):
    raw = portfolio_response() | {"accountId": "2000000001"}
    news = []
    if long_news:
        news = [{"id": "long", "title": "N"*5000, "source": "test", "ts": (NOW-timedelta(hours=1)).isoformat(),
                 "instrumentId": [{"instrument": {"instrumentUid": "share", "ticker": "SHARE"}}]}]
    return SourceTools({
        "invest_get_portfolio": [raw for _ in range(count)],
        "invest_get_operations": [{"items": [], "hasNext": False} for _ in range(count)],
        "invest_get_news": [{"items": news, "hasNext": False} for _ in range(count*2)],
    })


class VisibleHtml(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.links = []

    def handle_data(self, data):
        self.text += data

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs)["href"])


class ReportFormattingTests(unittest.TestCase):
    def test_unicode_safe_split_escaped_text_and_complete_source_link(self):
        analysis = portfolio_structure(snapshot_from(portfolio_response(), 1, "account", NOW))
        title = "<b>Source</b> & " + "😀"*2000
        link = "https://example.com/source?long=" + "x"*5000 + "&name=test"
        event = InstrumentEvent("news", "news", ["share"], title, "source", url=link, published_at=NOW)
        events = EventBatch([event], found=1)
        parts = portfolio_report_parts(analysis, events, {"available": False}, report_context(analysis, events), "report-id")
        visible = []
        links = []
        for part in parts:
            parser = VisibleHtml()
            parser.feed(part)
            self.assertLessEqual(text_units(parser.text), 4096)
            visible.append(parser.text)
            links.extend(parser.links)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(visible).count("😀"), 2000)
        self.assertIn("<b>Source</b>", "".join(visible))
        self.assertEqual(links, [link])


class ReportServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tools = report_tools()
        self.connections = make_connections(self.tools)
        self.now = NOW
        self.service = PortfolioReportService(self.connections, clock=lambda: self.now)
        self.sent = []

    async def asyncTearDown(self):
        self.connections.repo.close()

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))

    async def test_report_without_llm_is_complete_and_same_request_is_idempotent(self):
        run = await self.service.request(7, "message-1", self.send)
        self.assertEqual(run["state"], "delivered")
        saved = json.loads(run["payload"])
        self.assertEqual(saved["analysis"]["metrics"]["total"], "240")
        self.assertFalse(saved["explanation"]["available"])
        sent_count = len(self.sent)
        repeated = await self.service.request(7, "message-1", self.send)
        self.assertEqual(repeated["id"], run["id"])
        self.assertEqual(len(self.sent), sent_count)
        self.assertNotIn(TEST_TOKEN, json.dumps(saved))
        self.assertIn("240.00", "".join(part["text"] for part in self.sent))

    async def test_requests_of_one_user_are_serialized(self):
        original = self.tools.call
        active = 0
        peak = 0

        async def slow_call(name, arguments=None):
            nonlocal active, peak
            if name != "invest_get_portfolio":
                return await original(name, arguments)
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            try:
                return await original(name, arguments)
            finally:
                active -= 1

        self.tools.call = slow_call
        results = await asyncio.gather(self.service.request(7, "first", self.send), self.service.request(7, "second", self.send))
        self.assertTrue(all(run["state"] == "delivered" for run in results))
        self.assertEqual(peak, 1)

    async def test_retry_resumes_saved_parts_without_generating_again(self):
        self.tools.responses = report_tools(long_news=True).responses
        calls = 0

        async def interrupted(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TelegramRetryAfter(method=SendMessage(chat_id=7, text="part"), message="limited", retry_after=45)
            return await self.send(**kwargs)

        run = await self.service.request(7, "retry", interrupted)
        self.assertEqual(run["state"], "retry")
        parts = self.connections.repo.get_parts(run["user_id"], run["id"])
        self.assertEqual(parts[0]["state"], "sent")
        self.assertEqual(parts[1]["state"], "queued")
        self.now += timedelta(seconds=46)
        resumed = await self.service.execute_run(run, self.send)
        self.assertEqual(resumed["state"], "delivered")
        self.assertEqual(sum(name == "invest_get_portfolio" for name, _ in self.tools.calls), 1)

    async def test_unknown_send_and_cancellation_preserve_delivery_state(self):
        async def uncertain(**kwargs):
            raise TimeoutError("unconfirmed")

        run = await self.service.request(7, "unknown", uncertain)
        self.assertEqual(run["state"], "delivery_unknown")
        self.assertTrue(all(part["state"] != "sent" for part in self.connections.repo.get_parts(run["user_id"], run["id"])))
        self.assertEqual(self.connections.repo.ready_runs(self.now+timedelta(days=1)), [])

    async def test_cancelled_transport_is_unknown_and_does_not_advance_existing_baseline(self):
        first = await self.service.request(7, "first", self.send)
        self.now += timedelta(days=1)
        started = asyncio.Event()

        async def interrupted(**kwargs):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(self.service.request(7, "cancelled", interrupted))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        last = self.connections.repo.last_run(first["user_id"])
        self.assertEqual(last["state"], "delivery_unknown")
        baseline = self.connections.repo.baseline_snapshot(first["user_id"], first["account_id"], "manual", self.now+timedelta(days=1))
        self.assertEqual(baseline.id, first["snapshot_id"])
