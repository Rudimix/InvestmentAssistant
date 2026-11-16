"""Сохранённый подбор с отраслевыми основаниями и ограничениями пользователя."""

from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import json

from .broker_metadata import enrich_snapshot_sectors
from .models import SnapshotPosition, data_json, timestamp
from .stock_fundamentals import compare_industry, fundamental_analysis, load_capitalizations, issuer_capitalizations
from .stock_technicals import combined_rank, rank_technicals, technical_analysis
from .stock_universe import StockUniverseService
from .growth_forecast import ForecastService


MODES = {"fundamental", "technical", "combined"}


def candidate_suitability(entry, technical, snapshot, profile):
    """Проверить один лот по текущим оценкам без автоматического выбора сделки.

    Результат условный до проверки комиссии. Персональные лимиты рассчитываются
    в одной валюте, сумма портфеля при покупке за собственный остаток сохраняется.
    """
    reasons, restrictions, metrics = [], [], {}
    if profile is None:
        return {"status": "unknown", "reasons": ["Заполните /profile для проверки личных ограничений"], "metrics": metrics}
    if not entry.included or not entry.account_access or not entry.trading_now or entry.max_buy_lots is None or entry.max_buy_lots < 1:
        restrictions.append("Приобретение одного лота выбранным счётом сейчас не подтверждено")
    if profile.horizon_months < 12:
        restrictions.append("Горизонт профиля короче 12 месяцев")
    drawdown = technical.features.get("drawdown")
    if drawdown is not None and drawdown > profile.max_drawdown_pct:
        restrictions.append("Историческая просадка превышает ограничение профиля")
    unknown = False
    if (entry.price is None or entry.lot is None or entry.lot <= 0 or snapshot.total <= 0 or snapshot.cash is None
            or entry.currency.lower() != snapshot.currency.lower() or profile.currency.lower() != snapshot.currency.lower()):
        unknown = True
        reasons.append("Для проверки лота и денежного остатка недостаточно оценок в одной валюте")
    else:
        cost = entry.price*entry.lot
        after_cash = snapshot.cash-cost
        metrics.update(one_lot_before_costs=cost, cash_after_before_costs=after_cash)
        if after_cash < profile.minimum_cash:
            restrictions.append("Покупка одного лота уменьшает остаток ниже минимума профиля")
        positions = [item for item in snapshot.positions if item.instrument_type != "currency"]
        identifiers = {entry.instrument_id, entry.ticker, entry.asset_id, f"{entry.ticker}_{entry.class_code}"}
        same = [item for item in positions if (item.asset_id and item.asset_id == entry.asset_id)
                or item.instrument_id in identifiers or item.key in identifiers]
        if any(item.value is None for item in same):
            unknown = True
            reasons.append("Экспозиция эмитента оценена частично")
        else:
            weight = (sum((abs(item.value) for item in same), Decimal(0))+cost)/snapshot.total*100
            metrics["position_after_pct"] = weight
            if weight > profile.max_position_pct:
                restrictions.append("Один лот превышает максимальную долю бумаги в профиле")
        sector = [item for item in positions if item.sector == entry.sector]
        if not entry.sector or any(not item.sector or item.value is None for item in positions):
            unknown = True
            reasons.append("Сектора или оценки части портфеля неизвестны")
        else:
            weight = (sum((abs(item.value) for item in sector), Decimal(0))+cost)/snapshot.total*100
            metrics["sector_after_pct"] = weight
            if weight > profile.max_sector_pct:
                restrictions.append("Один лот превышает максимальную долю сектора в профиле")
    status = "unknown" if unknown else "conditional"
    if restrictions:
        status = "restricted"
    reasons.append("Комиссия и фактическая цена исполнения требуют проверки перед покупкой")
    return {"status": status, "reasons": restrictions+reasons, "metrics": metrics,
            "risk_level": profile.risk_level, "profile_revision": profile.revision}


