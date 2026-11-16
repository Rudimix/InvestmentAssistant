"""Рассчитанные варианты и проверенные предложения в пользовательском отчёте."""

from .formatting import money
from .models import decimal_value

EFFECT_LABELS = {"net_credit": "Чистое пополнение", "total_after": "Условная стоимость после изменения",
                 "cash_after": "Денежный остаток после изменения", "sale_amount": "Сумма сокращения позиции",
                 "estimated_cost": "Расчётные расходы", "weight_after": "Доля после изменения",
                 "sale_amount_before_costs": "Сумма сокращения до расходов", "weight_after_before_costs": "Доля до расходов",
                 "total_after_before_costs": "Стоимость до расходов", "cash_after_before_costs": "Остаток до расходов"}


def advice_lines(bundle: dict) -> list[str]:
    lines = []
    for scenario in bundle.get("scenarios", []):
        lines.append(scenario.title)
        if not scenario.admissible:
            lines.append("Для допустимости варианта требуется уточнение условий")
        for key, value in scenario.effects.items():
            unit = scenario.units[key]
            text = f"{decimal_value(value):.2f}%" if unit == "%" else money(decimal_value(value), unit)
            lines.append(f"{EFFECT_LABELS.get(key, key)} — {text}")
        lines.extend(scenario.conditions)
    result = bundle.get("result", {})
    if not result.get("available"):
        lines.append(result.get("reason", "AI-предложения недоступны"))
        return lines
    evidence = {item["id"]: item for item in bundle.get("context", {}).get("evidence", [])}
    for index, item in enumerate(result.get("proposals", []), 1):
        lines.extend([f"Предложение с учётом профиля · {index}", item["text"], "Риски", *item["risks"], "Условия пересмотра", *item["reconsider_when"]])
        for key in item["evidence_ids"]:
            source = evidence[key]
            page = f" · страница {source['page']}" if source.get("page") else ""
            lines.extend([source["title"]+page, source["url"]])
    lines.extend(result.get("limitations", []))
    return lines
