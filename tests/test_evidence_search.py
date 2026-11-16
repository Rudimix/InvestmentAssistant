"""Русские запросы, FTS5, эмитент, версии и доступность фрагментов."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import unittest

import _support  # noqa: F401
from investment_journal.evidence_search import document_chunks, index_document, search_evidence
from investment_journal.models import EvidenceDocument
from investment_journal.storage import JournalRepository
from test_analysis_models import NOW


class EvidenceSearchTests(unittest.TestCase):
    def setUp(self):
        self.repo = JournalRepository()
        self.addCleanup(self.repo.close)
        self.doc = EvidenceDocument("d", "v", "Issuer", "https://example.com/report", "Отчёт", "Чистая прибыль банка выросла. Объявлены дивиденды.",
                                    NOW, instrument_ids=["SBER"], kind="news")

    def test_russian_inflections_and_exact_issuer_scope(self):
        stored, _ = index_document(self.repo, self.doc)
        self.assertTrue(search_evidence(self.repo, "прибыли банков", ["SBER"], NOW))
        self.assertTrue(search_evidence(self.repo, "дивидендам", ["SBER"], NOW))
        self.assertEqual(search_evidence(self.repo, "дивиденды", ["OTHER"], NOW), [])
        self.assertEqual(search_evidence(self.repo, "несуществующее событие", ["SBER"], NOW), [])
        self.assertEqual(search_evidence(self.repo, "и на за", ["SBER"], NOW), [])
        self.assertEqual(search_evidence(self.repo, '" OR *', ["SBER"], NOW), [])
        self.assertEqual(search_evidence(self.repo, "прибыль", ["SBER"], NOW)[0]["document"].id, stored.id)

    def test_latest_version_available_at_requested_time(self):
        first, _ = index_document(self.repo, self.doc)
        later = NOW+timedelta(days=1)
        second, _ = index_document(self.repo, replace(self.doc, retrieved_at=later, content="Прибыль банка снизилась."))
        historical = search_evidence(self.repo, "прибыль", ["SBER"], NOW)
        current = search_evidence(self.repo, "прибыль", ["SBER"], later)
        self.assertEqual({row["document"].version for row in historical}, {first.version})
        self.assertEqual({row["document"].version for row in current}, {second.version})
        self.assertEqual(search_evidence(self.repo, "прибыль", ["SBER"], NOW-timedelta(seconds=1)), [])

    def test_new_instrument_binding_keeps_body_version_and_its_own_availability(self):
        first, _ = index_document(self.repo, self.doc)
        later = NOW+timedelta(days=1)
        second, _ = index_document(self.repo, replace(self.doc, instrument_ids=["SBERP"], retrieved_at=later))
        self.assertEqual(first.version, second.version)
        self.assertTrue(search_evidence(self.repo, "прибыль", ["SBER"], later))
        self.assertTrue(search_evidence(self.repo, "прибыль", ["SBERP"], later))
        self.assertFalse(search_evidence(self.repo, "прибыль", ["SBERP"], NOW))

    def test_pdf_chunks_and_financial_fact_have_page_and_bound_version(self):
        page = ("Консолидированная отчетность МСФО\nЗа год, закончившийся 31 декабря 2025 года\n"
                "в миллионах рублей    2025    2024\nЧистая прибыль    100    90")
        document = replace(self.doc, kind="report_content", content=page)
        stored, _ = index_document(self.repo, document, [page])
        found = search_evidence(self.repo, "чистая прибыль", ["SBER"], NOW)
        self.assertTrue(found)
        self.assertTrue(all(row["chunk"].page == 1 for row in found))
        facts = self.repo.get_financial_facts("SBER", as_of=NOW)
        self.assertEqual({fact.value for fact in facts}, {Decimal(100000000), Decimal(90000000)})
        self.assertTrue(all(fact.document_version == stored.version for fact in facts))
        index_document(self.repo, document, [page])
        self.assertEqual(len(self.repo.get_financial_facts("SBER", as_of=NOW)), 2)

    def test_chunk_size_retains_all_words_and_long_unicode_tokens(self):
        text = " ".join(f"слово{i}" for i in range(500))
        chunks = document_chunks(replace(self.doc, content=text), size=200)
        self.assertTrue(all(len(chunk.content) <= 200 for chunk in chunks))
        self.assertEqual(" ".join(chunk.content for chunk in chunks), text)
        long = document_chunks(replace(self.doc, content="😀"*500), size=200)
        self.assertEqual("".join(chunk.content for chunk in long), "😀"*500)


if __name__ == "__main__":
    unittest.main()
