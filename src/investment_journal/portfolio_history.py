"""Восстановление количества бумаг и денежных остатков по фактическим операциям."""

from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from .analytics import operation_category
from .models import (
    CorporateEvent, DataCoverage, HistoricalPosition, HistoricalState, OperationBatch, PortfolioOperation,
    PortfolioSnapshot, data_json, timestamp,
)


def reconstruction_inputs(anchor: PortfolioSnapshot, operations: OperationBatch,
                          corporate_events: list[CorporateEvent] | None = None, corporate_complete: bool = False) -> dict:
    """Версии исходных данных для точного повторения реконструкции."""
    operation_version = sha256(data_json(operations).encode()).hexdigest()
    inputs = {"anchor": {"version": anchor.id, "payload": asdict(anchor)},
              "operations": {"version": operation_version, "payload": asdict(operations)}}
    if corporate_events is not None:
        payload = {"complete": corporate_complete, "events": [asdict(item) for item in corporate_events]}
        inputs["corporate_events"] = {"version": sha256(data_json(payload).encode()).hexdigest(), "payload": payload}
    return inputs


class _HistoryBuilder:
    def __init__(self, anchor: PortfolioSnapshot, target: datetime, operations: OperationBatch):
        self.anchor, self.target, self.operations = anchor, target, operations
        self.positions = {}
        self.cash = {}
        self.aliases = {}
        self.quantity_events = []
        self.coverage = DataCoverage(positions="complete", cash="complete", operations="complete" if operations.complete else "partial",
                                     corporate_actions="partial", reasons=list(operations.quality))
        for position in anchor.positions:
            if position.instrument_type == "currency":
                if position.currency:
                    self.cash[position.currency] = self.cash.get(position.currency, Decimal(0)) + position.quantity
                else:
                    self.coverage.cash = "partial"
                continue
            sector_at = position.sector_as_of or anchor.observed_at
            self.positions[position.key] = HistoricalPosition(
                position.key, position.ticker, position.instrument_type, position.currency, position.quantity,
                instrument_id=position.instrument_id, aliases=list(position.aliases),
                sector=position.sector if sector_at <= target else "",
                sector_as_of=sector_at if position.sector and sector_at <= target else None,
                sector_source_version=position.sector_source_version if sector_at <= target else "",
            )
            for alias in {position.key, position.instrument_id, position.ticker, *position.aliases} - {""}:
                self.aliases.setdefault(alias, set()).add(position.key)
        if not self.cash and anchor.cash is not None:
            self.cash[anchor.currency] = anchor.cash
        if not self.cash:
            self.cash[anchor.currency] = None
            self.coverage.cash = "partial"
        if anchor.quality:
            self.coverage.reasons.extend(anchor.quality)
        self.coverage.reasons.append("Полнота истории корпоративных действий требует отдельного подтверждения")
        if not operations.complete and target < anchor.observed_at:
            self.coverage.positions = self.coverage.cash = "partial"
            for position in self.positions.values():
                position.quantity = None
            self.cash = {currency: None for currency in self.cash}

    def in_period(self, moment: datetime) -> bool:
        return self.target <= moment < self.anchor.observed_at

    def cash_change(self, currency: str, amount: Decimal | None):
        currency = currency or self.anchor.currency
        previous = self.cash.get(currency, Decimal(0))
        self.cash[currency] = previous-amount if previous is not None and amount is not None else None
        if self.cash[currency] is None:
            self.coverage.cash = "partial"

    def position(self, operation: PortfolioOperation | CorporateEvent) -> HistoricalPosition | None:
        exact = self.aliases.get(operation.instrument_id, set())
        if not exact and operation.class_code:
            exact = self.aliases.get(f"{operation.ticker}_{operation.class_code}", set())
        matches = exact or self.aliases.get(operation.ticker, set())
        if len(matches) > 1:
            for key in matches:
                self.positions[key].quantity = None
            self.coverage.positions = "partial"
            self.coverage.reasons.append("Привязка операции к инструменту неоднозначна")
            return None
        if matches:
            return self.positions[next(iter(matches))]
        key = operation.instrument_id or operation.ticker
        if not key:
            self.coverage.positions = "partial"
            self.coverage.reasons.append("У операции отсутствует идентификатор инструмента")
            return None
        currency = operation.price_currency or operation.currency if isinstance(operation, PortfolioOperation) else operation.currency
        position = HistoricalPosition(key, operation.ticker or key, "", currency,
                                      Decimal(0) if self.operations.complete else None, instrument_id=key)
        self.positions[key] = position
        for alias in {key, operation.ticker} - {""}:
            self.aliases.setdefault(alias, set()).add(key)
        return position

    def rewind_trade(self, operation: PortfolioOperation, fee_parents: set[str]):
        fills = [fill for fill in operation.trades if self.in_period(fill.executed_at)]
        name = operation.operation_type.removeprefix("OPERATION_TYPE_")
        direction = Decimal(1) if name in {"BUY", "BUY_MARGIN", "DELIVERY_BUY"} else Decimal(-1)
        if operation.execution_complete:
            quantity = direction*sum((fill.quantity for fill in fills), Decimal(0))
            if not fills:
                return
        elif self.in_period(operation.occurred_at):
            quantity = operation.quantity_change
            if quantity is None and operation.quantity is not None:
                quantity = direction*operation.quantity
            self.coverage.positions = "partial"
            self.coverage.reasons.append("Время отдельных исполнений недоступно, использована дата поручения")
        else:
            return
        position = self.position(operation)
        if position:
            if operation.execution_complete:
                self.quantity_events.extend((fill.executed_at, position.key, direction*fill.quantity, None) for fill in fills)
            else:
                self.quantity_events.append((operation.occurred_at, position.key, quantity, None))
        full_fills = operation.execution_complete and len(fills) == len(operation.trades)
        entire_operation = full_fills or (not operation.execution_complete and self.in_period(operation.occurred_at))
        if entire_operation and operation.state == "OPERATION_STATE_EXECUTED":
            self.cash_change(operation.currency, operation.payment)
        elif operation.execution_complete and all(fill.price is not None and fill.currency == operation.currency for fill in fills):
            cost = -direction*sum((fill.quantity*fill.price for fill in fills), Decimal(0))
            self.cash_change(operation.currency, cost)
        else:
            self.cash_change(operation.currency, None)
            self.coverage.reasons.append("Денежный эффект исполненной части сделки не подтверждён")
        if operation.commission and operation.id not in fee_parents and operation.parent_id not in fee_parents:
            # Комиссия является также полем родительской сделки. Подтверждённая
            # самостоятельная комиссия учитывается отдельной операцией ровно один раз.
            self.cash_change(operation.commission_currency or operation.currency, None)
            self.coverage.reasons.append("Списание комиссии не подтверждено отдельной связанной операцией")

    def rewind(self, corporate_events: list[CorporateEvent] | None = None, corporate_complete: bool = False):
        operations = list({item.id: item for item in self.operations.items}.values())
        events = list({(item.id, item.source_version): item for item in (corporate_events or [])}.values())
        confirmed = {item.operation_id for item in events if item.confirmed and item.operation_id
                     and item.account_id in {None, self.anchor.account_id}}
        fee_parents = {item.parent_id for item in operations if operation_category(item.operation_type) == "expenses"
                       and item.parent_id and item.state == "OPERATION_STATE_EXECUTED" and item.payment is not None}
        used = []
        for operation in reversed(sorted(operations, key=lambda item: (item.occurred_at, item.id))):
            if operation.state == "OPERATION_STATE_CANCELED":
                continue
            if operation.state not in {"OPERATION_STATE_EXECUTED", "OPERATION_STATE_PROGRESS"}:
                self.coverage.operations = "partial"
                self.coverage.reasons.append("Состояние части операций неизвестно")
                continue
            name = operation.operation_type.removeprefix("OPERATION_TYPE_")
            if not self.in_period(operation.occurred_at) and not any(self.in_period(fill.executed_at) for fill in operation.trades):
                if (operation.occurred_at < self.target and not operation.execution_complete and operation.quantity
                        and name in {"BUY", "SELL", "BUY_MARGIN", "SELL_MARGIN"}):
                    position = self.position(operation)
                    if position:
                        position.quantity = None
                    self.cash_change(operation.currency, None)
                    self.coverage.positions = "partial"
                    self.coverage.reasons.append("Время исполнения раннего поручения не подтверждено")
                continue
            used.append(operation.id)
            if name in {"BUY", "SELL", "BUY_MARGIN", "SELL_MARGIN", "DELIVERY_BUY", "DELIVERY_SELL"}:
                self.rewind_trade(operation, fee_parents)
            elif name in {"INPUT_SECURITIES", "OUTPUT_SECURITIES"}:
                position = self.position(operation)
                if position:
                    delta = operation.quantity_change
                    if delta is None and operation.quantity is not None:
                        delta = operation.quantity*(1 if name == "INPUT_SECURITIES" else -1)
                    if operation.id not in confirmed:
                        self.quantity_events.append((operation.occurred_at, position.key, delta, None))
                    if delta is None:
                        self.coverage.positions = "partial"
            elif name == "BOND_REPAYMENT_FULL":
                position = self.position(operation)
                if position and operation.id not in confirmed:
                    delta = -operation.quantity if operation.quantity is not None else None
                    self.quantity_events.append((operation.occurred_at, position.key, delta, None))
                self.cash_change(operation.currency, operation.payment)
            elif operation.id in confirmed:
                if operation.payment is not None:
                    self.cash_change(operation.currency, operation.payment)
            elif operation_category(operation.operation_type) != "unknown":
                if self.in_period(operation.occurred_at) and operation.state == "OPERATION_STATE_EXECUTED":
                    self.cash_change(operation.currency, operation.payment)
            else:
                self.coverage.operations = self.coverage.positions = self.coverage.cash = "partial"
                self.coverage.reasons.append("Неизвестный эффект операции требует детализации")
                position = self.position(operation) if operation.instrument_id or operation.ticker else None
                if position:
                    position.quantity = None
                self.cash_change(operation.currency, None)
        self.rewind_corporate_events(events, corporate_complete, set(used))
        self.apply_quantities()
        return used

    def rewind_corporate_events(self, events: list[CorporateEvent], complete: bool, used: set[str]):
        self.coverage.corporate_actions = "complete" if complete else "partial"
        if complete:
            self.coverage.reasons = [item for item in self.coverage.reasons
                                     if item != "Полнота истории корпоративных действий требует отдельного подтверждения"]
        for event in events:
            if not self.in_period(event.effective_at) or event.account_id not in {None, self.anchor.account_id}:
                continue
            position = self.position(event)
            if position is None:
                continue
            if not event.confirmed:
                position.quantity = None
                self.coverage.positions = self.coverage.corporate_actions = "partial"
                self.coverage.reasons.append("Корпоративное событие не подтверждено")
                continue
            if event.kind in {"split", "consolidation"} and event.ratio is not None:
                self.quantity_events.append((event.effective_at, position.key, None, event.ratio))
            elif event.kind in {"transfer", "redemption", "conversion"} and event.quantity_change is not None:
                if event.account_id is None:
                    position.quantity = None
                    self.coverage.positions = self.coverage.corporate_actions = "partial"
                    self.coverage.reasons.append("Направление события не подтверждено для выбранного счёта")
                else:
                    self.quantity_events.append((event.effective_at, position.key, event.quantity_change, None))
            elif event.kind != "amortization":
                position.quantity = None
                self.coverage.positions = self.coverage.corporate_actions = "partial"
                self.coverage.reasons.append("Количественный эффект корпоративного события неизвестен")
            if event.cash_payment is not None and event.operation_id not in used:
                self.cash_change(event.currency, event.cash_payment)

    def apply_quantities(self):
        groups = {}
        for at, key, delta, ratio in self.quantity_events:
            groups.setdefault((at, key), []).append((delta, ratio))
        for (at, key), effects in sorted(groups.items(), reverse=True):
            position = self.positions[key]
            if position.quantity is None:
                continue
            if len(effects) > 1 and any(ratio is not None for _, ratio in effects):
                position.quantity = None
                self.coverage.positions = "partial"
                self.coverage.reasons.append("Порядок корпоративного события и исполнения с одинаковым временем неизвестен")
                continue
            for delta, ratio in effects:
                if ratio is not None:
                    position.quantity /= ratio
                elif delta is not None:
                    position.quantity -= delta
                else:
                    position.quantity = None
                    self.coverage.positions = "partial"


