"""Фрагменты и русскоязычный поиск FTS5 с ограничением эмитента и даты."""

from hashlib import sha256
from dataclasses import replace
import re
import unicodedata

from .evidence_ingestion import ingest_document
from .financial_extraction import extract_financials
from .models import EvidenceChunk, EvidenceDocument

STOPWORDS = {"и", "в", "на", "по", "за", "с", "из", "для", "о", "об", "к", "от", "the", "of", "and"}
SUFFIXES = ("иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими", "ая", "яя", "ые", "ие", "ый", "ий", "ой", "ам", "ям", "ах", "ях", "ов", "ев", "а", "я", "ы", "и", "у", "ю", "е", "о", "ь")


def normalized_tokens(text: str) -> list[str]:
    """Нормализация ё/е и распространённых окончаний с поиском по префиксу."""
    words = re.findall(r"[a-zа-я0-9]+", unicodedata.normalize("NFKC", text).casefold().replace("ё", "е"))
    tokens = []
    for word in words:
        if word in STOPWORDS:
            continue
        if re.fullmatch(r"[а-я]+", word):
            for suffix in SUFFIXES:
                if word.endswith(suffix) and len(word)-len(suffix) >= 4:
                    word = word[:-len(suffix)]
                    break
        tokens.append(word)
    return tokens


def normalize_search_text(text: str) -> str:
    return " ".join(normalized_tokens(text))


def search_evidence(repo, query: str, instrument_ids: list[str], as_of, limit: int = 8) -> list[dict]:
    words = list(dict.fromkeys(normalized_tokens(query)))[:16]
    match = " AND ".join(f'"{word}"*' for word in words)
    return repo.search_evidence(match, sorted(set(instrument_ids)), as_of, limit)


def document_chunks(document: EvidenceDocument, pages: list[str] | None = None, size: int = 1200) -> list[EvidenceChunk]:
    if size < 100:
        raise ValueError("Размер фрагмента должен быть не меньше 100 символов")
    chunks = []
    for page_number, text in enumerate(pages or [document.content], 1):
        cursor = 0
        while cursor < len(text):
            end = min(cursor+size, len(text))
            boundary = text.rfind(" ", cursor+size//2, end)
            if end < len(text) and boundary > cursor:
                end = boundary
            content = text[cursor:end].strip()
            if content:
                identity = sha256(f"{document.id}:{document.version}:{page_number}:{cursor}:{end}".encode()).hexdigest()
                chunks.append(EvidenceChunk(identity, document.id, document.version, content,
                                            page=page_number if pages else None, section=document.title))
            cursor = end
    return chunks


def index_document(repo, document: EvidenceDocument, pages: list[str] | None = None, binding_confirmed_at=None) -> tuple[EvidenceDocument, list[str]]:
    """Сохранить каноническую версию, индекс и факты с исходными фрагментами."""
    instrument_ids = document.instrument_ids
    document = ingest_document(repo, document, binding_confirmed_at)
    return document, index_stored_document(repo, document, pages, instrument_ids)


def index_stored_document(repo, document: EvidenceDocument, pages: list[str] | None = None,
                           instrument_ids: list[str] | None = None) -> list[str]:
    """Построить производный индекс для уже сохранённой версии материала."""
    if repo.get_document(document.id, document.version) != document:
        raise ValueError("Версия исходного документа не сохранена")
    quality = []
    for chunk in document_chunks(document, pages):
        repo.save_evidence_chunk(chunk, normalize_search_text(chunk.content))
    if pages and document.kind == "report_content":
        for instrument_id in sorted(set(document.instrument_ids) | set(instrument_ids or [])):
            extracted = extract_financials(document, pages, instrument_id)
            quality.extend(extracted.quality)
            for chunk in extracted.chunks:
                repo.save_evidence_chunk(chunk, normalize_search_text(chunk.content))
            for fact in extracted.facts:
                confirmed_at = repo.evidence_instrument_time(document.id, document.version, instrument_id)
                if confirmed_at is not None and fact.available_at is not None:
                    fact = replace(fact, available_at=max(fact.available_at, confirmed_at))
                repo.save_financial_fact(fact)
    return list(dict.fromkeys(quality))
