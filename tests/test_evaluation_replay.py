"""Зафиксированные 30 сценариев проходят штатный путь отчётов с подменой LLM."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import _support  # noqa: F401
from investment_journal.evaluation_replay import run_evaluation, validate_dataset
from investment_journal.models import data_json
from investment_journal.storage import JournalRepository
from investment_journal.evaluation_cli import export_run


DIRECTORY = Path(__file__).parent/"fixtures"
DATASET = json.loads((DIRECTORY/"evaluation_scenarios.json").read_text(encoding="utf-8"))


class ContextLlm:
    async def complete(self, messages, tools):
        context = json.loads(messages[-1]["content"])
        scenario = context["scenarios"][0]
        metric = f"scenario:{scenario['id']}:net_credit"
        proposal = {"text": "Рассмотрите пополнение денежного резерва на {{metric:"+metric+"}} при наличии дополнительных средств.",
                    "profile_refs": scenario["profile_refs"], "metric_ids": [metric], "evidence_ids": [], "scenario_ids": [scenario["id"]],
                    "risks": ["Денежный остаток подвержен инфляционному риску."], "reconsider_when": ["Изменились цель или потребность в деньгах."]}
        return {"content": json.dumps({"proposals": [proposal], "limitations": []}, ensure_ascii=False)}


class EvaluationReplayTests(unittest.IsolatedAsyncioTestCase):
    def test_dataset_sources_dates_modes_and_refusal_coverage(self):
        validate_dataset(DATASET, DIRECTORY)
        self.assertEqual(len(DATASET["scenarios"]), 30)
        future = deepcopy(DATASET)
        future["scenarios"][0]["source_available_at"]["0"] = "2099-01-01T00:00:00Z"
        future["version"] = sha256(data_json({key: value for key, value in future.items() if key != "version"}).encode()).hexdigest()
        with self.assertRaises(ValueError):
            validate_dataset(future, DIRECTORY)

    async def test_full_fixed_replay_records_every_output_and_refusal(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        result = await run_evaluation(DATASET, DIRECTORY, repo, ContextLlm(), "test-double")
        self.assertEqual(len(result["outputs"]), 30)
        self.assertTrue(all(row["state"] == "delivered" for row in result["outputs"]))
        self.assertGreater(len(result["proposals"]), 0)
        self.assertGreater(len(result["refusals"]), 0)
        self.assertEqual({row["scenario_id"] for row in result["outputs"]}, {row["id"] for row in DATASET["scenarios"]})
        self.assertEqual(result["expert_reviews"], "pending")
        self.assertGreater(result["llm_calls"]["responses"], 0)
        self.assertEqual(result["llm_calls"]["provider_mode"], "test_double")
        with TemporaryDirectory() as directory:
            export_run(result, DATASET, directory)
            template = json.loads((Path(directory)/"reviews.template.json").read_text(encoding="utf-8"))
            self.assertEqual(len(template), len(result["proposals"]))
            self.assertTrue(all(all(value is None for value in row["criteria"].values()) for row in template))
            self.assertIn("test_double", (Path(directory)/"expert_review.md").read_text(encoding="utf-8"))
        saved = repo.get_evaluation_run(result["id"])
        self.assertEqual(saved["dataset_version"], DATASET["version"])


if __name__ == "__main__":
    unittest.main()
