"""Оценка гипотез и статистика свечей."""

import unittest
from datetime import datetime, timezone

import _support  # noqa: F401
from investment_journal.analytics import candle_stats, pct_change, review_decision
from investment_journal.models import Candle, Decision

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def decision(kind: str, closed: bool = False) -> Decision:
    return Decision(1, 1, kind, "SIZ6", "гипотеза", 100.0, NOW, closed_at=NOW if closed else None)


class PctChangeTests(unittest.TestCase):
    def test_pct_change(self):
        self.assertAlmostEqual(pct_change(100, 110), 10.0)

    def test_zero_base(self):
        with self.assertRaises(ValueError):
            pct_change(0, 1)


class ReviewDecisionTests(unittest.TestCase):
    CASES = [
        # kind, closed, price, verdict
        ("buy", False, 105, "confirmed"),
        ("buy", False, 95, "refuted"),
        ("buy", False, 101, "pending"),
        ("buy", True, 101, "confirmed"),
        ("buy", True, 99, "refuted"),
        ("sell", False, 95, "confirmed"),
        ("sell", False, 105, "refuted"),
        ("sell", False, 99, "pending"),
        ("sell", True, 99, "confirmed"),
        ("sell", True, 101, "refuted"),
        ("hold", False, 101, "pending"),
        ("hold", True, 101, "confirmed"),
        ("hold", False, 110, "refuted"),
        ("hold", True, 90, "refuted"),
    ]

    def test_verdicts(self):
        for kind, closed, price, verdict in self.CASES:
            with self.subTest(kind=kind, closed=closed, price=price):
                self.assertEqual(review_decision(decision(kind, closed), price).verdict, verdict)

    def test_result_sign_follows_direction(self):
        self.assertAlmostEqual(review_decision(decision("buy"), 110).result_pct, 10)
        self.assertAlmostEqual(review_decision(decision("sell"), 110).result_pct, -10)
        self.assertAlmostEqual(review_decision(decision("hold"), 101).result_pct, 2)

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            review_decision(decision("short"), 100)


class CandleStatsTests(unittest.TestCase):
    @staticmethod
    def candles(closes):
        return [Candle(str(i), c, c + 1, c - 1, c, 1) for i, c in enumerate(closes)]

    def test_uptrend(self):
        s = candle_stats(self.candles([100, 102, 101, 105, 103, 108]))
        self.assertAlmostEqual(s.change_pct, 8.0)
        self.assertEqual((s.high, s.low), (109, 99))
        self.assertAlmostEqual(s.sma, 103.1666, places=3)
        self.assertAlmostEqual(s.max_drawdown_pct, (105 - 103) / 105 * 100)
        self.assertEqual(s.trend, "up")
        self.assertGreater(s.volatility_pct, 0)

    def test_downtrend_and_flat(self):
        self.assertEqual(candle_stats(self.candles([110, 108, 104, 100])).trend, "down")
        self.assertEqual(candle_stats(self.candles([100, 101, 100, 100.5])).trend, "flat")

    def test_single_candle(self):
        s = candle_stats(self.candles([100]))
        self.assertEqual((s.volatility_pct, s.max_drawdown_pct, s.trend), (0.0, 0.0, "flat"))

    def test_empty(self):
        with self.assertRaises(ValueError):
            candle_stats([])


if __name__ == "__main__":
    unittest.main()
