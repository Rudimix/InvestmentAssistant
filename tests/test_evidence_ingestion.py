"""Повторы, исправления, ссылки отслеживания и объединение каналов."""

from dataclasses import replace
from datetime import timedelta
import unittest

import _support  # noqa: F401
from investment_journal.evidence_ingestion import canonical_url, ingest_document, ingest_event
from investment_journal.models import EvidenceDocument, InstrumentEvent
from investment_journal.storage import JournalRepository
from test_analysis_models import NOW


class EvidenceIngestionTests(unittest.TestCase):
    def setUp(self):
        self.repo = JournalRepository()
        self.addCleanup(self.repo.close)
        self.document = EvidenceDocument("source-id", "source-version", "Issuer", "https://example.com/report?utm_source=rss&id=2#table",
                                         "Отчёт", "Прибыль составила 100 млн руб.", NOW,
                                         published_at=NOW-timedelta(days=10), instrument_ids=["TEST"], kind="news")

    def test_canonical_url_preserves_meaningful_query_and_path(self):
        self.assertEqual(canonical_url("https://EXAMPLE.com:443/Report?id=2&utm_medium=mail&b=1#part"),
                         "https://example.com/Report?b=1&id=2")
        self.assertNotEqual(canonical_url("https://example.com/?id=1"), canonical_url("https://example.com/?id=2"))
        with self.assertRaises(ValueError):
            canonical_url("https://user:password@example.com/report")

    def test_repeat_fetch_preserves_version_and_initial_retrieval(self):
        first = ingest_document(self.repo, self.document)
        second = ingest_document(self.repo, replace(self.document, retrieved_at=NOW+timedelta(hours=1)))
        self.assertEqual(first, second)
        self.assertEqual(len(self.repo.document_versions(first.id)), 1)
        self.assertEqual(len(self.repo.evidence_attributions(first.id, first.version)), 2)
        self.assertEqual(first.available_at, NOW)
        self.assertEqual(first.published_at, self.document.published_at)

    def test_corrected_content_keeps_prior_version_and_new_availability(self):
        first = ingest_document(self.repo, self.document)
        correction = replace(self.document, retrieved_at=NOW+timedelta(days=1), content="Прибыль составила 90 млн руб.")
        second = ingest_document(self.repo, correction)
        self.assertEqual(first.id, second.id)
        self.assertNotEqual(first.version, second.version)
        self.assertEqual(second.available_at, correction.retrieved_at)
        self.assertEqual(self.repo.get_document(first.id, first.version).content, self.document.content)
        self.assertEqual(len(self.repo.document_versions(first.id)), 2)

    def test_same_material_across_mcp_and_open_source_retains_original_attributions(self):
        first = ingest_document(self.repo, self.document)
        event = InstrumentEvent("mcp-news", "news", ["TEST"], self.document.title, "MCP", "https://mirror.example.org/report",
                                published_at=self.document.published_at, content=self.document.content)
        second = ingest_event(self.repo, event, NOW)
        self.assertEqual((first.id, first.version), (second.id, second.version))
        rows = self.repo.evidence_attributions(first.id, first.version)
        self.assertEqual({row["source"] for row in rows}, {"Issuer", "MCP"})
        self.assertEqual({row["original_url"] for row in rows}, {self.document.url, event.url})
        self.assertIsNone(ingest_event(self.repo, replace(event, kind="report_calendar"), NOW))
        self.assertIsNone(ingest_event(self.repo, replace(event, url=None), NOW))

    def test_content_reversion_is_a_new_dated_revision(self):
        first = ingest_document(self.repo, self.document)
        ingest_document(self.repo, replace(self.document, content="Исправленный текст", retrieved_at=NOW+timedelta(days=1)))
        restored = ingest_document(self.repo, replace(self.document, retrieved_at=NOW+timedelta(days=2)))
        self.assertNotEqual(first.version, restored.version)
        self.assertEqual(restored.content, first.content)
        self.assertEqual(self.repo.available_documents(["TEST"], NOW)[0].version, first.version)
        self.assertEqual(self.repo.available_documents(["TEST"], NOW+timedelta(days=2))[0].version, restored.version)
        repeat = ingest_document(self.repo, replace(self.document, retrieved_at=NOW+timedelta(days=3)))
        self.assertEqual(repeat.version, restored.version)
        self.assertEqual(len(self.repo.document_versions(first.id)), 3)

    def test_late_binding_does_not_retroactively_associate_cached_body(self):
        document = ingest_document(self.repo, self.document, binding_confirmed_at=NOW+timedelta(days=2))
        self.assertEqual(document.instrument_ids, [])
        self.assertEqual(self.repo.available_documents(["TEST"], NOW+timedelta(days=1)), [])
        self.assertEqual(self.repo.available_documents(["TEST"], NOW+timedelta(days=2))[0].id, document.id)

    def test_mcp_alias_binding_has_its_own_confirmation_time(self):
        event = InstrumentEvent("news", "news", ["TEST"], "Отчёт", "MCP", self.document.url,
                                content=self.document.content, retrieved_at=NOW)
        stored = ingest_event(self.repo, event, NOW+timedelta(hours=1), bind=lambda document: replace(document, instrument_ids=["TEST", "TESTP"]))
        self.assertEqual(stored.available_at, NOW)
        self.assertEqual(len(self.repo.available_documents(["TEST"], NOW)), 1)
        self.assertEqual(self.repo.available_documents(["TESTP"], NOW), [])
        self.assertEqual(len(self.repo.available_documents(["TESTP"], NOW+timedelta(hours=1))), 1)


if __name__ == "__main__":
    unittest.main()
