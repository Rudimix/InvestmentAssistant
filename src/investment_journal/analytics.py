"""Расчёты: результат гипотезы и статистика по свечам."""

import math
from dataclasses import dataclass
from decimal import Decimal

from .models import Candle, Decision, OperationBatch, PortfolioAnalysis, PortfolioSnapshot

EXTERNAL_OPERATIONS = {
    "INPUT", "OUTPUT", "INPUT_SWIFT", "OUTPUT_SWIFT", "INPUT_ACQUIRING", "OUTPUT_ACQUIRING",
    "INP_MULTI", "OUT_MULTI",
}
TRADE_OPERATIONS = {"BUY", "SELL", "BUY_MARGIN", "SELL_MARGIN",
                    "BOND_REPAYMENT", "BOND_REPAYMENT_FULL", "DELIVERY_BUY", "DELIVERY_SELL"}
INCOME_OPERATIONS = {"DIVIDEND", "COUPON", "OVERNIGHT", "OVER_INCOME", "ACCRUING_VARMARGIN", "WRITING_OFF_VARMARGIN"}


def operation_category(operation_type: str) -> str:
    """Классификация известных типов; неоднозначные типы остаются в неполноте."""
    name = operation_type.removeprefix("OPERATION_TYPE_")
    if name in EXTERNAL_OPERATIONS:
        return "external"
    if name in TRADE_OPERATIONS:
        return "trade"
    if name in INCOME_OPERATIONS:
        return "income"
    if "FEE" in name or "TAX" in name or name in {"OUT_STAMP_DUTY", "OVER_COM", "OUTPUT_PENALTY", "FUNDING"}:
        return "expenses"
    return "unknown"


def portfolio_structure(snapshot: PortfolioSnapshot) -> PortfolioAnalysis:
    """Структура по денежным оценкам с отдельной базой коротких позиций и покрытием."""
    result = PortfolioAnalysis(snapshot, None, quality=list(snapshot.quality))
    values = {}
    currencies = {}
    kinds = {}
    cash_positions = Decimal(0)
    for position in snapshot.positions:
        result.quality.extend(f"{position.ticker}: {item}" for item in position.quality)
        if position.value is None:
            continue
        if position.key in values:
            result.quality.append("Повторяющийся идентификатор позиции, оценка требует проверки")
            continue
        values[position.key] = position.value
        currencies[position.currency or "unknown"] = currencies.get(position.currency or "unknown", Decimal(0)) + position.value
        kinds[position.instrument_type or "unknown"] = kinds.get(position.instrument_type or "unknown", Decimal(0)) + position.value
        if position.instrument_type == "currency":
            cash_positions += position.value
    if snapshot.cash is not None:
        remaining_cash = snapshot.cash - cash_positions
        if remaining_cash:
            values["cash:unassigned"] = remaining_cash
            currencies[snapshot.currency] = currencies.get(snapshot.currency, Decimal(0)) + remaining_cash
        kinds["currency"] = snapshot.cash
    result.by_currency = currencies
    result.by_type = kinds | snapshot.type_totals
    known = sum(values.values(), Decimal(0))
    gross = sum((abs(value) for value in values.values()), Decimal(0))
    signed = any(value < 0 for value in values.values())
    base = gross if signed else snapshot.total
    gap = snapshot.total - known
    result.metrics = {"total": snapshot.total, "known_value": known, "valuation_gap": gap,
                      "gross_exposure": gross, "allocation_base": base}
    if snapshot.cash is not None:
        result.metrics["cash"] = snapshot.cash
    if signed:
        result.quality.append("Доли рассчитаны по сумме абсолютных доступных оценок, позиции показаны со знаком")
    tolerance = Decimal(1).scaleb(min(snapshot.total.as_tuple().exponent, -2))
    if abs(gap) > tolerance or any(position.value is None for position in snapshot.positions):
        result.quality.append("Структура и концентрация по доступным оценкам частичные, показана нераспределённая стоимость")
    if snapshot.type_totals and abs(sum(result.by_type.values(), Decimal(0)) - snapshot.total) > tolerance:
        result.quality.append("Итоги по типам активов требуют сверки с общей стоимостью")
    if base > 0:
        result.allocation = {key: value / base * 100 for key, value in values.items()}
        ordered = sorted((abs(value) for value in result.allocation.values()), reverse=True)
        result.metrics["largest_weight"] = ordered[0] if ordered else Decimal(0)
        result.metrics["top_three_weight"] = sum(ordered[:3], Decimal(0))
    else:
        result.quality.append("Процентные доли недоступны при неположительной базе оценки")
    for key, value in values.items():
        result.metrics[f"position:{key}:value"] = value
    for key, value in result.allocation.items():
        result.metrics[f"position:{key}:weight"] = value
    for key, value in result.by_type.items():
        result.metrics[f"type:{key}:value"] = value
    result.quality = list(dict.fromkeys(result.quality))
    return result


