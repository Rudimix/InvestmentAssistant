"""Обновление открытых источников, датированный кэш и общий сбор материалов."""

import asyncio
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re

from .evidence_search import index_document, normalized_tokens
from .evidence_ingestion import ingest_document
from .evidence_sources import EvidenceHttpClient, source_registry
from .market_evidence import evidence_events, ingest_market_events
from .mcp_client import ToolError
from .models import EvidenceDocument, data_json, timestamp
from .portfolio_sources import fetch_events


class IssuerIndex:
    """Имена из общего публичного каталога, независимые от состава чьего-либо счёта."""

    def __init__(self, rows: list[dict]):
        self.aliases = defaultdict(set)
        self.words = 1
        for row in rows:
            identifiers = {str(row.get(key) or "") for key in ("uid", "positionUid", "assetUid", "ticker")}-{ "" }
            if row.get("ticker") and row.get("classCode"):
                identifiers.add(f"{row['ticker']}_{row['classCode']}")
            name = str(row.get("name") or "")
            ordinary = re.sub(r"\s*[-–—]\s*привилегированные акции$", "", name, flags=re.IGNORECASE)
            for alias in {str(row.get("ticker") or ""), name, ordinary}:
                tokens = normalized_tokens(alias)
                key = "".join(tokens)
                if len(key) >= 3:
                    self.aliases[key].update(identifiers)
                    self.words = max(self.words, len(tokens))

    def bind(self, document: EvidenceDocument) -> EvidenceDocument:
        if document.instrument_ids and document.kind != "news":
            return document
        words = normalized_tokens(document.title+" "+document.content)
        identifiers = set(document.instrument_ids)
        for start in range(len(words)):
            for count in range(1, min(self.words, len(words)-start)+1):
                identifiers.update(self.aliases.get("".join(words[start:start+count]), ()))
        return replace(document, instrument_ids=sorted(identifiers))


class EvidenceService:
    """Один кэш публичных источников с ограниченными повторами и возобновлением."""

    def __init__(self, repo, client: EvidenceHttpClient, sources=None, clock=None):
        self.repo, self.client = repo, client
        self.sources = tuple(sources) if sources is not None else source_registry()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._locks = defaultdict(asyncio.Lock)

    async def aclose(self):
        await self.client.aclose()

    async def _issuer_index(self, market) -> tuple[IssuerIndex, list[str]]:
        key = "evidence:public-share-names-v1"
        now = self.clock()
        cached = self.repo.get_source_refresh(key)
        if cached and cached["retry_at"] and timestamp(cached["retry_at"]) > now:
            return IssuerIndex(cached["payload"].get("rows", [])), ["Имена эмитентов используются из кэша до следующей попытки обновления"]
        if cached and cached["last_success_at"] and now-timestamp(cached["last_success_at"]) < timedelta(days=1):
            return IssuerIndex(cached["payload"].get("rows", [])), []
        try:
            raw = await market.report_call("invest_list_shares", {"instrumentStatus": "INSTRUMENT_STATUS_ALL", "responseView": ["IDENTIFIERS"]})
            rows = raw.get("instruments", raw.get("shares"))
            if not isinstance(rows, list) or not rows:
                raise ValueError("Каталог эмитентов пуст")
            self.repo.save_source_refresh(key, "complete", {"rows": rows}, self.clock())
            return IssuerIndex(rows), []
        except (ToolError, ValueError, TypeError, AttributeError):
            payload = cached["payload"] if cached else {"rows": []}
            self.repo.save_source_refresh(key, "partial" if payload.get("rows") else "unavailable", payload,
                                           retry_at=now+timedelta(minutes=5), failures=(cached["failures"] if cached else 0)+1)
            return IssuerIndex(cached["payload"].get("rows", []) if cached else []), ["Имена эмитентов недоступны или используются из датированного кэша"]

    async def _refresh(self, source) -> dict:
        key = "evidence-source:"+sha256(data_json(asdict(source)).encode()).hexdigest()
        async with self._locks[key]:
            now = self.clock()
            cached = self.repo.get_source_refresh(key)
            if cached and cached["retry_at"] and timestamp(cached["retry_at"]) > now:
                return cached
            if (cached and cached["status"] == "complete" and cached["last_success_at"]
                    and now-timestamp(cached["last_success_at"]) < timedelta(seconds=source.refresh_seconds)):
                return cached
            for attempt in range(2):
                result = await self.client.fetch(source, self.clock())
                if result.complete or not result.temporary or result.retry_at is not None or attempt == 1:
                    break
                await asyncio.sleep(0.2)
            now = self.clock()
            if result.complete:
                documents = [asdict(replace(doc, retrieved_at=now)) for doc in result.documents]
                payload = {"documents": documents, "pages": result.pages, "quality": result.quality}
                self.repo.save_source_refresh(key, "complete", json.loads(data_json(payload)), now)
            else:
                failures = (cached["failures"] if cached else 0)+1
                retry_at = result.retry_at or now+timedelta(seconds=min(3600, 30*2**min(failures-1, 10)))
                payload = dict(cached["payload"]) if cached else {"documents": [], "pages": {}}
                payload["quality"] = result.quality
                self.repo.save_source_refresh(key, "partial" if payload["documents"] else "unavailable", payload,
                                               retry_at=retry_at, failures=failures)
            return self.repo.get_source_refresh(key)

    async def collect(self, market, positions, start, end, as_of=None):
        """Совместить fetch_events и открытые каналы, ограничив версии заданной датой."""
        raw = await fetch_events(market, positions, start, end, clock=self.clock)
        quality = list(raw.quality)
        async with self._locks["issuer-index"]:
            issuers, limitations = await self._issuer_index(market)
        quality.extend(limitations)
        quality.extend(ingest_market_events(self.repo, raw, self.clock(), bind=issuers.bind))
        sources = dict(raw.sources)
        freshness = {}
        for source in self.sources:
            cached = await self._refresh(source)
            sources[source.id] = cached["status"]
            freshness[source.id] = {"name": source.name, "last_success_at": cached["last_success_at"],
                                    "status": cached["status"], "retry_at": cached["retry_at"]}
            payload = cached["payload"]
            quality.extend(payload.get("quality", []))
            if cached["status"] != "complete":
                quality.append(f"{source.name} · сохранённые данные получены {cached['last_success_at'] or 'успешная загрузка отсутствует'}")
            for raw_document in payload.get("documents", []):
                document = EvidenceDocument.from_dict(raw_document)
                pages = payload.get("pages", {}).get(document.id)
                original = document
                document = issuers.bind(original)
                added = set(document.instrument_ids)-set(original.instrument_ids)
                binding_at = max(original.retrieved_at, self.clock()) if added else original.retrieved_at
                if not document.instrument_ids:
                    continue
                try:
                    if added and original.instrument_ids:
                        ingest_document(self.repo, original)
                    _, extracted_quality = index_document(self.repo, document, pages, binding_confirmed_at=binding_at)
                    quality.extend(f"{source.name} · {reason}" for reason in extracted_quality)
                except (ValueError, TypeError, KeyError):
                    sources[source.id] = "partial"
                    quality.append(f"{source.name} · часть материала не удалось связать с индексом")
        cutoff = timestamp(as_of) if as_of is not None else self.clock()
        batch = evidence_events(self.repo, positions, start, end, cutoff, sources, list(dict.fromkeys(quality)))
        batch.freshness = freshness
        return batch
