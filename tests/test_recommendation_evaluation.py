"""Обязательные критерии, человеческие вердикты, 85% и незавершённые оценки."""

from dataclasses import replace
from datetime import timedelta
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import _support  # noqa: F401
from investment_journal.recommendation_evaluation import CRITERIA, ExpertReview, evaluation_metric
from investment_journal.storage import JournalRepository
from investment_journal.evaluation_cli import main
from test_analysis_models import NOW


def review(proposal_id, positive=True, reviewer="expert", **kwargs):
    criteria = {name: True for name in CRITERIA}
    criteria["evidence"] = positive
    return ExpertReview("run", proposal_id, reviewer, 1, NOW, criteria, "Проверено по исходным данным", **kwargs)


class EvaluationTests(unittest.TestCase):
    def test_fifty_one_of_sixty_and_incomplete_evaluation(self):
        ids = [str(index) for index in range(60)]
        reviews = [review(identity, int(identity) < 51) for identity in ids]
        result = evaluation_metric(ids, reviews, 30)
        self.assertEqual(result["percent"], 85)
        self.assertTrue(result["success"])
        partial = evaluation_metric(ids, reviews[:-1], 30)
        self.assertEqual(partial["pending"], 1)
        self.assertEqual(partial["checked"], 59)
        self.assertIsNone(partial["success"])
        self.assertEqual(partial["status"], "intermediate")

    def test_zero_denominator_and_consensus_bound_to_review_versions(self):
        empty = evaluation_metric([], [], 30, [{"reason": "Нет данных"}])
        self.assertIsNone(empty["percent"])
        self.assertIsNone(empty["success"])
        first, second = review("proposal", reviewer="first"), review("proposal", False, "second")
        result = evaluation_metric(["proposal"], [first, second])
        self.assertEqual(result["disagreements"], ["proposal"])
        consensus = review("proposal", reviewer="arbiter", review_kind="consensus", resolves={"first": 1, "second": 1})
        self.assertEqual(evaluation_metric(["proposal"], [first, second, consensus])["percent"], 100)
        revised = replace(second, revision=2, reviewed_at=NOW+timedelta(hours=1))
        self.assertEqual(evaluation_metric(["proposal"], [first, second, revised, consensus])["pending"], 1)
        with self.assertRaises(ValueError):
            review("proposal", reviewer_type="model")

    def test_immutable_review_history_and_missing_criteria(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        repo.save_evaluation_dataset("dataset", [{"id": "case", "as_of": NOW}])
        repo.save_evaluation_run("run", "dataset", NOW, {"scenario_ids": ["case"], "proposals": [{"id": "proposal"}]})
        first = review("proposal")
        repo.save_expert_review(first)
        repo.save_expert_review(first)
        repo.save_expert_review(replace(first, revision=2, reason="Дополненная оценка"))
        self.assertEqual(len(repo.get_expert_reviews("run")), 2)
        with self.assertRaises(ValueError):
            repo.save_expert_review(replace(first, reason="Подмена"))
        with self.assertRaises(ValueError):
            replace(first, criteria={"goal": True})

    def test_review_file_import_is_idempotent_and_keeps_pending_counts(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)/"run"
            directory.mkdir()
            repo = JournalRepository(Path(folder)/"evaluation.sqlite3")
            repo.save_evaluation_dataset("dataset", [{"id": "case", "as_of": NOW}])
            repo.save_evaluation_run("run", "dataset", NOW, {"scenario_ids": ["case"], "proposals": [{"id": "first"}, {"id": "second"}], "refusals": []})
            repo.close()
            (directory/"protocol.json").write_text(json.dumps({"id": "run"}), encoding="utf-8")
            row = {"run_id": "run", "proposal_id": "first", "reviewer": "test-human", "revision": 1, "reviewed_at": None,
                   "criteria": {name: True for name in CRITERIA}, "reason": "Проверка импорта", "reviewer_type": "human"}
            path = directory/"reviews.json"
            path.write_text(json.dumps([row]), encoding="utf-8")
            for _ in range(2):
                with redirect_stdout(StringIO()):
                    main(["review", str(directory), str(path)])
            metric = json.loads((directory/"metric.json").read_text(encoding="utf-8"))
            self.assertEqual((metric["checked"], metric["pending"]), (1, 1))
            self.assertIsNone(metric["success"])


if __name__ == "__main__":
    unittest.main()
