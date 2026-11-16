"""Модели инвестиционного журнала."""

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from calendar import monthrange
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints
from zoneinfo import ZoneInfo

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
    sector: str = ""
    sector_as_of: datetime | None = None
    sector_source_version: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> "SnapshotPosition":
        values = dict(raw)
        for name in ("quantity", "value", "price"):
            if values.get(name) is not None:
                values[name] = decimal_value(values[name])
        if values.get("sector_as_of") is not None:
            values["sector_as_of"] = timestamp(values["sector_as_of"])
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
    class_code: str = ""
    executed_at: datetime | None = None
    requested_quantity: Decimal | None = None
    quantity_change: Decimal | None = None
    price: Decimal | None = None
    price_currency: str = ""
    commission_currency: str = ""
    trades: list["OperationFill"] = field(default_factory=list)
    execution_complete: bool = False
    child_operations: list[dict] = field(default_factory=list)
    corporate_effect: dict = field(default_factory=dict)
    quality: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "PortfolioOperation":
        values = dict(raw)
        values["occurred_at"] = timestamp(values["occurred_at"])
        if values.get("executed_at") is not None:
            values["executed_at"] = timestamp(values["executed_at"])
        for name in ("payment", "quantity", "commission", "requested_quantity", "quantity_change", "price"):
            if values.get(name) is not None:
                values[name] = decimal_value(values[name])
        values["trades"] = [OperationFill.from_dict(item) for item in values.get("trades", [])]
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
    retrieved_at: datetime | None = None


@dataclass
class EventBatch:
    """Материалы с состоянием источников и фактическим покрытием."""

    items: list[InstrumentEvent] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    quality: list[str] = field(default_factory=list)
    found: int = 0
    freshness: dict[str, dict] = field(default_factory=dict)


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


def _restore_analytical_value(annotation, value):
    """Восстановить точные типы новых версионированных записей из JSON."""
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        options = get_args(annotation)
        if value is None and type(None) in options:
            return None
        for option in options:
            if option is type(None):
                continue
            try:
                return _restore_analytical_value(option, value)
            except (TypeError, ValueError):
                continue
        raise ValueError("Значение не соответствует модели")
    if origin is list:
        if not isinstance(value, list):
            raise ValueError("Ожидается список")
        return [_restore_analytical_value(get_args(annotation)[0], item) for item in value]
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError("Ожидается объект")
        key_type, item_type = get_args(annotation)
        return {_restore_analytical_value(key_type, key): _restore_analytical_value(item_type, item)
                for key, item in value.items()}
    if annotation is Decimal:
        return decimal_value(value)
    if annotation is datetime:
        return timestamp(value)
    if isinstance(annotation, type) and issubclass(annotation, AnalyticalRecord):
        return annotation.from_dict(value)
    if annotation is Any:
        return value
    if not isinstance(value, annotation) or (annotation is int and isinstance(value, bool)):
        raise ValueError("Неверный тип поля модели")
    return value


@dataclass(kw_only=True)
class AnalyticalRecord:
    """JSON-запись с явной версией схемы, Decimal и UTC-временами."""

    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Неподдерживаемая версия аналитической модели")

    @classmethod
    def from_dict(cls, raw: dict):
        if not isinstance(raw, dict):
            raise ValueError("Ожидается объект аналитической модели")
        hints = get_type_hints(cls)
        names = {item.name for item in fields(cls)}
        if raw.keys() - names:
            raise ValueError("Неизвестные поля аналитической модели")
        values = {key: _restore_analytical_value(hints[key], value) for key, value in raw.items()}
        return cls(**values)


@dataclass
class AnalysisPeriod(AnalyticalRecord):
    """Подтверждённые даты пользователя и зафиксированные UTC-границы [start, end)."""

    local_start: str
    local_end: str
    zone: str
    start: datetime
    end: datetime
    requested_at: datetime
    policy: str = "explicit"

    def __post_init__(self):
        super().__post_init__()
        first, last = date.fromisoformat(self.local_start), date.fromisoformat(self.local_end)
        self.start, self.end, self.requested_at = map(timestamp, (self.start, self.end, self.requested_at))
        if first > last or self.start >= self.end or self.end > self.requested_at:
            raise ValueError("Некорректные границы периода")
        ZoneInfo(self.zone)
        if self.policy not in {"explicit", "rolling", "legacy_delivered"}:
            raise ValueError("Неизвестная политика периода")


