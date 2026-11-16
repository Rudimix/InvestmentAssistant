"""Периодичность и границы времени без ожидания реальных запусков."""

from datetime import datetime, timedelta, timezone
import unittest
import zoneinfo

import _support  # noqa: F401
from investment_journal.report_scheduling import (
    CalendarRule, IntervalRule, initial_run, parse_calendar_rule, parse_interval_rule, rule_from_dict, schedule_preview,
)

NOW = datetime(2026, 11, 16, 9, tzinfo=timezone.utc)


class IntervalRuleTests(unittest.TestCase):
    def test_six_hours_and_delayed_execution_keep_anchor(self):
        rule = IntervalRule(6, "hours", NOW)
        self.assertEqual(rule.next_after(NOW-timedelta(seconds=1)), NOW)
        self.assertEqual(rule.next_after(NOW), NOW+timedelta(hours=6))
        self.assertEqual(rule.next_after(NOW+timedelta(hours=7)), NOW+timedelta(hours=12))
        self.assertEqual(rule_from_dict(rule.to_dict()), rule)

    def test_three_days_are_fixed_elapsed_time(self):
        rule = IntervalRule(3, "days", NOW)
        self.assertEqual(rule.next_after(NOW+timedelta(days=3, hours=1)), NOW+timedelta(days=6))

    def test_invalid_inputs_and_overflow(self):
        for value in (0, -1, 1.5, "6", True, 10**20):
            with self.subTest(value=value), self.assertRaises(ValueError):
                IntervalRule(value, "hours", NOW)
        with self.assertRaises(ValueError):
            IntervalRule(1, "minutes", NOW)
        with self.assertRaises(ValueError):
            IntervalRule(1, "hours", datetime(9999, 12, 31, 23, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            IntervalRule(1, "hours", NOW).next_after(datetime.max.replace(tzinfo=timezone.utc))


class CalendarRuleTests(unittest.TestCase):
    def test_daily_weekdays_sunday_and_timezone(self):
        daily = CalendarRule(tuple(range(7)), "19:00", "Europe/Moscow")
        self.assertEqual(daily.next_after(NOW), NOW.replace(hour=16))
        weekdays = CalendarRule((0, 1, 2, 3, 4), "19:00", "Europe/Moscow")
        friday_evening = datetime(2026, 11, 20, 17, tzinfo=timezone.utc)
        self.assertEqual(weekdays.next_after(friday_evening), datetime(2026, 11, 23, 16, tzinfo=timezone.utc))
        sunday = CalendarRule((6,), "12:00", "Europe/Moscow")
        self.assertEqual(sunday.next_after(NOW), datetime(2026, 11, 22, 9, tzinfo=timezone.utc))
        self.assertEqual(rule_from_dict(daily.to_dict()), daily)

    def test_invalid_timezone_day_and_time(self):
        for changes in ({"timezone": "Missing/Zone"}, {"weekdays": ()}, {"weekdays": (7,)},
                        {"local_time": "24:00"}, {"local_time": "9:00"}):
            values = {"weekdays": (0,), "local_time": "19:00", "timezone": "UTC"} | changes
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                CalendarRule(**values)

    def test_tzdata_fallback_without_system_iana_database(self):
        original = zoneinfo.TZPATH
        try:
            zoneinfo.reset_tzpath(())
            zoneinfo.ZoneInfo.clear_cache()
            rule = CalendarRule(tuple(range(7)), "19:00", "Europe/Moscow")
            self.assertEqual(rule.next_after(NOW), NOW.replace(hour=16))
        finally:
            zoneinfo.reset_tzpath(original)
            zoneinfo.ZoneInfo.clear_cache()


class TimeTransitionTests(unittest.TestCase):
    def test_spring_gap_uses_first_valid_moment(self):
        rule = CalendarRule(tuple(range(7)), "02:30", "America/New_York")
        before = datetime(2026, 3, 8, 6, tzinfo=timezone.utc)
        self.assertEqual(rule.next_after(before), datetime(2026, 3, 8, 7, tzinfo=timezone.utc))

    def test_fall_overlap_runs_once_at_first_occurrence(self):
        rule = CalendarRule(tuple(range(7)), "01:30", "America/New_York")
        before = datetime(2026, 11, 1, 4, tzinfo=timezone.utc)
        self.assertEqual(rule.next_after(before), datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
        after_first = datetime(2026, 11, 1, 5, 45, tzinfo=timezone.utc)
        self.assertEqual(rule.next_after(after_first), datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc))

    def test_past_anchor_and_preview_in_user_timezone(self):
        rule = IntervalRule(6, "hours", NOW-timedelta(hours=7), "Europe/Moscow")
        self.assertEqual(initial_run(rule, NOW), NOW+timedelta(hours=5))
        preview = schedule_preview("account", rule, NOW)
        self.assertIn("17:00 +0300", preview)
        self.assertIn("Europe/Moscow", preview)


class DocumentedInputTests(unittest.TestCase):
    def test_interval_examples_and_custom_first_time(self):
        self.assertEqual(parse_interval_rule("6ч", "Europe/Moscow", NOW).next_after(NOW), NOW+timedelta(hours=6))
        self.assertEqual(parse_interval_rule("3д", "Europe/Moscow", NOW).next_after(NOW), NOW+timedelta(days=3))
        custom = parse_interval_rule("6ч 17.11.2026 09:00", "Europe/Moscow", NOW)
        self.assertEqual(custom.first_at, datetime(2026, 11, 17, 6, tzinfo=timezone.utc))

    def test_calendar_examples(self):
        for text, days, local_time in (("ежедневно 09:00", tuple(range(7)), "09:00"),
                                       ("будни 19:00", (0, 1, 2, 3, 4), "19:00"),
                                       ("пн,ср,пт 12:00", (0, 2, 4), "12:00")):
            rule = parse_calendar_rule(text, "Europe/Moscow")
            self.assertEqual((rule.weekdays, rule.local_time), (days, local_time))
