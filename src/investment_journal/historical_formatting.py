"""Читаемый исторический отчёт с датами оценок и полнотой каждого компонента."""

from html import escape

from .formatting import money
from .portfolio_formatting import METRIC_LABELS, plain_chunks
from .profile_metrics import profile_assessment_lines
from .advice_formatting import advice_lines


def historical_report_parts(analysis, report_id: str, limit: int = 3500, profile_revision: int | None = None,
                            assessment: dict | None = None, events=None, advice: dict | None = None) -> list[str]:
    from .analysis_periods import period_description

    currency = analysis.end_state.currency
    lines = ["Исторический анализ портфеля", period_description(analysis.period)]
    if profile_revision is not None:
        lines.append(f"Профиль · версия {profile_revision}" if profile_revision else "Профиль потребностей доступен через /profile")
    for key, value in analysis.metrics.items():
        lines.append(f"{METRIC_LABELS.get(key, key)} — {money(value, currency)}")
    for title, state in (("Начало периода", analysis.start_state), ("Конец периода", analysis.end_state)):
        lines.extend([title, state.effective_at.strftime("%d.%m.%Y %H:%M UTC")])
        if state.total is None:
            lines.append(f"Полная стоимость недоступна. Оценённая часть — {money(state.known_value, currency)}")
        for p in state.positions:
            quantity = str(p.quantity) if p.quantity is not None else "недоступно"
            value = money(p.value, p.currency) if p.value is not None else "оценка недоступна"
            at = p.price_at.strftime("%d.%m.%Y %H:%M UTC") if p.price_at else "цена недоступна"
            lines.append(f"{p.ticker} · количество {quantity} · {value} · {at}")
        for code, amount in state.cash_by_currency.items():
            lines.append(f"Деньги {code.upper()} — {money(amount, code) if amount is not None else 'недоступно'}")
        labels = {"complete": "полное", "partial": "частичное", "unavailable": "недоступно"}
        for field, label in (("positions", "Состав"), ("cash", "Деньги"), ("operations", "Операции"),
                             ("corporate_actions", "Корпоративные события"), ("prices", "Цены")):
            lines.append(f"{label} — {labels[getattr(state.coverage, field)]}")
    if analysis.contributions:
        lines.append("Подтверждённые вклады позиций")
        names = {p.key: p.ticker for p in analysis.start_state.positions+analysis.end_state.positions}
        lines.extend(f"{names.get(key, key)} — {money(value, currency)}" for key, value in analysis.contributions.items())
    if analysis.quality:
        lines.extend(["Ограничения данных", *analysis.quality])
    if assessment:
        lines.extend(profile_assessment_lines(assessment))
    if advice:
        lines.extend(advice_lines(advice))
    if events:
        for event in events.items:
            lines.extend([event.title, f"Источник — {event.source}", event.url or "Исходная ссылка отсутствует"])
        for source in events.freshness.values():
            lines.append(f"{source['name']} · последнее получение {source['last_success_at'] or 'недоступно'} · {source['status']}")
        lines.extend(events.quality)
    header = f"Отчёт {report_id[:8]} · счёт {analysis.end_state.account_id}"
    chunks = list(plain_chunks("\n".join(lines), limit-len(header.encode("utf-16-le"))//2-40))
    return [f"{escape(header)} · {i}/{len(chunks)}\n\n{escape(chunk)}" for i, chunk in enumerate(chunks, 1)]
