"""Расчёты структуры и денежных изменений по согласованным примерам."""

from dataclasses import replace
from decimal import Decimal
from datetime import timedelta
import unittest

import _support  # noqa: F401
from investment_journal.analytics import (
    apply_portfolio_flows, apply_position_contributions, compare_portfolios, portfolio_structure,
)
from investment_journal.market import snapshot_from
from investment_journal.market import MarketDataService
from investment_journal.mcp_client import ToolError
from investment_journal.models import OperationBatch, PortfolioOperation, SnapshotPosition
from investment_journal.portfolio_analysis import PortfolioAnalysisService
from investment_journal.storage import JournalRepository
from test_portfolio_market import NOW, DetailedPortfolioTools, portfolio_response
from test_portfolio_sources import SourceTools


class StructureTests(unittest.TestCase):
    def test_cash_is_counted_once_and_groups_reconcile(self):
        snapshot = snapshot_from(portfolio_response(), 1, "account", NOW)
        result = portfolio_structure(snapshot)
        self.assertEqual(result.metrics["known_value"], Decimal(240))
        self.assertEqual(result.metrics["valuation_gap"], Decimal(0))
        self.assertEqual(result.by_type["currency"], Decimal(20))
        self.assertEqual(result.by_currency["rub"], Decimal(240))
        self.assertAlmostEqual(sum(result.allocation.values()), Decimal(100), places=12)
        self.assertAlmostEqual(result.metrics["largest_weight"], Decimal(200)/240*100, places=12)

    def test_missing_valuation_is_visible(self):
        snapshot = snapshot_from(portfolio_response(), 1, "account", NOW)
        snapshot.total += 20
        snapshot.positions.append(SnapshotPosition("future", "FUT", "futures", Decimal(1), "rub"))
        result = portfolio_structure(snapshot)
        self.assertEqual(result.metrics["valuation_gap"], Decimal(20))
        self.assertTrue(result.quality)

    def test_short_positions_use_gross_base_and_zero_has_no_weights(self):
        snapshot = snapshot_from(portfolio_response(), 1, "account", NOW)
        snapshot.positions[0].value = Decimal(-200)
        snapshot.total = Decimal(-160)
        result = portfolio_structure(snapshot)
        self.assertEqual(result.metrics["allocation_base"], Decimal(240))
        self.assertLess(result.allocation["share"], 0)
        empty = replace(snapshot, positions=[], total=Decimal(0), cash=Decimal(0), type_totals={})
        self.assertEqual(portfolio_structure(empty).allocation, {})


