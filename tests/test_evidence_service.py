"""Общий сбор каналов, повторы, свежесть и восстановление публичного кэша."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import _support  # noqa: F401
from investment_journal.evidence_service import EvidenceService
from investment_journal.evidence_sources import EvidenceSource, SourceResult
from investment_journal.models import EvidenceDocument, EventBatch, SnapshotPosition
from investment_journal.storage import JournalRepository
from test_analysis_models import NOW

CATALOG = json.loads((Path(__file__).parent / "fixtures/history_mcp_contracts.json").read_text(encoding="utf-8"))["examples"]["share_catalog"]["response"]


class EvidenceServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = JournalRepository()
        self.addCleanup(lambda: self.repo.close())
        self.now = NOW
        self.source = EvidenceSource("feed", "Feed", "https://example.com/feed", "rss", refresh_seconds=1)
        self.doc = EvidenceDocument("d", "v", "Feed", "https://example.com/news", "Сбербанк", "Чистая прибыль Сбербанка выросла", NOW,
                                    published_at=NOW-timedelta(hours=1), kind="news")
        self.client = AsyncMock()
        self.market = AsyncMock()
        self.market.report_call.return_value = CATALOG
        self.position = SnapshotPosition("uid", "SBER", "share", Decimal(1), "rub", instrument_id="uid")
        self.fetch = patch("investment_journal.evidence_service.fetch_events", AsyncMock(return_value=EventBatch()))
        self.fetch_mock = self.fetch.start()
        self.addCleanup(self.fetch.stop)

    def service(self, sources=None):
        return EvidenceService(self.repo, self.client, sources or [self.source], clock=lambda: self.now)

    async def collect(self, service, ticker="SBER", as_of=None):
        return await service.collect(self.market, [replace(self.position, ticker=ticker)], NOW-timedelta(days=7), self.now, as_of)

    async def test_global_catalog_binding_preserves_two_users_and_historical_cutoff(self):
        self.client.fetch.return_value = SourceResult("feed", NOW, [self.doc], complete=True)
        service = self.service()
        first = await self.collect(service)
        second = await self.collect(service, "SBERP")
        self.assertEqual(len(first.items), 1)
        self.assertEqual(len(second.items), 1)
        self.assertEqual(self.client.fetch.await_count, 1)
        self.fetch_mock.assert_awaited()
        historical = await self.collect(service, as_of=NOW-timedelta(seconds=1))
        self.assertFalse(historical.items)

    async def test_limited_retry_backoff_cache_and_recovery(self):
        success = SourceResult("feed", NOW, [self.doc], complete=True)
        failure = SourceResult("feed", NOW, quality=["temporary failure"], temporary=True)
        self.client.fetch.side_effect = [success, failure, failure, success]
        service = self.service()
        await self.collect(service)
        self.now += timedelta(seconds=2)
        stale = await self.collect(service)
        self.assertEqual(self.client.fetch.await_count, 3)
        self.assertEqual(stale.sources["feed"], "partial")
        self.assertTrue(stale.items)
        self.assertTrue(any("сохранённые данные" in reason for reason in stale.quality))
        await self.collect(service)
        self.assertEqual(self.client.fetch.await_count, 3)
        self.now += timedelta(seconds=31)
        fresh = await self.collect(service)
        self.assertEqual(fresh.sources["feed"], "complete")
        self.assertEqual(self.client.fetch.await_count, 4)

    async def test_one_unavailable_source_does_not_stop_another(self):
        second = replace(self.source, id="other", url="https://example.org/feed")
        self.client.fetch.side_effect = [SourceResult("feed", NOW, quality=["unavailable"]),
                                         SourceResult("other", NOW, [self.doc], complete=True)]
        batch = await self.collect(self.service([self.source, second]))
        self.assertEqual(batch.sources["feed"], "unavailable")
        self.assertEqual(batch.sources["other"], "complete")
        self.assertTrue(batch.items)

    async def test_cache_survives_repository_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"journal.db"
            self.repo.close()
            self.repo = JournalRepository(path)
            self.client.fetch.return_value = SourceResult("feed", NOW, [self.doc], complete=True)
            await self.collect(self.service())
            self.repo.close()
            self.repo = JournalRepository(path)
            self.client.fetch.reset_mock()
            batch = await self.collect(self.service())
            self.client.fetch.assert_not_awaited()
            self.assertTrue(batch.items)
            self.repo.close()


if __name__ == "__main__":
    unittest.main()
