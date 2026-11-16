"""Отклонения от подтверждённых ограничений и доступные экспозиции портфеля."""

from decimal import Decimal

from .models import PROFILE_FIELDS, HistoricalAnalysis, InvestorProfile, decimal_value


def profile_context(profile: InvestorProfile) -> dict:
    values = {name: getattr(profile, name) for name in sorted(PROFILE_FIELDS)}
    values.update(revision=profile.revision, confirmed_at=profile.confirmed_at)
    return values


def assess_profile(profile: InvestorProfile | None, currency: str, total: Decimal | None,
                   cash: Decimal | None, position_values: dict[str, Decimal | None],
                   position_currencies: dict[str, str], sectors: dict[str, str] | None = None) -> dict:
    """Все денежные входы должны быть уже оценены в currency на один момент.

    Валюты позиций обозначают экспозицию эмитента/инструмента, а не валюту
    переданных сумм. Отсутствующий сектор не восстанавливается по названию.
    """
    result = {"profile_revision": profile.revision if profile else None, "currency": currency,
              "metrics": {}, "units": {}, "violations": [], "limitations": [], "methodology": "profile-v1"}
    if profile is None:
        result["limitations"].append("Для персональных ограничений заполните /profile")
        return result
    sectors = sectors or {}
    metrics, units = result["metrics"], result["units"]

    def metric(key, value, unit):
        metrics[key], units[key] = value, unit

    def restriction(key, actual, limit, profile_ref, lower_bound=False):
        metric(key, actual, units.get(key, "%"))
        metric(key+":limit", limit, units[key])
        metric(key+":excess", max(actual-limit, Decimal(0)), units[key])
        if actual > limit:
            result["violations"].append({"metric_id": key, "profile_ref": profile_ref,
                                         "actual": actual, "limit": limit, "excess": actual-limit,
                                         "lower_bound": lower_bound})

    metric("profile:horizon_months", Decimal(profile.horizon_months), "months")
    metric("profile:max_drawdown_pct", profile.max_drawdown_pct, "%")
    metric("forecast:horizon_months", Decimal(12), "months")
    if total is not None:
        metric("portfolio:total", total, currency)
    if profile.horizon_months < 12:
        result["limitations"].append("Прогноз на 12 месяцев выходит за указанный горизонт пользователя")
    if currency.lower() == profile.currency.lower() and cash is not None:
        cash = decimal_value(cash)
        metric("profile:cash", cash, currency)
        metric("profile:minimum_cash", profile.minimum_cash, currency)
        shortfall = max(profile.minimum_cash-cash, Decimal(0))
        metric("profile:cash_shortfall", shortfall, currency)
        if shortfall:
            result["violations"].append({"metric_id": "profile:cash_shortfall", "profile_ref": "minimum_cash",
                                         "actual": cash, "limit": profile.minimum_cash, "excess": shortfall, "lower_bound": False})
    else:
        result["limitations"].append("Минимальный остаток требует подтверждённой суммы в валюте профиля")
    sector_values, currency_values = {}, {}
    incomplete = any(value is None for value in position_values.values())
    unknown_sector = False
    for key, raw in position_values.items():
        if raw is None:
            continue
        value = decimal_value(raw)
        metric(f"position:{key}:value", value, currency)
        code = position_currencies.get(key) or "unknown"
        currency_values[code] = currency_values.get(code, Decimal(0))+abs(value)
        sector = sectors.get(key)
        if sector:
            sector_values[sector] = sector_values.get(sector, Decimal(0))+abs(value)
        elif value:
            unknown_sector = True
        if total is not None and total > 0:
            restriction(f"position:{key}:weight", abs(value)/total*100, profile.max_position_pct, "max_position_pct", incomplete)
    for code, value in currency_values.items():
        metric(f"exposure:currency:{code}", value, currency)
    for sector, value in sector_values.items():
        metric(f"exposure:sector:{sector}", value, currency)
        if total is not None and total > 0:
            restriction(f"sector:{sector}:weight", value/total*100, profile.max_sector_pct, "max_sector_pct", unknown_sector or incomplete)
    if total is None or total <= 0:
        result["limitations"].append("Доли недоступны без положительной полной стоимости портфеля")
    if incomplete:
        result["limitations"].append("Экспозиции рассчитаны по доступной оценённой части")
    if unknown_sector:
        result["limitations"].append("Для части инструментов отсутствует подтверждённый сектор")
    return result


def analysis_profile_assessment(analysis, profile: InvestorProfile | None, historical_inputs: dict | None = None):
    """Привести текущие и исторические денежные оценки к единой базе профиля."""
    if isinstance(analysis, HistoricalAnalysis):
        state = analysis.end_state
        raw_rates = (historical_inputs or {}).get("evaluation", {}).get("payload", {}).get("valuations", {}).get("end", {}).get("rates", {})
        rates = {key: decimal_value(value) for key, value in raw_rates.items() if value is not None}
        rates[state.currency] = Decimal(1)
        values = {p.key: p.value*rates[p.currency] if p.value is not None and p.currency in rates else None for p in state.positions}
        cash_values = [amount*rates[code] if amount is not None and code in rates else None for code, amount in state.cash_by_currency.items()]
        cash = sum(cash_values, Decimal(0)) if all(value is not None for value in cash_values) else None
        return assess_profile(profile, state.currency, state.total, cash, values,
                              {p.key: p.currency for p in state.positions}, {p.key: p.sector for p in state.positions if p.sector})
    snapshot = analysis.snapshot
    positions = [p for p in snapshot.positions if p.instrument_type != "currency"]
    return assess_profile(profile, snapshot.currency, snapshot.total, snapshot.cash,
                          {p.key: p.value for p in positions}, {p.key: p.currency for p in positions},
                          {p.key: p.sector for p in positions if p.sector})


def profile_assessment_lines(assessment: dict) -> list[str]:
    """Подтверждённые отклонения с единицами и ограничениями покрытия."""
    labels = {"minimum_cash": "Минимальный денежный остаток", "max_position_pct": "Доля позиции",
              "max_sector_pct": "Доля сектора"}
    lines = ["Ограничения профиля"]
    for violation in assessment.get("violations", []):
        key = violation["metric_id"]
        unit = assessment["units"][key]
        label = labels[violation["profile_ref"]]
        actual, limit = violation["actual"], violation["limit"]
        lines.append(f"{label} · {key} · фактически {actual} {unit} · ограничение {limit} {unit}")
    lines.extend(assessment.get("limitations", []))
    if len(lines) == 1:
        lines.append("По доступным проверенным показателям отклонений не выявлено")
    return lines
