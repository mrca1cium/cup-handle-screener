"""Step 4: synthetic shape regression tests. Offline and deterministic."""
import unittest
from screener import pattern


def cup_with_handle():
    n = 300
    d = {
        "dates": [f"day-{i:03d}" for i in range(n)],
        "open": [102.0] * n,
        "close": [102.0] * n,
        "high": [105.0] * n,
        "low": [100.0] * n,
        "volume": [600_000] * n,
    }
    # Left rim, bottom, recovery, then 30-bar handle.
    d["high"][190] = 120.0
    for i in range(191, 270):
        d["high"][i] = 107.0
        d["low"][i] = 97.0
    d["low"][220] = 90.0
    for i in range(270, n):
        d["close"][i] = 110.0
        d["open"][i] = 109.0
        d["high"][i] = 118.0
        d["low"][i] = 106.0
    return d


class CupDurationTests(unittest.TestCase):
    def test_full_cup_includes_recovery(self):
        d = cup_with_handle()
        result = pattern.detect(d)
        self.assertIsNotNone(result, "Synthetic cup should be detected")
        self.assertEqual(result["rim_i"], 190)
        self.assertEqual(result["bot_i"], 220)
        self.assertEqual(result["handle_start"], 270)
        self.assertEqual(result["metrics"]["cup_days"], 80)
        self.assertEqual(result["metrics"]["handle_days"], 29)

    def test_reject_handle_longer_than_full_cup(self):
        d = cup_with_handle()
        # This test is intentionally limited to the existing 4-42-day handle gate.
        for i in range(245, 300):
            d["close"][i] = 110.0
            d["high"][i] = 118.0
            d["low"][i] = 106.0
        self.assertIsNone(pattern.detect(d))


if __name__ == "__main__":
    unittest.main()
