"""Устойчивые фоновые отчёты с управляемым временем."""

import asyncio
import json
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from _support import TEST_TOKEN, make_connections
from investment_journal.connections import Connections
from investment_journal.mcp_client import ToolError
from investment_journal.portfolio_reports import PortfolioReportService
from investment_journal.report_scheduling import IntervalRule, ReportScheduler, latest_due
from investment_journal.analysis_periods import rolling_period
from investment_journal.storage import JournalRepository
from test_portfolio_market import NOW
from test_portfolio_reports import report_tools


async def drain(scheduler):
    jobs = list(scheduler._jobs.values())
    if jobs:
        await asyncio.gather(*jobs)
    await asyncio.sleep(0)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tools = report_tools(count=10)
        self.connections = make_connections(self.tools)
        self.repo = self.connections.repo
        self.user = self.connections.user(7)
        self.now = NOW
        self.temporary = None
        self.sent = []
        self.reports = PortfolioReportService(self.connections, clock=lambda: self.now)
        self.scheduler = ReportScheduler(self.reports, self.send)
        self.rule = IntervalRule(6, "hours", NOW, "Europe/Moscow")
        self.schedule = self.repo.save_schedule(self.user.id, "2000000001", self.rule.to_dict(), NOW)

    async def asyncTearDown(self):
        await self.scheduler.stop()
        self.repo.close()
        if self.temporary:
            self.temporary.cleanup()

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))

    async def test_two_schedulers_observe_one_occurrence(self):
        other = ReportScheduler(self.reports, self.send)
        try:
            await asyncio.gather(self.scheduler.tick(), other.tick())
            await asyncio.gather(drain(self.scheduler), drain(other))
            rows = self.repo._db.execute("SELECT * FROM report_runs WHERE origin='scheduled'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], "delivered")
            self.assertEqual(self.repo.get_schedule(self.user.id)["next_run_at"], NOW+timedelta(hours=6))
        finally:
            await other.stop()

    async def test_rolling_period_uses_latest_due_and_is_frozen_in_queue(self):
        first = NOW-timedelta(days=21)
        rule = IntervalRule(7, "days", first, "Europe/Moscow")
        schedule = self.repo.save_schedule(self.user.id, "2000000001", rule.to_dict(), first,
                                          {"kind": "rolling", "days": 30}, ["analysis", "advice", "screening"])
        planned = latest_due(rule, NOW+timedelta(hours=2))
        self.assertEqual(planned, NOW)
        self.repo.enqueue_schedule(schedule, rule.next_after(NOW), NOW+timedelta(hours=2), planned)
        run = self.repo.last_run(self.user.id)
        expected = rolling_period(30, "Europe/Moscow", planned)
        period = json.loads(run["resolved_period"])
        self.assertEqual(period["local_start"], expected.local_start)
        self.assertEqual(period["local_end"], expected.local_end)
        self.assertEqual(json.loads(run["manifest"])["sections"], ["analysis", "advice", "screening"])
        self.repo.enqueue_schedule(schedule, rule.next_after(NOW), NOW+timedelta(days=1), planned)
        self.assertEqual(self.repo.get_run(self.user.id, run["id"])["resolved_period"], run["resolved_period"])

    async def test_renewed_then_expired_lease_and_future_nominal_time(self):
        self.repo.enqueue_schedule(self.schedule, NOW+timedelta(hours=6), NOW)
        run = self.repo.ready_runs(NOW)[0]
        self.assertTrue(self.repo.claim_run(self.user.id, run["id"], "stopped-worker", NOW))
        self.assertTrue(self.repo.renew_run(self.user.id, run["id"], "stopped-worker", NOW+timedelta(seconds=179)))
        self.now = NOW+timedelta(seconds=181)
        await self.scheduler.tick()
        await drain(self.scheduler)
        self.assertEqual(self.sent, [])
        self.now = NOW+timedelta(seconds=360)
        await self.scheduler.tick()
        await drain(self.scheduler)
        self.assertEqual(self.repo.get_run(self.user.id, run["id"])["state"], "delivered")
        self.assertEqual(self.repo.get_schedule(self.user.id)["next_run_at"], NOW+timedelta(hours=6))

    async def test_downtime_coalesces_to_one_current_report_and_keeps_rhythm(self):
        self.now = NOW+timedelta(hours=25)
        await self.scheduler.tick()
        await drain(self.scheduler)
        rows = self.repo._db.execute("SELECT * FROM report_runs WHERE origin='scheduled'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "delivered")
        snapshot = self.repo.get_snapshot(self.user.id, "2000000001", rows[0]["snapshot_id"])
        self.assertEqual(snapshot.observed_at, self.now)
        self.assertEqual(self.repo.get_schedule(self.user.id)["next_run_at"], NOW+timedelta(hours=30))

    async def test_long_execution_has_only_one_pending_successor(self):
        original = self.tools.call
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(name, arguments=None):
            if name == "invest_get_portfolio":
                started.set()
                await release.wait()
            return await original(name, arguments)

        self.tools.call = delayed
        await self.scheduler.tick()
        await asyncio.wait_for(started.wait(), 2)
        self.now = NOW+timedelta(hours=7)
        await self.scheduler.tick()
        self.now = NOW+timedelta(hours=19)
        await self.scheduler.tick()
        pending = self.repo._db.execute("SELECT count(*) FROM report_runs WHERE state='queued'").fetchone()[0]
        self.assertEqual(pending, 1)
        release.set()
        await drain(self.scheduler)
        self.now += timedelta(seconds=1)
        await self.scheduler.tick()
        await drain(self.scheduler)
        rows = self.repo._db.execute("SELECT * FROM report_runs WHERE origin='scheduled'").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["state"] == "delivered" for row in rows))
        self.assertEqual(self.repo.get_schedule(self.user.id)["next_run_at"], NOW+timedelta(hours=24))

    async def test_three_attempts_then_failure_preserves_future_occurrences(self):
        self.tools.responses["invest_get_portfolio"] = [ToolError("temporary") for _ in range(3)]
        for seconds in (0, 31, 152):
            self.now = NOW+timedelta(seconds=seconds)
            await self.scheduler.tick()
            await drain(self.scheduler)
        run = self.repo.last_run(self.user.id)
        self.assertEqual(run["attempts"], 3)
        self.assertEqual(run["state"], "failed")
        self.assertIsNone(run["retry_at"])
        self.assertTrue(self.repo.get_schedule(self.user.id)["enabled"])
        self.assertEqual(self.repo.get_schedule(self.user.id)["next_run_at"], NOW+timedelta(hours=6))

    async def test_authorization_failure_pauses_only_its_user_and_sends_notice(self):
        other = self.connections.user(8)
        self.repo.save_credentials(other.id, self.connections._cipher.encrypt(TEST_TOKEN), "2000000001")
        self.repo.save_schedule(other.id, "2000000001", self.rule.to_dict(), NOW)
        self.tools.responses["invest_get_portfolio"].insert(0, ToolError("denied", authorization=True, temporary=False))
        await self.scheduler.tick()
        await drain(self.scheduler)
        self.assertFalse(self.repo.get_schedule(self.user.id)["enabled"])
        self.assertTrue(self.repo.get_schedule(other.id)["enabled"])
        self.assertEqual(self.repo.last_run(other.id)["state"], "delivered")
        self.assertTrue(any(item["chat_id"] == 7 and "/connect" in item["text"] for item in self.sent))
        self.assertTrue(any(item["chat_id"] == 8 and item["parse_mode"] == "HTML" for item in self.sent))

    async def test_saved_retry_and_parts_survive_database_reopen(self):
        self.tools.responses = report_tools(long_news=True).responses
        calls = 0

        async def limited(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TelegramRetryAfter(method=SendMessage(chat_id=7, text="part"), message="limited", retry_after=45)
            return await self.send(**kwargs)

        self.scheduler.send = limited
        await self.scheduler.tick()
        await drain(self.scheduler)
        run = self.repo.last_run(self.user.id)
        self.assertEqual(run["state"], "retry")
        await self.scheduler.stop()
        self.temporary = tempfile.TemporaryDirectory()
        path = Path(self.temporary.name)/"journal.db"
        with closing(sqlite3.connect(path)) as backup:
            self.repo._db.backup(backup)
        previous = self.connections
        self.repo.close()
        self.repo = JournalRepository(path)
        self.connections = Connections(self.repo, previous._cipher, previous._settings, previous._opener)
        self.reports = PortfolioReportService(self.connections, clock=lambda: self.now)
        self.scheduler = ReportScheduler(self.reports, self.send)
        self.now += timedelta(seconds=46)
        await self.scheduler.tick()
        await drain(self.scheduler)
        self.assertEqual(self.repo.get_run(self.user.id, run["id"])["state"], "delivered")
        self.assertEqual(sum(name == "invest_get_portfolio" for name, _ in self.tools.calls), 1)
