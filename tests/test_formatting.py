"""Тексты сообщений."""

import unittest
from datetime import datetime, timezone

import _support  # noqa: F401
from investment_journal.analytics import review_decision
from investment_journal.formatting import format_journal, format_portfolio, format_reviews, money
from investment_journal.models import Decision, Portfolio, Position

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


class FormattingTests(unittest.TestCase):
    def test_money(self):
        self.assertEqual(money(1234567.891), "1 234 567.89 ₽")
        self.assertEqual(money(5, "usd"), "5.00 $")
        self.assertEqual(money(5, "cny"), "5.00 CNY")

    def test_journal(self):
        self.assertIn("пуст", format_journal([]))
        d = Decision(3, 1, "sell", "BRZ6", "Нефть подешевеет", 80, NOW, exit_price=75, closed_at=NOW)
        self.assertEqual(format_journal([d]), "#3 Продажа BRZ6 по 80 (01.11.2026, закрыто по 75)\n«Нефть подешевеет»")

    def test_reviews_summary(self):
        self.assertIn("нет", format_reviews([]))
        d = Decision(1, 1, "buy", "SIZ6", "x", 100, NOW)
        text = format_reviews([review_decision(d, 110), review_decision(d, 90)])
        self.assertIn("100 → 110 (+10.00%)", text)
        self.assertTrue(text.endswith("Итого: подтверждается 1, не подтверждается 1, всего 2."))

    def test_portfolio(self):
        p = Portfolio(1000, 100, -5, [Position("SIZ6", "futures", 1, 90, 89, -1)])
        text = format_portfolio(p)
        self.assertIn("Стоимость портфеля: 1 000.00 ₽", text)
        self.assertIn("• SIZ6 (futures): 1 шт., средняя 90, текущая 89, результат -1.00 ₽", text)
        self.assertNotIn("•", format_portfolio(Portfolio(0, 0, 0)))


if __name__ == "__main__":
    unittest.main()