def compare_portfolios(current: PortfolioSnapshot, baseline: PortfolioSnapshot) -> list[dict]:
    """Сопоставить позиции по стабильным ID в пределах одного счёта и валюты."""
    if (current.user_id, current.account_id, current.currency) != (baseline.user_id, baseline.account_id, baseline.currency):
        raise ValueError("Снимки относятся к разным владельцам, счетам или валютам оценки")
    if baseline.observed_at >= current.observed_at:
        raise ValueError("Точка сравнения должна предшествовать наблюдению")
    before = {position.key: position for position in baseline.positions}
    after = {position.key: position for position in current.positions}
    changes = []
    for key in sorted(before.keys() | after.keys()):
        old = before.get(key)
        new = after.get(key)
        quantity_before = old.quantity if old else Decimal(0)
        quantity_after = new.quantity if new else Decimal(0)
        value_before = old.value if old else Decimal(0)
        value_after = new.value if new else Decimal(0)
        value_change = None
        if value_before is not None and value_after is not None:
            value_change = value_after - value_before
        status = "changed"
        if old is None:
            status = "opened"
        elif new is None:
            status = "closed"
        elif quantity_before == quantity_after and value_before == value_after:
            status = "unchanged"
        position = new or old
        changes.append({
            "key": key, "ticker": position.ticker, "instrument_id": position.instrument_id,
            "instrument_type": position.instrument_type, "status": status,
            "quantity_before": quantity_before, "quantity_after": quantity_after,
            "value_before": value_before, "value_after": value_after, "value_change": value_change,
        })
    return changes


def apply_portfolio_flows(result: PortfolioAnalysis, baseline: PortfolioSnapshot,
                          operations: OperationBatch) -> PortfolioAnalysis:
    """Добавить фактическое сравнение и денежный результат после известных внешних потоков."""
    result.changes = compare_portfolios(result.snapshot, baseline)
    result.baseline = baseline
    result.operations_complete = operations.complete
    result.quality.extend(operations.quality)
    amounts = {"external": Decimal(0), "income": Decimal(0), "expenses": Decimal(0)}
    for item in {operation.id: operation for operation in operations.items}.values():
        if item.state in {"OPERATION_STATE_CANCELED", "OPERATION_STATE_PROGRESS"}:
            continue
        if item.state != "OPERATION_STATE_EXECUTED":
            result.operations_complete = False
            result.quality.append("Часть операций имеет неизвестное состояние")
            continue
        if not baseline.observed_at < item.occurred_at <= result.snapshot.observed_at:
            continue
        category = operation_category(item.operation_type)
        if category == "unknown" or item.payment is None or item.currency != result.snapshot.currency:
            result.operations_complete = False
            result.quality.append("Часть операций требует уточнения типа, суммы или валютного пересчёта")
            continue
        if category in amounts:
            amounts[category] += item.payment
    change = result.snapshot.total - baseline.total
    result.metrics.update({"baseline_total": baseline.total, "total_change": change,
                           "external_flow": amounts["external"], "income": amounts["income"],
                           "expenses": amounts["expenses"], "change_after_flows": change - amounts["external"]})
    if not result.operations_complete:
        result.quality.append("Учёт денежных потоков неполный, использованы подтверждённые суммы")
    result.quality = list(dict.fromkeys(result.quality))
    return result


def apply_position_contributions(result: PortfolioAnalysis, operations: OperationBatch) -> PortfolioAnalysis:
    """Сверить вклады с денежными сторонами сделок и сохранить необъяснённый остаток."""
    if result.baseline is None:
        return result
    aliases: dict[str, set[str]] = {}
    for position in result.baseline.positions + result.snapshot.positions:
        for alias in (position.key, position.instrument_id, position.ticker):
            if alias:
                aliases.setdefault(alias, set()).add(position.key)
    payments: dict[str, Decimal] = {}
    quantities: dict[str, Decimal] = {}
    incomplete = set()
    for operation in {item.id: item for item in operations.items}.values():
        if operation.state != "OPERATION_STATE_EXECUTED" or operation_category(operation.operation_type) != "trade":
            continue
        if not result.baseline.observed_at < operation.occurred_at <= result.snapshot.observed_at:
            continue
        matches = aliases.get(operation.instrument_id) or aliases.get(operation.ticker) or set()
        if len(matches) != 1:
            result.quality.append("Привязка части сделок к позициям неоднозначна")
            continue
        key = next(iter(matches))
        if operation.payment is None or operation.currency != result.snapshot.currency or operation.quantity is None:
            incomplete.add(key)
            continue
        payments[key] = payments.get(key, Decimal(0)) + operation.payment
        direction = Decimal(1) if operation.payment < 0 else Decimal(-1)
        quantities[key] = quantities.get(key, Decimal(0)) + direction * operation.quantity
    if result.operations_complete:
        for change in result.changes:
            key = change["key"]
            if change["instrument_type"] not in {"share", "etf", "bond"} or change["value_change"] is None:
                continue
            quantity_change = change["quantity_after"] - change["quantity_before"]
            if key in incomplete or quantity_change != quantities.get(key, Decimal(0)):
                result.quality.append(f"{change['ticker']}: изменение количества требует детализации")
                continue
            contribution = change["value_change"] + payments.get(key, Decimal(0))
            result.contributions[key] = contribution
            result.metrics[f"position:{key}:contribution"] = contribution
    explained = sum(result.contributions.values(), Decimal(0)) + result.metrics["income"] + result.metrics["expenses"]
    result.metrics["explained_change"] = explained
    result.metrics["unallocated_change"] = result.metrics["change_after_flows"] - explained
    if result.metrics["unallocated_change"]:
        result.quality.append("Нераспределённая часть учитывает недоступные оценки, валютные и корпоративные события")
    result.quality = list(dict.fromkeys(result.quality))
    return result