@dataclass
class DataCoverage(AnalyticalRecord):
    """Полнота независимых компонентов восстановления и исходные версии данных."""

    positions: str = "unavailable"
    cash: str = "unavailable"
    operations: str = "unavailable"
    corporate_actions: str = "unavailable"
    prices: str = "unavailable"
    reasons: list[str] = field(default_factory=list)
    source_versions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        super().__post_init__()
        if any(getattr(self, name) not in {"complete", "partial", "unavailable"}
               for name in ("positions", "cash", "operations", "corporate_actions", "prices")):
            raise ValueError("Неизвестное состояние полноты")


@dataclass
class HistoricalPosition(AnalyticalRecord):
    """Историческая позиция с независимой доступностью количества и оценки."""

    key: str
    ticker: str
    instrument_type: str
    currency: str
    quantity: Decimal | None
    value: Decimal | None = None
    price: Decimal | None = None
    price_at: datetime | None = None
    instrument_id: str = ""
    aliases: list[str] = field(default_factory=list)
    quality: list[str] = field(default_factory=list)
    exposure: Decimal | None = None
    sector: str = ""
    sector_as_of: datetime | None = None
    sector_source_version: str = ""


@dataclass
class HistoricalState(AnalyticalRecord):
    """Восстановленное состояние выбранного счёта с проверяемой опорой."""

    id: str
    user_id: int
    account_id: str
    effective_at: datetime
    observed_at: datetime
    currency: str
    positions: list[HistoricalPosition] = field(default_factory=list)
    cash_by_currency: dict[str, Decimal | None] = field(default_factory=dict)
    total: Decimal | None = None
    coverage: DataCoverage = field(default_factory=DataCoverage)
    anchor_id: str = ""
    operation_ids: list[str] = field(default_factory=list)
    methodology: str = "history-v1"
    reconstructed: bool = True
    known_value: Decimal | None = None

    def __post_init__(self):
        super().__post_init__()
        self.effective_at, self.observed_at = map(timestamp, (self.effective_at, self.observed_at))
        if self.effective_at > self.observed_at or not self.account_id or self.user_id <= 0:
            raise ValueError("Некорректная область исторического состояния")


PROFILE_FIELDS = frozenset({"goal", "horizon_months", "risk_level", "max_drawdown_pct", "minimum_cash", "max_position_pct", "max_sector_pct", "currency"})


@dataclass
class InvestorProfile(AnalyticalRecord):
    """Подтверждённые потребности владельца с отдельной ревизией."""

    user_id: int
    revision: int
    confirmed_at: datetime
    goal: str
    horizon_months: int
    risk_level: str
    max_drawdown_pct: Decimal
    minimum_cash: Decimal
    max_position_pct: Decimal
    max_sector_pct: Decimal
    currency: str = "rub"

    def __post_init__(self):
        super().__post_init__()
        self.confirmed_at = timestamp(self.confirmed_at)
        if self.user_id <= 0 or self.revision <= 0 or type(self.horizon_months) is not int or self.horizon_months <= 0:
            raise ValueError("Некорректный владелец, ревизия или горизонт профиля")
        if not self.goal.strip() or self.risk_level not in {"low", "medium", "high"}:
            raise ValueError("Не заданы цель или категория риска")
        for name in ("max_drawdown_pct", "max_position_pct", "max_sector_pct"):
            setattr(self, name, decimal_value(getattr(self, name)))
            if not 0 < getattr(self, name) <= 100:
                raise ValueError("Процент должен быть больше нуля и не больше 100")
        self.minimum_cash = decimal_value(self.minimum_cash)
        if self.minimum_cash < 0:
            raise ValueError("Ликвидный остаток должен быть неотрицательным")


@dataclass
class EvidenceDocument(AnalyticalRecord):
    """Версия публичного материала с происхождением и временем доступности."""

    id: str
    version: str
    source: str
    url: str
    title: str
    content: str
    retrieved_at: datetime
    published_at: datetime | None = None
    available_at: datetime | None = None
    instrument_ids: list[str] = field(default_factory=list)
    kind: str = "news"
    reporting_period: str = ""


@dataclass
class EvidenceChunk(AnalyticalRecord):
    """Фрагмент документа для поиска и ссылки на доказательство."""

    id: str
    document_id: str
    document_version: str
    content: str
    section: str = ""
    page: int | None = None


