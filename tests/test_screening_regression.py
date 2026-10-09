"""Offline regression tests: no API calls, no market-data downloads."""
import unittest
from screener import stage2, pattern
from screener.main import _cheap_filter
from screener.market import SECTORS


def rising_data(n=300, volume=600_000):
    close = [50 + i * 0.5 for i in range(n)]
    return {"close": close, "high": [v * 1.01 for v in close],
            "low": [v * 0.99 for v in close],
            "volume": [volume] * n, "dates": [f"day-{i:03d}" for i in range(n)]}


class Stage2Tests(unittest.TestCase):
    def test_all_eight_uptrend_checks(self):
        result = stage2.check_stage2(rising_data(), 70)
        self.assertTrue(result["pass"])
        self.assertEqual(len(result["checks"]), 8)
        self.assertTrue(all(result["checks"].values()))

    def test_rs_below_threshold(self):
        result = stage2.check_stage2(rising_data(), 69.9)
        self.assertFalse(result["pass"])
        self.assertFalse(result["checks"]["8_rs_above_70"])

    def test_volume_below_threshold(self):
        result = stage2.check_stage2(rising_data(volume=499_999), 90)
        self.assertFalse(result["pass"])

    def test_short_history(self):
        result = stage2.check_stage2(rising_data(n=200), 99)
        self.assertFalse(result["pass"])

    def test_sma(self):
        self.assertEqual(stage2.sma([1, 2, 3, 4], 3), [None, None, 2, 3])


class UniverseTests(unittest.TestCase):
    def test_cheap_filter_counts(self):
        rows = [
            {"symbol": "PASS", "price": 10, "current_volume": 50_000, "market_cap": 2_000_000_000},
            {"symbol": "FAIL", "price": 9, "current_volume": 50_000, "market_cap": 2_000_000_000},
            {"symbol": "UNKNOWN", "price": None, "current_volume": 50_000, "market_cap": 2_000_000_000},
        ]
        passed, stats = _cheap_filter(rows, 10, 50_000, 2_000_000_000)
        self.assertEqual([r["symbol"] for r in passed], ["PASS"])
        self.assertEqual(stats, {"input": 3, "passed": 1, "failed": 1, "unknown": 1})

    def test_sector_universe_has_15_etfs(self):
        self.assertEqual(len(SECTORS), 15)


class PatternTests(unittest.TestCase):
    def test_short_history_rejected(self):
        self.assertIsNone(pattern.detect(rising_data(n=259)))

    def test_flat_prices_rejected(self):
        d = rising_data()
        d["close"] = [100.0] * 300
        d["high"] = [101.0] * 300
        d["low"] = [99.0] * 300
        self.assertIsNone(pattern.detect(d))

    def test_returned_grade_and_checks_consistent(self):
        # A returned candidate must respect the currently implemented grading contract.
        d = rising_data()
        candidate = pattern.detect(d)
        if candidate is not None:
            checks = candidate["checks"]
            self.assertEqual(len(checks), 10)
            self.assertFalse(checks["not_too_sharp"] is False)
            self.assertFalse(checks["no_lower_lows"] is False)
            self.assertEqual(candidate["grade"], "match" if sum(checks.values()) >= 9 else "watch")


if __name__ == "__main__":
    unittest.main()
