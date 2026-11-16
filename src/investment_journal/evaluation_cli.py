"""Запуск эксперимента, материалы эксперта и ввод человеческих вердиктов."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

from .config import llm_settings, load_env
from .evaluation_replay import run_evaluation
from .llm import ChatCompletionsClient, LlmError
from .models import data_json
from .recommendation_evaluation import CRITERIA, ExpertReview, evaluation_metric
from .stock_formatting import candidate_lines
from .storage import JournalRepository


def score_run(repo, run_id):
    run = repo.get_evaluation_run(run_id)
    if not run:
        raise ValueError("Эксперимент отсутствует")
    payload = run["payload"]
    return evaluation_metric([row["id"] for row in payload["proposals"]], repo.get_expert_reviews(run_id),
                             len(payload["scenario_ids"]), payload["refusals"])


def export_run(result, dataset, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory/"protocol.json").write_text(json.dumps(json.loads(data_json(result)), ensure_ascii=False, indent=2), encoding="utf-8")
    cases = {case["id"]: case for case in dataset["scenarios"]}
    outputs = {row["scenario_id"]: row for row in result["outputs"]}
    lines = ["# Проверка обоснованности предложений", "", f"Эксперимент {result['id']}", f"Набор {dataset['version']}",
             f"Сценариев {len(cases)}, предложений {len(result['proposals'])}, отказов {len(result['refusals'])}", "",
             f"Модель {result['llm_model']} · режим провайдера {result['llm_calls']['provider_mode']}", "",
             "Публичные котировки зафиксированы для трёх банков. Портфели, профили и ограничения счёта сконструированы.",
             "Состояния отказа используют зафиксированные контрольные изменения публичных входов.",
             "Каждое предложение оценивается человеком по цели, горизонту, риску, доказательствам и существенным ограничениям.",
             "Положительный вердикт требует выполнения всех критериев. В шаблоне оценки заполните true или false и пояснение.", "",
             "## Ограничения", "", *["- "+value for value in result["limitations"]]]
    reviews = []
    for index, proposal in enumerate(result["proposals"], 1):
        case = cases[proposal["scenario_id"]]
        profile = case["profile"]
        lines.extend(["", f"## Предложение {index} · {proposal['id']}", "", f"Сценарий {case['id']} · состояние {case['state']} · дата {case['as_of']}"])
        if profile:
            lines.extend([f"Цель · {profile['goal']}", f"Горизонт · {profile['horizon_months']} месяцев, риск · {profile['risk_level']}",
                          f"Лимиты · просадка {profile['max_drawdown_pct']}%, позиция {profile['max_position_pct']}%, сектор {profile['max_sector_pct']}%",
                          f"Минимальный остаток · {profile['minimum_cash']} {profile['currency'].upper()}"])
        content = proposal["content"]
        if proposal["kind"] == "analysis":
            lines.extend(["", content["text"], "", "Риски", *content["risks"], "Условия пересмотра", *content["reconsider_when"], "", "Подтверждённые показатели"])
            context = outputs[case["id"]]["payload"]["advice"]["context"]
            for key in content["metric_ids"]:
                value = context["metrics"][key]
                lines.append(f"- {key} — {value['value']} {value['unit']}")
            lines.extend(["", "Условия исходных вариантов"])
            for scenario in context["scenarios"]:
                if scenario["id"] in content["scenario_ids"]:
                    lines.extend(scenario["conditions"])
        else:
            lines.extend(candidate_lines(content, case["mode"]))
        lines.append("Исходный портфель · "+data_json(case["snapshot"]))
        reviews.append({"run_id": result["id"], "proposal_id": proposal["id"], "reviewer": "", "revision": 1,
                        "reviewed_at": None, "criteria": {key: None for key in sorted(CRITERIA)}, "reason": "",
                        "review_kind": "individual", "resolves": {}, "reviewer_type": "human"})
    lines.extend(["", "## Отказы", ""])
    lines.extend(f"- {row['scenario_id']} · {row['reason']}" for row in result["refusals"])
    (directory/"expert_review.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    (directory/"reviews.template.json").write_text(json.dumps(reviews, ensure_ascii=False, indent=2), encoding="utf-8")


async def execute(dataset_path, output):
    settings = llm_settings(load_env())
    if settings is None:
        raise ValueError("Настройте LLM-провайдер перед запуском эксперимента")
    dataset_path, output = Path(dataset_path), Path(output)
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    repo = JournalRepository(output/"evaluation.sqlite3")
    client = ChatCompletionsClient(settings)
    try:
        try:
            await client.complete([{"role": "user", "content": "Ответь одним словом: готово"}], [])
        except LlmError as error:
            pending = {"state": "provider_unavailable", "checked_at": datetime.now(timezone.utc), "dataset_version": dataset["version"],
                       "scenario_count": len(dataset["scenarios"]), "model": settings.model,
                       "http_status": error.status, "error_code": error.code, "expert_reviews": "pending", "percent": None}
            (output/"pending.json").write_text(data_json(pending), encoding="utf-8")
            return pending
        result = await run_evaluation(dataset, dataset_path.parent, repo, client, settings.model, "configured")
        directory = output/result["id"]
        export_run(result, dataset, directory)
        score = score_run(repo, result["id"])
        (directory/"metric.json").write_text(data_json(score), encoding="utf-8")
        return {"run_id": result["id"], "directory": str(directory), "llm_calls": result["llm_calls"], "metric": score}
    finally:
        await client.aclose()
        repo.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Экспертная проверка инвестиционных предложений")
    subparsers = parser.add_subparsers(dest="action", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("dataset", type=Path)
    run.add_argument("--output", type=Path, default=Path("experiments"))
    review = subparsers.add_parser("review")
    review.add_argument("directory", type=Path)
    review.add_argument("reviews", type=Path)
    score = subparsers.add_parser("score")
    score.add_argument("directory", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.action == "run":
        result = asyncio.run(execute(arguments.dataset, arguments.output))
    else:
        directory = arguments.directory
        protocol = json.loads((directory/"protocol.json").read_text(encoding="utf-8"))
        repo = JournalRepository(directory.parent/"evaluation.sqlite3")
        try:
            if arguments.action == "review":
                rows = json.loads(arguments.reviews.read_text(encoding="utf-8"))
                previous = {(review.proposal_id, review.reviewer, review.revision): review for review in repo.get_expert_reviews(protocol["id"])}
                reviews = []
                for row in rows:
                    existing = previous.get((row["proposal_id"], row["reviewer"], row["revision"]))
                    reviewed_at = row.get("reviewed_at") or (existing.reviewed_at if existing else datetime.now(timezone.utc))
                    reviews.append(ExpertReview.from_dict({**row, "reviewed_at": reviewed_at}))
                if any(review.run_id != protocol["id"] for review in reviews):
                    raise ValueError("Оценка относится к другому эксперименту")
                for review in reviews:
                    repo.save_expert_review(review)
            result = score_run(repo, protocol["id"])
            (directory/"metric.json").write_text(data_json(result), encoding="utf-8")
        finally:
            repo.close()
    print(data_json(result))
    if arguments.action == "run" and result.get("state") == "provider_unavailable":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
