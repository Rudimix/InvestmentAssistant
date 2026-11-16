"""Реконструкция реальных количеств и денег, включая закрытые позиции и частичные сделки."""

from datetime import timedelta
from decimal import Decimal
import unittest

import _support  # noqa: F401
from investment_journal.models import OperationBatch, PortfolioOperation, PortfolioSnapshot, SnapshotPosition, data_json
from investment_journal.portfolio_history import reconstruct_state, reconstruction_inputs
from investment_journal.portfolio_sources import operation_from
from test_analysis_models import NOW


def movement(identifier, kind, day, amount, quantity=None, parent="", commission=None, trades=None):
    raw = {"id": identifier, "date": (NOW+timedelta(days=day)).isoformat(), "state": "OPERATION_STATE_EXECUTED",
           "type": "OPERATION_TYPE_"+kind, "payment": {"currency": "rub", "value": str(amount)},
           "parentOperationId": parent}
    if quantity is not None:
        raw.update(ticker="TEST", classCode="TQBR", quantity=str(quantity), quantityDone=str(quantity), lot=10)
        price = abs(Decimal(str(amount))/Decimal(str(quantity)))
        raw["price"] = {"currency": "rub", "value": str(price)}
        raw["tradesInfo"] = {"trades": trades if trades is not None else [
            {"num": identifier+"-fill", "date": raw["date"], "quantity": str(quantity), "price": raw["price"]}]}
    if commission is not None:
        raw["commission"] = {"currency": "rub", "value": str(commission)}
    return operation_from(raw)


def anchor(quantity, cash):
    positions = [SnapshotPosition("rub", "RUB000UTSTOM", "currency", Decimal(cash), "rub", value=Decimal(cash))]
    if quantity:
        positions.append(SnapshotPosition("uid", "TEST", "share", Decimal(quantity), "rub", instrument_id="uid",
                                          aliases=["TEST_TQBR"]))
    return PortfolioSnapshot("anchor", 1, "account", NOW+timedelta(days=10), "rub", Decimal(1000), Decimal(cash), positions)


