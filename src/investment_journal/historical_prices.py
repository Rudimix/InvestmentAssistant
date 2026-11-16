"""Исторические часовые цены MCP с доказуемым временем доступности и кэшем запросов."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import TYPE_CHECKING

from .mcp_client import ToolError
from .models import CurrencyPair, HistoricalPriceBatch, PriceObservation, data_json, decimal_value, timestamp

if TYPE_CHECKING:
    from .market import MarketDataService
    from .storage import JournalRepository


def _observations(raw: dict, instrument_id: str, start: datetime, end: datetime,
                   received_at: datetime) -> tuple[list[PriceObservation], list[str]]:
    currency = str(raw.get("priceCurrency") or "").lower()
    if currency == "pt.":
        currency = "pt"
    if not currency:
        return [], ["Источник не указал валюту исторической цены"]
    rows = raw.get("candles")
    if not isinstance(rows, list):
        return [], ["Источник не вернул список исторических свечей"]
    observations, quality = {}, []
    for item in rows:
        try:
            at = timestamp(item["time"])
            closes_at = at+timedelta(hours=1)
            if not start <= closes_at <= end:
                continue
            if item.get("isComplete") is not True:
                quality.append("Источник не подтвердил завершённость части свечей")
                continue
            price = decimal_value(item["close"])
            version = sha256(data_json({"instrument": instrument_id, "at": at, "close": price,
                                       "currency": currency, "interval": "hour"}).encode()).hexdigest()
            observation = PriceObservation(instrument_id, at, closes_at, price, currency, received_at, version)
            if at in observations and observations[at].close != price:
                quality.append("Одинаковые времена свечей содержат разные цены")
            observations[at] = observation
        except (ValueError, TypeError, KeyError, OverflowError):
            quality.append("Часть исторических свечей непригодна или не содержит UTC-времени")
    return sorted(observations.values(), key=lambda item: item.candle_end), list(dict.fromkeys(quality))


async def historical_prices(market: "MarketDataService", instrument_id: str, start: datetime, end: datetime,
                             repo: "JournalRepository | None" = None, refresh: bool = False,
                             now: datetime | None = None) -> HistoricalPriceBatch:
    """Загрузить [start, end) блоками до 30 дней, включая только завершённые часовые свечи."""
    start, end = timestamp(start), timestamp(end)
    now = timestamp(now or datetime.now(timezone.utc))
    if start >= end or end > now:
        raise ValueError("Некорректный исторический диапазон цен")
    request = {"instrumentId": instrument_id, "from": start, "to": end, "interval": "hour"}
    cache_key = "prices:"+sha256(data_json(request).encode()).hexdigest()
    cached = repo.get_source_refresh(cache_key) if repo is not None else None
    if cached and cached["status"] == "complete" and not refresh:
        return HistoricalPriceBatch.from_dict(cached["payload"])
    batch = HistoricalPriceBatch(instrument_id, start, end, now)
    cursor, observations = start, {}
    try:
        while cursor < end:
            next_at = cursor+min(timedelta(days=30), end-cursor)
            query_start = cursor.replace(minute=0, second=0, microsecond=0)
            if query_start > datetime.min.replace(tzinfo=timezone.utc):
                query_start -= timedelta(hours=1)
            raw = await market.report_call("invest_get_candles", {
                "instrumentId": instrument_id, "interval": "CANDLE_INTERVAL_HOUR", "from": query_start.isoformat(),
                "to": next_at.isoformat(), "limit": 2400,
            })
            items, quality = _observations(raw, instrument_id, cursor, next_at, now)
            batch.quality.extend(quality)
            for item in items:
                if item.candle_start in observations and observations[item.candle_start].close != item.close:
                    batch.quality.append("Источники расходятся в исторической цене")
                observations[item.candle_start] = item
            cursor = next_at
        batch.complete = not batch.quality
    except (ToolError, ValueError, TypeError, AttributeError):
        batch.quality.append("Исторические цены недоступны или получены частично")
    batch.items = sorted(observations.values(), key=lambda item: item.candle_end)
    batch.quality = list(dict.fromkeys(batch.quality))
    previous_complete = cached and cached["payload"].get("complete") is True
    if not batch.complete and previous_complete:
        previous = HistoricalPriceBatch.from_dict(cached["payload"])
        batch = replace(previous, complete=False, quality=list(dict.fromkeys(
            previous.quality+batch.quality+["Использована сохранённая версия исторических цен"])))
    if repo is not None:
        # Версия цены не зависит от времени повторного получения. Сохраняем
        # первое наблюдение прежней версии и явно фиксируем исправленные цены.
        for index, item in enumerate(batch.items):
            stored = repo.get_historical_prices(instrument_id, item.candle_end, item.candle_end+timedelta(microseconds=1))
            previous = next((row for row in stored if row["source"] == item.source and row["version"] == item.version), None)
            if previous:
                batch.items[index] = PriceObservation.from_dict(previous["payload"])
            else:
                repo.save_historical_price(instrument_id, item.candle_end, item.source, item.version,
                                           item.retrieved_at, json.loads(data_json(item)))
        payload = cached["payload"] if not batch.complete and previous_complete else json.loads(data_json(batch))
        repo.save_source_refresh(cache_key, "complete" if batch.complete else "partial", payload,
                                 now if batch.complete else None)
    return batch


def price_before(batch: HistoricalPriceBatch, at: datetime, max_age: timedelta = timedelta(days=14)) -> PriceObservation | None:
    """Последняя завершённая цена до границы включительно, с ограничением её возраста."""
    at = timestamp(at)
    if max_age <= timedelta(0):
        raise ValueError("Допустимый возраст цены должен быть положительным")
    eligible = [item for item in batch.items if item.candle_end <= at and at-item.candle_end <= max_age]
    return max(eligible, key=lambda item: item.candle_end) if eligible else None


def fx_rate(observation: PriceObservation | None, pair: CurrencyPair | None,
             price_currency: str, base_currency: str) -> Decimal | None:
    """Курс по подтверждённой котировке валютной пары, без подстановки отсутствующего курса."""
    if price_currency.lower() == base_currency.lower():
        return Decimal(1)
    if observation is None or pair is None or observation.instrument_id != pair.instrument_id:
        return None
    if observation.currency != pair.quote_currency or observation.close <= 0:
        return None
    direction = (price_currency.lower(), base_currency.lower())
    if direction == (pair.source_currency, pair.quote_currency):
        return observation.close/pair.nominal
    if direction == (pair.quote_currency, pair.source_currency):
        return pair.nominal/observation.close
    return None


def currency_pairs(raw: dict) -> list[CurrencyPair]:
    pairs = []
    for item in raw.get("instruments", raw.get("currencies", [])):
        nominal = item.get("nominal", {})
        try:
            pairs.append(CurrencyPair(item.get("uid") or f"{item['ticker']}_{item['classCode']}",
                                       nominal.get("currency", ""), item.get("currency", ""), nominal.get("value")))
        except (ValueError, TypeError, KeyError):
            continue
    return pairs


async def currency_rates(market, repo, currencies, base, at, observed_at, catalog):
    """Датированные курсы и параметры использованных пар для повторения расчёта."""
    rates, proofs = {}, {}
    for currency in sorted(set(currencies)-{base, ""}):
        matches = [pair for pair in catalog if {pair.source_currency, pair.quote_currency} == {currency, base}]
        for pair in matches:
            batch = await historical_prices(market, pair.instrument_id, at-timedelta(days=14), at, repo, now=observed_at)
            quote = price_before(batch, at)
            rate = fx_rate(quote, pair, currency, base)
            if rate is not None:
                rates[currency] = rate
                proofs[currency] = {"pair": asdict(pair), "price": asdict(quote)}
                break
    return rates, proofs