def reconstruct_state(anchor: PortfolioSnapshot, target: datetime, operations: OperationBatch,
                       corporate_events: list[CorporateEvent] | None = None, corporate_complete: bool = False) -> HistoricalState:
    """Обратить подтверждённые эффекты между target включительно и временем опоры исключительно."""
    target = timestamp(target)
    if target > anchor.observed_at:
        raise ValueError("Историческая граница следует после опорного состояния")
    if corporate_complete and corporate_events is None:
        raise ValueError("Подтверждение полноты требует явного набора корпоративных событий")
    builder = _HistoryBuilder(anchor, target, operations)
    used = builder.rewind(corporate_events, corporate_complete)
    inputs = reconstruction_inputs(anchor, operations, corporate_events, corporate_complete)
    builder.coverage.source_versions = {key: value["version"] for key, value in inputs.items()}
    builder.coverage.reasons = list(dict.fromkeys(builder.coverage.reasons))
    identity = data_json({"user": anchor.user_id, "account": anchor.account_id,
                          "target": target, "sources": builder.coverage.source_versions})
    return HistoricalState(
        sha256(identity.encode()).hexdigest(), anchor.user_id, anchor.account_id, target, anchor.observed_at,
        anchor.currency, [replace(position) for position in builder.positions.values() if position.quantity != 0],
        dict(builder.cash), coverage=builder.coverage, anchor_id=anchor.id, operation_ids=sorted(used),
    )
