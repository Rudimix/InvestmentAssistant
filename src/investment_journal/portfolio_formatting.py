"""Полные отчёты Telegram с безопасным HTML и сохранением исходных ссылок."""

from decimal import Decimal
from html import escape

from .formatting import money
from .models import EventBatch, PortfolioAnalysis, decimal_value
from .portfolio_events import event_window

METRIC_LABELS = {
    "total": "Стоимость портфеля", "cash": "Денежные позиции", "known_value": "Стоимость оценённой части",
    "valuation_gap": "Нераспределённая стоимость", "gross_exposure": "Сумма абсолютных оценок",
    "allocation_base": "База расчёта долей", "largest_weight": "Доля крупнейшей позиции",
    "top_three_weight": "Доля трёх крупнейших позиций", "baseline_total": "Стоимость на начало периода",
    "total_change": "Изменение стоимости", "external_flow": "Внешние пополнения за вычетом выводов",
    "income": "Подтверждённые доходы", "expenses": "Подтверждённые расходы со знаком",
    "change_after_flows": "Денежное изменение после учёта внешних потоков",
    "explained_change": "Объяснённая часть изменения", "unallocated_change": "Нераспределённая часть изменения",
}
TYPE_LABELS = {"share": "Акции", "etf": "Фонды", "bond": "Облигации", "currency": "Валюты",
               "futures": "Фьючерсы", "option": "Опционы", "unknown": "Другие активы"}
CONFIDENCE_LABELS = {"high": "высокая", "medium": "средняя", "low": "низкая"}


def text_units(text: str) -> int:
    """Telegram ограничивает текст в UTF-16 code units после разбора сущностей."""
    return len(text.encode("utf-16-le")) // 2


def plain_chunks(text: str, limit: int):
    """Разбить обычный текст по границам строк или слов, сохранив Unicode-символы."""
    start = 0
    while start < len(text):
        end, units = start, 0
        while end < len(text) and units + (2 if ord(text[end]) > 0xFFFF else 1) <= limit:
            units += 2 if ord(text[end]) > 0xFFFF else 1
            end += 1
        if end == start:
            raise ValueError("Слишком маленький размер части")
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            if boundary > start+(end-start)//2:
                end = boundary+1
        yield text[start:end]
        start = end


def metric_label(key: str, analysis: PortfolioAnalysis) -> str:
    if key in METRIC_LABELS:
        return METRIC_LABELS[key]
    if key.startswith("type:"):
        kind = key.split(":")[1]
        return TYPE_LABELS.get(kind, kind)
    if key.startswith("position:"):
        identifier, suffix = key.removeprefix("position:").rsplit(":", 1)
        positions = (analysis.baseline.positions if analysis.baseline else []) + analysis.snapshot.positions
        names = {item.key: item.ticker for item in positions}
        name = names.get(identifier, "Денежный остаток")
        label = {"weight": "Доля", "value": "Стоимость", "contribution": "Вклад"}.get(suffix, suffix)
        return f"{label} {name}"
    return key


def metric_text(key: str, value: Decimal, analysis: PortfolioAnalysis) -> str:
    if key.endswith(":weight") or key in {"largest_weight", "top_three_weight"}:
        return f"{metric_label(key, analysis)} — {value:.2f}%"
    return f"{metric_label(key, analysis)} — {money(value, analysis.snapshot.currency)}"


