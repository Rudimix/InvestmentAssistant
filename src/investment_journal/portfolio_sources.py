"""Адаптеры операций и событий для проверенных MCP-схем отчётов.

Контракты получены через list_tools и сохранены в тестовых фикстурах.
Операции требуют responseView PAYMENTS, новости фильтруются по времени
на стороне приложения, а instrument_reports возвращает календарь публикаций.
"""

from datetime import datetime, timezone
from hashlib import sha256
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import UUID

from .market import optional_decimal
from .mcp_client import ToolError
from .models import (
    EventBatch, InstrumentEvent, OperationBatch, PortfolioOperation, SnapshotPosition, data_json, timestamp,
)

if TYPE_CHECKING:
    from .market import MarketDataService


def operation_from(raw: dict) -> PortfolioOperation:
    """Прочитать операцию с сохранением состояний и исходной валюты."""
    payment = raw.get("payment") or {}
    return PortfolioOperation(
        id=str(raw["id"]), occurred_at=timestamp(raw["date"]),
        operation_type=raw.get("type") or raw.get("operationType", ""),
        state=raw.get("state", ""), payment=optional_decimal(payment),
        currency=str(payment.get("currency") or raw.get("currency") or "").lower(),
        instrument_id=raw.get("instrumentUid") or raw.get("positionUid") or raw.get("figi") or "",
        ticker=raw.get("ticker", ""), quantity=optional_decimal(raw.get("quantityDone", raw.get("quantity"))),
        parent_id=raw.get("parentOperationId", ""), commission=optional_decimal(raw.get("commission")),
    )


async def fetch_operations(market: "MarketDataService", start: datetime, end: datetime) -> OperationBatch:
    """Получить все доступные страницы за (start, end] без предположения о полноте."""
    batch = OperationBatch()
    items: dict[str, PortfolioOperation] = {}
    cursor = ""
    seen = set()
    try:
        account_id = await market.account_id()
        for _ in range(100):
            raw = await market.report_call("invest_get_operations", {
                "accountId": account_id, "from": timestamp(start).isoformat(),
                "to": timestamp(end).isoformat(), "cursor": cursor, "limit": 1000,
                "responseView": ["FULL"],
            })
            for item in raw.get("items", []):
                try:
                    operation = operation_from(item)
                except (ValueError, KeyError, TypeError):
                    batch.quality.append("Часть операций не удалось прочитать")
                    continue
                if start < operation.occurred_at <= end:
                    items[operation.id] = operation
            if raw.get("hasNext") is False:
                batch.complete = not batch.quality
                break
            next_cursor = raw.get("nextCursor")
            if not next_cursor or next_cursor in seen:
                batch.quality.append("Источник не подтвердил полноту истории операций")
                break
            seen.add(next_cursor)
            cursor = next_cursor
        else:
            batch.quality.append("Достигнут предел страниц операций")
    except (ToolError, ValueError, TypeError, AttributeError):
        batch.quality.append("История операций недоступна или получена частично")
    batch.items = sorted(items.values(), key=lambda item: (item.occurred_at, item.id))
    return batch


def event_time(value: str | None) -> datetime | None:
    """Время публикации либо календарная дата источника."""
    if not value:
        return None
    try:
        if len(value) == 10:
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        return timestamp(value)
    except (ValueError, TypeError):
        return None


