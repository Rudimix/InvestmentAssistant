"""Идентичность материалов, неизменяемые версии и исходная атрибуция каналов."""

from dataclasses import replace
from hashlib import sha256
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import EvidenceDocument, InstrumentEvent, data_json

TRACKING_PARAMETERS = {"fbclid", "gclid", "yclid", "_openstat", "mc_cid", "mc_eid"}


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Требуется публичная ссылка HTTP или HTTPS")
    host = parts.hostname.lower().encode("idna").decode("ascii")
    if ":" in host:
        host = f"[{host}]"
    if parts.port and (parts.scheme, parts.port) not in {("http", 80), ("https", 443)}:
        host += f":{parts.port}"
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS]
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(sorted(query)), ""))


def ingest_document(repo, document: EvidenceDocument, binding_confirmed_at=None) -> EvidenceDocument:
    """Сохранять доступность новой версии со времени первого наблюдения её содержания.

    Дата публикации остаётся атрибуцией источника. Первое получение текущего
    текста само по себе не подтверждает доступность этой версии в прошлом.
    """
    url = canonical_url(document.url)
    normalized = lambda text: " ".join(unicodedata.normalize("NFKC", text).split())
    material = {"title": normalized(document.title), "content": normalized(document.content),
                "kind": document.kind,
                "period": document.reporting_period, "published_at": document.published_at}
    fingerprint = sha256(data_json(material).encode()).hexdigest()
    material.update(title=document.title, content=document.content)
    version = sha256(data_json(material).encode()).hexdigest()
    document = replace(document, id=sha256(url.encode()).hexdigest(), version=version)
    return repo.ingest_evidence(document, url, fingerprint, binding_confirmed_at)


def ingest_event(repo, event: InstrumentEvent, retrieved_at, bind=None) -> EvidenceDocument | None:
    """Календарь и событие без исходной ссылки не становятся содержанием отчётности."""
    if not event.url or not event.content.strip() or event.kind == "report_calendar":
        return None
    document = EvidenceDocument(event.id, "", event.source, event.url, event.title,
                                event.content, event.retrieved_at or retrieved_at, published_at=event.published_at,
                                instrument_ids=event.instrument_ids,
                                kind="news" if event.kind == "news" else "report_content", reporting_period=event.period)
    bound = bind(document) if bind else document
    added = set(bound.instrument_ids)-set(document.instrument_ids)
    if added and document.instrument_ids:
        ingest_document(repo, document)
    binding_at = max(document.retrieved_at, retrieved_at) if added else document.retrieved_at
    return ingest_document(repo, bound, binding_confirmed_at=binding_at)
