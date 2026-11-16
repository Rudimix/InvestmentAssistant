"""Воспроизводимая Ridge-модель с датированными признаками и временной проверкой."""

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
import math
import os
import argparse
from pathlib import Path
from datetime import timezone
from uuid import uuid4

import numpy as np
import sklearn
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .models import AnalyticalRecord, data_json, decimal_value, timestamp


TECHNICAL_FEATURES = ("momentum_1m", "momentum_3m", "momentum_12m", "sma_spread", "rsi14", "volatility", "drawdown", "relative_volume")
FEATURE_SETS = {"technical-v1": TECHNICAL_FEATURES,
                "combined-v1": TECHNICAL_FEATURES+("pe", "pb", "revenue_growth", "net_profit_growth")}


def months_later(at, months):
    at = timestamp(at)
    year, month = divmod(at.year*12+at.month-1+months, 12)
    return at.replace(year=year, month=month+1, day=min(at.day, monthrange(year, month+1)[1]))


@dataclass
class ForecastExample(AnalyticalRecord):
    id: str
    instrument_id: str
    as_of: datetime
    target_at: datetime
    target_available_at: datetime
    source_price: Decimal
    target_price: Decimal
    currency: str
    features: dict[str, Decimal]
    feature_available_at: dict[str, datetime]
    feature_versions: dict[str, str]
    universe_version: str
    universe_available_at: datetime
    corporate_action_version: str
    corporate_actions_complete: bool
    limitations: list[str] = field(default_factory=list)

    def __post_init__(self):
        super().__post_init__()
        for name in ("as_of", "target_at", "target_available_at", "universe_available_at"):
            setattr(self, name, timestamp(getattr(self, name)))
        self.source_price, self.target_price = decimal_value(self.source_price), decimal_value(self.target_price)
        self.features = {key: decimal_value(value) for key, value in self.features.items()}
        self.feature_available_at = {key: timestamp(value) for key, value in self.feature_available_at.items()}
        if (not self.id or not self.instrument_id or not self.currency or self.target_at != months_later(self.as_of, 12)
                or self.target_available_at < self.target_at or self.source_price <= 0 or self.target_price <= 0):
            raise ValueError("Непригодный пример прогноза")


def eligible_examples(examples, feature_set, created_at):
    names = FEATURE_SETS[feature_set]
    created_at = timestamp(created_at)
    accepted, rejected = [], {}
    if len({example.id for example in examples}) != len(examples):
        raise ValueError("Идентификаторы примеров повторяются")
    for example in examples:
        reasons = []
        if example.target_available_at > created_at:
            reasons.append("horizon_incomplete")
        if not example.corporate_actions_complete or not example.corporate_action_version:
            reasons.append("corporate_actions_incomplete")
        if not example.universe_version or example.universe_available_at > example.as_of:
            reasons.append("universe_unavailable_at_origin")
        for name in names:
            if name not in example.features or not math.isfinite(float(example.features[name])):
                reasons.append("missing_or_nonfinite_feature:"+name)
            elif (name not in example.feature_available_at or example.feature_available_at[name] > example.as_of
                    or not example.feature_versions.get(name)):
                reasons.append("future_or_unversioned_feature:"+name)
        if reasons:
            rejected[example.id] = reasons
        else:
            accepted.append(example)
    return sorted(accepted, key=lambda item: (item.as_of, item.instrument_id, item.id)), rejected


def _fit(examples, names, alpha):
    x = np.asarray([[float(example.features[name]) for name in names] for example in examples])
    y = np.asarray([math.log(float(example.target_price/example.source_price)) for example in examples])
    pipeline = make_pipeline(StandardScaler(), Ridge(alpha=alpha, solver="svd"))
    pipeline.fit(x, y)
    return pipeline


