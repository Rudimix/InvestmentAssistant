"""Получение, сохранение и сравнение снимков выбранного портфеля."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

from .analytics import apply_portfolio_flows, apply_position_contributions, portfolio_structure
from .market import MarketDataService
from .models import AnalysisPeriod, HistoricalAnalysis, PortfolioAnalysis
from .historical_service import historical_analysis
from .broker_metadata import enrich_snapshot_sectors
from .portfolio_sources import fetch_operations
from .storage import JournalRepository


class PortfolioAnalysisService:
    """Единые точные расчёты для ручных и автоматических отчётов."""

    def __init__(self, repo: JournalRepository, clock: Callable[[], datetime] | None = None):
        self.repo = repo
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def analyze(self, user_id: int, market: MarketDataService, origin: str,
                      period: AnalysisPeriod | None = None) -> PortfolioAnalysis | HistoricalAnalysis:
        if period is not None:
            return await historical_analysis(self.repo, market, user_id, period, self.clock)
        snapshot = await market.snapshot(user_id, self.clock())
        snapshot.observed_at = self.clock()
        await enrich_snapshot_sectors(self.repo, market, snapshot, self.clock)
        self.repo.save_snapshot(snapshot)
        result = portfolio_structure(snapshot)
        baseline = self.repo.baseline_snapshot(user_id, snapshot.account_id, origin, snapshot.observed_at)
        if baseline is None:
            return result
        if baseline.currency != snapshot.currency:
            result.quality.append("Валюта оценки изменилась, сформирована новая исходная точка")
            return result
        # Старые MCP-снимки содержали только ticker_classCode. Сопоставляем их
        # с новыми UID только при точном и однозначном совпадении этой пары.
        aliases = {}
        for position in snapshot.positions:
            for alias in position.aliases:
                aliases.setdefault(alias, []).append(position)
        reconciled = []
        occupied = {position.key for position in baseline.positions}
        for position in baseline.positions:
            matches = aliases.get(position.key, [])
            if ("Составной идентификатор позиции" in position.quality and len(matches) == 1
                    and matches[0].key not in occupied):
                current = matches[0]
                position = replace(position, key=current.key, instrument_id=current.instrument_id,
                                   instrument_type=position.instrument_type or current.instrument_type)
                occupied.add(current.key)
            reconciled.append(position)
        baseline = replace(baseline, positions=reconciled)
        operations = await fetch_operations(market, baseline.observed_at, snapshot.observed_at)
        self.repo.save_operations(user_id, snapshot.account_id, operations.items)
        operations.items = self.repo.list_operations(user_id, snapshot.account_id, baseline.observed_at, snapshot.observed_at)
        apply_portfolio_flows(result, baseline, operations)
        return apply_position_contributions(result, operations)
