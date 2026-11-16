"""Прогнозы цены на 12 календарных месяцев и датированные локальные артефакты."""

from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path

from .forecast_training import load_artifact, months_later, validate_artifact
from .models import GrowthForecast, data_json, decimal_value, timestamp


def predict_growth(artifact, instrument_id, as_of, price, currency, features, available_at, versions,
                   corporate_action_version="", corporate_actions_complete=False):
    """Сценарные границы используют 10/90-процентили ошибок временной проверки."""
    validate_artifact(artifact)
    as_of, price = timestamp(as_of), decimal_value(price)
    if price <= 0 or not currency or not instrument_id:
        raise ValueError("Исходная цена, валюта или инструмент непригодны")
    created = timestamp(artifact["created_at"])
    trained_until = timestamp(artifact["trained_until"])
    maximum_age = timedelta(days=artifact["maximum_age_days"])
    if created > as_of or trained_until > as_of or as_of-created > maximum_age or as_of-trained_until > maximum_age:
        raise ValueError("Версия модели недоступна на дату оценки или устарела")
    if artifact.get("usage") != "analysis":
        raise ValueError("Артефакт предназначен для проверки на синтетических данных")
    if not corporate_actions_complete or not corporate_action_version:
        raise ValueError("Согласованность цен с корпоративными действиями не подтверждена")
    names = artifact["feature_names"]
    values = []
    for index, name in enumerate(names):
        if (features.get(name) is None or not versions.get(name) or name not in available_at
                or timestamp(available_at[name]) > as_of):
            raise ValueError("Обязательные признаки недоступны на дату оценки")
        value = float(decimal_value(features[name]))
        if not math.isfinite(value):
            raise ValueError("Признак не является конечным числом")
        values.append((value-artifact["scaler_mean"][index])/artifact["scaler_scale"][index])
    estimate = artifact["intercept"]+sum(value*coefficient for value, coefficient in zip(values, artifact["coef"]))
    try:
        ratios = [math.exp(estimate+error) for error in (0, artifact["error_quantiles"]["0.1"], artifact["error_quantiles"]["0.9"])]
    except OverflowError as exc:
        raise ValueError("Прогноз выходит за конечную численную область") from exc
    if not all(math.isfinite(value) and value > 0 for value in ratios):
        raise ValueError("Прогнозная цена непригодна")
    base, low, high = [price*Decimal(str(value)) for value in ratios]
    inputs = {"instrument_id": instrument_id, "as_of": as_of, "model": artifact["version"], "price": price,
              "currency": currency, "features": features, "available_at": available_at, "versions": versions,
              "corporate_action_version": corporate_action_version}
    identity = sha256(data_json(inputs).encode()).hexdigest()
    return GrowthForecast(identity, instrument_id, as_of, months_later(as_of, 12), artifact["version"], currency.lower(), price,
                          base, low, high, evidence_ids=sorted(set(versions.values()) | {corporate_action_version}), limitations=[
                              "Сценарный диапазон по эмпирическим 10/90-процентилям ошибок временной проверки",
                              "Вероятностное покрытие диапазона не калибровано",
                              "Ценовой прогноз без дивидендов, будущие корпоративные изменения требуют пересчёта единиц",
                              *artifact.get("limitations", [])])