class StockScreeningService:
    def __init__(self, repo, clock, evidence=None, universe=None, forecast_directory=None):
        self.repo, self.clock, self.evidence = repo, clock, evidence
        self.universe = universe or StockUniverseService(repo, clock)
        self.forecasts = ForecastService(repo, forecast_directory)

    async def prepare(self, user_id, market, mode, profile):
        if mode not in MODES:
            raise ValueError("Неизвестный режим подбора")
        universe = await self.universe.collect(user_id, market)
        entries = [entry for entry in universe.entries if entry.included]
        snapshot = await market.snapshot(user_id, self.clock())
        snapshot.observed_at = timestamp(self.clock())
        await enrich_snapshot_sectors(self.repo, market, snapshot, self.clock)
        quality = list(universe.quality)
        capitals = {}
        if mode != "technical":
            if self.evidence:
                positions = [SnapshotPosition(entry.instrument_id, entry.ticker, "share", Decimal(0), entry.currency,
                    instrument_id=entry.instrument_id, asset_id=entry.asset_id) for entry in entries]
                end = timestamp(self.clock())
                events = await self.evidence.collect(market, positions, end-timedelta(days=1100), end)
                quality.extend(events.quality)
            capitals, limitations = await load_capitalizations(self.repo, market, [entry.asset_id for entry in universe.entries if entry.in_scope], self.clock)
            quality.extend(limitations)
            capitals = issuer_capitalizations(universe.entries, capitals)
        as_of = timestamp(self.clock())
        forecasts = self.forecasts.for_request(mode, as_of)
        if snapshot.account_id != universe.account_id:
            raise ValueError("Счёт изменился при подготовке подборки")
        fundamentals, technicals, proofs, source_facts = [], [], [], []
        for entry in entries:
            financial_ids = {identifier for item in universe.entries if entry.issuer_id and item.issuer_id == entry.issuer_id
                             for identifier in (item.instrument_id, item.ticker, item.asset_id, f"{item.ticker}_{item.class_code}")}
            entry.inputs["issuer_financial_ids"] = sorted(financial_ids)
            facts = {}
            if mode != "technical":
                for identifier in {entry.instrument_id, entry.asset_id, entry.ticker, f"{entry.ticker}_{entry.class_code}", *financial_ids}:
                    for fact in self.repo.get_financial_facts(identifier, as_of):
                        facts[fact.id, fact.document_version] = fact
            fundamental = fundamental_analysis(entry, list(facts.values()), capitals.get(entry.asset_id), as_of)
            fundamentals.append(fundamental)
            used = {identity for metric in fundamental.metrics.values() for identity in metric.fact_ids}
            evidence = []
            for fact in facts.values():
                if fact.id not in used:
                    continue
                document = self.repo.get_document(fact.document_id, fact.document_version)
                chunk = self.repo.get_evidence_chunk(fact.chunk_id)
                if document and chunk:
                    evidence.append({"fact": fact, "url": document.url, "source": document.source,
                                     "document_version": document.version, "page": chunk.page, "text": chunk.text})
            proofs.append(evidence)
            source_facts.append(list(facts.values()))
            technicals.append(technical_analysis(entry, as_of))
        compare_industry(fundamentals)
        rank_technicals(technicals)
        candidates = []
        for entry, fundamental, technical, evidence, facts in zip(entries, fundamentals, technicals, proofs, source_facts):
            combined = combined_rank(fundamental, technical)
            score = {"fundamental": fundamental.score, "technical": technical.score, "combined": combined["score"]}[mode]
            candidates.append({"entry": replace(entry, series=[]), "fundamental": fundamental, "technical": technical,
                "combined": combined, "score": score, "evidence": evidence,
                "forecast": forecasts.forecast(user_id, universe.account_id, entry, fundamental, technical, mode, as_of, facts),
                "suitability": candidate_suitability(entry, technical, snapshot, profile)})
        priority = {"conditional": 0, "unknown": 1, "restricted": 2}
        candidates.sort(key=lambda item: (priority[item["suitability"]["status"]], item["score"] is None,
                                         -(item["score"] or Decimal(0)), item["entry"].ticker))
        result = {"user_id": user_id, "account_id": universe.account_id, "as_of": as_of, "universe_id": universe.id,
            "mode": mode, "profile_revision": profile.revision if profile else None, "candidates": candidates,
            "quality": list(dict.fromkeys(quality)), "methodology": "sector-percentiles-v1",
            "snapshot": asdict(snapshot), "capitalizations": capitals,
            "manifest": {"universe": universe.id, "profile_revision": profile.revision if profile else None,
                         "fundamental_methodology": "confirmed-fundamentals-v1", "technical_methodology": "sector-percentiles-v1"}}
        result["id"] = sha256(data_json(result).encode()).hexdigest()
        result = json.loads(data_json(result))
        self.repo.save_screening(user_id, universe.account_id, result)
        return result
