"""Подбор через общую очередь, личные ограничения, повторы и изоляция."""

from contextlib import asynccontextmanager
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock
import unittest

from _support import make_connections
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from investment_journal.connections import Connections
from investment_journal.storage import JournalRepository
from investment_journal.mcp_client import ToolError
from investment_journal.models import Account, PortfolioSnapshot, SnapshotPosition
from investment_journal.portfolio_reports import PortfolioReportService
from investment_journal.stock_screening import StockScreeningService, candidate_suitability
from investment_journal.stock_formatting import candidate_lines
from investment_journal.stock_universe import StockUniverseService
from test_analysis_models import NOW, profile
from test_stock_universe import UniverseMarket
import test_stock_technicals as technical_fixtures


class ScreeningMarket(UniverseMarket):
    async def account_id(self):
        return "2000000001"

    async def accounts(self):
        return [Account("2000000001", type="ACCOUNT_TYPE_TINKOFF")]

    async def snapshot(self, user_id, observed_at):
        return PortfolioSnapshot("current", user_id, await self.account_id(), observed_at, "rub", D(100000), D(90000), [
            SnapshotPosition("SBER_TQBR", "SBER", "share", D(100), "rub", value=D(10000), price=D(100),
                             instrument_id="SBER_TQBR", asset_id="asset", sector="financial", sector_as_of=NOW)])


class ScreeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connections = make_connections(connected=(7, 8))
        self.addCleanup(self.connections.repo.close)
        self.repo = self.connections.repo
        self.user = self.connections.user(7)
        self.market = ScreeningMarket()
        self.service = StockScreeningService(self.repo, lambda: NOW,
            universe=StockUniverseService(self.repo, lambda: NOW, call_interval=0))

    async def test_service_missing_profile_snapshot_and_replay_scope(self):
        result = await self.service.prepare(self.user.id, self.market, "technical", None)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["suitability"]["status"], "unknown")
        self.assertEqual(result, self.repo.get_screening(self.user.id, "2000000001", result["id"]))
        self.assertIsNone(self.repo.get_screening(self.connections.user(8).id, "2000000001", result["id"]))
        self.assertEqual(self.repo._db.execute("SELECT count(*) FROM portfolio_snapshots").fetchone()[0], 0)
        fundamental = "\n".join(candidate_lines(candidate, "fundamental"))
        technical = "\n".join(candidate_lines(candidate, "technical"))
        self.assertIn("Подтверждённый показатель", fundamental)
        self.assertNotIn("RSI 14", fundamental)
        self.assertIn("RSI 14", technical)
        self.assertNotIn("Подтверждённый показатель", technical)

    async def test_profile_concentration_cash_horizon_and_partial_currency(self):
        entry = technical_fixtures.TechnicalTests().entry()
        entry.price, entry.lot = D(100), D(10)
        entry.included, entry.account_access, entry.trading_now, entry.max_buy_lots = True, True, True, D(100)
        from investment_journal.stock_technicals import technical_analysis
        technical = technical_analysis(entry, NOW)
        snapshot = await self.market.snapshot(self.user.id, NOW)
        limits = replace(profile(), user_id=self.user.id)
        result = candidate_suitability(entry, technical, snapshot, limits)
        self.assertEqual(result["status"], "conditional")
        concentrated = replace(snapshot, total=D(12000), cash=D(2000))
        result = candidate_suitability(entry, technical, concentrated, replace(limits, horizon_months=6))
        self.assertEqual(result["status"], "restricted")
        self.assertTrue(any("долю бумаги" in reason for reason in result["reasons"]))
        self.assertTrue(any("остаток" in reason for reason in result["reasons"]))
        self.assertEqual(candidate_suitability(replace(entry, currency="usd"), technical, snapshot, limits)["status"], "unknown")

    async def test_queue_duplicate_request_and_no_portfolio_baseline(self):
        reports = PortfolioReportService(self.connections, clock=lambda: NOW)
        reports.screening = self.service

        @asynccontextmanager
        async def market(chat_id):
            yield self.market

        self.connections.market = market
        send = AsyncMock(return_value=SimpleNamespace(message_id=77))
        run = await reports.request(7, "stocks-1", send, report_kind="screening", mode="technical")
        self.assertEqual(run["state"], "delivered")
        calls = len(self.market.calls)
        sent = send.await_count
        repeated = await reports.request(7, "stocks-1", send, report_kind="screening", mode="combined")
        self.assertEqual(repeated["id"], run["id"])
        self.assertEqual(send.await_count, sent)
        self.assertEqual(len(self.market.calls), calls)
        self.assertIsNone(run["snapshot_id"])
        self.assertIsNone(self.repo.baseline_snapshot(self.user.id, "2000000001", "manual", NOW))
        self.assertEqual(len(self.repo.screening_history(self.user.id, "2000000001")), 1)
        self.assertEqual(self.repo.screening_history(self.connections.user(8).id, "2000000001"), [])

    async def test_screening_resumes_from_database_with_same_manifest(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/"journal.db"
            repo = JournalRepository(path)
            user = repo.get_or_create_user(7)
            repo.save_credentials(user.id, *self.repo.get_credentials(self.user.id))
            connections = Connections(repo, self.connections._cipher, self.connections._settings)
            reports = PortfolioReportService(connections, clock=lambda: NOW)
            reports.screening = StockScreeningService(repo, lambda: NOW, universe=StockUniverseService(repo, lambda: NOW, call_interval=0))

            @asynccontextmanager
            async def session(chat_id):
                yield self.market

            connections.market = session
            limited = AsyncMock(side_effect=TelegramRetryAfter(method=SendMessage(chat_id=7, text="test"), message="rate", retry_after=30))
            run = await reports.request(7, "recovery", limited, report_kind="screening", mode="technical")
            self.assertEqual(run["state"], "retry")
            manifest, payload = run["manifest"], run["payload"]
            await reports.aclose()
            repo.close()
            repo = JournalRepository(path)
            try:
                connections = Connections(repo, self.connections._cipher, self.connections._settings)
                reports = PortfolioReportService(connections, clock=lambda: NOW+timedelta(seconds=60))
                reports.screening.prepare = AsyncMock(side_effect=AssertionError("must use stored result"))
                send = AsyncMock(return_value=SimpleNamespace(message_id=88))
                result = await reports.execute_run(repo.get_run(user.id, run["id"]), send)
                self.assertEqual(result["state"], "delivered")
                self.assertEqual((result["manifest"], result["payload"]), (manifest, payload))
                self.assertIn('"universe_version"', manifest)
                reports.screening.prepare.assert_not_awaited()
            finally:
                await reports.aclose()
                repo.close()

    async def test_analysis_and_screening_share_four_execution_slots(self):
        connections = make_connections(connected=range(20, 26))
        self.addCleanup(connections.repo.close)
        reports = PortfolioReportService(connections, clock=lambda: NOW)
        active, peak = 0, 0
        kinds = set()

        async def prepare(run, owner, chat_id):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            kinds.add(run["report_kind"])
            try:
                await asyncio.sleep(0.01)
                raise ToolError("verification", temporary=False)
            finally:
                active -= 1

        reports._prepare = prepare
        send = AsyncMock()
        await asyncio.gather(*(reports.request(chat_id, "capacity", send, report_kind="analysis" if chat_id % 2 else "screening")
                               for chat_id in range(20, 26)))
        self.assertEqual(kinds, {"analysis", "screening"})
        self.assertEqual(peak, 4)


if __name__ == "__main__":
    unittest.main()
