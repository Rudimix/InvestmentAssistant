"""Фоновые обращения завершаются до закрытия публичного клиента и базы."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

from _support import TEST_KEY, make_connections
from investment_journal import __main__ as application
from investment_journal.config import load_settings
from investment_journal.portfolio_reports import PortfolioReportService
from test_portfolio_reports import report_tools


class ApplicationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_stops_collector_before_client_and_database(self):
        order = []
        connections = make_connections(report_tools())
        repo = connections.repo
        close = repo.close

        def close_repo():
            order.append("database")
            close()

        repo.close = close_repo
        started = asyncio.Event()

        class Evidence:
            def __init__(self, *args):
                pass

            async def collect(self, *args, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    order.append("collector")

            async def aclose(self):
                order.append("client")

        class Dispatcher(dict):
            def __init__(self, evidence):
                super().__init__(reports=PortfolioReportService(connections, evidence=evidence))
                self._handle_update_tasks = set()

            async def start_polling(self, *args, **kwargs):
                task = asyncio.create_task(self["reports"].request(7, "shutdown", AsyncMock()))
                self._handle_update_tasks.add(task)
                await asyncio.wait_for(started.wait(), 2)

        bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()), set_my_commands=AsyncMock(), send_message=AsyncMock())
        settings = load_settings({"TELEGRAM_BOT_TOKEN": "42:TEST", "TOKEN_ENCRYPTION_KEY": TEST_KEY}, dotenv=None)
        with (patch.object(application, "JournalRepository", return_value=repo),
              patch.object(application, "Connections", return_value=connections),
              patch.object(application, "EvidenceService", Evidence),
              patch.object(application, "EvidenceHttpClient"),
              patch.object(application, "Bot", return_value=bot),
              patch.object(application, "build_dispatcher", side_effect=lambda *args: Dispatcher(args[3]))):
            await application.run(settings)
        self.assertEqual(order, ["collector", "client", "database"])
        bot.session.close.assert_awaited_once()
