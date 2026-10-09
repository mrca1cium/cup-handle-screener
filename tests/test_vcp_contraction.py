"""Offline tests for progressive VCP proxy; no API requests."""
import unittest
from screener.pattern import handle_contractions


class VcpContractionTests(unittest.TestCase):
    def test_three_progressively_tighter_segments(self):
        highs = [120] * 3 + [118] * 3 + [116] * 3
        lows = [100] * 3 + [106] * 3 + [110] * 3
        out = handle_contractions(highs, lows)
        self.assertTrue(out["confirmed"])
        self.assertEqual(len(out["ranges_pct"]), 3)
        self.assertGreater(out["ranges_pct"][0], out["ranges_pct"][1])
        self.assertGreater(out["ranges_pct"][1], out["ranges_pct"][2])

    def test_tight_but_not_contracting_is_rejected(self):
        highs = [110] * 9
        lows = [106] * 9
        self.assertFalse(handle_contractions(highs, lows)["confirmed"])

    def test_widening_ranges_rejected(self):
        highs = [110] * 9
        lows = [108] * 3 + [106] * 3 + [103] * 3
        self.assertFalse(handle_contractions(highs, lows)["confirmed"])

    def test_short_handle_not_enough_to_confirm(self):
        out = handle_contractions([110] * 8, [105] * 8)
        self.assertFalse(out["confirmed"])
        self.assertEqual(out["ranges_pct"], [])

    def test_non_divisible_segment_length(self):
        highs = [120] * 4 + [118] * 4 + [116] * 5
        lows = [100] * 4 + [106] * 4 + [110] * 5
        self.assertTrue(handle_contractions(highs, lows)["confirmed"])


if __name__ == "__main__":
    unittest.main()
