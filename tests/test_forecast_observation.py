"""Промежуточный горизонт, фактическая ошибка, диапазон и сплит."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

import _support  # noqa: F401
from investment_journal.forecast_observation import observe_forecast, refresh_forecast
from investment_journal.models import CorporateEvent, GrowthForecast, PriceObservation
from test_analysis_models import NOW
from investment_journal.forecast_training import months_later
from investment_journal.growth_forecast import ForecastService
from investment_journal.storage import JournalRepository


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.forecast = GrowthForecast("forecast", "TEST", NOW, months_later(NOW, 12), "model", "rub", D(100), D(120), D(90), D(140))
        self.at = self.forecast.target_at
        self.price = PriceObservation("TEST", self.at-timedelta(hours=1), self.at, D(130), "rub", self.at, "quote-v1")

    def test_incomplete_and_mature_horizon_changes_error_and_range(self):
        self.assertEqual(observe_forecast(self.forecast, self.at-timedelta(seconds=1))["status"], "intermediate")
        result = observe_forecast(self.forecast, self.at, self.price, corporate_complete=True, corporate_version="actions-v1")
        self.assertEqual(result["status"], "mature")
        self.assertEqual(result["actual_change_pct"], 30)
        self.assertEqual(result["error_pp"], 10)
        self.assertEqual(result["range_position"], "inside")
        self.assertEqual(observe_forecast(self.forecast, self.at, replace(self.price, close=D(160)), corporate_complete=True, corporate_version="v")["range_position"], "above")

    def test_split_keeps_original_share_unit_and_unconfirmed_actions_block_result(self):
        event = CorporateEvent("split", "TEST", NOW+timedelta(days=30), "split", "Issuer", "split-v1", True,
                               ratio=D(10), available_at=NOW+timedelta(days=20))
        price = replace(self.price, close=D(13))
        result = observe_forecast(self.forecast, self.at, price, [event], True, "actions-v1")
        self.assertEqual(result["actual_price"], 130)
        self.assertEqual(result["split_factor"], 10)
        self.assertEqual(observe_forecast(self.forecast, self.at, price, [replace(event, confirmed=False)], True, "actions-v1")["status"], "unavailable")
        self.assertEqual(observe_forecast(self.forecast, self.at, price)["status"], "unavailable")
        self.assertEqual(observe_forecast(self.forecast, self.at, replace(price, candle_end=self.at+timedelta(hours=1)), [], True, "v")["status"], "unavailable")


class ObservationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_persists_actual_source_and_isolates_owner(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        user = repo.get_or_create_user(7)
        forecast = GrowthForecast("forecast", "TEST", NOW, months_later(NOW, 12), "model", "rub", D(100), D(120), D(90), D(140))
        repo.save_forecast_model("model", NOW, NOW, {})
        repo.save_forecast(user.id, "account", forecast)
        market = AsyncMock()
        market.report_call.return_value = {"priceCurrency": "rub", "candles": [{"time": (forecast.target_at-timedelta(hours=1)).isoformat(),
            "close": {"value": "130"}, "isComplete": True}]}
        at = forecast.target_at+timedelta(hours=1)
        with TemporaryDirectory() as directory:
            proof = {"instrument_id": "TEST", "complete": True, "no_unit_changes": True, "source_url": "https://example.com/actions",
                     "version": "actions-v1", "observed_at": at.isoformat(), "from": NOW.date().isoformat(), "to": at.date().isoformat()}
            (Path(directory)/"price-bases.json").write_text(json.dumps([proof]), encoding="utf-8")
            service = ForecastService(repo, directory)
            result = await refresh_forecast(repo, market, service, user.id, "account", "forecast", at)
            self.assertEqual(result["status"], "mature")
            self.assertEqual(D(result["actual_change_pct"]), 30)
            self.assertEqual(repo.latest_forecast_observation(user.id, "account", "forecast"), result)
            self.assertIsNone(repo.latest_forecast_observation(user.id+1, "account", "forecast"))
            repeated = await refresh_forecast(repo, market, service, user.id, "account", "forecast", at)
            self.assertEqual(repeated, result)


if __name__ == "__main__":
    unittest.main()