@dataclass
class FinancialFact(AnalyticalRecord):
    """Финансовый показатель с единицами, периодом и подтверждающим фрагментом."""

    id: str
    instrument_id: str
    metric: str
    value: Decimal | None
    unit: str
    currency: str
    period_start: str
    period_end: str
    document_id: str
    document_version: str
    chunk_id: str
    available_at: datetime | None
    standard: str = ""
    consolidated: bool | None = None


@dataclass
class AdviceProposal(AnalyticalRecord):
    """Предложение с областью владельца, ревизией профиля и основаниями."""

    id: str
    user_id: int
    account_id: str
    profile_revision: int
    created_at: datetime
    text: str
    metric_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    scenario_ids: list[str] = field(default_factory=list)
    profile_refs: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    reconsider_when: list[str] = field(default_factory=list)
    report_id: str = ""


@dataclass
class GrowthForecast(AnalyticalRecord):
    """Зафиксированный прогноз цены с горизонтом, версией и сценарными границами."""

    id: str
    instrument_id: str
    as_of: datetime
    target_at: datetime
    model_version: str
    currency: str
    source_price: Decimal
    base_price: Decimal | None = None
    low_price: Decimal | None = None
    high_price: Decimal | None = None
    horizon_months: int = 12
    evidence_ids: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def __post_init__(self):
        super().__post_init__()
        self.as_of, self.target_at = map(timestamp, (self.as_of, self.target_at))
        self.source_price = decimal_value(self.source_price)
        if self.source_price <= 0 or self.target_at <= self.as_of or self.horizon_months != 12:
            raise ValueError("Некорректная исходная цена или горизонт прогноза")
        target_year = self.as_of.year+1
        expected = self.as_of.replace(year=target_year, day=min(self.as_of.day, monthrange(target_year, self.as_of.month)[1]))
        if self.target_at != expected:
            raise ValueError("Горизонт прогноза должен составлять 12 календарных месяцев")
        for name in ("base_price", "low_price", "high_price"):
            if getattr(self, name) is not None:
                setattr(self, name, decimal_value(getattr(self, name)))
                if getattr(self, name) <= 0:
                    raise ValueError("Сценарная цена должна быть положительной")
        if self.low_price is not None and self.high_price is not None and self.low_price > self.high_price:
            raise ValueError("Неверный порядок сценарных границ")
        if self.base_price is not None and ((self.low_price is not None and self.base_price < self.low_price)
                                            or (self.high_price is not None and self.base_price > self.high_price)):
            raise ValueError("Базовый сценарий должен находиться внутри сценарных границ")


@dataclass
class OperationFill(AnalyticalRecord):
    """Фактически исполненная часть сделки со временем и количеством в штуках."""

    id: str
    executed_at: datetime
    quantity: Decimal
    price: Decimal | None
    currency: str

    def __post_init__(self):
        super().__post_init__()
        self.executed_at = timestamp(self.executed_at)
        self.quantity = decimal_value(self.quantity)
        if self.quantity <= 0:
            raise ValueError("Количество исполненной части должно быть положительным")
        if self.price is not None:
            self.price = decimal_value(self.price)


@dataclass
class CorporateEvent(AnalyticalRecord):
    """Подтверждённый эффект события с происхождением и областью применения."""

    id: str
    instrument_id: str
    effective_at: datetime
    kind: str
    source: str
    source_version: str
    confirmed: bool = False
    ratio: Decimal | None = None
    quantity_change: Decimal | None = None
    nominal_change: Decimal | None = None
    cash_payment: Decimal | None = None
    currency: str = ""
    account_id: str | None = None
    operation_id: str | None = None
    ticker: str = ""
    class_code: str = ""
    available_at: datetime | None = None

    def __post_init__(self):
        super().__post_init__()
        self.effective_at = timestamp(self.effective_at)
        if self.available_at is not None:
            self.available_at = timestamp(self.available_at)
        if not self.id or not self.instrument_id or not self.source or not self.source_version:
            raise ValueError("У корпоративного события отсутствуют происхождение или идентификатор")
        for name in ("ratio", "quantity_change", "nominal_change", "cash_payment"):
            if getattr(self, name) is not None:
                setattr(self, name, decimal_value(getattr(self, name)))
        if self.ratio is not None and self.ratio <= 0:
            raise ValueError("Коэффициент изменения количества должен быть положительным")
        if self.cash_payment is not None and not self.account_id:
            raise ValueError("Денежная выплата требует подтверждения в границах счёта")


