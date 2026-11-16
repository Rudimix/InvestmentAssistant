"""Новые аналитические модели сохраняют точность, область и неизвестные значения."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

import _support  # noqa: F401
from investment_journal.models import (
    AdviceProposal, AnalysisPeriod, DataCoverage, EvidenceChunk, EvidenceDocument,
    FinancialFact, GrowthForecast, HistoricalPosition, HistoricalState, InvestorProfile, data_json,
)

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


def profile():
    return InvestorProfile(1, 1, NOW, "Долгосрочный рост", 36, "medium", Decimal(20),
                           Decimal("10000.000000001"), Decimal(25), Decimal(40))


class AnalysisModelTests(unittest.TestCase):
    def test_all_records_round_trip_with_versions_and_provenance(self):
        position = HistoricalPosition("uid", "TEST", "share", "rub", None,
                                      price=Decimal("100.000000001"), price_at=NOW-timedelta(days=1))
        state = HistoricalState("state", 1, "account", NOW-timedelta(days=1), NOW, "rub",
                                [position], {"rub": Decimal(0), "usd": None},
                                coverage=DataCoverage(positions="partial", reasons=["Missing quantity"],
                                                      source_versions={"operations": "revision-1"}), anchor_id="anchor")
        records = [
            position, state, profile(),
            AnalysisPeriod("2026-10-01", "2026-10-31", "Europe/Moscow",
                           NOW-timedelta(days=46), NOW-timedelta(days=15), NOW),
            EvidenceDocument("doc", "v1", "issuer", "https://example.com/report", "Report", "Content", NOW,
                             available_at=NOW, instrument_ids=["uid"], kind="report_content"),
            EvidenceChunk("chunk", "doc", "v1", "Content", page=2),
            FinancialFact("fact", "uid", "revenue", Decimal("123456789.123456789"), "rub", "rub",
                          "2025-01-01", "2025-12-31", "doc", "v1", "chunk", NOW, consolidated=True),
            AdviceProposal("advice", 1, "account", 1, NOW, "Review concentration", metric_ids=["weight"],
                           evidence_ids=["chunk"], risks=["Liquidity"], reconsider_when=["Profile changes"]),
            GrowthForecast("forecast", "uid", NOW, NOW+timedelta(days=365), "ridge-1", "rub", Decimal(100),
                           base_price=Decimal("110.000000001"), low_price=Decimal(90), high_price=Decimal(130)),
        ]
        for item in records:
            with self.subTest(model=type(item).__name__):
                restored = type(item).from_dict(json.loads(data_json(item)))
                self.assertEqual(restored, item)
                self.assertEqual(restored.schema_version, 1)
        restored = HistoricalState.from_dict(json.loads(data_json(state)))
        self.assertIsNone(restored.positions[0].quantity)
        self.assertIsNone(restored.cash_by_currency["usd"])
        self.assertEqual(restored.cash_by_currency["rub"], Decimal(0))
        self.assertIsNone(restored.total)

    def test_aware_times_normalize_and_naive_times_fail(self):
        raw = json.loads(data_json(profile()))
        raw["confirmed_at"] = "2026-11-16T03:00:00+03:00"
        self.assertEqual(InvestorProfile.from_dict(raw).confirmed_at, NOW)
        raw["confirmed_at"] = "2026-11-16T03:00:00"
        with self.assertRaises(ValueError):
            InvestorProfile.from_dict(raw)

    def test_unknown_versions_fields_and_incompatible_types_fail(self):
        raw = json.loads(data_json(profile()))
        for patch in ({"schema_version": 2}, {"extra": "unexpected"}, {"horizon_months": True},
                      {"horizon_months": "12"}, {"risk_level": "anything"}, {"user_id": 0}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                InvestorProfile.from_dict(raw | patch)

    def test_profile_percentages_cash_and_precision(self):
        raw = json.loads(data_json(profile()))
        self.assertEqual(InvestorProfile.from_dict(raw).minimum_cash, Decimal("10000.000000001"))
        for patch in ({"max_position_pct": "101"}, {"max_drawdown_pct": "0"},
                      {"minimum_cash": "-1"}, {"minimum_cash": "NaN"}, {"max_sector_pct": "Infinity"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                InvestorProfile.from_dict(raw | patch)

    def test_invalid_period_coverage_and_forecast_fail(self):
        with self.assertRaises(ValueError):
            AnalysisPeriod("2026-10-31", "2026-10-01", "UTC", NOW-timedelta(days=1), NOW, NOW)
        with self.assertRaises(ValueError):
            DataCoverage(prices="unknown")
        for price in ("0", "-1", "NaN", "Infinity"):
            with self.subTest(price=price), self.assertRaises(ValueError):
                GrowthForecast("f", "uid", NOW, NOW+timedelta(days=365), "model", "rub", Decimal(price))
        with self.assertRaises(ValueError):
            GrowthForecast("f", "uid", NOW, NOW+timedelta(days=365), "model", "rub", Decimal(100),
                           low_price=Decimal(120), high_price=Decimal(110))
