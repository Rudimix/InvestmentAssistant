"""Денежные варианты опираются на профиль, ограничения и подтверждённые расходы."""

from dataclasses import replace
from decimal import Decimal as D
import unittest

import _support  # noqa: F401
from investment_journal.portfolio_advice import check_scenario, improvement_scenarios
from investment_journal.profile_metrics import assess_profile
from test_analysis_models import NOW, profile


class PortfolioAdviceTests(unittest.TestCase):
    def setUp(self):
        self.profile = profile()
        self.assessment = assess_profile(self.profile, "rub", D(100000), D(5000), {"a": D(60000), "b": D(35000)},
                                         {"a": "rub", "b": "rub"}, {"a": "finance", "b": "industry"})

    def test_cash_credit_reduces_concentration_and_preserves_minimum(self):
        items = improvement_scenarios(self.profile, self.assessment, "account", NOW, "snapshot")
        credit = next(item for item in items if item.kind == "net_cash_credit")
        self.assertTrue(credit.admissible)
        self.assertEqual(credit.effects["net_credit"], D(140000))
        self.assertEqual(credit.effects["total_after"], D(240000))
        self.assertEqual(credit.effects["cash_after"], D(145000))
        self.assertIn("max_position_pct", credit.profile_refs)
        unknown = next(item for item in items if item.kind.startswith("reduce_position:"))
        self.assertNotIn("estimated_cost", unknown.effects)
        self.assertFalse(unknown.admissible)
        self.assertTrue(any("Расходы неизвестны" in text for text in unknown.conditions))

    def test_known_costs_change_denominator_and_cash_effect(self):
        items = improvement_scenarios(self.profile, self.assessment, "account", NOW, "snapshot", D("0.01"))
        sale = next(item for item in items if item.kind == "reduce_position:a")
        self.assertTrue(sale.admissible)
        self.assertAlmostEqual(sale.effects["weight_after"], D(25), places=20)
        self.assertEqual(sale.effects["estimated_cost"], sale.effects["sale_amount"]*D("0.01"))
        self.assertEqual(sale.effects["total_after"], D(100000)-sale.effects["estimated_cost"])
        repeated = improvement_scenarios(self.profile, self.assessment, "account", NOW, "snapshot", D("0.01"))
        self.assertEqual(items, repeated)

    def test_short_horizon_and_incomplete_values(self):
        item = improvement_scenarios(self.profile, self.assessment, "account", NOW, "snapshot")[0]
        short = replace(self.profile, horizon_months=6)
        incompatible = check_scenario(replace(item, required_horizon_months=12), short)
        self.assertFalse(incompatible.admissible)
        self.assertTrue(item.admissible)
        incomplete = assess_profile(self.profile, "rub", None, D(5000), {"a": None}, {"a": "rub"})
        self.assertEqual(improvement_scenarios(self.profile, incomplete, "account", NOW, "snapshot"), [])
        self.assertEqual(improvement_scenarios(None, self.assessment, "account", NOW, "snapshot"), [])
        with self.assertRaises(ValueError):
            improvement_scenarios(self.profile, self.assessment, "account", NOW, "snapshot", D(1))


if __name__ == "__main__":
    unittest.main()