def source_url(value: str | None) -> str | None:
    """Сохранить исходную веб-ссылку с проверкой допустимого протокола."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username:
            return value
    except ValueError:
        pass
    return None


def news_from(raw: dict, requested: SnapshotPosition) -> InstrumentEvent:
    """instrumentId новостей содержит массив обёрток instrument, ts — UTC-время."""
    identifiers = []
    for wrapper in raw.get("instrumentId") or []:
        instrument = wrapper.get("instrument", {}) if isinstance(wrapper, dict) else {}
        identifiers.extend(str(instrument[key]) for key in ("instrumentUid", "ticker", "assetUid")
                           if instrument.get(key))
    if not identifiers:
        identifiers = [requested.instrument_id, requested.ticker]
    source = str(raw.get("source") or "T-Invest MCP")
    key = str(raw.get("id") or sha256(data_json(raw).encode()).hexdigest())
    return InstrumentEvent(
        id=f"news:{source}:{key}", kind="news", instrument_ids=identifiers,
        title=str(raw.get("title") or "Новость источника"), source=source,
        url=source_url(raw.get("url") or raw.get("link")),
        published_at=event_time(raw.get("ts") or raw.get("publishedAt")),
        content=str(raw.get("content") or raw.get("summary") or ""),
    )


def report_from(raw: dict, instrument_id: str) -> InstrumentEvent:
    """Календарные метаданные сохраняются отдельно от реально полученного содержания."""
    period = " ".join(str(raw.get(key) or "") for key in ("periodYear", "periodType", "periodNum")).strip()
    date = event_time(raw.get("reportDate"))
    key = sha256(data_json([instrument_id, raw.get("reportDate"), period]).encode()).hexdigest()
    content = str(raw.get("content") or "")
    return InstrumentEvent(
        id=f"report:{key}", kind="report_content" if content else "report_calendar",
        instrument_ids=[instrument_id], title=str(raw.get("title") or "Публикация отчётности"),
        source=str(raw.get("source") or "T-Invest MCP"), url=source_url(raw.get("url") or raw.get("link")),
        published_at=event_time(raw.get("publishedAt")), event_at=date, period=period, content=content,
    )


async def report_instrument_id(market: "MarketDataService", position: SnapshotPosition) -> str:
    """Календарь принимает UID, новости принимают тикер."""
    try:
        return str(UUID(position.instrument_id))
    except ValueError:
        raw = await market.report_call("invest_find_instrument", {
            "query": position.ticker, "responseView": ["IDENTIFIERS"],
        })
        matches = [item for item in raw.get("instruments", [])
                   if item.get("ticker", "").upper() == position.ticker.upper() and item.get("uid")]
        if not matches:
            raise ToolError("UID инструмента для календаря недоступен")
        return next((item["uid"] for item in matches if item.get("classCode") == "TQBR"), matches[0]["uid"])


async def fetch_news(market: "MarketDataService", position: SnapshotPosition,
                     start: datetime, end: datetime, batch: EventBatch) -> None:
    """Проверить страницы новостей и фактические времена в заданном периоде."""
    source = f"news:{position.ticker}"
    batch.sources[source] = "partial"
    cursor = ""
    seen = set()
    try:
        for _ in range(10):
            raw = await market.report_call("invest_get_news", {
                "instrumentId": position.ticker, "cursor": cursor, "limit": 100,
            })
            for item in raw.get("items", []):
                event = news_from(item, position)
                if event.published_at is None or start < event.published_at <= end:
                    batch.items.append(event)
            if raw.get("hasNext") is False:
                batch.sources[source] = "complete"
                return
            next_cursor = raw.get("nextCursor")
            if not next_cursor or next_cursor in seen:
                break
            seen.add(next_cursor)
            cursor = next_cursor
        batch.quality.append(f"Новости {position.ticker} получены частично")
    except (ToolError, ValueError, TypeError, AttributeError):
        batch.sources[source] = "unavailable"
        batch.quality.append(f"Источник новостей {position.ticker} недоступен")


async def fetch_reports(market: "MarketDataService", position: SnapshotPosition,
                        start: datetime, end: datetime, batch: EventBatch) -> None:
    """Получить календарь и явно сохранить отсутствие текста и исходных ссылок."""
    source = f"reports:{position.instrument_id}"
    try:
        instrument_id = await report_instrument_id(market, position)
        raw = await market.report_call("invest_get_instrument_reports", {
            "instrumentId": instrument_id, "from": timestamp(start).isoformat(), "to": timestamp(end).isoformat(),
        })
        for item in raw.get("events", []):
            event = report_from(item, instrument_id)
            event.instrument_ids = list(dict.fromkeys(event.instrument_ids + [position.instrument_id, position.ticker]))
            if event.event_at is None or start.date() <= event.event_at.date() <= end.date():
                batch.items.append(event)
        batch.sources[source] = "calendar"
    except (ToolError, ValueError, TypeError, AttributeError):
        batch.sources[source] = "unavailable"
        batch.quality.append(f"Источник отчётности {position.ticker} недоступен")


async def fetch_events(market: "MarketDataService", positions: list[SnapshotPosition],
                       start: datetime, end: datetime) -> EventBatch:
    """Собрать источники для позиций с отдельным состоянием каждого поиска."""
    batch = EventBatch()
    instruments = {item.instrument_id: item for item in positions if item.instrument_type != "currency"}
    news_tickers = set()
    for position in instruments.values():
        if position.ticker not in news_tickers:
            await fetch_news(market, position, start, end, batch)
            news_tickers.add(position.ticker)
        await fetch_reports(market, position, start, end, batch)
    batch.found = len(batch.items)
    if any(item.url is None for item in batch.items):
        batch.quality.append("Часть событий получена без исходной ссылки")
    if any(item.published_at is None for item in batch.items if item.kind == "news"):
        batch.quality.append("Время публикации части новостей недоступно")
    return batch
