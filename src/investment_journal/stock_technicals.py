"""Технические признаки и равновесные отраслевые ранги двух блоков."""

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import mean

from .analytics import max_drawdown_pct, volatility_pct
from .models import AnalyticalRecord, timestamp


METHODOLOGY = "sector-percentiles-v1"
FEATURES = ("momentum_1m", "momentum_3m", "momentum_12m", "sma50", "sma200", "sma_spread",
            "rsi14", "volatility", "drawdown", "relative_volume")
RANK_FEATURES = {"momentum_12m": True, "momentum_3m": True, "sma_spread": True, "volatility": False, "drawdown": False}


@dataclass
class TechnicalAnalysis(AnalyticalRecord):
    instrument_id: str
    asset_id: str
    sector: str
    features: dict[str, Decimal | None] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    ranks: dict = field(default_factory=dict)
    score: Decimal | None = None
    series_version: str = ""
    methodology: str = METHODOLOGY


def _months_before(day, count):
    year, month = divmod(day.year*12+day.month-1-count, 12)
    return day.replace(year=year, month=month+1, day=min(day.day, monthrange(year, month+1)[1]))


def _rsi(closes):
    differences = [right-left for left, right in zip(closes, closes[1:])]
    gain = mean(max(value, Decimal(0)) for value in differences[:14])
    loss = mean(max(-value, Decimal(0)) for value in differences[:14])
    for value in differences[14:]:
        gain = (gain*13+max(value, Decimal(0)))/14
        loss = (loss*13+max(-value, Decimal(0)))/14
    if gain == 0 and loss == 0:
        return Decimal(50)
    return 100-100/(1+gain/loss) if loss else Decimal(100)


def technical_analysis(entry, as_of):
    """Моментум за календарные месяцы, SMA по свечам, RSI Уайлдера.

Волатильность дневных изменений и просадка показаны в процентах без
годового пересчёта. Средний объём сравнивает последнюю свечу с 20 предыдущими.
"""
    result = TechnicalAnalysis(entry.instrument_id, entry.issuer_id or entry.asset_id, entry.sector,
                               {name: None for name in FEATURES}, series_version=entry.source_versions.get("daily_series", ""))
    rows = sorted((row for row in entry.series if row.available_at <= timestamp(as_of)), key=lambda row: row.day)
    if len({row.day for row in rows}) != len(rows) or any(row.currency.lower() != entry.currency.lower() for row in rows):
        result.reasons.append("Дневной ряд содержит повторы дат или несовместимые валюты")
        return result
    if not rows:
        result.reasons.append("Дневная история недоступна")
        return result
    closes = [row.close for row in rows]
    last = closes[-1]
    for months in (1, 3, 12):
        target = _months_before(date.fromisoformat(rows[-1].day), months)
        candidates = [row for row in rows if row.day <= target.isoformat() and (target-date.fromisoformat(row.day)).days <= 7]
        if candidates:
            result.features[f"momentum_{months}m"] = (last/candidates[-1].close-1)*100
    for window in (50, 200):
        if len(closes) >= window:
            result.features[f"sma{window}"] = mean(closes[-window:])
    if result.features["sma200"] is not None:
        result.features["sma_spread"] = (result.features["sma50"]/result.features["sma200"]-1)*100
    if len(closes) >= 15:
        result.features["rsi14"] = _rsi(closes)
    if len(closes) >= 21:
        history = [float(value) for value in closes[-253:]]
        result.features["volatility"] = Decimal(str(volatility_pct(history)))
        result.features["drawdown"] = Decimal(str(max_drawdown_pct(history)))
        volume = mean(row.volume for row in rows[-21:-1])
        if volume > 0:
            result.features["relative_volume"] = rows[-1].volume/volume
    sma = result.features["sma200"]
    if sma is not None:
        result.signals.append("Цена выше SMA 200" if last > sma else "Цена ниже SMA 200" if last < sma else "Цена на SMA 200")
    rsi = result.features["rsi14"]
    if rsi is not None and (rsi > 70 or rsi < 30):
        result.signals.append("RSI выше 70, риск перекупленности" if rsi > 70 else "RSI ниже 30, риск продолжающегося снижения")
    missing = [name for name, value in result.features.items() if value is None]
    if missing:
        result.reasons.append("Недостаточно истории для признаков "+", ".join(missing))
    if any((date.fromisoformat(right.day)-date.fromisoformat(left.day)).days > 14 for left, right in zip(rows, rows[1:])):
        result.reasons.append("В ряду есть перерыв более 14 дней, технический ранг недоступен")
    return result


def rank_technicals(analyses):
    """Среднее отраслевых процентилей, минимум два других актива в каждом."""
    for analysis in analyses:
        analysis.ranks, analysis.score = {}, None
        if analysis.reasons or not analysis.sector or not analysis.asset_id:
            continue
        for name, higher in RANK_FEATURES.items():
            value = analysis.features.get(name)
            peers = {}
            for other in analyses:
                if (other.asset_id and other.asset_id != analysis.asset_id and other.sector == analysis.sector
                        and not other.reasons and other.features.get(name) is not None):
                    peers.setdefault(other.asset_id, []).append((other.instrument_id, other.features[name]))
            peers = {asset: rows[0] for asset, rows in peers.items() if len({item for _, item in rows}) == 1}
            if value is None or len(peers) < 2:
                continue
            rank = sum(Decimal("0.5") if value == peer else Decimal(int((value > peer) == higher))
                       for _, peer in peers.values())/len(peers)*100
            analysis.ranks[name] = {"percentile": rank, "peer_ids": [key for key, _ in peers.values()]}
        if len(analysis.ranks) == len(RANK_FEATURES):
            analysis.score = mean(item["percentile"] for item in analysis.ranks.values())
    return analyses


def combined_rank(fundamental, technical):
    """Каждый обязательный блок имеет вес 1/2, пропуски сохраняются явно."""
    first, second = fundamental.score, technical.score
    complete = first is not None and second is not None
    return {"methodology": METHODOLOGY, "fundamental": first, "technical": second,
            "score": (first+second)/2 if complete else None,
            "status": "complete" if complete else "partial",
            "agreement": ("conflict" if (first >= 50) != (second >= 50) else "agree") if complete else "unavailable"}