def portfolio_report_parts(analysis: PortfolioAnalysis, events: EventBatch, explanation: dict,
                           context: dict, report_id: str, limit: int = 3500) -> list[str]:
    """Все численные разделы строятся из расчётов, ссылки AI разрешаются через ID источников."""
    segments: list[tuple[str, int]] = []

    def add(text: str):
        for chunk in plain_chunks(text, min(limit, 3300)):
            segments.append((escape(chunk), text_units(chunk)))

    observed = analysis.snapshot.observed_at.strftime("%d.%m.%Y %H:%M UTC")
    add(f"Анализ портфеля\nДанные получены {observed}")
    if analysis.snapshot.source_time:
        add(f"Время данных источника — {analysis.snapshot.source_time}")
    add("\n".join(metric_text(key, analysis.metrics[key], analysis)
                  for key in ("total", "cash", "largest_weight", "top_three_weight", "valuation_gap") if key in analysis.metrics))
    add("Структура по типам активов\n" + "\n".join(
        f"{TYPE_LABELS.get(kind, kind)} — {money(value, analysis.snapshot.currency)}" for kind, value in analysis.by_type.items()))
    add("Валюты инструментов, стоимость в валюте портфеля\n" + "\n".join(
        f"{currency.upper()} — {money(value, analysis.snapshot.currency)}" for currency, value in analysis.by_currency.items()))
    positions = {item.key: item for item in analysis.snapshot.positions}
    add("Позиции и доли\n" + "\n".join(
        f"{positions[key].ticker if key in positions else 'Денежный остаток'} — {weight:.2f}%"
        for key, weight in sorted(analysis.allocation.items(), key=lambda item: abs(item[1]), reverse=True)))
    missing = [item.ticker for item in analysis.snapshot.positions if item.value is None]
    if missing:
        add("Позиции с недоступной денежной оценкой — " + ", ".join(missing))
    if analysis.baseline:
        period = analysis.baseline.observed_at.strftime("%d.%m.%Y %H:%M UTC")
        note = "полная доступная история" if analysis.operations_complete else "по доступной части истории"
        add(f"Динамика с {period} до {observed}\nОперации — {note}\n" + "\n".join(
            metric_text(key, analysis.metrics[key], analysis) for key in (
                "total_change", "external_flow", "change_after_flows", "income", "expenses", "unallocated_change")
            if key in analysis.metrics))
        for item in analysis.changes:
            if item["status"] != "unchanged":
                value = "недоступно" if item["value_change"] is None else money(item["value_change"], analysis.snapshot.currency)
                add(f"{item['ticker']}\nКоличество {item['quantity_before']:g} → {item['quantity_after']:g}\nИзменение оценки — {value}")
        if analysis.contributions:
            add("Подтверждённые вклады позиций\n" + "\n".join(
                metric_text(f"position:{key}:contribution", value, analysis) for key, value in analysis.contributions.items()))
    else:
        add("Сформирована исходная точка. Следующие отчёты смогут показать изменения между наблюдениями.")
    if explanation.get("available"):
        add("Выводы AI\n" + explanation["summary"])
        event_numbers = {event.id: index for index, event in enumerate(events.items, 1)}
        for finding in explanation["findings"]:
            label = "Возможное объяснение" if finding["kind"] == "possible_explanation" else "Наблюдение"
            add(f"{label}, уверенность {CONFIDENCE_LABELS[finding['confidence']]}\n{finding['text']}")
            for key in finding["metric_ids"]:
                add(metric_text(key, decimal_value(context["metrics"][key]), analysis))
            if finding["event_ids"]:
                add("События — " + ", ".join(str(event_numbers[key]) for key in finding["event_ids"]))
    else:
        add(explanation.get("reason", "AI-объяснение недоступно"))
    start, end = event_window(analysis)
    add(f"События за период {start:%d.%m.%Y %H:%M} — {end:%d.%m.%Y %H:%M} UTC")
    for index, event in enumerate(events.items, 1):
        event_at = event.published_at or event.event_at
        date = event_at.strftime("%d.%m.%Y %H:%M UTC") if event_at else "дата источником не предоставлена"
        kind = "Календарь публикаций" if event.kind == "report_calendar" else "Опубликованный материал"
        add(f"{index}. {event.title}\n{kind}, {date}\nИсточник — {event.source}\n{event.period}".rstrip())
        if event.url:
            link_text = "Открыть источник"
            segments.append((f'<a href="{escape(event.url, quote=True)}">{link_text}</a>', text_units(link_text)))
        else:
            add("Исходная ссылка источником не предоставлена")
    if not events.items:
        if any(state in {"unavailable", "partial"} for state in events.sources.values()):
            add("Сведения о событиях недоступны или получены частично")
        elif events.sources:
            add("Релевантные события за указанный период не найдены")
        else:
            add("В портфеле нет инструментов для поиска событий")
    coverage = context.get("coverage", {})
    if coverage:
        add(f"Контекст для AI: событий {coverage['events_included']} из {coverage['events_found']}, "
            f"позиций {coverage['positions_included']} из {coverage['positions_total']}")
        if "positions_valued" in coverage:
            add(f"Денежная оценка доступна для {coverage['positions_valued']} из {coverage['positions_total']} позиций")
    limitations = analysis.quality + events.quality + explanation.get("limitations", [])
    if limitations:
        add("Ограничения данных\n" + "\n".join(dict.fromkeys(limitations)))
    bodies = []
    current = []
    units = 0
    for markup, size in segments:
        if current and units+size+2 > limit:
            bodies.append("\n\n".join(current))
            current, units = [], 0
        current.append(markup)
        units += size+2
    if current:
        bodies.append("\n\n".join(current))
    return [f"Отчёт {escape(report_id[:8])} · счёт {escape(analysis.snapshot.account_id)} · {index}/{len(bodies)}\n\n{body}"
            for index, body in enumerate(bodies, 1)]
