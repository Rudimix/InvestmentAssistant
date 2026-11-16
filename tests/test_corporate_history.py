"""Корпоративные эффекты восстанавливаются в порядке фактических дат и в области счёта."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import unittest

import _support  # noqa: F401
from investment_journal.models import CorporateEvent, OperationBatch
from investment_journal.portfolio_history import reconstruct_state
from investment_journal.storage import JournalRepository
from test_analysis_models import NOW
from test_portfolio_history import anchor, movement


def event(kind, day=5, **kwargs):
    instrument_id = kwargs.pop("instrument_id", "uid")
    return CorporateEvent("event", instrument_id, NOW+timedelta(days=day), kind, "verified-source", "v1", confirmed=True,
                          ticker="TEST", available_at=NOW, **kwargs)


class CorporateHistoryTests(unittest.TestCase):
    def test_split_between_two_executions_of_one_order(self):
        fills = [
            {"num": "before", "date": (NOW+timedelta(days=2)).isoformat(), "quantity": "10",
             "price": {"value": "100", "currency": "rub"}},
            {"num": "after", "date": (NOW+timedelta(days=7)).isoformat(), "quantity": "5",
             "price": {"value": "120", "currency": "rub"}},
        ]
        operations = OperationBatch([movement("buy", "BUY", 1, -1600, 15, trades=fills)], True)
        split = event("split", ratio=Decimal(2))
        early = reconstruct_state(anchor(25, 0), NOW, operations, [split], True)
        middle = reconstruct_state(anchor(25, 0), NOW+timedelta(days=6), operations, [split], True)
        self.assertEqual(early.positions, [])
        self.assertEqual(early.cash_by_currency["rub"], Decimal(1600))
        self.assertEqual(middle.positions[0].quantity, Decimal(20))
        self.assertEqual(middle.cash_by_currency["rub"], Decimal(600))
        self.assertEqual(middle.coverage.corporate_actions, "complete")

    def test_consolidation_and_unknown_event(self):
        consolidation = event("consolidation", ratio=Decimal("0.1"))
        result = reconstruct_state(anchor(1, 100), NOW, OperationBatch([], True), [consolidation], True)
        self.assertEqual(result.positions[0].quantity, Decimal(10))
        unknown = replace(consolidation, confirmed=False)
        result = reconstruct_state(anchor(1, 100), NOW, OperationBatch([], True), [unknown], True)
        self.assertIsNone(result.positions[0].quantity)
        self.assertEqual(result.coverage.corporate_actions, "partial")

    def test_redemption_and_amortization_cash_are_counted_once(self):
        repayment = movement("repay", "BOND_REPAYMENT_FULL", 5, 1000, 1)
        redemption = event("redemption", instrument_id="TEST_TQBR", account_id="account", operation_id="repay",
                           quantity_change=Decimal(-1), cash_payment=Decimal(1000), currency="rub")
        result = reconstruct_state(anchor(0, 1000), NOW, OperationBatch([repayment], True), [redemption], True)
        self.assertEqual(result.positions[0].quantity, Decimal(1))
        self.assertEqual(result.cash_by_currency["rub"], Decimal(0))
        amortization = event("amortization", nominal_change=Decimal(-100))
        amortization_payment = movement("amort", "BOND_REPAYMENT", 5, 100)
        result = reconstruct_state(anchor(10, 1100), NOW, OperationBatch([amortization_payment], True), [amortization], True)
        self.assertEqual(result.positions[0].quantity, Decimal(10))
        self.assertEqual(result.cash_by_currency["rub"], Decimal(1000))

    def test_transfers_require_confirmed_direction_and_respect_account(self):
        transfer = movement("transfer", "TRANS_BS_BS", 5, 0, 5)
        verified = event("transfer", account_id="account", operation_id="transfer", quantity_change=Decimal(5))
        result = reconstruct_state(anchor(10, 100), NOW, OperationBatch([transfer], True), [verified], True)
        self.assertEqual(result.positions[0].quantity, Decimal(5))
        other = replace(verified, account_id="other")
        untouched = reconstruct_state(anchor(10, 100), NOW, OperationBatch([], True), [other], True)
        self.assertEqual(untouched.positions[0].quantity, Decimal(10))
        unknown = replace(verified, quantity_change=None)
        result = reconstruct_state(anchor(10, 100), NOW, OperationBatch([transfer], True), [unknown], True)
        self.assertIsNone(result.positions[0].quantity)

    def test_ambiguous_same_time_and_unscoped_cash_are_rejected(self):
        split = event("split", ratio=Decimal(2))
        purchase = movement("buy", "BUY", 5, -100, 1)
        result = reconstruct_state(anchor(10, 100), NOW, OperationBatch([purchase], True), [split], True)
        self.assertIsNone(result.positions[0].quantity)
        self.assertEqual(result.coverage.positions, "partial")
        with self.assertRaises(ValueError):
            event("amortization", cash_payment=Decimal(100), currency="rub")

    def test_versioned_corporate_events_have_scope_and_date_filters(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        split = event("split", ratio=Decimal(2))
        repo.save_corporate_event(split)
        repo.save_corporate_event(split)
        self.assertEqual(repo.get_corporate_events("uid", NOW, NOW+timedelta(days=10), as_of=NOW), [split])
        self.assertEqual(repo.get_corporate_events("uid", NOW, NOW+timedelta(days=10), as_of=NOW-timedelta(days=1)), [])
        with self.assertRaises(ValueError):
            repo.save_corporate_event(replace(split, ratio=Decimal(3)))
        transfer = event("transfer", account_id="account", quantity_change=Decimal(1))
        transfer.id = "transfer"
        with self.assertRaises(ValueError):
            repo.save_corporate_event(transfer)
        self.assertEqual(repo.get_corporate_events("uid", NOW, NOW+timedelta(days=10), account_id="other"), [split])
