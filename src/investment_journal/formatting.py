"""Тексты сообщений бота."""

from .analytics import CandleStats, DecisionReview
from .models import Decision, Portfolio

KIND_LABELS = {"buy": "Покупка", "sell": "Продажа", "hold": "Удержание"}
VERDICT_LABELS = {
    "confirmed": "✅ подтверждается",
    "refuted": "❌ не подтверждается",
    "pending": "⏳ пока рано судить",
}
TREND_LABELS = {"up": "восходящий", "down": "нисходящий", "flat": "боковой"}


def money(value: float, currency: str = "rub") -> str:
    symbol = {"rub": "₽", "usd": "$", "eur": "€"}.get(currency.lower(), currency.upper())
    return f"{value:,.2f}".replace(",", " ") + f" {symbol}"


def signed_pct(value: float) -> str:
    return f"{value:+.2f}%"


def format_decision(d: Decision) -> str:
    status = "открыто" if d.is_open else f"закрыто по {d.exit_price:g}"
    return (
        f"#{d.id} {KIND_LABELS.get(d.kind, d.kind)} {d.ticker} по {d.entry_price:g} "
        f"({d.created_at:%d.%m.%Y}, {status})\n«{d.hypothesis}»"
    )


def format_journal(decisions: list[Decision]) -> str:
    if not decisions:
        return "Журнал пуст. Добавьте гипотезу командой /idea."
    return "\n\n".join(format_decision(d) for d in decisions)


def format_review(r: DecisionReview) -> str:
    d = r.decision
    return (
        f"#{d.id} {KIND_LABELS.get(d.kind, d.kind)} {d.ticker}: {d.entry_price:g} → {r.price:g} "
        f"({signed_pct(r.change_pct)})\n{VERDICT_LABELS[r.verdict]}"
    )


def format_reviews(reviews: list[DecisionReview]) -> str:
    if not reviews:
        return "Открытых гипотез нет."
    confirmed = sum(1 for r in reviews if r.verdict == "confirmed")
    refuted = sum(1 for r in reviews if r.verdict == "refuted")
    body = "\n\n".join(format_review(r) for r in reviews)
    return f"{body}\n\nИтого: подтверждается {confirmed}, не подтверждается {refuted}, всего {len(reviews)}."


def format_portfolio(p: Portfolio) -> str:
    lines = [
        f"Стоимость портфеля: {money(p.total)}",
        f"Свободные деньги: {money(p.cash)}",
        f"Нереализованный результат: {money(p.unrealized_pnl)}",
    ]
    if p.positions:
        lines.append("")
        for pos in p.positions:
            lines.append(
                f"• {pos.ticker} ({pos.instrument_type}): {pos.quantity:g} шт., "
                f"средняя {pos.average_price:g}, текущая {pos.current_price:g}, "
                f"результат {money(pos.expected_yield, pos.currency)}"
            )
    return "\n".join(lines)


def format_stats(ticker: str, interval: str, s: CandleStats, bars: int) -> str:
    return (
        f"{ticker.upper()}, {bars} свечей {interval}\n"
        f"Цена: {s.first:g} → {s.last:g} ({signed_pct(s.change_pct)})\n"
        f"Максимум / минимум: {s.high:g} / {s.low:g}\n"
        f"Средняя (SMA): {s.sma:.2f}\n"
        f"Волатильность: {s.volatility_pct:.2f}% за свечу\n"
        f"Макс. просадка: {s.max_drawdown_pct:.2f}%\n"
        f"Тренд: {TREND_LABELS[s.trend]}"
    )
