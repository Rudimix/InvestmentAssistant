"""Два профиля, история, советы, подбор, расписание и восстановление доставки."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from _support import TEST_KEY, TEST_TOKEN
from investment_journal.analysis_periods import explicit_period
from investment_journal.config import McpSettings
from investment_journal.connections import Connections, TokenCipher
from investment_journal.evaluation_replay import ReplayMarket
from investment_journal.llm import LlmError
from investment_journal.models import EventBatch, InvestorProfile, timestamp
from investment_journal.portfolio_reports import PortfolioReportService
from investment_journal.report_scheduling import IntervalRule
from investment_journal.stock_universe import StockUniverseService
from investment_journal.storage import JournalRepository
from test_evaluation_replay import ContextLlm, DATASET, DIRECTORY


class ExpandedEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_profiles_full_reports_partial_sources_llm_failure_and_restart(self):
        source = json.loads((DIRECTORY/DATASET["market_file"]).read_text(encoding="utf-8"))
        case = deepcopy(DATASET["scenarios"][0])
        now = [timestamp(case["as_of"])]
        with TemporaryDirectory() as folder:
            path = Path(folder)/"journal.db"
            repo = JournalRepository(path)
            reports = None
            try:
                cipher = TokenCipher(TEST_KEY)
                connections = Connections(repo, cipher, McpSettings())
                users = [connections.user(chat_id) for chat_id in (7, 8)]
                for user in users:
                    repo.save_credentials(user.id, cipher.encrypt(TEST_TOKEN), "evaluation-account")
                    original = InvestorProfile.from_dict(case["profile"])
                    repo.save_profile(user.id, replace(original, user_id=user.id, goal="Рост капитала" if user == users[0] else "Денежный резерв",
                                                      horizon_months=24 if user == users[0] else 60, max_drawdown_pct=Decimal(20)))

                @asynccontextmanager
                async def market(chat_id):
                    yield ReplayMarket(source["records"], case)

                connections.market = market

                class PartialEvidence:
                    async def collect(self, *args, **kwargs):
                        return EventBatch(sources={"public": "partial"}, quality=["Открытый источник временно недоступен"])

                reports = PortfolioReportService(connections, ContextLlm(), clock=lambda: now[0], evidence=PartialEvidence())
                reports.screening.universe = StockUniverseService(repo, lambda: now[0], call_interval=0)
                send = AsyncMock(return_value=SimpleNamespace(message_id=100))
                period = explicit_period("01.10.2026", "16.11.2026", "UTC", now[0])
                analyses = await asyncio.gather(*(reports.request(chat_id, "history", send, period=period) for chat_id in (7, 8)))
                self.assertTrue(all(run["state"] == "delivered" for run in analyses))
                first_payload = json.loads(analyses[0]["payload"])
                self.assertEqual(first_payload["events"]["sources"]["public"], "partial")
                self.assertTrue(first_payload["advice"]["result"]["proposals"])
                state_id = first_payload["historical_analysis"]["end_state"]["id"]
                self.assertIsNone(repo.get_historical_state(users[1].id, "evaluation-account", state_id))
                first_advice = first_payload["advice"]["result"]["proposals"][0]["id"]
                self.assertIsNone(repo.get_advice(users[1].id, "evaluation-account", first_advice))
                stocks = await asyncio.gather(*(reports.request(chat_id, "stocks", send, report_kind="screening", mode="technical") for chat_id in (7, 8)))
                self.assertTrue(all(run["state"] == "delivered" for run in stocks))
                screening = json.loads(stocks[0]["payload"])["screening"]
                self.assertTrue(screening["candidates"])
                self.assertIsNone(repo.get_screening(users[1].id, "evaluation-account", screening["id"]))

                class FailedLlm:
                    async def complete(self, *args):
                        raise LlmError("Провайдер недоступен")

                reports.advice.client = FailedLlm()
                failed_ai = await reports.request(8, "llm-unavailable", send, period=period)
                self.assertEqual(failed_ai["state"], "delivered")
                self.assertFalse(json.loads(failed_ai["payload"])["advice"]["result"]["available"])
                reports.advice.client = ContextLlm()
                rule = IntervalRule(7, "days", now[0], "UTC")
                schedule = repo.save_schedule(users[0].id, "evaluation-account", rule.to_dict(), now[0],
                                             {"kind": "rolling", "days": 30}, ["analysis", "advice", "screening"])
                repo.enqueue_schedule(schedule, rule.next_after(now[0]), now[0], now[0])
                pending = repo.ready_runs(now[0])[0]
                calls = 0

                async def limited(**kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise TelegramRetryAfter(method=SendMessage(chat_id=7, text="part"), message="rate", retry_after=30)
                    return SimpleNamespace(message_id=calls)

                prepared = await reports.execute_run(pending, limited)
                self.assertEqual(prepared["state"], "retry")
                payload = json.loads(prepared["payload"])
                self.assertIn("historical_analysis", payload)
                self.assertIn("screening", payload)
                parts = repo.get_parts(users[0].id, pending["id"])
                self.assertEqual(parts[0]["state"], "sent")
                await reports.aclose()
                repo.close()
                repo = JournalRepository(path)
                connections = Connections(repo, cipher, McpSettings())
                now[0] += timedelta(seconds=60)
                reports = PortfolioReportService(connections, clock=lambda: now[0])
                reports._prepare = AsyncMock(side_effect=AssertionError("saved report must be reused"))
                finished = await reports.execute_run(repo.get_run(users[0].id, pending["id"]), send)
                self.assertEqual(finished["state"], "delivered")
                self.assertEqual(finished["manifest"], prepared["manifest"])
                self.assertEqual(repo.get_parts(users[0].id, pending["id"])[0]["message_id"], parts[0]["message_id"])
                reports._prepare.assert_not_awaited()
            finally:
                if reports:
                    await reports.aclose()
                repo.close()


if __name__ == "__main__":
    unittest.main()
