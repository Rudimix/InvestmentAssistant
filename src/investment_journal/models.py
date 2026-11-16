"""Модели инвестиционного журнала."""

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Any

#: Допустимые типы инвестиционного решения.
DECISION_KINDS = ("buy", "sell", "hold")


@dataclass
class User:
    """Пользователь журнала.

    :param id: Идентификатор пользователя.
    :param chat_id: Идентификатор личного чата.
    """

    id: int
    chat_id: int


@dataclass(frozen=True)
class Account:
    """Счёт в Т-Инвестициях.

    :param id: Идентификатор счёта.
    :param name: Название, которое пользователь дал счёту.
    :param type: Тип счёта (``ACCOUNT_TYPE_TINKOFF``, ``ACCOUNT_TYPE_TINKOFF_IIS``...).
    :param read_only: Токен даёт к счёту доступ только на чтение.
    """

    id: str
    name: str = ""
    type: str = ""
    read_only: bool = False


@dataclass
class DecisionDraft:
    """Черновик инвестиционной гипотезы.

    :param user_id: Идентификатор владельца.
    :param kind: Тип решения: buy, sell или hold.
    :param ticker: Тикер инструмента.
    :param hypothesis: Текст инвестиционной гипотезы.
    """

    user_id: int
    kind: str
    ticker: str
    hypothesis: str


@dataclass
class Decision:
    """Зафиксированное решение с ценой входа.

    :param id: Идентификатор записи журнала.
    :param user_id: Идентификатор владельца.
    :param kind: Тип решения: buy, sell или hold.
    :param ticker: Тикер инструмента.
    :param hypothesis: Текст гипотезы.
    :param entry_price: Цена инструмента в момент фиксации.
    :param created_at: Время фиксации (UTC).
    :param exit_price: Цена закрытия, если решение закрыто.
    :param closed_at: Время закрытия (UTC).
    """

    id: int
    user_id: int
    kind: str
    ticker: str
    hypothesis: str
    entry_price: float
    created_at: datetime
    exit_price: float | None = None
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        """Решение ещё не закрыто."""
        return self.closed_at is None


@dataclass
class Candle:
    """Свеча OHLCV."""

    time: str
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class Position:
    """Позиция портфеля."""

    ticker: str
    instrument_type: str
    quantity: float
    average_price: float
    current_price: float
    expected_yield: float
    currency: str = "rub"

    @property
    def is_empty_cash(self) -> bool:
        """Нулевой остаток валюты — такие строки в портфеле не показываем."""
        return self.instrument_type == "currency" and self.quantity == 0


@dataclass
class Portfolio:
    """Портфель счёта."""

    total: float
    cash: float
    unrealized_pnl: float
    positions: list[Position] = field(default_factory=list)


def decimal_value(value: Any) -> Decimal:
    """Прочитать точное конечное число из денежных форматов MCP.

    Отсутствующее значение обрабатывает вызывающий код. Ноль в ответе
    отличается от отсутствующей цены или оценки.
    """
    if isinstance(value, dict):
        if "value" in value:
            return decimal_value(value["value"])
        if "units" in value or "nano" in value:
            return decimal_value(value.get("units", 0)) + decimal_value(value.get("nano", 0)) / Decimal(10**9)
        raise ValueError("Денежное значение отсутствует")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Некорректное денежное значение") from exc
    if not number.is_finite():
        raise ValueError("Денежное значение должно быть конечным")
    return number


def timestamp(value: str | datetime) -> datetime:
    """ISO-время источника, нормализованное в UTC."""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Укажите часовой пояс времени")
    return parsed.astimezone(timezone.utc)


def data_json(value: Any) -> str:
    """JSON аналитических данных с точными десятичными строками и UTC-временем."""
    def encode(item):
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, datetime):
            return timestamp(item).isoformat()
        if is_dataclass(item):
            return asdict(item)
        raise TypeError(f"Unsupported data type: {type(item).__name__}")

    return json.dumps(value, default=encode, ensure_ascii=False, allow_nan=False)


@dataclass
class SnapshotPosition:
    """Позиция снимка с оценкой в валюте портфеля и отдельной исходной ценой."""

    key: str
    ticker: str
    instrument_type: str
    quantity: Decimal
    currency: str
    value: Decimal | None = None
    price: Decimal | None = None
    instrument_id: str = ""
    asset_id: str = ""
    price_currency: str = ""
    source_time: str | None = None
    quality: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "SnapshotPosition":
        values = dict(raw)
        for name in ("quantity", "value", "price"):
            if values.get(name) is not None:
                values[name] = decimal_value(values[name])
        return cls(**values)


@dataclass
class PortfolioSnapshot:
    """Наблюдение одного счёта одного владельца."""

    id: str
    user_id: int
    account_id: str
    observed_at: datetime
    currency: str
    total: Decimal
    cash: Decimal | None
    positions: list[SnapshotPosition] = field(default_factory=list)
    type_totals: dict[str, Decimal] = field(default_factory=dict)
    quality: list[str] = field(default_factory=list)
    source_time: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "PortfolioSnapshot":
        values = dict(raw)
        values["observed_at"] = timestamp(values["observed_at"])
        values["total"] = decimal_value(values["total"])
        values["cash"] = None if values.get("cash") is None else decimal_value(values["cash"])
        values["positions"] = [SnapshotPosition.from_dict(item) for item in values.get("positions", [])]
        values["type_totals"] = {key: decimal_value(value) for key, value in values.get("type_totals", {}).items()}
        return cls(**values)


@dataclass
class PortfolioOperation:
    """Операция источника, включая состояние и валюту исходной денежной суммы."""

    id: str
    occurred_at: datetime
    operation_type: str
    state: str
    payment: Decimal | None
    currency: str
    instrument_id: str = ""
    ticker: str = ""
    quantity: Decimal | None = None
    parent_id: str = ""
    commission: Decimal | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "PortfolioOperation":
        values = dict(raw)
        values["occurred_at"] = timestamp(values["occurred_at"])
        for name in ("payment", "quantity", "commission"):
            if values.get(name) is not None:
                values[name] = decimal_value(values[name])
        return cls(**values)


@dataclass
class OperationBatch:
    """Операции за период с подтверждением полноты пагинации."""

    items: list[PortfolioOperation] = field(default_factory=list)
    complete: bool = False
    quality: list[str] = field(default_factory=list)


@dataclass
class InstrumentEvent:
    """Проверяемая новость, календарная запись или содержание отчётности."""

    id: str
    kind: str
    instrument_ids: list[str]
    title: str
    source: str
    url: str | None = None
    published_at: datetime | None = None
    event_at: datetime | None = None
    period: str = ""
    content: str = ""


@dataclass
class EventBatch:
    """Материалы с состоянием источников и фактическим покрытием."""

    items: list[InstrumentEvent] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    quality: list[str] = field(default_factory=list)
    found: int = 0


@dataclass
class PortfolioAnalysis:
    """Рассчитанные показатели, изменения и ограничения анализа."""

    snapshot: PortfolioSnapshot
    baseline: PortfolioSnapshot | None
    metrics: dict[str, Decimal] = field(default_factory=dict)
    allocation: dict[str, Decimal] = field(default_factory=dict)
    by_type: dict[str, Decimal] = field(default_factory=dict)
    by_currency: dict[str, Decimal] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    contributions: dict[str, Decimal] = field(default_factory=dict)
    quality: list[str] = field(default_factory=list)
    operations_complete: bool = False
