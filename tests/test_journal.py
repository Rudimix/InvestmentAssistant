"""Хранилище и сценарии журнала."""

import tempfile
import unittest
from pathlib import Path

from _support import FakeToolClient
from investment_journal.journal import JournalError, JournalService
from investment_journal.market import MarketDataService
from investment_journal.mcp_client import ToolError
from investment_journal.models import DecisionDraft
from investment_journal.storage import JournalRepository

HYPOTHESIS = "Ожидаю укрепления рубля после решения ЦБ"


class RepositoryTests(unittest.TestCase):
    def test_user_is_created_once(self):
        repo = JournalRepository()
        first = repo.get_or_create_user(100)
        self.assertEqual(repo.get_or_create_user(100), first)
        self.assertNotEqual(repo.get_or_create_user(200).id, first.id)

    def test_decisions_persist_between_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "j.sqlite3"
            repo = JournalRepository(path)
            user = repo.get_or_create_user(1)
            saved = repo.add_decision(DecisionDraft(user.id, "buy", "siz6", HYPOTHESIS), 90000)
            repo.close()
            repo = JournalRepository(path)
            loaded = repo.get_decision(user.id, saved.id)
            repo.close()
        self.assertEqual(loaded, saved)
        self.assertEqual(loaded.ticker, "SIZ6")

    def test_close_and_filter(self):
        repo = JournalRepository()
        user = repo.get_or_create_user(1)
        a = repo.add_decision(DecisionDraft(user.id, "buy", "SIZ6", HYPOTHESIS), 100)
        b = repo.add_decision(DecisionDraft(user.id, "sell", "BRZ6", HYPOTHESIS), 80)
        repo.close_decision(a, 110)
        self.assertEqual([d.id for d in repo.list_decisions(user.id, open_only=True)], [b.id])
        closed = repo.get_decision(user.id, a.id)
        self.assertFalse(closed.is_open)
        self.assertEqual(closed.exit_price, 110)

    def test_other_users_decision_is_invisible(self):
        repo = JournalRepository()
        owner = repo.get_or_create_user(1)
        stranger = repo.get_or_create_user(2)
        d = repo.add_decision(DecisionDraft(owner.id, "hold", "SIZ6", HYPOTHESIS), 100)
        self.assertIsNone(repo.get_decision(stranger.id, d.id))


class JournalServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tools = FakeToolClient()
        self.journal = JournalService(JournalRepository(), MarketDataService(self.tools))
        self.user = self.journal.user(42)

    async def test_record_uses_current_price(self):
        d = await self.journal.record(DecisionDraft(self.user.id, "buy", "SiZ6", HYPOTHESIS))
        self.assertEqual((d.ticker, d.entry_price), ("SIZ6", 90000.0))

    async def test_validation(self):
        bad = [
            DecisionDraft(self.user.id, "short", "SIZ6", HYPOTHESIS),
            DecisionDraft(self.user.id, "buy", "SI-Z6", HYPOTHESIS),
            DecisionDraft(self.user.id, "buy", "SIZ6", "коротко"),
        ]
        for draft in bad:
            with self.subTest(draft=draft), self.assertRaises(JournalError):
                await self.journal.record(draft)
        self.assertEqual(self.tools.calls, [])

    async def test_unknown_ticker_is_not_saved(self):
        with self.assertRaises(ToolError):
            await self.journal.record(DecisionDraft(self.user.id, "buy", "SBER", HYPOTHESIS))
        self.assertEqual(self.journal.repo.list_decisions(self.user.id), [])

    async def test_review_fetches_each_ticker_once(self):
        for _ in range(3):
            await self.journal.record(DecisionDraft(self.user.id, "buy", "SIZ6", HYPOTHESIS))
        self.tools.prices["SIZ6"] = 99000.0
        self.tools.calls.clear()
        reviews = await self.journal.review(self.user)
        self.assertEqual([r.verdict for r in reviews], ["confirmed"] * 3)
        self.assertEqual(len(self.tools.calls), 1)

    async def test_review_all_uses_exit_price_for_closed(self):
        d = await self.journal.record(DecisionDraft(self.user.id, "sell", "BRZ6", HYPOTHESIS))
        self.tools.prices["BRZ6"] = 70.0
        await self.journal.close(self.user, d.id)
        self.tools.prices["BRZ6"] = 100.0
        [review] = await self.journal.review(self.user, open_only=False)
        self.assertEqual((review.price, review.verdict), (70.0, "confirmed"))

    async def test_close_errors(self):
        with self.assertRaisesRegex(JournalError, "не найдено"):
            await self.journal.close(self.user, 999)
        d = await self.journal.record(DecisionDraft(self.user.id, "hold", "SIZ6", HYPOTHESIS))
        await self.journal.close(self.user, d.id)
        with self.assertRaisesRegex(JournalError, "уже закрыто"):
            await self.journal.close(self.user, d.id)


if __name__ == "__main__":
    unittest.main()