def walk_forward(examples, names, alpha=1.0, minimum_train=40, minimum_validation=10):
    """Три последовательных блока, эмбарго 12 календарных месяцев по дате признаков.

Обучающая цель и её фактическая доступность должны строго предшествовать
началу проверочного блока. Каждый scaler обучается только на своей тренировке.
"""
    dates = sorted({example.as_of for example in examples})
    blocks = np.array_split(np.asarray(dates[len(dates)//2:], dtype=object), 3)
    predictions, folds = [], []
    for block in blocks:
        if not len(block):
            continue
        start, end = block[0], block[-1]
        embargo = months_later(start, -12)
        train = [example for example in examples if example.as_of < embargo and example.target_at < start
                 and example.target_available_at < start]
        validation = [example for example in examples if start <= example.as_of <= end]
        if len(train) < minimum_train or len(validation) < minimum_validation:
            continue
        model = _fit(train, names, alpha)
        predicted = model.predict([[float(example.features[name]) for name in names] for example in validation])
        for example, estimate in zip(validation, predicted):
            actual = math.log(float(example.target_price/example.source_price))
            predictions.append({"id": example.id, "predicted": float(estimate), "actual": actual, "residual": actual-float(estimate)})
        folds.append({"train_ids": [example.id for example in train], "validation_ids": [example.id for example in validation],
                      "validation_start": start.isoformat(), "validation_end": end.isoformat(), "embargo_until": embargo.isoformat(),
                      "scaler_mean": model[0].mean_.tolist(), "scaler_scale": model[0].scale_.tolist()})
    return predictions, folds


def train_model(examples, feature_set, created_at, alpha=1.0, minimum_train=40, minimum_validation=10):
    """Обучить фиксированный набор признаков и сохранить переносимые JSON-коэффициенты."""
    if feature_set not in FEATURE_SETS or not math.isfinite(alpha) or alpha <= 0 or minimum_train < 2 or minimum_validation < 2:
        raise ValueError("Непригодная методика обучения")
    examples = list(examples)
    accepted, rejected = eligible_examples(examples, feature_set, created_at)
    names = FEATURE_SETS[feature_set]
    if len(accepted) < minimum_train:
        raise ValueError("Недостаточно завершённых подтверждённых примеров")
    predictions, folds = walk_forward(accepted, names, alpha, minimum_train, minimum_validation)
    if len(folds) < 2 or len(predictions) < minimum_validation*2:
        raise ValueError("Недостаточно последовательных временных проверок")
    residuals = np.asarray([item["residual"] for item in predictions])
    actual = np.asarray([item["actual"] for item in predictions])
    correction = float(np.median(residuals))
    model = _fit(accepted, names, alpha)
    body = {"schema_version": 1, "methodology": "ridge-log-price-12m-v1", "feature_set": feature_set, "feature_names": list(names),
            "created_at": timestamp(created_at).isoformat(), "trained_until": max(item.target_available_at for item in accepted).isoformat(),
            "alpha": alpha, "sklearn_version": sklearn.__version__, "minimum_train": minimum_train, "minimum_validation": minimum_validation,
            "dataset_version": sha256(data_json(sorted(examples, key=lambda item: item.id)).encode()).hexdigest(),
            "accepted_ids": [item.id for item in accepted], "rejected": rejected, "folds": folds,
            "coef": model[1].coef_.tolist(), "intercept": float(model[1].intercept_)+correction,
            "scaler_mean": model[0].mean_.tolist(), "scaler_scale": model[0].scale_.tolist(),
            "error_quantiles": {"0.1": float(np.quantile(residuals, 0.1))-correction, "0.9": float(np.quantile(residuals, 0.9))-correction},
            "bias_correction": {"method": "median_walk_forward_residual", "offset_log": correction,
                                "validation_metrics": "before_correction", "post_correction_holdout": False},
            "validation": {"count": len(predictions), "mae_log": float(np.mean(np.abs(residuals))), "bias_log": float(np.mean(-residuals)),
                           "persistence_mae_log": float(np.mean(np.abs(actual))), "persistence_bias_log": float(np.mean(-actual)),
                           "predictions": predictions}, "maximum_age_days": 180,
            "limitations": sorted({limitation for example in accepted for limitation in example.limitations})}
    body["usage"] = "verification_only" if "synthetic_fixture" in body["limitations"] else "analysis"
    if not all(math.isfinite(value) for value in body["coef"]+body["scaler_mean"]+body["scaler_scale"]+[body["intercept"]]):
        raise ValueError("Обучение создало неконечные коэффициенты")
    return {**body, "version": sha256(data_json(body).encode()).hexdigest()}


def validate_artifact(artifact):
    body = {key: value for key, value in artifact.items() if key != "version"}
    if artifact.get("version") != sha256(data_json(body).encode()).hexdigest() or artifact.get("schema_version") != 1:
        raise ValueError("Версия артефакта повреждена")
    names = FEATURE_SETS.get(artifact.get("feature_set"))
    if names is None or artifact.get("feature_names") != list(names):
        raise ValueError("Набор признаков артефакта неизвестен")
    for key in ("coef", "scaler_mean", "scaler_scale"):
        if len(artifact[key]) != len(names) or not all(math.isfinite(float(value)) for value in artifact[key]):
            raise ValueError("Некорректные коэффициенты артефакта")
    if any(value <= 0 for value in artifact["scaler_scale"]):
        raise ValueError("Некорректный масштаб признаков")
    numbers = [artifact["intercept"], artifact["error_quantiles"]["0.1"], artifact["error_quantiles"]["0.9"]]
    if (not all(math.isfinite(float(value)) for value in numbers)
            or not artifact["error_quantiles"]["0.1"] <= 0 <= artifact["error_quantiles"]["0.9"]
            or type(artifact["maximum_age_days"]) is not int or artifact["maximum_age_days"] <= 0
            or timestamp(artifact["trained_until"]) > timestamp(artifact["created_at"])):
        raise ValueError("Непригодные сценарные границы или даты модели")
    return artifact


def save_artifact(directory, artifact):
    validate_artifact(artifact)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory/(artifact["version"]+".json")
    temporary = directory/("."+uuid4().hex+".tmp")
    try:
        temporary.write_text(data_json(artifact), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_artifact(path):
    return validate_artifact(json.loads(Path(path).read_text(encoding="utf-8")))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Обучение датированной прогнозной модели Ridge")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--feature-set", choices=FEATURE_SETS, required=True)
    parser.add_argument("--created-at", default=None)
    parser.add_argument("--output", type=Path, default=Path("forecast-models"))
    arguments = parser.parse_args(argv)
    dataset = json.loads(arguments.dataset.read_text(encoding="utf-8"))
    if dataset.get("schema_version") != 1 or not isinstance(dataset.get("examples"), list):
        parser.error("Неподдерживаемый формат набора")
    created_at = timestamp(arguments.created_at) if arguments.created_at else datetime.now(timezone.utc)
    examples = [ForecastExample.from_dict(row) for row in dataset["examples"]]
    artifact = train_model(examples, arguments.feature_set, created_at)
    path = save_artifact(arguments.output, artifact)
    print(json.dumps({"artifact": str(path), "version": artifact["version"], "usage": artifact["usage"],
                      "validation": {key: value for key, value in artifact["validation"].items() if key != "predictions"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
