"""Изоляция и неизменяемость новых аналитических записей и их доказательств."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

import _support  # noqa: F401
from investment_journal.models import (
    AdviceProposal, DataCoverage, EvidenceChunk, EvidenceDocument, FinancialFact,
    GrowthForecast, HistoricalState,
)
from investment_journal.storage import JournalRepository
from test_analysis_models import NOW, profile


class AnalysisStorageTests(unittest.TestCase):
    def setUp(self):
        self.repo = JournalRepository()
        self.addCleanup(self.repo.close)
        self.repo.get_or_create_user(7)
        self.repo.get_or_create_user(8)

    def evidence(self):
        document = EvidenceDocument("d", "v1", "issuer", "https://example.com/report", "Report", "Profit 100", NOW,
                                    available_at=NOW, instrument_ids=["uid"])
        chunk = EvidenceChunk("c", "d", "v1", "Profit 100")
        self.repo.save_document(document)
        self.repo.save_evidence_chunk(chunk, "profit 100")
        return document, chunk

    def test_profiles_are_scoped_revisioned_and_immutable(self):
        item = profile()
        self.repo.save_profile(1, item)
        self.repo.save_profile(1, item)
        self.assertEqual(self.repo.get_profile(1), item)
        self.assertIsNone(self.repo.get_profile(2))
        for owner, changed in ((2, item), (1, replace(item, goal="Changed")), (1, replace(item, revision=4))):
            with self.subTest(owner=owner, revision=changed.revision), self.assertRaises(ValueError):
                self.repo.save_profile(owner, changed)
        second = replace(item, revision=2, goal="Liquidity")
        self.repo.save_profile(1, second)
        self.assertEqual(self.repo.get_profile(1), second)
        self.assertEqual(self.repo.get_profile(1, 1), item)

    def test_historical_state_and_inputs_are_atomic_scoped_and_immutable(self):
        item = HistoricalState("state", 1, "account", NOW-timedelta(days=1), NOW, "rub",
                               total=Decimal("100.000000001"), coverage=DataCoverage(source_versions={"ops": "v1"}))
        inputs = {"ops": {"version": "v1", "payload": {"id": "operation"}}}
        self.repo.save_historical_state(1, "account", item, inputs)
        self.repo.save_historical_state(1, "account", item, inputs)
        self.assertEqual(self.repo.get_historical_state(1, "account", item.id), item)
        self.assertEqual(self.repo.get_historical_inputs(1, "account", item.id), inputs)
        for user_id, account in ((2, "account"), (1, "other")):
            with self.subTest(owner=user_id, account=account):
                self.assertIsNone(self.repo.get_historical_state(user_id, account, item.id))
                self.assertEqual(self.repo.get_historical_inputs(user_id, account, item.id), {})
                with self.assertRaises(ValueError):
                    self.repo.save_historical_state(user_id, account, item, inputs)
        with self.assertRaises(ValueError):
            self.repo.save_historical_state(1, "account", item, {"ops": {"version": "v1", "payload": {"changed": True}}})
        self.assertEqual(self.repo.get_historical_inputs(1, "account", item.id), inputs)
        with self.assertRaises(ValueError):
            self.repo.save_historical_state(1, "account", replace(item, id="new"), {})
        self.assertIsNone(self.repo.get_historical_state(1, "account", "new"))

    def test_document_versions_chunk_links_and_financial_availability(self):
        document, chunk = self.evidence()
        self.repo.save_document(document)
        self.repo.save_evidence_chunk(chunk, "profit 100")
        self.assertEqual(self.repo._db.execute("SELECT count(*) FROM evidence_search").fetchone()[0], 1)
        with self.assertRaises(ValueError):
            self.repo.save_document(replace(document, content="Changed"))
        self.repo.save_document(replace(document, version="v2", content="Corrected",
                                        retrieved_at=NOW+timedelta(days=1), available_at=NOW+timedelta(days=1)))
        self.assertEqual(self.repo.get_document("d", "v1"), document)
        fact = FinancialFact("f", "uid", "profit", Decimal(100), "rub", "rub", "2025-01-01", "2025-12-31",
                             "d", "v1", "c", NOW)
        self.repo.save_financial_fact(fact)
        self.assertEqual(self.repo.get_financial_facts("uid", NOW), [fact])
        self.assertEqual(self.repo.get_financial_facts("uid", NOW+timedelta(days=1)), [])
        self.assertEqual(self.repo.get_financial_facts("uid", NOW-timedelta(seconds=1)), [])
        self.assertEqual(self.repo.get_financial_facts("other", NOW), [])
        for changed in (replace(fact, document_version="v2"), replace(fact, chunk_id="absent"),
                        replace(fact, available_at=NOW-timedelta(days=1))):
            with self.subTest(fact=changed), self.assertRaises(ValueError):
                self.repo.save_financial_fact(changed)

    def test_forecasts_and_advice_enforce_scope_versions_and_evidence(self):
        self.evidence()
        self.repo.save_profile(1, profile())
        self.repo.save_forecast_model("model", NOW-timedelta(days=1), NOW-timedelta(days=2), {"features": ["momentum"]})
        item = GrowthForecast("f", "uid", NOW, NOW+timedelta(days=365), "model", "rub", Decimal(100),
                              base_price=Decimal(110))
        self.repo.save_forecast(1, "account", item)
        self.assertEqual(self.repo.get_forecast(1, "account", "f"), item)
        self.assertIsNone(self.repo.get_forecast(2, "account", "f"))
        self.assertIsNone(self.repo.get_forecast(1, "other", "f"))
        with self.assertRaises(ValueError):
            self.repo.save_forecast(2, "account", item)
        with self.assertRaises(ValueError):
            self.repo.save_forecast(1, "account", replace(item, id="bad", as_of=NOW-timedelta(days=3)))
        advice = AdviceProposal("a", 1, "account", 1, NOW, "Review concentration", evidence_ids=["c"])
        self.repo.save_advice(1, "account", advice)
        self.assertEqual(self.repo.get_advice(1, "account", "a"), advice)
        self.assertIsNone(self.repo.get_advice(2, "account", "a"))
        for owner, account, changed in ((2, "account", advice), (1, "other", advice),
                                       (1, "account", replace(advice, id="bad", evidence_ids=["absent"]))):
            with self.subTest(owner=owner, account=account), self.assertRaises(ValueError):
                self.repo.save_advice(owner, account, changed)

    def test_analytical_versions_survive_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "journal.db"
            repo = JournalRepository(path)
            try:
                repo.get_or_create_user(7)
                repo.save_profile(1, profile())
                repo.save_historical_price("uid", NOW-timedelta(days=1), "mcp", "v1", NOW, {"price": "100.000000001"})
            finally:
                repo.close()
            repo = JournalRepository(path)
            try:
                self.assertEqual(repo.get_profile(1), profile())
                values = repo.get_historical_prices("uid", NOW-timedelta(days=1), NOW)
                self.assertEqual(values[0]["payload"]["price"], "100.000000001")
                self.assertEqual(repo.get_historical_prices("uid", NOW, NOW+timedelta(days=1)), [])
                with self.assertRaises(ValueError):
                    repo.save_historical_price("uid", NOW-timedelta(days=1), "mcp", "v1", NOW, {"price": "999"})
            finally:
                repo.close()
