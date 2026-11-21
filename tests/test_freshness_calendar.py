"""Calendar and queue tests use fixed clocks and synthetic cache metadata."""
import datetime as dt
import unittest
from unittest.mock import patch

from screener.freshness import latest_session, is_session, coverage


class CalendarTests(unittest.TestCase):
    def target(self, stamp):
        return latest_session(dt.datetime.fromisoformat(stamp))

    def test_weekend(self):
        self.assertEqual(self.target("2026-11-14T10:00:00+00:00"), "2026-11-13")

    def test_close_and_provider_grace(self):
        self.assertEqual(self.target("2026-11-13T21:59:59+00:00"), "2026-11-12")
        self.assertEqual(self.target("2026-11-13T22:00:00+00:00"), "2026-11-13")

    def test_daylight_saving(self):
        self.assertEqual(self.target("2026-07-06T20:59:59+00:00"), "2026-07-02")
        self.assertEqual(self.target("2026-07-06T21:00:00+00:00"), "2026-07-06")

    def test_holidays(self):
        for date in ("2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
                     "2026-11-26", "2026-12-25", "2025-01-09"):
            with self.subTest(date=date):
                self.assertFalse(is_session(dt.date.fromisoformat(date)))
        self.assertTrue(is_session(dt.date(2026, 11, 11)))

    def test_new_year_saturday_does_not_close_friday(self):
        self.assertTrue(is_session(dt.date(2021, 12, 31)))
        self.assertFalse(is_session(dt.date(2023, 1, 2)))

    def test_early_close_with_grace(self):
        self.assertEqual(self.target("2026-11-27T18:59:59+00:00"), "2026-11-25")
        self.assertEqual(self.target("2026-11-27T19:00:00+00:00"), "2026-11-27")
        self.assertEqual(self.target("2026-12-24T19:00:00+00:00"), "2026-12-24")

    def test_coverage_does_not_count_future_dates(self):
        self.assertEqual(coverage("2026-11-13", [None, "2026-11-12", "2026-11-13", "2026-11-16"])["current"], 1)