class ForecastService:
    def __init__(self, repo, directory=None):
        self.repo = repo
        self.directory = Path(directory) if directory else None
        self._frozen_artifacts = None
        self._frozen_bases = None

    def for_request(self, mode, as_of):
        frozen = ForecastService(self.repo, self.directory)
        feature_set = "technical-v1" if mode == "technical" else "combined-v1"
        frozen._frozen_artifacts = {feature_set: self._artifacts(feature_set, as_of)}
        frozen._frozen_bases = self._read_price_bases()
        return frozen

    def _artifacts(self, feature_set, as_of):
        if self._frozen_artifacts is not None:
            return self._frozen_artifacts.get(feature_set, [])
        if self.directory is None or not self.directory.is_dir():
            return []
        artifacts = []
        for path in sorted(self.directory.glob("*.json")):
            if path.name == "price-bases.json":
                continue
            try:
                artifact = load_artifact(path)
                if artifact["feature_set"] == feature_set and timestamp(artifact["created_at"]) <= as_of and artifact.get("usage") == "analysis":
                    artifacts.append(artifact)
            except (ValueError, TypeError, KeyError, OSError):
                continue
        return sorted(artifacts, key=lambda item: (timestamp(item["created_at"]), item["version"]), reverse=True)

    def _read_price_bases(self):
        if self.directory is not None:
            try:
                rows = json.loads((self.directory/"price-bases.json").read_text(encoding="utf-8"))
                return rows if isinstance(rows, list) else []
            except (ValueError, OSError):
                pass
        return []

    def _price_basis(self, entry, as_of):
        """Подтверждение отсутствия изменений единиц на диапазоне исходных свечей.

Файл содержит публичные ссылки, время проверки, начало/конец покрытия и
версию. При наличии сплита исходный ряд требует отдельного согласования.
"""
        if self.directory is None:
            return None
        try:
            rows = self._frozen_bases if self._frozen_bases is not None else self._read_price_bases()
            for row in rows:
                if (row["instrument_id"] == entry.instrument_id and row["complete"] is True and row["no_unit_changes"] is True
                        and row["version"] and row["source_url"].startswith("https://") and timestamp(row["observed_at"]) <= as_of
                        and entry.series and row["from"] <= entry.series[0].day and row["to"] >= entry.series[-1].day):
                    return row
        except (ValueError, TypeError, KeyError, OSError, AttributeError):
            pass
        return None

    def forecast(self, user_id, account_id, entry, fundamental, technical, mode, as_of, facts):
        feature_set = "technical-v1" if mode == "technical" else "combined-v1"
        artifacts = self._artifacts(feature_set, as_of)
        if not artifacts:
            return {"available": False, "reason": "Проверенный артефакт модели для режима отсутствует"}
        artifact = artifacts[0]
        try:
            basis = self._price_basis(entry, as_of)
            if basis is None:
                raise ValueError("Корпоративная согласованность исходной истории не подтверждена")
            features = dict(technical.features)
            available_at = {name: max(row.available_at for row in entry.series) for name in features}
            versions = {name: technical.series_version for name in features}
            for name, metric in fundamental.metrics.items():
                features[name] = metric.value
                if metric.capitalization_version and metric.capitalization_scope != "issuer":
                    continue
                inputs = [fact for fact in facts if fact.id in metric.fact_ids]
                if len({fact.id for fact in inputs}) != len(set(metric.fact_ids)):
                    continue
                available_at[name] = max(timestamp(fact.available_at) for fact in inputs)
                if metric.capitalization_version:
                    available_at[name] = as_of
                versions[name] = sha256(data_json(metric).encode()).hexdigest()
            if (entry.price is None or not entry.price_at or not timedelta(0) <= as_of-timestamp(entry.price_at) <= timedelta(days=7)
                    or technical.reasons):
                raise ValueError("Цена или техническая история неполны")
            forecast = predict_growth(artifact, entry.instrument_id, as_of, entry.price, entry.currency, features, available_at, versions,
                                      basis["version"], True)
            self.repo.save_forecast_model(artifact["version"], timestamp(artifact["created_at"]), timestamp(artifact["trained_until"]), artifact)
            self.repo.save_forecast(user_id, account_id, forecast)
            card = {key: artifact[key] for key in ("version", "feature_set", "created_at", "trained_until", "methodology", "error_quantiles", "limitations")}
            card["validation"] = {key: value for key, value in artifact["validation"].items() if key != "predictions"}
            return {"available": True, "forecast": forecast, "model_card": card,
                    "price_basis": basis, "inputs": {"features": features, "available_at": available_at, "versions": versions}}
        except (ValueError, TypeError, KeyError, OverflowError):
            return {"available": False, "reason": "Версия модели, корпоративная согласованность или обязательные признаки непригодны",
                    "model_version": artifact["version"]}
