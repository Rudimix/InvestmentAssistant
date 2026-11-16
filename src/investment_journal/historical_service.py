"""Получение и воспроизведение датированных входов исторического анализа."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from .historical_analysis import compare_history
from .historical_prices import currency_pairs, currency_rates, historical_prices, price_before
from .historical_valuation import ValuationTerms, bond_terms, value_position, value_state
from .mcp_client import ToolError
from .models import (
    AnalysisPeriod, CorporateEvent, OperationBatch, PortfolioOperation,
    PortfolioSnapshot, PriceObservation, data_json, timestamp,
)
from .portfolio_history import reconstruct_state, reconstruction_inputs
from .portfolio_sources import fetch_operations
from .broker_metadata import enrich_snapshot_sectors, historical_sector


def evaluate_history(anchor: PortfolioSnapshot, period: AnalysisPeriod, operations: OperationBatch,
                     events: list[CorporateEvent], evaluations: dict, corporate_complete: bool = False):
    """Чистый повтор расчёта по зафиксированному набору исходных данных."""
    states = []
    for name, at in (("start", period.start), ("end", period.end)):
        valuation = evaluations[name]
        keys = {item.get("instrument_id"): key for key, item in valuation.get("metadata", {}).items()}
        reconstruction_events = [replace(event, instrument_id=keys.get(event.instrument_id, event.instrument_id)) for event in events]
        state = reconstruct_state(anchor, at, operations, reconstruction_events, corporate_complete)
        valued = []
        for original in state.positions:
            metadata = valuation.get("metadata", {}).get(original.key, {})
            p = replace(original, instrument_id=metadata.get("instrument_id", original.instrument_id),
                        instrument_type=metadata.get("instrument_type", original.instrument_type),
                        currency=metadata.get("currency", original.currency), sector=metadata.get("sector", original.sector),
                        sector_as_of=timestamp(metadata["sector_as_of"]) if metadata.get("sector_as_of") else original.sector_as_of,
                        sector_source_version=metadata.get("sector_source_version", original.sector_source_version))
            quote = valuation.get("prices", {}).get(p.key)
            parameters = valuation.get("terms", {}).get(p.key)
            valued.append(value_position(p, at, PriceObservation.from_dict(quote) if quote else None,
                                        ValuationTerms.from_dict(parameters) if parameters else None, events))
        state = value_state(state, valued, valuation.get("rates", {}))
        for coverage in valuation.get("price_coverage", {}).values():
            if not coverage["complete"]:
                state.coverage.prices = "partial"
                state.coverage.reasons.extend(coverage["quality"])
        state.coverage.reasons = list(dict.fromkeys(state.coverage.reasons))
        states.append(state)
    return compare_history(period, *states, operations, evaluations.get("operation_rates", {}))


def replay_history(inputs: dict):
    """Восстановить расчёт без сетевых запросов и текущего справочника."""
    anchor = PortfolioSnapshot.from_dict(inputs["anchor"]["payload"])
    raw = inputs["operations"]["payload"]
    batch = OperationBatch([PortfolioOperation.from_dict(item) for item in raw["items"]], raw["complete"], raw["quality"])
    corporate = inputs["corporate_events"]["payload"]
    events = [CorporateEvent.from_dict(item) for item in corporate["events"]]
    evaluation = inputs["evaluation"]["payload"]
    result = evaluate_history(anchor, AnalysisPeriod.from_dict(evaluation["period"]), batch, events,
                              evaluation["valuations"], corporate["complete"])
    _bind_inputs(result, inputs)
    return result


def _bind_inputs(result, inputs):
    for state in (result.start_state, result.end_state):
        state.coverage.source_versions = {key: inputs[key]["version"] for key in sorted(inputs)}
        state.id = sha256(data_json({"owner": state.user_id, "account": state.account_id, "at": state.effective_at,
                                    "sources": state.coverage.source_versions}).encode()).hexdigest()


async def historical_analysis(repo, market, user_id: int, period: AnalysisPeriod, clock):
    """Собрать обе границы, сохранив отдельные версии всех применённых входов."""
    anchor = await market.snapshot(user_id, clock())
    anchor.observed_at = clock()
    if period.end > anchor.observed_at:
        raise ValueError("Период следует после наблюдения портфеля")
    await enrich_snapshot_sectors(repo, market, anchor, clock)
    repo.save_snapshot(anchor)
    # Дата поручения может предшествовать исполнению на годы. Запрашиваем
    # историю с начала Unix-эпохи, а не только поручения выбранного периода.
    beginning = datetime(1970, 1, 1, tzinfo=timezone.utc)
    operations = await fetch_operations(market, beginning, anchor.observed_at, boundary="closed_open")
    if period.start < beginning:
        operations.complete = False
        operations.quality.append("Запрошенная дата предшествует диапазону полученной истории")
    preliminary = [reconstruct_state(anchor, at, operations) for at in (period.start, period.end)]
    metadata, events = {}, {}
    assets = {p.key: p.asset_id for p in anchor.positions}
    for p in preliminary[0].positions+preliminary[1].positions:
        if p.key in metadata:
            continue
        metadata[p.key] = {"instrument_id": p.instrument_id, "instrument_type": p.instrument_type, "currency": p.currency}
        metadata[p.key]["asset_id"] = assets.get(p.key, "")
        if not p.instrument_type:
            try:
                raw = await market.report_call("invest_find_instrument", {"query": p.ticker, "responseView": ["FULL"]})
                matches = [item for item in raw.get("instruments", []) if item.get("ticker") == p.ticker
                           and f"{p.ticker}_{item.get('classCode')}" in {p.key, *p.aliases}]
                if len(matches) == 1:
                    item = matches[0]
                    metadata[p.key] = {"instrument_id": item.get("uid") or p.instrument_id,
                                       "instrument_type": str(item.get("instrumentKind", "")).removeprefix("INSTRUMENT_TYPE_").lower(),
                                       "currency": item.get("currency") or p.currency, "asset_id": item.get("assetUid") or ""}
            except (ToolError, ValueError, TypeError, AttributeError):
                pass
        uid = metadata[p.key]["instrument_id"] or p.key
        for event in repo.get_corporate_events(uid, period.start-timedelta(days=14), anchor.observed_at, as_of=anchor.observed_at):
            events[event.id] = event
    catalog = []
    currencies = {p["currency"] for p in metadata.values()} | {op.currency for op in operations.items}
    currencies.update(currency for state in preliminary for currency in state.cash_by_currency)
    if currencies-{anchor.currency, ""}:
        try:
            raw = await market.report_call("invest_list_currencies", {"instrumentStatus": "INSTRUMENT_STATUS_ALL", "responseView": ["FULL"]})
            catalog = currency_pairs(raw)
        except (ToolError, ValueError, TypeError, AttributeError):
            pass
    valuations = {"operation_rates": {}, "operation_fx_proofs": {}}
    for name, state in zip(("start", "end"), preliminary):
        at = state.effective_at
        dated_metadata = {key: dict(item) for key, item in metadata.items()}
        for item in dated_metadata.values():
            if item.get("asset_id"):
                item.update(historical_sector(repo, item["asset_id"], at))
        values = {"metadata": dated_metadata, "prices": {}, "terms": {}, "rates": {}, "price_coverage": {}}
        for p in state.positions:
            item = metadata[p.key]
            uid = item["instrument_id"] or p.key
            prices = await historical_prices(market, uid, at-timedelta(days=14), at, repo, now=anchor.observed_at)
            quote = price_before(prices, at)
            values["prices"][p.key] = asdict(quote) if quote else None
            values["price_coverage"][p.key] = {"complete": prices.complete, "quality": list(prices.quality)}
            if item["instrument_type"] == "bond":
                try:
                    raw = await market.report_call("invest_get_bond_accrued", {
                        "instrumentId": uid, "from": (at-timedelta(days=1)).isoformat(), "to": (at+timedelta(days=1)).isoformat()})
                    parameters = bond_terms(raw, uid, item["currency"], at)
                    values["terms"][p.key] = asdict(parameters) if parameters else None
                except (ToolError, ValueError, TypeError, AttributeError):
                    values["terms"][p.key] = None
        values["rates"], values["fx_proofs"] = await currency_rates(
            market, repo, currencies, anchor.currency, at, anchor.observed_at, catalog)
        valuations[name] = values
    for operation in operations.items:
        if (operation.currency != anchor.currency and period.start <= operation.occurred_at < period.end
                and not operation.trades):
            rates, proofs = await currency_rates(market, repo, [operation.currency], anchor.currency,
                                                  operation.occurred_at, anchor.observed_at, catalog)
            valuations["operation_rates"][operation.id] = rates.get(operation.currency)
            valuations["operation_fx_proofs"][operation.id] = proofs
    result = evaluate_history(anchor, period, operations, list(events.values()), valuations)
    inputs = reconstruction_inputs(anchor, operations, list(events.values()))
    payload = {"period": asdict(period), "valuations": valuations, "methodology": "history-valuation-v1"}
    inputs["evaluation"] = {"version": sha256(data_json(payload).encode()).hexdigest(), "payload": payload}
    _bind_inputs(result, inputs)
    for state in (result.start_state, result.end_state):
        repo.save_historical_state(user_id, anchor.account_id, state, inputs)
    return result