class ComparisonTests(unittest.TestCase):
    def test_new_closed_renamed_and_changed_positions(self):
        before = snapshot_from(portfolio_response(), 1, "account", NOW)
        after = replace(before, id="after", observed_at=NOW+timedelta(days=1), positions=[
            replace(before.positions[0], ticker="RENAMED", quantity=Decimal(3), value=Decimal(300)),
            before.positions[-1], SnapshotPosition("new", "NEW", "share", Decimal(1), "rub", Decimal(50)),
        ])
        changes = {item["key"]: item for item in compare_portfolios(after, before)}
        self.assertEqual(changes["bond"]["status"], "closed")
        self.assertEqual(changes["new"]["status"], "opened")
        self.assertEqual(changes["share"]["ticker"], "RENAMED")
        self.assertEqual(changes["share"]["value_change"], Decimal(100))
        self.assertEqual(changes["share"]["quantity_after"], Decimal(3))

    def test_scope_currency_and_time_must_match(self):
        before = snapshot_from(portfolio_response(), 1, "account", NOW)
        for changes in ({"user_id": 2}, {"account_id": "other"}, {"currency": "usd"}, {"observed_at": NOW}):
            after = replace(before, observed_at=NOW+timedelta(days=1), **changes) if "observed_at" not in changes else replace(before, **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                compare_portfolios(after, before)


class FlowTests(unittest.TestCase):
    def calculate(self, before_total, after_total, operations, complete=True):
        before = replace(snapshot_from(portfolio_response(), 1, "account", NOW), total=Decimal(before_total))
        after = replace(before, id="after", observed_at=NOW+timedelta(days=1), total=Decimal(after_total))
        return apply_portfolio_flows(portfolio_structure(after), before, OperationBatch(operations, complete))

    def operation(self, kind, amount, number="op", **changes):
        return PortfolioOperation(number, NOW+timedelta(hours=1), "OPERATION_TYPE_"+kind,
                                  "OPERATION_STATE_EXECUTED", Decimal(amount), "rub", **changes)

    def test_deposit_is_external_flow(self):
        result = self.calculate("100000", "110000", [self.operation("INPUT", "10000")])
        self.assertEqual(result.metrics["total_change"], Decimal(10000))
        self.assertEqual(result.metrics["external_flow"], Decimal(10000))
        self.assertEqual(result.metrics["change_after_flows"], Decimal(0))

    def test_withdrawal_trades_income_commission_and_duplicate(self):
        operations = [self.operation("OUTPUT", "-1000", "out"), self.operation("BUY", "-500", "buy"),
                      self.operation("SELL", "700", "sell"), self.operation("DIVIDEND", "100", "div"),
                      self.operation("BROKER_FEE", "-5", "fee")]
        result = self.calculate("100000", "99095", operations + [operations[-1]])
        self.assertEqual(result.metrics["external_flow"], Decimal(-1000))
        self.assertEqual(result.metrics["income"], Decimal(100))
        self.assertEqual(result.metrics["expenses"], Decimal(-5))
        self.assertEqual(result.metrics["change_after_flows"], Decimal(95))
        self.assertTrue(result.operations_complete)

    def test_unknown_foreign_missing_and_cancelled_operations(self):
        operations = [replace(self.operation("INPUT", "100"), state="OPERATION_STATE_CANCELED"),
                      self.operation("OTHER", "10", "unknown"),
                      replace(self.operation("INPUT", "50", "foreign"), currency="usd")]
        result = self.calculate("100000", "100000", operations, complete=False)
        self.assertEqual(result.metrics["external_flow"], 0)
        self.assertFalse(result.operations_complete)
        self.assertTrue(result.quality)


class ContributionTests(unittest.TestCase):
    def test_purchase_and_fee_reconcile_to_total_change(self):
        source = snapshot_from(portfolio_response(), 1, "account", NOW)
        before = replace(source, total=Decimal(1000), cash=Decimal(800), positions=source.positions[:1], type_totals={})
        after = replace(before, id="after", observed_at=NOW+timedelta(days=1), total=Decimal(1020), cash=Decimal(690),
                        positions=[replace(before.positions[0], quantity=Decimal(3), value=Decimal(330))])
        buy = PortfolioOperation("buy", NOW+timedelta(hours=1), "OPERATION_TYPE_BUY", "OPERATION_STATE_EXECUTED",
                                 Decimal(-105), "rub", instrument_id="share", quantity=Decimal(1))
        fee = replace(buy, id="fee", operation_type="OPERATION_TYPE_BROKER_FEE", payment=Decimal(-5))
        operations = OperationBatch([buy, fee], True)
        result = apply_position_contributions(apply_portfolio_flows(portfolio_structure(after), before, operations), operations)
        self.assertEqual(result.contributions["share"], Decimal(25))
        self.assertEqual(result.metrics["unallocated_change"], Decimal(0))
        self.assertEqual(result.metrics["explained_change"], Decimal(20))

    def test_missing_currency_and_corporate_quantity_change_go_to_residual(self):
        before = snapshot_from(portfolio_response(), 1, "account", NOW)
        after = replace(before, id="after", observed_at=NOW+timedelta(days=1), total=Decimal(340),
                        positions=[replace(before.positions[0], quantity=Decimal(3), value=Decimal(300))] + before.positions[1:])
        operations = OperationBatch([], True)
        result = apply_position_contributions(apply_portfolio_flows(portfolio_structure(after), before, operations), operations)
        self.assertNotIn("share", result.contributions)
        self.assertEqual(result.metrics["unallocated_change"], Decimal(100))
        self.assertEqual(result.metrics["explained_change"] + result.metrics["unallocated_change"], result.metrics["change_after_flows"])
        foreign = PortfolioOperation("foreign", NOW+timedelta(hours=1), "OPERATION_TYPE_BUY", "OPERATION_STATE_EXECUTED",
                                     Decimal(-1), "usd", instrument_id="share", quantity=Decimal(1))
        partial = OperationBatch([foreign], True)
        result = apply_position_contributions(apply_portfolio_flows(portfolio_structure(after), before, partial), partial)
        self.assertFalse(result.operations_complete)
        self.assertEqual(result.metrics["unallocated_change"], Decimal(100))


class AnalysisServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_ticker_class_ids_match_enriched_positions_without_false_trades(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        tools = DetailedPortfolioTools()
        legacy = snapshot_from(tools._invest_get_portfolio({}), 1, "account", NOW-timedelta(days=1))
        repo.save_snapshot(legacy)
        result = await PortfolioAnalysisService(repo, lambda: NOW).analyze(1, MarketDataService(tools, "account"), "manual")
        self.assertEqual(len(result.changes), 3)
        self.assertFalse(any(item["status"] in {"opened", "closed"} for item in result.changes))
        self.assertTrue(all(item["quantity_before"] == item["quantity_after"] for item in result.changes))
        self.assertIsNone(result.baseline.positions[0].value)
        self.assertEqual(repo.get_snapshot(1, "account", legacy.id).positions[0].key, "SHARE_TQBR")

    async def test_first_snapshot_partial_optional_source_and_core_failure(self):
        repo = JournalRepository()
        now = NOW
        service = PortfolioAnalysisService(repo, lambda: now)
        tools = SourceTools({"invest_get_portfolio": [portfolio_response(), portfolio_response(), ToolError("failed")],
                             "invest_get_operations": [ToolError("unavailable")]})
        market = MarketDataService(tools, "account")
        first = await service.analyze(1, market, "manual")
        self.assertIsNone(first.baseline)
        self.assertEqual(repo.get_snapshot(1, "account", first.snapshot.id), first.snapshot)
        now += timedelta(days=1)
        second = await service.analyze(1, market, "manual")
        self.assertEqual(second.baseline, first.snapshot)
        self.assertFalse(second.operations_complete)
        self.assertIn("total_change", second.metrics)
        with self.assertRaises(ToolError):
            await service.analyze(1, market, "manual")
        self.assertEqual(repo._db.execute("SELECT count(*) FROM portfolio_snapshots").fetchone()[0], 2)
        repo.close()
