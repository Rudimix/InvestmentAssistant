"""Подтверждённые основания, отраслевые шаблоны и сопоставимость отчётности."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import unittest
from unittest.mock import AsyncMock

import _support  # noqa: F401
from investment_journal.models import BrokerMetadataRecord, FinancialFact, StockUniverseEntry
from investment_journal.stock_fundamentals import compare_industry, fundamental_analysis, load_capitalizations, issuer_capitalizations
from investment_journal.storage import JournalRepository
from test_analysis_models import NOW


def fact(metric, value, year=2025, **kwargs):
    stock = metric in {"equity", "total_debt", "cash"}
    return FinancialFact(metric+str(year), "TEST", metric, D(value), "money", "rub",
        f"{year}-12-31" if stock else f"{year}-01-01", f"{year}-12-31", "report", "v1", "chunk", NOW,
        "IFRS", True, **kwargs)


class FundamentalTests(unittest.TestCase):
    def setUp(self):
        self.entry = StockUniverseEntry("TEST", "TEST", "TQBR", "asset", sector="financial")
        self.cap = BrokerMetadataRecord("invest_get_asset_fundamentals", "asset", "cap-v1", NOW,
                                       {"currency": "RUB", "marketCapitalization": "1000", "capitalization_scope": "issuer"})
        self.facts = [fact("net_profit", 100), fact("equity", 500), fact("revenue", 300), fact("revenue", 200, 2024)]

    def test_bank_template_and_growth(self):
        analysis = fundamental_analysis(self.entry, self.facts, self.cap, NOW)
        self.assertEqual(analysis.metrics["pe"].value, 10)
        self.assertEqual(analysis.metrics["pb"].value, 2)
        self.assertEqual(analysis.metrics["revenue_growth"].value, 50)
        self.assertNotIn("ev_ebitda", analysis.metrics)
        self.assertEqual(analysis.metrics["pe"].capitalization_version, "cap-v1")
        self.assertEqual(analysis.metrics["pe"].fact_ids, ["net_profit2025"])

    def test_loss_future_currency_and_ambiguous_standard(self):
        loss = fundamental_analysis(self.entry, [fact("net_profit", -100), fact("equity", 0)], self.cap, NOW)
        self.assertNotIn("pe", loss.metrics)
        self.assertNotIn("pb", loss.metrics)
        future = [replace(item, available_at=NOW+timedelta(seconds=1)) for item in self.facts]
        self.assertFalse(fundamental_analysis(self.entry, future, self.cap, NOW).metrics)
        foreign = [replace(item, currency="usd") for item in self.facts]
        self.assertNotIn("pe", fundamental_analysis(self.entry, foreign, self.cap, NOW).metrics)
        mixed = self.facts+[replace(self.facts[0], id="ras", standard="RAS")]
        self.assertNotIn("pe", fundamental_analysis(self.entry, mixed, self.cap, NOW).metrics)
        self.assertNotIn("pe", fundamental_analysis(self.entry, self.facts, replace(self.cap, observed_at=NOW+timedelta(seconds=1)), NOW).metrics)

    def test_corporate_ev_requires_matching_period_debt_and_cash(self):
        entry = replace(self.entry, sector="energy")
        facts = self.facts+[fact("ebitda", 200), fact("total_debt", 400), fact("cash", 100)]
        result = fundamental_analysis(entry, facts, self.cap, NOW)
        self.assertEqual(result.metrics["ev_ebitda"].value, D("6.5"))
        self.assertEqual(result.metrics["net_debt_to_ebitda"].value, D("1.5"))
        mismatched = [replace(item, period_end="2025-09-30") if item.metric == "total_debt" else item for item in facts]
        self.assertNotIn("ev_ebitda", fundamental_analysis(entry, mismatched, self.cap, NOW).metrics)
        self.assertNotIn("ev_ebitda", fundamental_analysis(entry, self.facts, self.cap, NOW).metrics)

    def test_sector_comparison_excludes_duplicates_periods_and_currencies(self):
        analyses = []
        for index, cap in enumerate((1000, 2000, 3000)):
            entry = replace(self.entry, instrument_id=str(index), asset_id=str(index))
            data = [replace(item, instrument_id=str(index)) for item in self.facts]
            capital = replace(self.cap, identifier=str(index), payload={"currency": "rub", "marketCapitalization": cap, "capitalization_scope": "issuer"})
            analyses.append(fundamental_analysis(entry, data, capital, NOW))
        compare_industry(analyses)
        self.assertEqual(analyses[0].score, 100)
        self.assertEqual(analyses[0].comparisons["pe"]["median"], 25)
        self.assertEqual(analyses[0].comparisons["pe"]["discount_percent"], 60)
        self.assertTrue(analyses[0].comparisons["pe"]["potential_undervaluation"])
        duplicate = replace(analyses[1], instrument_id="preferred")
        compare_industry(analyses+[duplicate])
        self.assertEqual(len(analyses[0].comparisons["pe"]["peer_ids"]), 2)
        analyses[2].metrics["pe"] = replace(analyses[2].metrics["pe"], currency="usd")
        compare_industry(analyses)
        self.assertIsNone(analyses[0].score)
        self.assertNotIn("pe", analyses[0].comparisons)

    def test_share_classes_are_aggregated_and_source_scope_is_explicit(self):
        ordinary = replace(self.entry, issuer_id="issuer")
        preferred = replace(ordinary, instrument_id="TESTP", ticker="TESTP", asset_id="preferred")
        ordinary.in_scope = preferred.in_scope = True
        values = {"asset": self.cap, "preferred": replace(self.cap, identifier="preferred", version="preferred-cap",
                    payload={"currency": "rub", "marketCapitalization": 100})}
        capitals = issuer_capitalizations([ordinary, preferred], values)
        self.assertEqual(capitals["asset"].payload["marketCapitalization"], 1100)
        result = fundamental_analysis(ordinary, self.facts, capitals["asset"], NOW)
        self.assertEqual(result.metrics["pe"].value, 11)
        self.assertEqual(result.asset_id, "issuer")
        self.assertEqual(result.metrics["pe"].capitalization_scope, "catalog_classes")
        raw = replace(self.cap, payload={**self.cap.payload, "capitalization_scope": "asset"})
        self.assertNotIn("pe", fundamental_analysis(ordinary, self.facts, raw, NOW).metrics)


class CapitalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_batches_cache_and_missing_zero(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        market = AsyncMock()
        now = [NOW]

        async def respond(tool, args):
            self.assertEqual(tool, "invest_get_asset_fundamentals")
            self.assertLessEqual(len(args["assets"]), 100)
            return {"fundamentals": [{"assetUid": asset, "currency": "RUB", "marketCapitalization": 0}
                                     for asset in args["assets"]]}

        market.report_call.side_effect = respond
        records, quality = await load_capitalizations(repo, market, [str(index) for index in range(101)], lambda: now[0])
        self.assertEqual(len(records), 101)
        self.assertEqual(market.report_call.await_count, 2)
        self.assertEqual(quality, [])
        first = records["0"]
        cached, _ = await load_capitalizations(repo, market, ["0"], lambda: now[0])
        self.assertEqual(cached["0"].version, first.version)
        self.assertEqual(market.report_call.await_count, 2)
        now[0] += timedelta(days=1)
        refreshed, _ = await load_capitalizations(repo, market, ["0"], lambda: now[0])
        self.assertNotEqual(refreshed["0"].version, first.version)
        entry = StockUniverseEntry("TEST", "TEST", "TQBR", "0", sector="financial")
        result = fundamental_analysis(entry, [fact("net_profit", 100)], refreshed["0"], now[0])
        self.assertNotIn("pe", result.metrics)


if __name__ == "__main__":
    unittest.main()
