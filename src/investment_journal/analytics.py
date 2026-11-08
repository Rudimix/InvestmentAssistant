"""Расчёты: результат гипотезы и статистика по свечам."""

import math
from dataclasses import dataclass

from .models import Candle, Decision

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
