"""Единицы, валюты, периоды, страницы и неоднозначные ячейки финансовых таблиц."""

from dataclasses import replace
from decimal import Decimal as D
import unittest

import _support  # noqa: F401
from investment_journal.financial_extraction import extract_financials
from investment_journal.models import EvidenceDocument
from test_analysis_models import NOW


class FinancialExtractionTests(unittest.TestCase):
    def setUp(self):
        self.doc = EvidenceDocument("report", "v1", "Issuer", "https://example.com/report", "Отчёт", "", NOW,
                                    available_at=NOW, kind="report_content")

    def test_millions_thousands_and_explicit_missing_number(self):
        header = ("Консолидированная финансовая отчетность по МСФО\n"
                  "За год, закончившийся 31 декабря 2025 года\n"
                  "в миллионах российских рублей    2025 года    2024 года\n")
        result = extract_financials(self.doc, [header+"Выручка    1 200,5    1 000\nЧистая прибыль    (10)    —"], "TEST")
        facts = {(fact.metric, fact.period_end): fact for fact in result.facts}
        self.assertEqual(facts["revenue", "2025-12-31"].value, D(1200500000))
        self.assertEqual(facts["net_profit", "2025-12-31"].value, D(-10000000))
        self.assertIsNone(facts["net_profit", "2024-12-31"].value)
        self.assertEqual(facts["revenue", "2025-12-31"].period_start, "2025-01-01")
        self.assertEqual(facts["revenue", "2025-12-31"].currency, "rub")
        self.assertEqual(result.chunks[0].page, 1)
        smaller = extract_financials(self.doc, [header.replace("миллионах", "тысячах")+"Выручка    100    90"], "TEST")
        self.assertEqual(smaller.facts[0].value, D(100000))

    def test_quarter_and_cumulative_periods_are_distinct(self):
        header = ("Консолидированная отчетность МСФО\nЗа {duration} месяцев, закончившихся 30 сентября 2025 года\n"
                  "в миллионах рублей    2025    2024\nВыручка    100    90")
        cumulative = extract_financials(self.doc, [header.format(duration=9)], "TEST")
        quarter = extract_financials(self.doc, [header.format(duration=3)], "TEST")
        self.assertEqual(cumulative.facts[0].period_start, "2025-01-01")
        self.assertEqual(cumulative.facts[1].period_end, "2024-09-30")
        self.assertEqual(quarter.facts[0].period_start, "2025-07-01")
        self.assertNotEqual(cumulative.facts[0].id, quarter.facts[0].id)

    def test_currency_ambiguity_conflict_and_calendar_are_not_facts(self):
        header = "Консолидированная отчетность МСФО\nЗа год, закончившийся 31 декабря 2025 года\nв миллионах долларов США    2025    2024\n"
        result = extract_financials(self.doc, [header+"Выручка    100    90"], "TEST")
        self.assertEqual(result.facts[0].currency, "usd")
        conflict = extract_financials(self.doc, [header+"Выручка    100    90\nВыручка    101    90"], "TEST")
        self.assertFalse(any(f.period_end == "2025-12-31" for f in conflict.facts))
        ambiguous = extract_financials(self.doc, [header.replace("долларов США", "долларов США и рублей")+"Выручка    100    90"], "TEST")
        self.assertFalse(ambiguous.facts)
        self.assertFalse(extract_financials(replace(self.doc, kind="report_calendar"), [header], "TEST").facts)


if __name__ == "__main__":
    unittest.main()
