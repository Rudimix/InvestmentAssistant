"""Завершение приложения и ограничение одновременных отчётов."""

import asyncio
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from _support import TEST_KEY, TEST_TOKEN, FakeOpener, make_connections
from investment_journal.config import McpSettings
from investment_journal.connections import Connections, TokenCipher
from investment_journal import __main__ as entrypoint
from investment_journal.config import load_settings
from investment_journal.mcp_client import ToolError
from investment_journal.portfolio_reports import PortfolioReportService
from investment_journal.report_scheduling import IntervalRule
from investment_journal.storage import JournalRepository, SCHEMA
from test_portfolio_market import NOW
from test_portfolio_reports import report_tools


class ReportLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_migration_report_and_restore_on_temporary_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"journal.db"
            backup = Path(temporary)/"journal.backup.db"
            cipher = TokenCipher(TEST_KEY)
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript(SCHEMA)
                db.execute("INSERT INTO users(id,chat_id) VALUES(1,7)")
                db.execute("INSERT INTO credentials VALUES(1,?,'2000000001','date')", (cipher.encrypt(TEST_TOKEN),))
            shutil.copy2(path, backup)
            original_backup = backup.read_bytes()
            repo = JournalRepository(path)
            connections = Connections(repo, cipher, McpSettings(), FakeOpener(report_tools()))
            reports = PortfolioReportService(connections, clock=lambda: NOW)

            async def send(**kwargs):
                return SimpleNamespace(message_id=1)

            result = await reports.request(7, "message", send)
            self.assertEqual(result["state"], "delivered")
            await reports.aclose()
            repo.close()
            self.assertEqual(backup.read_bytes(), original_backup)
            shutil.copy2(backup, path)
            with closing(sqlite3.connect(path)) as restored:
                self.assertEqual(restored.execute("SELECT chat_id FROM users").fetchone()[0], 7)
                self.assertEqual(restored.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertIsNone(restored.execute("SELECT name FROM sqlite_master WHERE name='report_runs'").fetchone())

    async def test_run_cancels_worker_and_handlers_before_clients_and_database(self):
        tools = report_tools(count=10)
        connections = make_connections(tools, connected=(7, 8))
        repo = connections.repo
        settings = load_settings({"TELEGRAM_BOT_TOKEN": "42:TEST", "TOKEN_ENCRYPTION_KEY": TEST_KEY,
                                  "LLM_BASE_URL": "https://llm.example/v1"}, dotenv=None)
        dispatcher = entrypoint.build_dispatcher(settings, connections)
        reports = PortfolioReportService(connections, clock=lambda: NOW)
        dispatcher["reports"] = reports
        user = connections.user(7)
        rule = IntervalRule(6, "hours", NOW, "UTC")
        repo.save_schedule(user.id, "2000000001", rule.to_dict(), NOW)
        events = []
        both_started = asyncio.Event()
        active = 0
        original_call = tools.call

        async def delayed(name, arguments=None):
            nonlocal active
            if name != "invest_get_portfolio":
                return await original_call(name, arguments)
            active += 1
            if active == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
                events.append("source_exit")

        tools.call = delayed
        bot = SimpleNamespace(
            set_my_commands=AsyncMock(), send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            session=SimpleNamespace(close=AsyncMock(side_effect=lambda: events.append("bot_closed"))),
        )
        llm = SimpleNamespace(aclose=AsyncMock(side_effect=lambda: events.append("llm_closed")))

        async def polling(*args, **kwargs):
            self.assertFalse(kwargs["close_bot_session"])
            handler = asyncio.create_task(reports.request(8, "manual", bot.send_message))
            dispatcher._handle_update_tasks.add(handler)
            handler.add_done_callback(dispatcher._handle_update_tasks.discard)
            await asyncio.wait_for(both_started.wait(), 2)

        dispatcher.start_polling = AsyncMock(side_effect=polling)
        original_close = repo.close

        def close_database():
            self.assertEqual(active, 0)
            self.assertFalse(reports._active)
            events.append("db_closed")
            original_close()

        repo.close = close_database
        with patch.object(entrypoint, "JournalRepository", return_value=repo), \
                patch.object(entrypoint, "Bot", return_value=bot), \
                patch.object(entrypoint, "ChatCompletionsClient", return_value=llm), \
                patch.object(entrypoint, "build_dispatcher", return_value=dispatcher):
            await entrypoint.run(settings)
        self.assertEqual(events.count("source_exit"), 2)
        self.assertLess(max(index for index, value in enumerate(events) if value == "source_exit"), events.index("bot_closed"))
        self.assertLess(events.index("llm_closed"), events.index("db_closed"))
        self.assertTrue(reports._closed)
        with self.assertRaises(ToolError):
            await reports.request(7, "after-close", bot.send_message)

    async def test_service_shutdown_during_send_preserves_unknown_delivery(self):
        connections = make_connections(report_tools())
        reports = PortfolioReportService(connections, clock=lambda: NOW)
        started = asyncio.Event()

        async def interrupted(**kwargs):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(reports.request(7, "message", interrupted))
        await asyncio.wait_for(started.wait(), 2)
        await reports.aclose()
        self.assertTrue(task.cancelled())
        user = connections.user(7)
        self.assertEqual(connections.repo.last_run(user.id)["state"], "delivery_unknown")
        self.assertFalse(reports._active)
        connections.repo.close()

    async def test_concurrency_is_bounded_across_users(self):
        tools = report_tools(count=10)
        connections = make_connections(tools, connected=tuple(range(1, 9)))
        reports = PortfolioReportService(connections, clock=lambda: NOW)
        original = tools.call
        active, peak = 0, 0
        full, release = asyncio.Event(), asyncio.Event()

        async def delayed(name, arguments=None):
            nonlocal active, peak
            if name != "invest_get_portfolio":
                return await original(name, arguments)
            active += 1
            peak = max(peak, active)
            if active == 4:
                full.set()
            try:
                await release.wait()
                return await original(name, arguments)
            finally:
                active -= 1

        async def send(**kwargs):
            return SimpleNamespace(message_id=1)

        tools.call = delayed
        tasks = [asyncio.create_task(reports.request(chat_id, "message", send)) for chat_id in range(1, 9)]
        await asyncio.wait_for(full.wait(), 2)
        self.assertEqual(peak, 4)
        release.set()
        results = await asyncio.gather(*tasks)
        self.assertTrue(all(result["state"] == "delivered" for result in results))
        self.assertEqual(peak, 4)
        await reports.aclose()
        connections.repo.close()
