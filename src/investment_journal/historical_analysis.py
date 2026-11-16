"""Динамика восстановленного портфеля и вклад подтверждённых денежных движений."""

from decimal import Decimal

from .analytics import operation_category
from .models import AnalysisPeriod, HistoricalAnalysis, HistoricalState, OperationBatch, decimal_value


def compare_history(period: AnalysisPeriod, start: HistoricalState, end: HistoricalState,
                    operations: OperationBatch, operation_rates: dict[str, Decimal | None] | None = None) -> HistoricalAnalysis:
    """Сравнить [start,end) с курсами на время денежных движений, без смешения счетов."""
    if (start.user_id, start.account_id, start.currency) != (end.user_id, end.account_id, end.currency):
        raise ValueError("Исторические состояния принадлежат разным счетам или валютам")
    if (start.effective_at, end.effective_at) != (period.start, period.end):
        raise ValueError("Состояния не соответствуют выбранному периоду")
    result = HistoricalAnalysis(period, start, end, quality=list(dict.fromkeys(start.coverage.reasons+end.coverage.reasons)),
                                operations_complete=operations.complete)
    result.quality.extend(operations.quality)
    rates = {key: decimal_value(value) if value is not None else None for key, value in (operation_rates or {}).items()}
    amounts = {"external_flow": Decimal(0), "income": Decimal(0), "expenses": Decimal(0)}
    payments, quantity_changes, uncertain = {}, {}, set()
    aliases = {}
    for p in start.positions+end.positions:
        for alias in {p.key, p.instrument_id, p.ticker, *p.aliases}-{ "" }:
            aliases.setdefault(alias, set()).add(p.key)
    for operation in {item.id: item for item in operations.items}.values():
        if operation.state == "OPERATION_STATE_CANCELED":
            continue
        category = operation_category(operation.operation_type)
        if category == "trade" and operation.execution_complete:
            fills = [fill for fill in operation.trades if period.start <= fill.executed_at < period.end]
            if not fills:
                continue
            signed_quantity = sum((fill.quantity for fill in fills), Decimal(0))
            direction = Decimal(1) if operation.operation_type.removeprefix("OPERATION_TYPE_") in {
                "BUY", "BUY_MARGIN", "DELIVERY_BUY"} else Decimal(-1)
            quantity = signed_quantity*direction
            # Полная payment годится только если все исполнения попали в период.
            # Для разнесённых исполнений денежная сторона подтверждается отдельно.
            amount = operation.payment if len(fills) == len(operation.trades) else None
        elif period.start <= operation.occurred_at < period.end:
            amount, quantity = operation.payment, operation.quantity_change
            if operation.state != "OPERATION_STATE_EXECUTED":
                amount = None
        else:
            continue
        rate = Decimal(1) if operation.currency == start.currency else rates.get(operation.id)
        converted = amount*rate if amount is not None and rate is not None and rate > 0 else None
        if category == "unknown" or converted is None:
            result.operations_complete = False
            result.quality.append("Часть денежных движений, исполнений или курсов требует подтверждения")
        amount_key = {"external": "external_flow", "income": "income", "expenses": "expenses"}.get(category)
        if amount_key and converted is not None:
            amounts[amount_key] += converted
        if category == "trade":
            matches = aliases.get(operation.instrument_id) or aliases.get(operation.ticker) or set()
            if len(matches) != 1:
                result.operations_complete = False
                result.quality.append("Привязка сделки к исторической позиции неоднозначна")
                continue
            key = next(iter(matches))
            if converted is None or quantity is None:
                uncertain.add(key)
            else:
                payments[key] = payments.get(key, Decimal(0))+converted
                quantity_changes[key] = quantity_changes.get(key, Decimal(0))+quantity
    before, after = {p.key: p for p in start.positions}, {p.key: p for p in end.positions}
    for key in sorted(before.keys() | after.keys()):
        old, new = before.get(key), after.get(key)
        quantity_before, quantity_after = old.quantity if old else Decimal(0), new.quantity if new else Decimal(0)
        old_value, new_value = old.value if old else Decimal(0), new.value if new else Decimal(0)
        # value хранится в валюте инструмента. Вклад в базовой валюте доступен
        # здесь лишь при совпадении валют, иначе нужна отдельная валютная атрибуция.
        p = new or old
        delta = new_value-old_value if old_value is not None and new_value is not None else None
        result.changes.append({"key": key, "ticker": p.ticker, "quantity_before": quantity_before,
                               "quantity_after": quantity_after, "value_before": old_value, "value_after": new_value,
                               "value_change": delta, "currency": p.currency})
        if (result.operations_complete and key not in uncertain and delta is not None and p.currency == start.currency
                and quantity_before is not None and quantity_after is not None
                and quantity_after-quantity_before == quantity_changes.get(key, Decimal(0))
                and p.instrument_type in {"share", "etf", "bond"}):
            result.contributions[key] = delta+payments.get(key, Decimal(0))
    result.metrics.update(amounts)
    if start.total is not None:
        result.metrics["baseline_total"] = start.total
    if end.total is not None:
        result.metrics["total"] = end.total
    if start.total is not None and end.total is not None:
        change = end.total-start.total
        result.metrics["total_change"] = change
        if result.operations_complete:
            net = change-amounts["external_flow"]
            explained = sum(result.contributions.values(), Decimal(0))+amounts["income"]+amounts["expenses"]
            result.metrics.update(change_after_flows=net, explained_change=explained, unallocated_change=net-explained)
    if not result.operations_complete:
        result.quality.append("Показаны известные денежные суммы, итог после потоков недоступен")
    if result.metrics.get("unallocated_change"):
        result.quality.append("Нераспределённая часть включает валютную переоценку и неподтверждённые эффекты")
    result.quality = list(dict.fromkeys(result.quality))
    return result
