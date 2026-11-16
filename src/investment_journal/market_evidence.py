"""Объединение публичных каналов с MCP по доступным на дату версиям."""

from dataclasses import replace
import re

from .evidence_ingestion import ingest_event
from .evidence_search import index_stored_document
from .models import EvidenceDocument, EventBatch, InstrumentEvent
from .portfolio_events import select_events


def position_identifiers(positions) -> list[str]:
    return sorted({value for p in positions for value in (p.key, p.instrument_id, p.ticker, getattr(p, "asset_id", "")) if value})


def bind_document(document: EvidenceDocument, positions, aliases: dict[str, list[str]] | None = None) -> EvidenceDocument:
    """Связать новость с точным тикером или подтверждённым именем эмитента."""
    if document.instrument_ids:
        return document
    text = document.title+"\n"+document.content
    identifiers = set()
    for p in positions:
        names = {p.ticker, *(aliases or {}).get(p.instrument_id, [])}
        if any(len(name) >= 3 and re.search(r"(?<!\w)"+re.escape(name)+r"(?!\w)", text, re.IGNORECASE) for name in names):
            identifiers.update({p.key, p.instrument_id, p.ticker}-{ "" })
    return replace(document, instrument_ids=sorted(identifiers))


def evidence_events(repo, positions, start, end, as_of, sources: dict[str, str] | None = None,
                    quality: list[str] | None = None) -> EventBatch:
    documents = repo.available_documents(position_identifiers(positions), as_of)
    items = [InstrumentEvent(f"evidence:{doc.id}:{doc.version}", doc.kind, doc.instrument_ids, doc.title, doc.source,
                              doc.url, published_at=doc.published_at, event_at=doc.published_at,
                              period=doc.reporting_period, content=doc.content) for doc in documents]
    limitations = list(quality or [])
    if not documents:
        limitations.append("Материалы с подтверждённой доступностью на дату анализа не найдены")
    if any(doc.published_at is None for doc in documents):
        limitations.append("Время публикации части материалов неизвестно, доступность ограничена первым получением версии")
    return select_events(EventBatch(items, dict(sources or {}), limitations, len(items)), positions, start, end)


def ingest_market_events(repo, batch: EventBatch, retrieved_at, bind=None) -> list[str]:
    quality = []
    for event in batch.items:
        document = ingest_event(repo, event, retrieved_at, bind)
        if document is None:
            if event.kind != "report_calendar":
                quality.append("Часть MCP-материалов не имеет содержания или исходной ссылки для индекса")
            continue
        index_stored_document(repo, document)
    return quality
