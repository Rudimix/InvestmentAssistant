"""Проверки моделей инвестиционного журнала."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from investment_journal.models import User, DecisionDraft


class UserTests(unittest.TestCase):
    def test_user_stores_identifiers(self):
        user = User(id=1, chat_id=100)
        self.assertEqual(user.id, 1)
        self.assertEqual(user.chat_id, 100)


class DecisionDraftTests(unittest.TestCase):
    def test_draft_stores_owner_and_hypothesis(self):
        user = User(id=1, chat_id=100)
        draft = DecisionDraft(user.id, "buy", "SBER", "Ожидаю роста дивидендов")
        self.assertEqual(
            (draft.user_id, draft.kind, draft.ticker, draft.hypothesis),
            (1, "buy", "SBER", "Ожидаю роста дивидендов"),
        )

    def test_hypothesis_can_be_edited(self):
        draft = DecisionDraft(1, "hold", "SBER", "Исходная гипотеза")
        draft.hypothesis = "Обновлённая гипотеза"
        self.assertEqual(draft.hypothesis, "Обновлённая гипотеза")
        self.assertEqual((draft.user_id, draft.kind, draft.ticker), (1, "hold", "SBER"))