#: Порог (в процентах), внутри которого цена считается «не изменившейся».
HOLD_THRESHOLD_PCT = 3.0


@dataclass
class DecisionReview:
    """Оценка решения по текущей (или закрывающей) цене."""

    decision: Decision
    price: float
    change_pct: float
    result_pct: float
    verdict: str  # confirmed | refuted | pending


@dataclass
class CandleStats:
    """Сводка по ряду свечей."""

    first: float
    last: float
    change_pct: float
    high: float
    low: float
    sma: float
    volatility_pct: float
    max_drawdown_pct: float
    trend: str  # up | down | flat


def pct_change(old: float, new: float) -> float:
    if old == 0:
        raise ValueError("Исходная цена не может быть нулевой")
    return (new - old) / old * 100


def review_decision(decision: Decision, price: float, threshold: float = HOLD_THRESHOLD_PCT) -> DecisionReview:
    """Сравнить гипотезу с фактом.

    * buy подтверждается ростом, sell — падением цены больше порога;
    * hold подтверждается, если цена осталась в коридоре ±порог;
    * для открытых решений внутри коридора вердикт — pending.
    """
    if decision.kind not in _JUDGES:
        raise ValueError(f"Неизвестный тип решения: {decision.kind}")
    change = pct_change(decision.entry_price, price)
    result, verdict = _JUDGES[decision.kind](change, threshold, decision.is_open)
    return DecisionReview(decision, price, change, result, verdict)


def _judge_direction(sign: int):
    """Оценка buy (sign=+1) и sell (sign=-1): результат — изменение цены в нужную сторону."""

    def judge(change: float, threshold: float, is_open: bool) -> tuple[float, str]:
        result = sign * change
        if abs(result) >= threshold or not is_open:
            return result, "confirmed" if result > 0 else "refuted"
        return result, "pending"

    return judge


def _judge_hold(change: float, threshold: float, is_open: bool) -> tuple[float, str]:
    """hold: результат — запас до границы коридора ±threshold."""
    result = threshold - abs(change)
    if result <= 0:
        return result, "refuted"
    return result, "pending" if is_open else "confirmed"


_JUDGES = {"buy": _judge_direction(+1), "sell": _judge_direction(-1), "hold": _judge_hold}


def returns(closes: list[float]) -> list[float]:
    """Относительные изменения между соседними ценами закрытия."""
    return [(b - a) / a for a, b in zip(closes, closes[1:]) if a != 0]


def volatility_pct(closes: list[float]) -> float:
    """Выборочное стандартное отклонение доходностей, в процентах."""
    r = returns(closes)
    if len(r) < 2:
        return 0.0
    mean = sum(r) / len(r)
    return math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1)) * 100


def max_drawdown_pct(closes: list[float]) -> float:
    """Наибольшее падение от предыдущего максимума, в процентах."""
    peak, worst = closes[0], 0.0
    for price in closes:
        peak = max(peak, price)
        if peak > 0:
            worst = max(worst, (peak - price) / peak * 100)
    return worst


def trend(change: float, last: float, sma: float) -> str:
    """Тренд: изменение больше 1% и цена по ту же сторону от средней."""
    if change > 1 and last > sma:
        return "up"
    if change < -1 and last < sma:
        return "down"
    return "flat"


def candle_stats(candles: list[Candle]) -> CandleStats:
    """Изменение, SMA, волатильность и максимальная просадка по свечам."""
    if not candles:
        raise ValueError("Нет свечей")
    closes = [c.close for c in candles]
    sma = sum(closes) / len(closes)
    change = pct_change(closes[0], closes[-1]) if closes[0] else 0.0
    return CandleStats(
        first=closes[0],
        last=closes[-1],
        change_pct=change,
        high=max(c.high for c in candles),
        low=min(c.low for c in candles),
        sma=sma,
        volatility_pct=volatility_pct(closes),
        max_drawdown_pct=max_drawdown_pct(closes),
        trend=trend(change, closes[-1], sma),
    )
