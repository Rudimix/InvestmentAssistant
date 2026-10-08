"""Проверки моделей инвестиционного журнала."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from investment_journal.models import User


class UserTests(unittest.TestCase):
    def test_user_stores_identifiers(self):
        user = User(id=1, chat_id=100)
        self.assertEqual(user.id, 1)
        self.assertEqual(user.chat_id, 100)
