"""Дневные ряды для подбора и модели с явным лагом доступности даты свечи."""

from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json

from .mcp_client import ToolError
from .models import DailyObservation, data_json, decimal_value, timestamp


async def daily_prices(market, instrument_id: str, start: datetime, end: datetime, repo=None, call=None):
    """Разбить диапазон блоками до пяти лет и вернуть свечи, известные к end.

    MCP отдаёт дневные даты без зоны. Методика daily-v1 подтверждает их
    доступность после следующих полных UTC-суток, сохраняя консервативный лаг.
    """
    start, end = timestamp(start), timestamp(end)
    call = call or market.report_call
    if start >= end:
        raise ValueError("Некорректный диапазон дневных цен")
    first_midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
    start = first_midnight+timedelta(days=1) if first_midnight < start else first_midnight
    end = end.replace(hour=0, minute=0, second=0, microsecond=0)
    key = "daily-v1:"+sha256(data_json({"id": instrument_id, "start": start, "end": end}).encode()).hexdigest()
    cached = repo.get_source_refresh(key) if repo is not None else None
    if cached and cached["status"] == "complete":
        payload = cached["payload"]
        rows = repo.get_historical_prices(instrument_id, end, end+timedelta(microseconds=1))
        stored = next((row["payload"] for row in rows if row["source"] == "mcp-daily-series-v1" and row["version"] == payload["version"]), payload)
        if "items" in stored:
            return [DailyObservation.from_dict(item) for item in stored["items"]], payload["version"], []
    items, quality = {}, []
    cursor = start
    try:
        while cursor <= end:
            next_at = cursor+min(timedelta(days=365*5), end-cursor)
            raw = await call("invest_get_candles", {"instrumentId": instrument_id, "interval": "CANDLE_INTERVAL_DAY",
                                                                  "from": (cursor-timedelta(days=2)).isoformat(), "to": next_at.isoformat(), "limit": 2400})
            currency = str(raw.get("priceCurrency") or "").lower()
            if not isinstance(raw.get("candles"), list) or not currency:
                raise ValueError("Отсутствует валюта или массив свечей")
            for row in raw["candles"]:
                if row.get("isComplete") is not True:
                    continue
                try:
                    day = date.fromisoformat(row["time"][:10])
                    available = datetime.combine(day+timedelta(days=2), time(), timezone.utc)
                    if not start <= available <= end:
                        continue
                    item = DailyObservation(day.isoformat(), available, decimal_value(row["close"]), decimal_value(row["volume"]), currency)
                    if item.day in items and items[item.day] != item:
                        quality.append("Противоречащие версии дневной свечи")
                    items[item.day] = item
                except (ValueError, TypeError, KeyError, OverflowError):
                    quality.append("Часть дневных свечей непригодна")
            if next_at == end:
                break
            cursor = next_at
    except (ToolError, ValueError, TypeError, AttributeError, OverflowError):
        quality.append("Дневная история недоступна или получена частично")
    ordered = sorted(items.values(), key=lambda item: item.available_at)
    quality = list(dict.fromkeys(quality))
    version = sha256(data_json({"items": ordered, "quality": quality}).encode()).hexdigest()
    if repo is not None:
        payload = {"items": [asdict(item) for item in ordered], "version": version, "quality": quality}
        received_at = datetime.now(timezone.utc)
        rows = repo.get_historical_prices(instrument_id, end, end+timedelta(microseconds=1))
        if not any(row["source"] == "mcp-daily-series-v1" and row["version"] == version for row in rows):
            repo.save_historical_price(instrument_id, end, "mcp-daily-series-v1", version, received_at, json.loads(data_json(payload)))
        repo.save_source_refresh(key, "partial" if quality else "complete", {"version": version, "quality": quality},
                                 received_at if not quality else None)
    return ordered, version, quality
