"""Потоки выбранного периода и неизменяемое воспроизведение исторических входов."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
import unittest
from unittest.mock import AsyncMock, patch

import _support  # noqa: F401
from investment_journal.analysis_periods import explicit_period
from investment_journal.historical_analysis import compare_history
from investment_journal.historical_service import historical_analysis, replay_history
from investment_journal.models import HistoricalPosition, HistoricalState, OperationBatch, data_json
from investment_journal.portfolio_analysis import PortfolioAnalysisService
from investment_journal.storage import JournalRepository
from test_analysis_models import NOW
from test_portfolio_history import anchor, movement


class HistoricalAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.period = explicit_period("01.11.2026", "10.11.2026", "UTC", NOW)
        self.first = HistoricalState("first", 1, "account", self.period.start, NOW, "rub", total=D(100000))
        self.last = replace(self.first, id="last", effective_at=self.period.end, total=D(110000))

    def test_deposit_at_start_included_and_end_excluded(self):
        deposit = replace(movement("deposit", "INPUT", -10, 10000), occurred_at=self.period.start)
        excluded = replace(deposit, id="next-period", occurred_at=self.period.end)
        result = compare_history(self.period, self.first, self.last, OperationBatch([deposit, excluded], True))
        self.assertEqual(result.metrics["total_change"], D(10000))
        self.assertEqual(result.metrics["external_flow"], D(10000))
        self.assertEqual(result.metrics["change_after_flows"], D(0))

    def test_partial_history_does_not_claim_result_after_flows(self):
        result = compare_history(self.period, self.first, replace(self.last, total=None), OperationBatch([], False))
        self.assertNotIn("total_change", result.metrics)
        self.assertNotIn("change_after_flows", result.metrics)
        self.assertFalse(result.operations_complete)
        with self.assertRaises(ValueError):
            compare_history(self.period, self.first, replace(self.last, user_id=2), OperationBatch([], True))

    def test_foreign_cash_flow_requires_rate_at_operation_time(self):
        deposit = replace(movement("usd", "INPUT", -10, 100), currency="usd", occurred_at=self.period.start)
        result = compare_history(self.period, self.first, self.last, OperationBatch([deposit], True), {"usd": "90"})
        self.assertEqual(result.metrics["external_flow"], D(9000))
        self.assertEqual(result.metrics["change_after_flows"], D(1000))
        self.assertFalse(compare_history(self.period, self.first, self.last, OperationBatch([deposit], True)).operations_complete)

    def test_closed_position_contribution_includes_sale_cash(self):
        p = HistoricalPosition("TEST_TQBR", "TEST", "share", "rub", D(10), value=D(1000), instrument_id="TEST_TQBR")
        first = replace(self.first, total=D(1000), positions=[p])
        last = replace(self.last, total=D(1100))
        sale = replace(movement("sale", "SELL", -10, 1100, 10), occurred_at=self.period.start+timedelta(days=2))
        sale.trades = [replace(fill, executed_at=sale.occurred_at) for fill in sale.trades]
        result = compare_history(self.period, first, last, OperationBatch([sale], True))
        self.assertEqual(result.contributions[p.key], D(100))
        self.assertEqual(result.metrics["unallocated_change"], D(0))


class HistoricalServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_period_remains_frozen_after_midnight(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        user = repo.get_or_create_user(1)
        period = explicit_period("01.11.2026", "15.11.2026", "UTC", NOW)
        run = repo.create_run(user.id, "account", "manual", "request", NOW, period=period)
        tomorrow = NOW+timedelta(days=1)
        newer = explicit_period("01.11.2026", "16.11.2026", "UTC", tomorrow)
        repeated = repo.create_run(user.id, "account", "manual", "request", tomorrow, period=newer)
        self.assertEqual(run["resolved_period"], repeated["resolved_period"])
        legacy = repo.create_run(user.id, "account", "manual", "legacy", NOW)
        self.assertIsNone(repo.create_run(user.id, "account", "manual", "legacy", tomorrow, period=newer)["resolved_period"])
        self.assertIsNone(legacy["resolved_period"])

    async def test_service_entry_and_replay_without_network(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        repo.get_or_create_user(1)
        period = explicit_period("01.11.2026", "10.11.2026", "UTC", NOW)
        observation = replace(anchor(0, 110000), observed_at=NOW, total=D(110000))
        deposit = replace(movement("deposit", "INPUT", -10, 10000), occurred_at=period.start+timedelta(days=2))
        market = AsyncMock()
        market.snapshot.return_value = observation
        with patch("investment_journal.historical_service.fetch_operations", AsyncMock(return_value=OperationBatch([deposit], True))) as fetch:
            result = await PortfolioAnalysisService(repo, lambda: NOW).analyze(1, market, "manual", period)
        self.assertEqual(result.metrics["baseline_total"], D(100000))
        self.assertEqual(result.metrics["change_after_flows"], 0)
        self.assertEqual(fetch.call_args.args[1].year, 1970)
        inputs = repo.get_historical_inputs(1, "account", result.start_state.id)
        repeated = replay_history(json.loads(data_json(inputs)))
        self.assertEqual(data_json(result), data_json(repeated))
        self.assertEqual(repo.get_historical_inputs(2, "account", result.start_state.id), {})
        market.report_call.assert_not_awaited()
        self.assertEqual(result.start_state.coverage.corporate_actions, "partial")

    async def test_current_anchor_cash_reconciles_without_movements(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        repo.get_or_create_user(1)
        period = explicit_period("01.11.2026", "10.11.2026", "UTC", NOW)
        observation = replace(anchor(0, 1000), observed_at=NOW)
        market = AsyncMock()
        market.snapshot.return_value = observation
        with patch("investment_journal.historical_service.fetch_operations", AsyncMock(return_value=OperationBatch([], True))):
            result = await historical_analysis(repo, market, 1, period, lambda: NOW)
        self.assertEqual(result.start_state.total, observation.total)
        self.assertEqual(result.end_state.total, observation.total)


if __name__ == "__main__":
    unittest.main()