class PortfolioHistoryTests(unittest.TestCase):
    def test_legacy_operation_without_new_fields_preserves_buy_direction(self):
        old = PortfolioOperation("old", NOW+timedelta(days=2), "OPERATION_TYPE_BUY", "OPERATION_STATE_EXECUTED",
                                 Decimal(-100), "rub", instrument_id="uid", ticker="TEST", quantity=Decimal(1))
        restored = PortfolioOperation.from_dict({"id": "old", "occurred_at": old.occurred_at.isoformat(),
                                                  "operation_type": old.operation_type, "state": old.state,
                                                  "payment": "-100", "currency": "rub", "instrument_id": "uid",
                                                  "ticker": "TEST", "quantity": "1"})
        result = reconstruct_state(anchor(1, 900), NOW, OperationBatch([restored], True))
        self.assertEqual(result.positions, [])
        self.assertEqual(result.cash_by_currency["rub"], Decimal(1000))

    def test_both_past_boundaries_and_fully_sold_position(self):
        source = anchor(0, 1300)
        operations = OperationBatch([movement("buy", "BUY", 2, -1000, 10),
                                     movement("sell", "SELL", 8, 1200, 10), movement("deposit", "INPUT", 9, 100)], True)
        before = reconstruct_state(source, NOW+timedelta(days=1), operations)
        middle = reconstruct_state(source, NOW+timedelta(days=5), operations)
        self.assertEqual(before.positions, [])
        self.assertEqual(before.cash_by_currency["rub"], Decimal(1000))
        self.assertEqual(middle.positions[0].quantity, Decimal(10))
        self.assertEqual(middle.positions[0].ticker, "TEST")
        self.assertEqual(middle.cash_by_currency["rub"], Decimal(0))
        self.assertEqual(middle.coverage.positions, "complete")
        self.assertEqual(source.positions[0].quantity, Decimal(1300))

    def test_partial_execution_across_boundary_uses_actual_fills_and_units(self):
        fills = [
            {"num": "one", "date": (NOW+timedelta(days=2)).isoformat(), "quantity": "1",
             "price": {"value": "100", "currency": "rub"}},
            {"num": "two", "date": (NOW+timedelta(days=7)).isoformat(), "quantity": "2",
             "price": {"value": "110", "currency": "rub"}},
        ]
        operation = movement("buy", "BUY", 1, -320, 3, trades=fills)
        operation.requested_quantity = Decimal(5)
        operation.state = "OPERATION_STATE_PROGRESS"
        result = reconstruct_state(anchor(3, 680), NOW+timedelta(days=5), OperationBatch([operation], True))
        self.assertEqual(result.positions[0].quantity, Decimal(1))
        self.assertEqual(result.cash_by_currency["rub"], Decimal(900))

    def test_commission_and_duplicate_operations_are_counted_once(self):
        purchase = movement("buy", "BUY", 2, -1000, 10, commission=-1)
        fee = movement("fee", "BROKER_FEE", 2, -1, parent="buy")
        result = reconstruct_state(anchor(10, -1), NOW, OperationBatch([purchase, fee, purchase], True))
        self.assertEqual(result.positions, [])
        self.assertEqual(result.cash_by_currency["rub"], Decimal(1000))
        self.assertEqual(result.coverage.cash, "complete")

    def test_external_income_expenses_and_security_transfers(self):
        operations = OperationBatch([movement("in", "INPUT", 1, 100), movement("div", "DIVIDEND", 2, 10),
                                     movement("tax", "TAX", 3, -2), movement("out", "OUTPUT", 4, -50),
                                     movement("shares", "INPUT_SECURITIES", 5, 0, 10)], True)
        result = reconstruct_state(anchor(10, 1058), NOW, operations)
        self.assertEqual(result.positions, [])
        self.assertEqual(result.cash_by_currency["rub"], Decimal(1000))

    def test_incomplete_and_unknown_effects_preserve_unknown_values(self):
        source = anchor(10, 100)
        incomplete = reconstruct_state(source, NOW, OperationBatch([], False, ["Missing page"]))
        self.assertIsNone(incomplete.positions[0].quantity)
        self.assertIsNone(incomplete.cash_by_currency["rub"])
        self.assertIsNone(incomplete.total)
        unknown = movement("unknown", "OTHER", 2, -10, 1)
        result = reconstruct_state(source, NOW, OperationBatch([unknown], True))
        self.assertIsNone(result.positions[0].quantity)
        self.assertEqual(result.coverage.operations, "partial")

    def test_missing_execution_times_and_unconfirmed_fee_make_coverage_partial(self):
        old = movement("old", "BUY", 1, -100, 1)
        old.trades = []
        old.execution_complete = False
        result = reconstruct_state(anchor(1, 900), NOW+timedelta(days=5), OperationBatch([old], True))
        self.assertIsNone(result.positions[0].quantity)
        self.assertIsNone(result.cash_by_currency["rub"])
        purchase = movement("buy", "BUY", 2, -100, 1, commission=-1)
        result = reconstruct_state(anchor(1, 899), NOW, OperationBatch([purchase], True))
        self.assertEqual(result.coverage.cash, "partial")

    def test_repeated_calculation_has_identical_identity_and_source_versions(self):
        source = anchor(1, 900)
        operations = OperationBatch([movement("buy", "BUY", 2, -100, 1)], True)
        first = reconstruct_state(source, NOW, operations)
        second = reconstruct_state(source, NOW, operations)
        self.assertEqual(data_json(first), data_json(second))
        inputs = reconstruction_inputs(source, operations)
        self.assertEqual(first.coverage.source_versions, {key: value["version"] for key, value in inputs.items()})
        with self.assertRaises(ValueError):
            reconstruct_state(source, source.observed_at+timedelta(seconds=1), operations)
