"""Наблюдение завершённых прогнозов с проверкой целевой цены и единиц акции."""

from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
import json

from .historical_prices import historical_prices, price_before
from .models import CorporateEvent, data_json, timestamp


def observe_forecast(forecast, observed_at, price=None, events=(), corporate_complete=False, corporate_version=""):
    observed_at = timestamp(observed_at)
    result = {"forecast_id": forecast.id, "observed_at": observed_at, "target_at": forecast.target_at,
              "model_version": forecast.model_version, "status": "intermediate", "actual_price": None}
    if observed_at < forecast.target_at:
        result["reason"] = "Двенадцатимесячный горизонт ещё не завершён"
        return result
    result["status"] = "unavailable"
    if (price is None or price.instrument_id != forecast.instrument_id or price.currency.lower() != forecast.currency.lower()
            or price.retrieved_at > observed_at or not timedelta(0) <= forecast.target_at-price.candle_end <= timedelta(days=7)
            or price.close <= 0 or not price.version):
        result["reason"] = "Подтверждённая цена завершения горизонта недоступна"
        return result
    if not corporate_complete or not corporate_version:
        result["reason"] = "Согласованность единиц цены на горизонте не подтверждена"
        return result
    ratio = Decimal(1)
    versions = []
    for event in events:
        if event.instrument_id != forecast.instrument_id or not forecast.as_of < event.effective_at <= price.candle_end:
            continue
        if (not event.confirmed or event.available_at is None or event.available_at > observed_at or not event.source_version
                or event.kind not in {"split", "consolidation"} or event.ratio is None or event.ratio <= 0):
            result["reason"] = "Корпоративное изменение требует уточнения единиц цены"
            return result
        ratio *= event.ratio
        versions.append(event.source_version)
    actual = price.close*ratio
    if any(value is None for value in (forecast.base_price, forecast.low_price, forecast.high_price)):
        result["reason"] = "Исходный сценарный прогноз неполон"
        return result
    position = "below" if actual < forecast.low_price else "above" if actual > forecast.high_price else "inside"
    result.update(status="mature", actual_price=actual, price=asdict(price), split_factor=ratio,
                  actual_change_pct=(actual/forecast.source_price-1)*100,
                  base_change_pct=(forecast.base_price/forecast.source_price-1)*100,
                  error_pp=(actual-forecast.base_price)/forecast.source_price*100,
                  log_error=(actual/forecast.base_price).ln(), range_position=position,
                  corporate_version=corporate_version, corporate_event_versions=versions)
    return result


def observation_lines(observation):
    if not observation:
        return []
    lines = [f"Наблюдение исходного прогноза на {observation['observed_at']}"]
    if observation["status"] != "mature":
        return lines+[observation["reason"]]
    labels = {"below": "ниже сценарного диапазона", "inside": "в сценарном диапазоне", "above": "выше сценарного диапазона"}
    lines.extend([f"Фактическая цена в единице исходной акции — {Decimal(str(observation['actual_price'])):.2f}",
                  f"Фактическое изменение — {Decimal(str(observation['actual_change_pct'])):.2f}%",
                  f"Ошибка базового изменения — {Decimal(str(observation['error_pp'])):.2f} п.п.",
                  labels[observation["range_position"]]])
    return lines


async def refresh_forecast(repo, market, forecasts, user_id, account_id, forecast_id, observed_at):
    forecast = repo.get_forecast(user_id, account_id, forecast_id)
    if forecast is None:
        raise ValueError("Прогноз недоступен для выбранного счёта")
    observed_at = timestamp(observed_at)
    price, events, proof = None, [], None
    if observed_at >= forecast.target_at:
        batch = await historical_prices(market, forecast.instrument_id, forecast.target_at-timedelta(days=7), forecast.target_at,
                                        repo, now=observed_at)
        price = price_before(batch, forecast.target_at)
        if forecasts.directory:
            try:
                rows = json.loads((forecasts.directory/"price-bases.json").read_text(encoding="utf-8"))
                proof = next((row for row in rows if row["instrument_id"] == forecast.instrument_id and row["complete"] is True
                              and row["version"] and row["source_url"].startswith("https://") and timestamp(row["observed_at"]) <= observed_at
                              and row["from"] <= forecast.as_of.date().isoformat() and row["to"] >= forecast.target_at.date().isoformat()), None)
                if proof and proof.get("no_unit_changes") is not True:
                    events = [CorporateEvent.from_dict(row) for row in proof.get("events", [])]
                    if not events:
                        proof = None
            except (ValueError, TypeError, KeyError, OSError, AttributeError):
                proof = None
    result = observe_forecast(forecast, observed_at, price, events, bool(proof), proof["version"] if proof else "")
    if proof:
        result["corporate_proof"] = proof
    result = json.loads(data_json(result))
    repo.save_forecast_observation(user_id, account_id, forecast_id, observed_at, result)
    return result
