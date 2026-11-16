"""Точность и восстановление аналитических моделей."""

import json
from datetime import datetime, timezone
from decimal import Decimal
import unittest

import _support  # noqa: F401
from investment_journal.models import (
    PortfolioOperation, PortfolioSnapshot, SnapshotPosition, data_json, decimal_value,
)


class PortfolioModelTests(unittest.TestCase):
    def test_snapshot_round_trip_preserves_precision_and_missing_values(self):
        position = SnapshotPosition("uid-1", "TEST", "share", Decimal("2.000000001"), "rub",
                                    price=Decimal("123456789.123456789"))
        snapshot = PortfolioSnapshot("snapshot", 1, "account", datetime.now(timezone.utc),
                                     "rub", Decimal("123456789.123456789"), None, [position])
        restored = PortfolioSnapshot.from_dict(json.loads(data_json(snapshot)))
        self.assertEqual(restored, snapshot)
        self.assertIsNone(restored.positions[0].value)
        self.assertIsNone(restored.cash)

    def test_money_formats_and_zero(self):
        self.assertEqual(decimal_value({"units": "-2", "nano": -1}), Decimal("-2.000000001"))
        self.assertEqual(decimal_value({"value": "0"}), Decimal(0))
        self.assertEqual(decimal_value("0.123456789123456789"), Decimal("0.123456789123456789"))
        for value in (None, {}, "NaN", "Infinity", "invalid", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decimal_value(value)

    def test_operation_round_trip(self):
        operation = PortfolioOperation("op", datetime.now(timezone.utc), "OPERATION_TYPE_BUY",
                                       "OPERATION_STATE_EXECUTED", Decimal("-10.000000001"), "rub")
        self.assertEqual(PortfolioOperation.from_dict(json.loads(data_json(operation))), operation)
