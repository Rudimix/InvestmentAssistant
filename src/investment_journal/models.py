"""Модели инвестиционного журнала."""

from dataclasses import dataclass, field
from datetime import datetime

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
