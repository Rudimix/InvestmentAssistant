"""Исторический номинал, НКД, валюты, корректировки и маржинальная оценка."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
from pathlib import Path
import unittest

from investment_journal.historical_valuation import ValuationTerms, bond_terms, value_position, value_state
from investment_journal.models import CorporateEvent, HistoricalPosition, HistoricalState, PriceObservation, timestamp

AT = timestamp("2026-10-02T12:00:00Z")
FIXTURE = json.loads((Path(__file__).parent / "fixtures/history_mcp_contracts.json").read_text(encoding="utf-8"))


def position(kind="share", quantity="10", currency="rub"):
    return HistoricalPosition("uid", "TEST", kind, currency, D(quantity), instrument_id="uid")


def price(value="100", currency="rub"):
    return PriceObservation("uid", AT-timedelta(hours=2), AT-timedelta(hours=1), D(value), currency, AT, "v1")


def terms(**kwargs):
    return ValuationTerms("uid", "rub", AT-timedelta(days=1), AT+timedelta(days=1), "confirmed", "v1", **kwargs)


class HistoricalValuationTests(unittest.TestCase):
    def test_bond_real_response_uses_same_day_nominal_and_accrued(self):
        raw = FIXTURE["examples"]["bond_accrued"]["response"]
        parameters = bond_terms(raw, "uid", "rub", AT)
        self.assertEqual(parameters.nominal, D(1000))
        self.assertEqual(parameters.accrued_interest, D("41.61"))
        valued = value_position(position("bond", "2"), AT, price("90", "pt"), parameters)
        self.assertEqual(valued.value, D("1883.22"))
        amortized = replace(parameters, nominal=D(500))
        self.assertEqual(value_position(position("bond", "2"), AT, price("90", "pt"), amortized).value, D("983.22"))

    def test_missing_or_wrong_day_parameters_stay_unknown(self):
        self.assertIsNone(bond_terms(FIXTURE["examples"]["bond_accrued"]["response"], "uid", "rub", AT-timedelta(days=8)))
        for parameters in (None, terms(nominal=D(1000)), replace(terms(), valid_until=AT)):
            result = value_position(position("bond"), AT, price(currency="pt"), parameters)
            self.assertIsNone(result.value)
            self.assertTrue(result.quality)
        self.assertEqual(value_position(position("bond", "0"), AT, None).value, 0)

    def test_futures_value_is_unsettled_margin_cash_is_counted_once(self):
        p = position("future", "2")
        parameters = terms(price_step=D(5), step_value=D(10), settlement_price=D(95))
        valued = value_position(p, AT, price(currency="pt"), parameters)
        self.assertEqual(valued.value, D(20))
        self.assertEqual(valued.exposure, D(400))
        state = HistoricalState("s", 1, "account", AT, AT, "rub", positions=[p], cash_by_currency={"rub": D(1000)})
        self.assertEqual(value_state(state, [valued], {}).total, D(1020))
        self.assertIsNone(value_position(p, AT, price(currency="pt"), terms(price_step=D(5))).value)

    def test_split_adjustment_and_unknown_adjustment_basis(self):
        event = CorporateEvent("split", "uid", AT-timedelta(minutes=30), "split", "issuer", "v1", confirmed=True, ratio=D(2))
        p = position(quantity="20")
        result = value_position(p, AT, price(), terms(adjustment_factor=D(1)), [event])
        self.assertEqual(result.value, D(1000))
        self.assertIsNone(value_position(p, AT, price(), None, [event]).value)
        future_event = replace(event, effective_at=AT+timedelta(days=1))
        adjusted_price = price("50")
        restored = value_position(position(), AT, adjusted_price, terms(adjustment_factor=D(2)), [future_event])
        self.assertEqual(restored.value, D(1000))

    def test_currency_conversion_and_known_subtotal(self):
        p = position(currency="usd")
        valued = value_position(p, AT, price(currency="usd"))
        state = HistoricalState("s", 1, "account", AT, AT, "rub", positions=[p], cash_by_currency={"rub": D(100), "usd": D(5)})
        result = value_state(state, [valued], {"usd": D(90)})
        self.assertEqual(result.total, D(90550))
        missing = value_state(state, [valued], {})
        self.assertIsNone(missing.total)
        self.assertEqual(missing.known_value, D(100))
        self.assertEqual(missing.coverage.prices, "partial")
        with self.assertRaises(ValueError):
            value_state(state, [], {})

    def test_invalid_units_identity_age_and_unknown_contract(self):
        for quote in (price(currency="pt"), replace(price(), instrument_id="other"),
                      replace(price(), candle_end=AT+timedelta(hours=1)),
                      replace(price(), candle_start=AT-timedelta(days=21), candle_end=AT-timedelta(days=20))):
            self.assertIsNone(value_position(position(), AT, quote).value)
        self.assertIsNone(value_position(position("option"), AT, price()).value)
        with self.assertRaises(ValueError):
            terms(price_step=D(0))


if __name__ == "__main__":
    unittest.main()