@dataclass
class PriceObservation(AnalyticalRecord):
    """Цена завершённой свечи, доступная после её конечной UTC-границы."""

    instrument_id: str
    candle_start: datetime
    candle_end: datetime
    close: Decimal
    currency: str
    retrieved_at: datetime
    version: str
    source: str = "mcp-hourly"

    def __post_init__(self):
        super().__post_init__()
        self.candle_start, self.candle_end, self.retrieved_at = map(
            timestamp, (self.candle_start, self.candle_end, self.retrieved_at))
        self.close = decimal_value(self.close)
        if self.candle_start >= self.candle_end or not self.currency or not self.instrument_id:
            raise ValueError("Некорректная историческая свеча")


@dataclass
class HistoricalPriceBatch(AnalyticalRecord):
    """Покрытие диапазонного запроса цен и версия источников."""

    instrument_id: str
    start: datetime
    end: datetime
    retrieved_at: datetime
    items: list[PriceObservation] = field(default_factory=list)
    complete: bool = False
    quality: list[str] = field(default_factory=list)


@dataclass
class CurrencyPair(AnalyticalRecord):
    """Котировка nominal единиц исходной валюты в валюте цены."""

    instrument_id: str
    source_currency: str
    quote_currency: str
    nominal: Decimal = Decimal(1)

    def __post_init__(self):
        super().__post_init__()
        self.source_currency = self.source_currency.lower()
        self.quote_currency = self.quote_currency.lower()
        self.nominal = decimal_value(self.nominal)
        if not self.instrument_id or not self.source_currency or not self.quote_currency or self.nominal <= 0:
            raise ValueError("Непригодные характеристики валютной пары")


@dataclass
class HistoricalAnalysis(AnalyticalRecord):
    """Исторические границы и только подтверждённые численные результаты."""

    period: AnalysisPeriod
    start_state: HistoricalState
    end_state: HistoricalState
    metrics: dict[str, Decimal] = field(default_factory=dict)
    contributions: dict[str, Decimal] = field(default_factory=dict)
    changes: list[dict] = field(default_factory=list)
    quality: list[str] = field(default_factory=list)
    operations_complete: bool = False


@dataclass
class DailyObservation(AnalyticalRecord):
    day: str
    available_at: datetime
    close: Decimal
    volume: Decimal
    currency: str

    def __post_init__(self):
        super().__post_init__()
        date.fromisoformat(self.day)
        self.available_at = timestamp(self.available_at)
        self.close, self.volume = decimal_value(self.close), decimal_value(self.volume)
        self.currency = self.currency.lower()
        if self.close <= 0 or self.volume < 0 or not self.currency:
            raise ValueError("Непригодная дневная свеча")


@dataclass
class BrokerMetadataRecord(AnalyticalRecord):
    tool: str
    identifier: str
    version: str
    observed_at: datetime
    payload: dict

    def __post_init__(self):
        super().__post_init__()
        self.observed_at = timestamp(self.observed_at)
        if not self.tool or not self.identifier or not self.version:
            raise ValueError("Не указаны источник и версия справочника")


@dataclass
class StockUniverseEntry(AnalyticalRecord):
    instrument_id: str
    ticker: str
    class_code: str
    asset_id: str
    country_of_risk: str = ""
    sector: str = ""
    currency: str = ""
    lot: Decimal | None = None
    in_scope: bool = False
    account_access: bool = False
    trading_now: bool = False
    max_buy_lots: Decimal | None = None
    included: bool = False
    price: Decimal | None = None
    price_at: datetime | None = None
    median_turnover: Decimal | None = None
    median_daily_lots: Decimal | None = None
    series: list[DailyObservation] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    source_versions: dict[str, str] = field(default_factory=dict)
    inputs: dict = field(default_factory=dict)
    issuer_id: str = ""


@dataclass
class StockUniverse(AnalyticalRecord):
    id: str
    user_id: int
    account_id: str
    requested_at: datetime
    as_of: datetime
    entries: list[StockUniverseEntry] = field(default_factory=list)
    catalog_count: int = 0
    methodology: str = "russian-business-v1"
    quality: list[str] = field(default_factory=list)
    inputs: dict = field(default_factory=dict)

    def __post_init__(self):
        super().__post_init__()
        self.requested_at, self.as_of = map(timestamp, (self.requested_at, self.as_of))
        if self.user_id <= 0 or not self.account_id or self.requested_at > self.as_of:
            raise ValueError("Некорректная область или даты выборки")
