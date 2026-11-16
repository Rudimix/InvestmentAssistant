"""Ликвидность, короткий горизонт, концентрация и неполные экспозиции."""

from dataclasses import replace
from decimal import Decimal as D
import unittest

import _support  # noqa: F401
from investment_journal.profile_metrics import assess_profile
from test_analysis_models import profile


class ProfileMetricTests(unittest.TestCase):
    def test_concentration_cash_and_currency_exposure(self):
        result = assess_profile(profile(), "rub", D(100000), D(5000), {"a": D(60000), "b": D(35000)},
                                {"a": "rub", "b": "usd"}, {"a": "finance", "b": "finance"})
        self.assertEqual(result["metrics"]["position:a:weight:excess"], D(35))
        self.assertEqual(result["metrics"]["sector:finance:weight"], D(95))
        self.assertEqual(result["metrics"]["exposure:currency:usd"], D(35000))
        self.assertEqual(result["metrics"]["profile:cash_shortfall"], D("5000.000000001"))
        self.assertEqual({v["profile_ref"] for v in result["violations"]}, {"minimum_cash", "max_position_pct", "max_sector_pct"})

    def test_short_horizon_missing_values_and_other_cash_currency(self):
        result = assess_profile(replace(profile(), horizon_months=6), "usd", None, D(100),
                                {"a": None, "b": D(100)}, {"b": "usd"})
        self.assertNotIn("position:b:weight", result["metrics"])
        self.assertNotIn("profile:cash_shortfall", result["metrics"])
        self.assertTrue(any("12 месяцев" in reason for reason in result["limitations"]))
        self.assertTrue(any("сектор" in reason for reason in result["limitations"]))
        self.assertFalse(result["violations"])

    def test_unknown_sector_is_not_assigned_and_shorts_use_absolute_exposure(self):
        result = assess_profile(profile(), "rub", D(100000), D(20000), {"short": D(-30000), "unknown": D(20000)},
                                {"short": "usd", "unknown": "rub"}, {"short": "finance"})
        self.assertEqual(result["metrics"]["position:short:weight"], D(30))
        self.assertNotIn("sector:unknown:weight", result["metrics"])
        self.assertTrue(any("сектор" in item for item in result["limitations"]))
        self.assertEqual(assess_profile(None, "rub", D(100), D(10), {}, {})["violations"], [])


if __name__ == "__main__":
    unittest.main()
