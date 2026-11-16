"""Календарные границы и политики периода, включая DST и незавершённый день."""

from datetime import date, datetime, timedelta, timezone
import json
import unittest
from zoneinfo import ZoneInfo

import _support  # noqa: F401
from investment_journal.analysis_periods import (
    analysis_date, explicit_period, period_description, resolve_period_request, rolling_period,
)
from investment_journal.models import AnalysisPeriod, data_json

NOW = datetime(2026, 11, 16, 8, 30, tzinfo=timezone.utc)


class AnalysisPeriodTests(unittest.TestCase):
    def test_explicit_range_includes_all_local_days_and_never_uses_server_timezone(self):
        period = explicit_period("01.10.2026", "31.10.2026", "Europe/Moscow", NOW)
        self.assertEqual(period.start, datetime(2026, 9, 30, 21, tzinfo=timezone.utc))
        self.assertEqual(period.end, datetime(2026, 10, 31, 21, tzinfo=timezone.utc))
        self.assertEqual(period.end-period.start, timedelta(days=31))
        self.assertIn("Europe/Moscow", period_description(period))
        self.assertNotIn("не завершён", period_description(period))

    def test_today_is_capped_at_confirmation_and_survives_serialization(self):
        period = explicit_period("16.11.2026", "16.11.2026", "Europe/Moscow", NOW)
        self.assertEqual(period.end, NOW)
        self.assertIn("ещё не завершён", period_description(period))
        restored = AnalysisPeriod.from_dict(json.loads(data_json(period)))
        self.assertEqual(restored, period)
        self.assertEqual(restored.end, NOW)

    def test_rolling_presets_end_at_last_completed_local_day(self):
        for days in (7, 30, 90):
            with self.subTest(days=days):
                period = rolling_period(days, "Europe/Moscow", NOW)
                self.assertEqual(period.local_end, "2026-11-15")
                self.assertEqual(period.end, datetime(2026, 11, 15, 21, tzinfo=timezone.utc))
                self.assertEqual(period.end-period.start, timedelta(days=days))
                self.assertEqual(period.policy, "rolling")

    def test_spring_and_fall_days_use_local_calendar_boundaries(self):
        spring = explicit_period("08.03.2026", "08.03.2026", "America/New_York", NOW)
        fall = explicit_period("01.11.2026", "01.11.2026", "America/New_York", NOW)
        self.assertEqual(spring.end-spring.start, timedelta(hours=23))
        self.assertEqual(fall.end-fall.start, timedelta(hours=25))
        midnight_gap = explicit_period("04.11.2018", "04.11.2018", "America/Sao_Paulo", NOW)
        self.assertEqual(midnight_gap.start.astimezone(ZoneInfo("America/Sao_Paulo")).hour, 1)

    def test_date_formats_policies_and_fixed_schedule_reference(self):
        self.assertEqual(analysis_date(date(2026, 10, 1)), date(2026, 10, 1))
        self.assertEqual(analysis_date("2026-10-01"), date(2026, 10, 1))
        request = {"kind": "rolling", "days": 30, "zone": "Europe/Moscow"}
        nominal = NOW-timedelta(days=7)
        period = resolve_period_request(request, nominal)
        self.assertEqual(period.requested_at, nominal)
        self.assertEqual(period.local_end, "2026-11-08")
        explicit = resolve_period_request({"kind": "explicit", "first": "01.10.2026", "last": "31.10.2026"}, NOW)
        self.assertEqual(explicit.policy, "explicit")

    def test_bad_dates_zones_and_overflow_fail_before_execution(self):
        for first, last, zone in (("31.10.2026", "01.10.2026", "UTC"),
                                  ("01.10.2026", "17.11.2026", "UTC"),
                                  ("31.02.2026", "01.03.2026", "UTC"),
                                  ("01.10.2026", "31.10.2026", "Missing/Zone")):
            with self.subTest(first=first, last=last, zone=zone), self.assertRaises(ValueError):
                explicit_period(first, last, zone, NOW)
        for days in (0, -1, 1.5, True, "7", 10**20):
            with self.subTest(days=days), self.assertRaises(ValueError):
                rolling_period(days, "UTC", NOW)
        with self.assertRaises(ValueError):
            resolve_period_request({"kind": "anything"}, NOW)
        with self.assertRaises(ValueError):
            explicit_period("16.11.2026", "16.11.2026", "UTC", NOW.replace(tzinfo=None))
