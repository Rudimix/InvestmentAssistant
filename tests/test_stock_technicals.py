"""Детерминированные индикаторы, календарный моментум и ранги блоков."""

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal as D
import unittest

import _support  # noqa: F401
from investment_journal.models import DailyObservation, StockUniverseEntry
from investment_journal.stock_fundamentals import FundamentalAnalysis
from investment_journal.stock_technicals import combined_rank, rank_technicals, technical_analysis
from test_analysis_models import NOW


class TechnicalTests(unittest.TestCase):
    def entry(self, size=400, growing=True):
        rows = []
        for index in range(size):
            day = (NOW-timedelta(days=size-index+2)).date()
            price = D(index+100) if growing else D(100)
            rows.append(DailyObservation(day.isoformat(), datetime.combine(day+timedelta(days=2), time(), timezone.utc), price, D(100), "rub"))
        return StockUniverseEntry("TEST", "TEST", "TQBR", "asset", sector="financial", currency="rub", series=rows)

    def test_sma_wilder_rsi_momentum_and_volume(self):
        entry = self.entry()
        result = technical_analysis(entry, NOW)
        self.assertEqual(result.features["sma50"], D("474.5"))
        self.assertEqual(result.features["sma200"], D("399.5"))
        self.assertEqual(result.features["rsi14"], 100)
        self.assertEqual(result.features["relative_volume"], 1)
        self.assertEqual(result.features["drawdown"], 0)
        self.assertGreater(result.features["momentum_12m"], 0)
        self.assertEqual(result.reasons, [])
        self.assertEqual(technical_analysis(self.entry(growing=False), NOW).features["rsi14"], 50)

    def test_short_series_future_and_duplicate(self):
        entry = self.entry(10)
        result = technical_analysis(entry, NOW)
        self.assertIsNone(result.features["rsi14"])
        self.assertIsNone(result.features["sma200"])
        self.assertTrue(result.reasons)
        entry.series.append(replace(entry.series[-1], close=D(99999), available_at=NOW+timedelta(days=1)))
        self.assertEqual(result.features, technical_analysis(entry, NOW).features)
        entry.series.append(entry.series[0])
        self.assertTrue(all(value is None for value in technical_analysis(entry, NOW).features.values()))

    def test_sector_percentiles_and_missing_combined_block(self):
        base = technical_analysis(self.entry(), NOW)
        analyses = [replace(base, instrument_id=str(index), asset_id=str(index), ranks={},
                            features={key: value+D(index) for key, value in base.features.items()}) for index in range(3)]
        rank_technicals(analyses)
        self.assertEqual(analyses[0].score, 40)
        self.assertEqual(analyses[2].score, 60)
        fundamental = FundamentalAnalysis("0", "0", "financial", "bank", score=D(90))
        combined = combined_rank(fundamental, analyses[0])
        self.assertEqual(combined["score"], 65)
        self.assertEqual(combined["agreement"], "conflict")
        fundamental.score = None
        self.assertIsNone(combined_rank(fundamental, analyses[0])["score"])
        self.assertEqual(combined_rank(fundamental, analyses[0])["status"], "partial")
        analyses[2].sector = "energy"
        rank_technicals(analyses)
        self.assertIsNone(analyses[0].score)


if __name__ == "__main__":
    unittest.main()
