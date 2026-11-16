"""Фиксированные наборы, зрелость горизонтов и изоляция временной проверки."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import math
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
import unittest

import _support  # noqa: F401
from investment_journal.forecast_training import (
    FEATURE_SETS, ForecastExample, eligible_examples, load_artifact, months_later, save_artifact, train_model,
)


CREATED = datetime(2026, 11, 16, tzinfo=timezone.utc)


def fixed_examples():
    result = []
    for month in range(120):
        at = months_later(datetime(2015, 1, 1, tzinfo=timezone.utc), month)
        features = {name: D(str((month % 12+1)*(index+1)/10)) for index, name in enumerate(FEATURE_SETS["combined-v1"])}
        target = D(str(100*math.exp(0.002*month+0.02*math.sin(month))))
        result.append(ForecastExample(str(month), "SYNTHETIC", at, months_later(at, 12), months_later(at, 12)+timedelta(days=2),
            D(100), target, "rub", features, {name: at for name in features}, {name: "fixture-v1" for name in features},
            "fixture-universe-v1", at, "fixture-actions-v1", True, ["synthetic_fixture"]))
    return result


class ForecastTrainingTests(unittest.TestCase):
    def test_two_feature_sets_reproducible_artifact_and_tampering(self):
        examples = fixed_examples()
        for feature_set in FEATURE_SETS:
            artifact = train_model(examples, feature_set, CREATED)
            self.assertEqual(artifact, train_model(examples, feature_set, CREATED))
            self.assertEqual(artifact["feature_names"], list(FEATURE_SETS[feature_set]))
            self.assertEqual(artifact["usage"], "verification_only")
            self.assertLessEqual(artifact["error_quantiles"]["0.1"], 0)
            self.assertGreaterEqual(artifact["error_quantiles"]["0.9"], 0)
            with TemporaryDirectory() as folder:
                path = save_artifact(folder, artifact)
                self.assertEqual(load_artifact(path), artifact)
                self.assertEqual(save_artifact(folder, artifact), path)
                self.assertEqual(len(list(Path(folder).iterdir())), 1)
            with self.assertRaises(ValueError):
                save_artifact("unused", {**artifact, "intercept": 999})

    def test_future_fact_missing_feature_unmatured_and_corporate_actions(self):
        example = fixed_examples()[0]
        name = FEATURE_SETS["technical-v1"][0]
        future = replace(example, feature_available_at={**example.feature_available_at, name: example.as_of+timedelta(seconds=1)})
        self.assertFalse(eligible_examples([future], "technical-v1", CREATED)[0])
        missing = replace(example, features={key: value for key, value in example.features.items() if key != name})
        self.assertFalse(eligible_examples([missing], "technical-v1", CREATED)[0])
        self.assertFalse(eligible_examples([replace(example, corporate_actions_complete=False)], "technical-v1", CREATED)[0])
        self.assertFalse(eligible_examples([example], "technical-v1", example.target_at)[0])
        self.assertFalse(eligible_examples([replace(example, universe_available_at=example.as_of+timedelta(days=1))], "technical-v1", CREATED)[0])

    def test_purged_chronological_validation_scaler_and_persistence(self):
        examples = fixed_examples()
        by_id = {example.id: example for example in examples}
        artifact = train_model(examples, "technical-v1", CREATED)
        self.assertEqual(len(artifact["folds"]), 3)
        for fold in artifact["folds"]:
            start = datetime.fromisoformat(fold["validation_start"])
            train = [by_id[key] for key in fold["train_ids"]]
            self.assertTrue(all(item.target_available_at < start for item in train))
            self.assertTrue(all(item.as_of < months_later(start, -12) for item in train))
            self.assertFalse(set(fold["train_ids"]) & set(fold["validation_ids"]))
            expected = float(mean(item.features[FEATURE_SETS["technical-v1"][0]] for item in train))
            self.assertAlmostEqual(fold["scaler_mean"][0], expected)
        results = artifact["validation"]["predictions"]
        self.assertAlmostEqual(artifact["validation"]["persistence_mae_log"], mean(abs(item["actual"]) for item in results))
        self.assertAlmostEqual(artifact["validation"]["mae_log"], mean(abs(item["residual"]) for item in results))


if __name__ == "__main__":
    unittest.main()
