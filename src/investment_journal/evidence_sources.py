"""Публичные RSS, HTML и PDF с собственным HTTP-клиентом и происхождением."""

from dataclasses import dataclass, field
import asyncio
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

import httpx
from pypdf.errors import PyPdfError

from .mcp_client import ssl_context
from .financial_extraction import extract_pdf_pages
from .models import EvidenceDocument, timestamp


@dataclass(frozen=True)
class EvidenceSource:
    id: str
    name: str
    url: str
    format: str
    instrument_ids: tuple[str, ...] = ()
    reporting_period: str = ""
    refresh_seconds: int = 1800
    max_bytes: int = 20_000_000

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (not self.id or not self.name or parsed.scheme != "https" or not parsed.hostname or parsed.username
                or self.format not in {"rss", "html", "pdf"} or self.refresh_seconds <= 0 or self.max_bytes <= 0):
            raise ValueError("Некорректный публичный источник")


DEFAULT_SOURCES = (
    EvidenceSource("interfax", "Интерфакс", "https://www.interfax.ru/rss.asp", "rss"),
    EvidenceSource("sber-ifrs-2025", "Сбербанк · МСФО", "https://www.sberbank.com/common/img/uploaded/files/info/reporting_q4_aolql6na_2025.pdf",
                   "pdf", ("SBER", "SBERP"), "2025", 86400),
)


def source_registry(path: Path | None = None) -> tuple[EvidenceSource, ...]:
    if path is None:
        return DEFAULT_SOURCES
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources = tuple(EvidenceSource(**(item | {"instrument_ids": tuple(item.get("instrument_ids", []))})) for item in raw)
    if len({item.id for item in sources}) != len(sources):
        raise ValueError("Повторяются идентификаторы источников")
    return sources


@dataclass
class SourceResult:
    source_id: str
    retrieved_at: datetime
    documents: list[EvidenceDocument] = field(default_factory=list)
    pages: dict[str, list[str]] = field(default_factory=dict)
    complete: bool = False
    quality: list[str] = field(default_factory=list)
    retry_at: datetime | None = None
    temporary: bool = False


def publication_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return timestamp(value)
    except (ValueError, TypeError):
        try:
            return timestamp(parsedate_to_datetime(value))
        except (ValueError, TypeError, OverflowError):
            return None


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.title, self.dates = [], [], []
        self.ignored, self.in_title = 0, False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self.ignored += 1
        self.in_title = self.in_title or tag == "title"
        if tag == "meta" and (attrs.get("property") or attrs.get("name")) in {"article:published_time", "datePublished", "date"}:
            self.dates.append(attrs.get("content"))
        if tag == "time":
            self.dates.append(attrs.get("datetime"))

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.ignored = max(0, self.ignored-1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.ignored and data.strip():
            self.text.append(data.strip())
            if self.in_title:
                self.title.append(data.strip())


def html_text(value: str) -> tuple[str, str, datetime | None]:
    parser = _PageText()
    parser.feed(value)
    dates = [at for item in parser.dates if (at := publication_time(item)) is not None]
    # Несовпадающие даты метаданных не подтверждают время публикации.
    published = dates[0] if dates and len(set(dates)) == 1 else None
    return "\n".join(parser.text), " ".join(parser.title), published


def _document(source, url, title, content, at, published=None):
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username:
        raise ValueError("У материала отсутствует публичная HTTP-ссылка")
    return EvidenceDocument(sha256(url.encode()).hexdigest(), sha256(content.encode()).hexdigest(),
                            source.name, url, title, content, at, published_at=published,
                            available_at=published or at, instrument_ids=list(source.instrument_ids),
                            kind="news" if source.format == "rss" else "report_content",
                            reporting_period=source.reporting_period)


def parse_feed(source: EvidenceSource, content: bytes, at: datetime) -> list[EvidenceDocument]:
    root = ET.fromstring(content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    documents = []
    for item in root.findall("./channel/item")+root.findall("a:entry", ns):
        title = item.findtext("title") or item.findtext("a:title", namespaces=ns) or "Публикация источника"
        link = item.findtext("link")
        if link is None:
            links = [node.get("href") for node in item.findall("a:link", ns) if node.get("rel", "alternate") == "alternate"]
            link = next(iter(links), None)
        if not link:
            continue
        description = item.findtext("description") or item.findtext("a:summary", namespaces=ns) or item.findtext("a:content", namespaces=ns) or ""
        text, _, _ = html_text(description)
        published = publication_time(item.findtext("pubDate") or item.findtext("a:published", namespaces=ns))
        documents.append(_document(source, urljoin(source.url, link), title, title+"\n"+text, at, published))
    return documents


class EvidenceHttpClient:
    """HTTP-загрузка публичных материалов без заголовков брокерского подключения."""

    def __init__(self, ca_files: tuple[str, ...] = (), transport=None):
        self.client = httpx.AsyncClient(verify=ssl_context(ca_files), transport=transport,
                                        timeout=30, follow_redirects=True, headers={"User-Agent": "InvestmentAssistant/0.2"})

    async def aclose(self):
        await self.client.aclose()

    async def fetch(self, source: EvidenceSource, at: datetime) -> SourceResult:
        result = SourceResult(source.id, timestamp(at))
        try:
            async with self.client.stream("GET", source.url) as response:
                if response.status_code == 429:
                    retry = response.headers.get("Retry-After", "60")
                    result.retry_at = at+timedelta(seconds=int(retry)) if retry.isdigit() else publication_time(retry)
                response.raise_for_status()
                if response.url.scheme != "https":
                    raise ValueError("Источник перенаправил загрузку за пределы HTTPS")
                parts, size = [], 0
                async for part in response.aiter_bytes():
                    size += len(part)
                    if size > source.max_bytes:
                        raise ValueError("Материал превышает настроенный предел загрузки")
                    parts.append(part)
                content = b"".join(parts)
                url = str(response.url)
                encoding = response.encoding or "utf-8"
            if source.format == "rss":
                result.documents = parse_feed(source, content, at)
            elif source.format == "html":
                text, title, published = html_text(content.decode(encoding))
                result.documents = [_document(source, url, title or source.name, text, at, published)]
            else:
                pages = await asyncio.to_thread(extract_pdf_pages, content)
                if not any(page.strip() for page in pages):
                    raise ValueError("PDF не содержит извлекаемого текста")
                doc = _document(source, url, source.name, "\n\n".join(pages), at)
                result.documents, result.pages[doc.id] = [doc], pages
            result.complete = True
        except (httpx.HTTPError, ValueError, TypeError, UnicodeError, OverflowError, ET.ParseError, PyPdfError) as exc:
            result.temporary = isinstance(exc, httpx.RequestError) or isinstance(exc, httpx.HTTPStatusError) and (
                exc.response.status_code == 429 or exc.response.status_code >= 500)
            result.quality.append(f"{source.name} · загрузка или извлечение недоступны · {type(exc).__name__}")
        return result
