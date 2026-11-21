"""Offline, deterministic tests for the isolated VCP B-mode detector."""
import copy
import unittest

from screener.vcp import confirmed_pivots, detect, validate_ohlcv


def fixture():
    n = 290
    dates = ["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28) for i in range(n)]
    # 2026-11 is outside the calendar but ISO strings are ordered; this
    # fixture tests data-shape mechanics, not trading-calendar validity.
    c = [100.0] * n
    h = [101.0] * n
    l = [99.0] * n
    o = [100.0] * n
    v = [1_000_000.0] * n
    return dict(dates=dates, open=o, high=h, low=l, close=c, volume=v)


class TestVCP(unittest.TestCase):
    def test_reject_short_data(self):
        d = fixture()
        for k in d:
            d[k] = d[k][:200]
        self.assertFalse(validate_ohlcv(d))
        self.assertIsNone(detect(d, 90, True))

    def test_reject_inconsistent_lengths(self):
        d = fixture()
        d["volume"].pop()
        self.assertFalse(validate_ohlcv(d))

    def test_reject_duplicate_dates(self):
        d = fixture()
        d["dates"][-1] = d["dates"][-2]
        self.assertFalse(validate_ohlcv(d))

    def test_reject_invalid_ohlc(self):
        d = fixture()
        d["low"][-1] = 105
        self.assertFalse(validate_ohlcv(d))

    def test_stage2_gate(self):
        self.assertIsNone(detect(fixture(), 95, False))

    def test_rs_gate(self):
        self.assertIsNone(detect(fixture(), 79.9, True))

    def test_no_swing_no_signal(self):
        self.assertIsNone(detect(fixture(), 90, True))

    def test_deterministic(self):
        d = fixture()
        self.assertEqual(detect(d, 90, True), detect(copy.deepcopy(d), 90, True))

    def test_confirmation_waits_for_right_bars(self):
        d = fixture()
        # An isolated peak at index 280 is not confirmed before index 283.
        d["high"][280] = 115
        d["close"][280] = 100
        self.assertFalse(any(p["index"] == 280 for p in confirmed_pivots(d, 260, 283, 3)))
        self.assertTrue(any(p["index"] == 280 for p in confirmed_pivots(d, 260, 284, 3)))

    def test_historical_cutoff_rejects_future_only_swing(self):
        d = fixture()
        d["high"][280] = 115
        before = confirmed_pivots(d, 260, 283, 3)
        d["high"][288] = 120
        after = confirmed_pivots(d, 260, 283, 3)
        self.assertEqual(before, after)

    def test_no_mutation(self):
        d = fixture()
        old = copy.deepcopy(d)
        detect(d, 90, True)
        self.assertEqual(d, old)

    def test_invalid_asof(self):
        d = fixture()
        self.assertIsNone(detect(d, 90, True, as_of=999))

    def test_insufficient_liquidity(self):
        d = fixture()
        d["volume"] = [100.0] * len(d["volume"])
        self.assertIsNone(detect(d, 90, True))


if __name__ == "__main__":
    unittest.main()
