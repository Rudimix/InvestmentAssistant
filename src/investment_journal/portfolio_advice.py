"""Проверяемые денежные сценарии улучшения структуры по подтверждённому профилю."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from .models import AnalyticalRecord, InvestorProfile, data_json, decimal_value, timestamp


@dataclass
class PortfolioScenario(AnalyticalRecord):
    id: str
    user_id: int
    account_id: str
    profile_revision: int
    as_of: datetime
    kind: str
    title: str
    currency: str
    effects: dict[str, Decimal]
    units: dict[str, str]
    profile_refs: list[str]
    metric_ids: list[str]
    required_horizon_months: int = 0
    admissible: bool = True
    conditions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    reconsider_when: list[str] = field(default_factory=list)


def check_scenario(scenario: PortfolioScenario, profile: InvestorProfile) -> PortfolioScenario:
    """Проверить владельца, горизонт, резерв и положительную базу сценария."""
    if (scenario.user_id, scenario.profile_revision) != (profile.user_id, profile.revision):
        raise ValueError("Сценарий относится к другому профилю")
    scenario = replace(scenario, conditions=list(scenario.conditions))
    if scenario.required_horizon_months > profile.horizon_months:
        scenario.admissible = False
        scenario.conditions.append("Горизонт варианта превышает потребность пользователя")
    if scenario.currency != profile.currency:
        scenario.admissible = False
        scenario.conditions.append("Требуется подтверждённый пересчёт минимального остатка в валюту сценария")
    cash = scenario.effects.get("cash_after")
    if cash is None or cash < profile.minimum_cash:
        scenario.admissible = False
        scenario.conditions.append("Не подтверждено сохранение минимального денежного остатка")
    if scenario.effects.get("total_after", Decimal(0)) <= 0:
        scenario.admissible = False
        scenario.conditions.append("Не подтверждена положительная стоимость после изменения")
    return scenario


def improvement_scenarios(profile: InvestorProfile | None, assessment: dict, account_id: str, as_of: datetime,
                          source_version: str, cost_rate: Decimal | None = None) -> list[PortfolioScenario]:
    """Рассчитать пополнение и снижение доли без подстановки неизвестных расходов.

    cost_rate — подтверждённая доля совокупных расходов от суммы продажи.
    Пополнение обозначает чистую сумму, поступившую на брокерский счёт.
    Проектируемые суммы задают денежные сценарии до округления к лотам.
    """
    if profile is None:
        return []
    if assessment.get("profile_revision") != profile.revision:
        raise ValueError("Оценка ограничений относится к другой версии профиля")
    metrics = {key: decimal_value(value) for key, value in assessment["metrics"].items()}
    total, cash = metrics.get("portfolio:total"), metrics.get("profile:cash")
    if total is None or total <= 0 or cash is None:
        return []
    currency = assessment["currency"]
    as_of = timestamp(as_of)
    if cost_rate is not None:
        cost_rate = decimal_value(cost_rate)
        if not Decimal(0) <= cost_rate < Decimal(1):
            raise ValueError("Доля расходов должна быть от 0 включительно до 1 исключительно")
    scenarios = []

    def scenario(kind, title, effects, units, refs, metric_ids, conditions, risks):
        identity = {"user": profile.user_id, "account": account_id, "profile": profile.revision,
                    "as_of": as_of, "source": source_version, "kind": kind, "effects": effects, "methodology": "advice-v1"}
        item = PortfolioScenario(sha256(data_json(identity).encode()).hexdigest(), profile.user_id, account_id,
                                 profile.revision, as_of, kind, title, currency, effects, units, refs, metric_ids,
                                 conditions=list(dict.fromkeys(conditions+assessment.get("limitations", []))), risks=risks,
                                 reconsider_when=["Изменились цены, доступность инструмента или профиль", "Уточнены комиссии, налоги или потребность в деньгах"])
        scenarios.append(check_scenario(item, profile))

    shortfall = metrics.get("profile:cash_shortfall", Decimal(0))
    required_credit = shortfall
    concentration_refs, concentration_metrics = [], []
    for violation in assessment["violations"]:
        reference, key = violation["profile_ref"], violation["metric_id"]
        if reference not in {"max_position_pct", "max_sector_pct"}:
            continue
        value_key = key.removesuffix(":weight")+":value" if reference == "max_position_pct" else "exposure:sector:"+key.split(":")[1]
        value = metrics.get(value_key)
        limit = getattr(profile, reference)/100
        if value is not None:
            required_credit = max(required_credit, value/limit-total)
            concentration_refs.append(reference)
            concentration_metrics.append(key)
    if required_credit > 0:
        effects = {"net_credit": required_credit, "total_after": total+required_credit, "cash_after": cash+required_credit}
        scenario("net_cash_credit", "Направить дополнительные средства в денежный резерв", effects,
                 {key: currency for key in effects}, sorted(set(["minimum_cash", *concentration_refs])),
                 ["profile:cash_shortfall", *concentration_metrics],
                 ["Есть дополнительные средства вне портфеля", "Указана чистая сумма поступления на счёт"],
                 ["Увеличивается доля денег и сохраняется риск снижения их покупательной способности"])
    for violation in assessment["violations"]:
        if violation["profile_ref"] != "max_position_pct":
            continue
        key = violation["metric_id"]
        value = metrics.get(key.removesuffix(":weight")+":value")
        if value is None or value <= 0:
            continue
        limit = profile.max_position_pct/100
        conditions = ["Есть ликвидность для указанной суммы", "Сумму и эффект нужно уточнить после округления к лотам", "Налоговый эффект требует отдельного подтверждения"]
        if cost_rate is None:
            amount = value-limit*total
            effects = {"sale_amount_before_costs": amount, "weight_after_before_costs": limit*100,
                       "total_after_before_costs": total, "cash_after_before_costs": cash+amount}
            conditions.append("Расходы неизвестны, показан условный эффект до расходов")
        else:
            amount = (value-limit*total)/(1-limit*cost_rate)
            cost = amount*cost_rate
            effects = {"sale_amount": amount, "estimated_cost": cost, "total_after": total-cost,
                       "cash_after": cash+amount-cost, "weight_after": (value-amount)/(total-cost)*100}
        units = {name: "%" if name.startswith("weight_") else currency for name in effects}
        scenario("reduce_position:"+key.removeprefix("position:").removesuffix(":weight"), "Снизить долю концентрированной позиции", effects,
                 units, ["max_position_pct", "minimum_cash"], [key, "portfolio:total", "profile:cash"], conditions,
                 ["Цена исполнения и расходы могут изменить результат", "Сокращение позиции уменьшает участие в её дальнейшем росте"])
    return scenarios
