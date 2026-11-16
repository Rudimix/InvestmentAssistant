"""Цена, календарный горизонт, сценарные границы и ограничения модели."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from hashlib import sha256
import math
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from dataclasses import replace

import _support  # noqa: F401
from investment_journal.forecast_training import FEATURE_SETS
from investment_journal.growth_forecast import predict_growth
from investment_journal.growth_forecast import ForecastService
from investment_journal.forecast_training import save_artifact
from investment_journal.stock_fundamentals import FundamentalAnalysis
from investment_journal.stock_technicals import technical_analysis
from investment_journal.storage import JournalRepository
from investment_journal.models import data_json
from investment_journal.stock_formatting import candidate_lines
import test_stock_technicals as technical_fixtures
from test_analysis_models import NOW


AS_OF = datetime(2024, 2, 29, 12, tzinfo=timezone.utc)


def artifact(as_of=AS_OF, **changes):
    names = list(FEATURE_SETS["technical-v1"])
    body = {"schema_version": 1, "feature_set": "technical-v1", "feature_names": names, "usage": "analysis",
            "created_at": (as_of-timedelta(days=1)).isoformat(), "trained_until": (as_of-timedelta(days=2)).isoformat(),
            "coef": [0.0]*len(names), "intercept": math.log(1.1), "scaler_mean": [0.0]*len(names), "scaler_scale": [1.0]*len(names),
            "maximum_age_days": 180, "error_quantiles": {"0.1": -0.2, "0.9": 0.3}, "limitations": [],
            "methodology": "unit-test", "validation": {"count": 20, "mae_log": 0.2, "bias_log": 0.0, "persistence_mae_log": 0.3}}
    body.update(changes)
    return {**body, "version": sha256(data_json(body).encode()).hexdigest()}


class GrowthForecastTests(unittest.TestCase):
    def predict(self, model=None, **changes):
        names = FEATURE_SETS["technical-v1"]
        arguments = {"artifact": model or artifact(), "instrument_id": "TEST", "as_of": AS_OF, "price": D(100), "currency": "RUB",
                     "features": {name: D(1) for name in names}, "available_at": {name: AS_OF for name in names},
                     "versions": {name: "source-v1" for name in names}, "corporate_action_version": "actions-v1", "corporate_actions_complete": True}
        arguments.update(changes)
        return predict_growth(**arguments)

    def test_leap_day_positive_prices_units_and_empirical_errors(self):
        result = self.predict()
        self.assertEqual(result.target_at, datetime(2025, 2, 28, 12, tzinfo=timezone.utc))
        self.assertEqual(result.currency, "rub")
        self.assertAlmostEqual(float(result.base_price), 110)
        self.assertAlmostEqual(float(result.low_price), 110*math.exp(-0.2))
        self.assertAlmostEqual(float(result.high_price), 110*math.exp(0.3))
        self.assertIn("Вероятностное покрытие диапазона не калибровано", result.limitations)
        self.assertEqual(result.id, self.predict().id)
        with self.assertRaises(ValueError):
            replace(result, target_at=result.target_at+timedelta(days=1))
        with self.assertRaises(ValueError):
            replace(result, base_price=result.high_price+1)

    def test_future_stale_synthetic_missing_and_corporate_actions(self):
        for model in (artifact(created_at=(AS_OF+timedelta(seconds=1)).isoformat()),
                      artifact(trained_until=(AS_OF-timedelta(days=181)).isoformat()), artifact(usage="verification_only")):
            with self.assertRaises(ValueError):
                self.predict(model)
        with self.assertRaises(ValueError):
            self.predict(corporate_actions_complete=False)
        with self.assertRaises(ValueError):
            self.predict(available_at={})
        with self.assertRaises(ValueError):
            self.predict(features={})

    def test_invalid_zero_overflow_underflow_and_nan(self):
        for price in (D(0), D(-1), D("Infinity"), D("NaN")):
            with self.assertRaises(ValueError):
                self.predict(price=price)
        for intercept in (1e9, -1e9):
            with self.assertRaises(ValueError):
                self.predict(artifact(intercept=intercept))

    def test_service_artifact_history_missing_stale_short_and_price_basis(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        user = repo.get_or_create_user(7)
        entry = technical_fixtures.TechnicalTests().entry()
        entry.price, entry.price_at = D(499), NOW-timedelta(days=3)
        entry.source_versions["daily_series"] = "series-v1"
        technical = technical_analysis(entry, NOW)
        fundamental = FundamentalAnalysis(entry.instrument_id, entry.asset_id, entry.sector, "bank")
        with TemporaryDirectory() as directory:
            service = ForecastService(repo, directory)

            def forecast():
                return service.forecast(user.id, "account", entry, fundamental, technical, "technical", NOW, [])

            self.assertFalse(forecast()["available"])
            save_artifact(directory, artifact(NOW))
            self.assertFalse(forecast()["available"])
            basis = {"instrument_id": entry.instrument_id, "complete": True, "no_unit_changes": True,
                     "version": "basis-v1", "source_url": "https://example.com/issuer/actions", "observed_at": NOW.isoformat(),
                     "from": entry.series[0].day, "to": entry.series[-1].day}
            (Path(directory)/"price-bases.json").write_text(json.dumps([basis]), encoding="utf-8")
            first = forecast()
            self.assertTrue(first["available"])
            self.assertEqual(first["forecast"], repo.get_forecast(user.id, "account", first["forecast"].id))
            self.assertIsNone(repo.get_forecast(user.id+1, "account", first["forecast"].id))
            self.assertEqual(first["forecast"], forecast()["forecast"])
            candidate = json.loads(data_json({"entry": entry, "technical": technical, "fundamental": fundamental,
                "score": None, "forecast": first, "suitability": {"status": "conditional", "reasons": [], "metrics": {}}}))
            text = "\n".join(candidate_lines(candidate, "technical"))
            self.assertIn("MAE", text)
            self.assertIn(first["forecast"].model_version, text)
            self.assertIn("Неблагоприятный сценарий", text)
            technical.reasons.append("Недостаточная история")
            self.assertFalse(forecast()["available"])


if __name__ == "__main__":
    unittest.main()
