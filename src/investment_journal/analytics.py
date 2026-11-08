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
    change = pct_change(decision.entry_price, price)
    if decision.kind == "buy":
        result = change
        if change >= threshold:
            verdict = "confirmed"
        elif change <= -threshold:
            verdict = "refuted"
        else:
            if decision.is_open:
                verdict = "pending"
            else:
                verdict = "confirmed" if change > 0 else "refuted"
    elif decision.kind == "sell":
        result = -change
        if change <= -threshold:
            verdict = "confirmed"
        elif change >= threshold:
            verdict = "refuted"
        else:
            if decision.is_open:
                verdict = "pending"
            else:
                verdict = "confirmed" if change < 0 else "refuted"
    elif decision.kind == "hold":
        result = threshold - abs(change)
        if abs(change) < threshold:
            if decision.is_open:
                verdict = "pending"
            else:
                verdict = "confirmed"
        else:
            verdict = "refuted"
    else:
        raise ValueError(f"Неизвестный тип решения: {decision.kind}")
    return DecisionReview(decision, price, change, result, verdict)


def candle_stats(candles: list[Candle]) -> CandleStats:
    """Изменение, SMA, волатильность (σ дневных доходностей) и максимальная просадка."""
    if not candles:
        raise ValueError("Нет свечей")
    closes = [c.close for c in candles]
    first, last = closes[0], closes[-1]
    sma = sum(closes) / len(closes)
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] != 0:
            returns.append((closes[i] - closes[i - 1]) / closes[i - 1])
    if len(returns) > 1:
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        volatility = math.sqrt(var) * 100
    else:
        volatility = 0.0
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        if peak > 0:
            dd = (peak - c) / peak * 100
            if dd > max_dd:
                max_dd = dd
    change = pct_change(first, last) if first else 0.0
    if change > 1 and last > sma:
        trend = "up"
    elif change < -1 and last < sma:
        trend = "down"
    else:
        trend = "flat"
    return CandleStats(
        first=first,
        last=last,
        change_pct=change,
        high=max(c.high for c in candles),
        low=min(c.low for c in candles),
        sma=sma,
        volatility_pct=volatility,
        max_drawdown_pct=max_dd,
        trend=trend,
    )
