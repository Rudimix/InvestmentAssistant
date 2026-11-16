"""Оценка восстановленного состава по датированным характеристикам инструментов."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from zoneinfo import ZoneInfo

from .models import (
    AnalyticalRecord, CorporateEvent, HistoricalPosition, HistoricalState,
    PriceObservation, data_json, decimal_value, timestamp,
)


@dataclass
class ValuationTerms(AnalyticalRecord):
    """Подтверждённые параметры для полуоткрытого интервала оценки.

    Для фьючерса settlement_price задаёт цену последнего расчёта, уже
    отражённого в денежном остатке. Гарантийное обеспечение отдельно не вычитается.
    adjustment_factor возвращает цену свечи в единицы на дату самой свечи.
    """

    instrument_id: str
    currency: str
    valid_from: datetime
    valid_until: datetime
    source: str
    version: str
    nominal: Decimal | None = None
    accrued_interest: Decimal | None = None
    price_step: Decimal | None = None
    step_value: Decimal | None = None
    settlement_price: Decimal | None = None
    adjustment_factor: Decimal | None = None

    def __post_init__(self):
        super().__post_init__()
        self.valid_from, self.valid_until = map(timestamp, (self.valid_from, self.valid_until))
        self.currency = self.currency.lower()
        if self.valid_from >= self.valid_until or not all((self.instrument_id, self.currency, self.source, self.version)):
            raise ValueError("Не подтверждены срок или происхождение параметров оценки")
        for name in ("nominal", "accrued_interest", "price_step", "step_value", "settlement_price", "adjustment_factor"):
            value = getattr(self, name)
            if value is not None:
                value = decimal_value(value)
                if name in {"nominal", "price_step", "step_value", "adjustment_factor"} and value <= 0:
                    raise ValueError("Параметр оценки должен быть положительным")
                setattr(self, name, value)


def bond_terms(raw: dict, instrument_id: str, currency: str, at: datetime,
               calendar_zone: str = "Europe/Moscow") -> ValuationTerms | None:
    """НКД и остаточный номинал строго на местную дату оценки из get_bond_accrued."""
    at = timestamp(at)
    zone = ZoneInfo(calendar_zone)
    local = at.astimezone(zone)
    rows = [row for row in raw.get("accruedInterests", []) if row.get("date") == local.date().isoformat()]
    if len(rows) != 1:
        return None
    row = rows[0]
    try:
        nominal, accrued = decimal_value(row["nominal"]), decimal_value(row["value"])
        beginning = datetime.combine(local.date(), datetime.min.time(), zone)
        return ValuationTerms(instrument_id, currency, beginning, beginning+timedelta(days=1),
                              "mcp-bond-accrued", sha256(data_json(row).encode()).hexdigest(),
                              nominal=nominal, accrued_interest=accrued)
    except (KeyError, ValueError, TypeError):
        return None


def value_position(position: HistoricalPosition, at: datetime, price: PriceObservation | None,
                   terms: ValuationTerms | None = None,
                   events: list[CorporateEvent] | None = None) -> HistoricalPosition:
    """Оценить в валюте инструмента, сохраняя неизвестные компоненты как None."""
    at = timestamp(at)
    result = replace(position, value=None, price=None, price_at=None, exposure=None, quality=list(position.quality))
    if position.quantity == 0:
        result.value = Decimal(0)
        return result
    if position.quantity is None or price is None:
        result.quality.append("Отсутствует количество или историческая цена")
        return result
    identity = position.instrument_id or position.key
    if price.instrument_id != identity or price.candle_end > at or at-price.candle_end > timedelta(days=14):
        result.quality.append("Цена не соответствует инструменту, времени или пределу возраста 14 дней")
        return result
    valid_terms = terms is not None and terms.instrument_id == identity and terms.valid_from <= at < terms.valid_until
    if terms is not None and (not valid_terms or terms.currency != position.currency.lower()):
        result.quality.append("Параметры оценки не соответствуют инструменту, валюте или дате")
        return result
    quote = price.close
    splits = [event for event in events or [] if event.instrument_id == identity and event.ratio is not None
              and event.effective_at > price.candle_end]
    if splits and (not valid_terms or terms.adjustment_factor is None or any(not event.confirmed for event in splits)):
        result.quality.append("Не подтверждена база корректировки цены при корпоративном действии")
        return result
    if valid_terms and terms.adjustment_factor is not None:
        quote *= terms.adjustment_factor
    for event in splits:
        if event.effective_at <= at:
            quote /= event.ratio
    kind = position.instrument_type.lower()
    if kind not in {"future", "futures"} and quote <= 0:
        result.quality.append("Историческая цена должна быть положительной")
        return result
    if kind in {"bond", "bonds"}:
        if not valid_terms or terms.nominal is None or terms.accrued_interest is None or price.currency not in {"pt", "pt."}:
            result.quality.append("Для облигации нужны процентная цена, исторический номинал и НКД")
            return result
        unit = quote*terms.nominal/100+terms.accrued_interest
    elif kind in {"future", "futures"}:
        if not valid_terms or any(value is None for value in (terms.price_step, terms.step_value, terms.settlement_price)):
            result.quality.append("Для фьючерса нужны историческая стоимость шага и цена последнего денежного расчёта")
            return result
        if price.currency not in {"pt", "pt.", position.currency.lower()}:
            result.quality.append("Не подтверждены единицы цены контракта")
            return result
        unit = (quote-terms.settlement_price)/terms.price_step*terms.step_value
        result.exposure = position.quantity*quote/terms.price_step*terms.step_value
    elif kind in {"share", "stock", "etf", "currency"} and price.currency == position.currency.lower():
        unit = quote
    else:
        result.quality.append("Метод денежной оценки или валюта цены инструмента не подтверждены")
        return result
    result.price, result.price_at = quote, price.candle_end
    result.value = position.quantity*unit
    return result


def value_state(state: HistoricalState, positions: list[HistoricalPosition],
                rates: dict[str, Decimal | None]) -> HistoricalState:
    """Сложить оценки и денежные остатки однократно в базовой валюте."""
    if len(positions) != len(state.positions) or sorted(p.key for p in positions) != sorted(p.key for p in state.positions):
        raise ValueError("Оценённый состав не совпадает с восстановленным")
    rates = {key.lower(): decimal_value(value) if value is not None else None for key, value in rates.items()}
    if any(value is not None and value <= 0 for value in rates.values()):
        raise ValueError("Курс должен быть положительным")
    rates[state.currency.lower()] = Decimal(1)
    coverage = replace(state.coverage, reasons=list(state.coverage.reasons), source_versions=dict(state.coverage.source_versions))
    total, missing = Decimal(0), False
    components = [(p.currency, p.value) for p in positions]+list(state.cash_by_currency.items())
    for currency, value in components:
        rate = rates.get(currency.lower())
        if value == 0:
            continue
        if value is None or rate is None:
            missing = True
            continue
        total += value*rate
    coverage.prices = "partial" if missing else "complete"
    coverage.reasons.extend(reason for p in positions for reason in p.quality)
    if missing:
        coverage.reasons.append("Часть стоимости или исторических валютных курсов недоступна")
    coverage.reasons = list(dict.fromkeys(coverage.reasons))
    return replace(state, positions=positions, known_value=total, total=None if missing else total, coverage=coverage)
