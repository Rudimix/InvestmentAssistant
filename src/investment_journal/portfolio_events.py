"""Релевантность событий и компактный контекст AI-отчёта."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json

from .models import EventBatch, PortfolioAnalysis, SnapshotPosition, data_json
from .evidence_ingestion import canonical_url


def canonical_event_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return canonical_url(url)
    except ValueError:
        return None


def select_events(batch: EventBatch, positions: list[SnapshotPosition], start: datetime, end: datetime) -> EventBatch:
    """Сопоставить инструменты и эмитентов, объединить материалы по ID или исходной ссылке."""
    identifiers = {value.casefold() for position in positions
                   for value in (position.key, position.instrument_id, position.ticker, getattr(position, "asset_id", "")) if value}
    selected = []
    by_id = {}
    by_url = {}
    ordered = sorted(batch.items, key=lambda item: item.published_at or item.event_at or datetime.min.replace(tzinfo=timezone.utc),
                     reverse=True)
    for event in ordered:
        if not identifiers.intersection(value.casefold() for value in event.instrument_ids):
            continue
        event_at = event.published_at if event.kind == "news" else event.event_at
        if event_at is not None:
            in_period = start < event_at <= end
            if event.kind != "news":
                in_period = start.date() <= event_at.date() <= end.date()
            if not in_period:
                continue
        url = canonical_event_url(event.url)
        existing = by_id.get(event.id) or by_url.get(url)
        if existing is None:
            existing = replace(event, instrument_ids=list(event.instrument_ids))
            selected.append(existing)
        else:
            existing.instrument_ids = sorted(set(existing.instrument_ids) | set(event.instrument_ids))
            if event.kind == "report_content" and not existing.content:
                existing.kind, existing.content = event.kind, event.content
        by_id[event.id] = existing
        if url:
            by_url[url] = existing
    return EventBatch(selected, dict(batch.sources), list(batch.quality), len(selected), dict(batch.freshness))


def event_window(analysis: PortfolioAnalysis) -> tuple[datetime, datetime]:
    end = analysis.snapshot.observed_at
    start = analysis.baseline.observed_at if analysis.baseline else end-timedelta(days=7)
    return start, end


def report_context(analysis: PortfolioAnalysis, events: EventBatch, limit: int = 24000) -> dict:
    """Ограниченный контекст с явным покрытием и ссылками на рассчитанные показатели."""
    start, end = event_window(analysis)
    largest = sorted(analysis.allocation, key=lambda key: abs(analysis.allocation[key]), reverse=True)[:20]
    contributors = sorted(analysis.contributions, key=lambda key: abs(analysis.contributions[key]), reverse=True)[:10]
    positions = {item.key: item for item in (analysis.baseline.positions if analysis.baseline else []) + analysis.snapshot.positions}
    selected = list(dict.fromkeys(largest + contributors + list(positions)[:20]))
    limitations = list(dict.fromkeys(analysis.quality + events.quality))
    context = {
        "currency": analysis.snapshot.currency, "observed_at": end,
        "baseline_at": analysis.baseline.observed_at if analysis.baseline else None,
        "event_window": {"from": start, "to": end}, "operations_complete": analysis.operations_complete,
        "metrics": {}, "positions": [], "events": [], "source_states": dict(events.sources),
        "limitations": limitations[:20],
        "coverage": {"events_found": events.found, "events_included": 0, "positions_total": len(positions),
                     "positions_included": 0, "positions_valued": sum(item.value is not None for item in positions.values()),
                     "limitations_total": len(limitations)},
    }
    while True:
        allowed = set(selected)
        context["metrics"] = {key: str(value) for key, value in analysis.metrics.items()
                              if not key.startswith("position:") or key.rsplit(":", 1)[0].removeprefix("position:") in allowed}
        context["positions"] = [{"id": key, "ticker": positions[key].ticker,
                                 "instrument_type": positions[key].instrument_type,
                                 "currency": positions[key].currency, "valuation_available": positions[key].value is not None}
                                for key in selected if key in positions]
        context["coverage"]["positions_included"] = len(context["positions"])
        if len(data_json(context)) <= limit:
            break
        if not selected:
            raise ValueError("Рассчитанный контекст превышает доступный объём")
        selected.pop()
    for event in events.items[:30]:
        item = asdict(event)
        item["content_truncated"] = len(item["content"]) > 1000
        item["content"] = item["content"][:1000]
        context["events"].append(item)
        context["coverage"]["events_included"] = len(context["events"])
        if len(data_json(context)) > limit:
            context["events"].pop()
            context["coverage"]["events_included"] = len(context["events"])
            break
    return json.loads(data_json(context))
