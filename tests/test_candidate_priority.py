"""Offline regression tests for candidate-first scheduling."""
import unittest
from scripts.candidate_priority import (
    eligible_for_refresh, last_bar_date, priority_symbols, schedule,
    update_candidate_history, historical_priority, select_provider_jobs,
    credit_remaining, reserve_credit,
)


class CandidatePriorityTests(unittest.TestCase):
    def test_candidate_set_from_existing_results(self):
        previous = {"results": [{"symbol": "ABC"}], "watchlist": [{"symbol": "XYZ"}]}
        self.assertEqual(priority_symbols(previous, ["ABC", "XYZ", "DEF"]), {"ABC", "XYZ"})

    def test_candidates_beat_general_universe(self):
        jobs = [{"symbol": "LOW", "tier": 3, "age_days": 100},
                {"symbol": "MATCH", "tier": 1, "age_days": 2},
                {"symbol": "NEW", "tier": 2, "age_days": 50}]
        self.assertEqual(schedule(jobs, 3)[0]["symbol"], "MATCH")

    def test_general_reserve_prevents_starvation(self):
        jobs = [{"symbol": "P%d" % i, "tier": 1} for i in range(50)]
        jobs += [{"symbol": "G%d" % i, "tier": 3} for i in range(50)]
        result = schedule(jobs, 10)
        self.assertEqual(len(result), 10)
        self.assertGreaterEqual(sum(j["tier"] == 3 for j in result), 2)

    def test_never_exceeds_limit_or_duplicates(self):
        jobs = [{"symbol": "A", "tier": 3}, {"symbol": "A", "tier": 1},
                {"symbol": "B", "tier": 2}]
        result = schedule(jobs, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "A")

    def test_stale_priority_respects_retry_throttle(self):
        payload = {"fetched_at": 1000, "data": {"dates": ["2026-11-13"]}}
        self.assertFalse(eligible_for_refresh(payload, "2026-11-20", 1000 + 3600, 7 * 86400, True))
        self.assertTrue(eligible_for_refresh(payload, "2026-11-20", 1000 + 13 * 3600, 7 * 86400, True))
        self.assertFalse(eligible_for_refresh(payload, "2026-11-20", 1000 + 13 * 3600, 7 * 86400, False))

    def test_up_to_date_priority_not_refetched(self):
        payload = {"fetched_at": 1000, "data": {"dates": ["2026-11-20"]}}
        self.assertFalse(eligible_for_refresh(payload, "2026-11-20", 1000 + 3600, 7 * 86400, True))

    def test_bad_date_safe(self):
        self.assertIsNone(last_bar_date({"data": {"dates": ["n/a"]}}))

    def test_history_retains_former_candidates(self):
        old = {"2026-11-19": ["OLD"]}
        new = update_candidate_history(old, {"results": [{"symbol": "NEW"}]},
                                       ["OLD", "NEW"], "2026-11-20")
        self.assertEqual(historical_priority(new, ["OLD", "NEW"]), {"OLD", "NEW"})

    def test_history_bounded_by_sessions(self):
        old = {"2026-10-%02d" % i: ["A"] for i in range(1, 28)}
        result = update_candidate_history(old, {}, ["A"], "2026-11-20")
        self.assertEqual(len(result), 20)

    def test_provider_cap_skips_and_backfills(self):
        jobs = [{"symbol": "TD1", "provider": "twelvedata"},
                {"symbol": "TD2", "provider": "twelvedata"},
                {"symbol": "SG", "provider": "stashgamma"}]
        result = select_provider_jobs(jobs, 2, 1)
        self.assertEqual([x["symbol"] for x in result], ["TD1", "SG"])

    def test_provider_zero_credits(self):
        jobs = [{"symbol": "TD", "provider": "twelvedata"},
                {"symbol": "SG", "provider": "stashgamma"}]
        self.assertEqual([x["symbol"] for x in select_provider_jobs(jobs, 2, 0)], ["SG"])

    def test_credit_reservations_accumulate(self):
        now = 1795266000
        ledger = {"window_start": now, "credits": 0}
        for i in range(3):
            ledger = reserve_credit(ledger, now, 24)
            self.assertEqual(ledger["credits"], (i + 1) * 8)
        self.assertIsNone(reserve_credit(ledger, now, 24))
        self.assertEqual(credit_remaining(ledger, now, 24), 0)

    def test_credit_ledger_rolling_window(self):
        now = 1795266000
        ledger = reserve_credit({"window_start": now, "credits": 0}, now, 8)
        self.assertEqual(credit_remaining(ledger, now + 86399, 8), 0)
        self.assertEqual(credit_remaining(ledger, now + 86400, 8), 1)

    def test_missing_or_invalid_ledger_fails_closed(self):
        now = 1795266000
        self.assertEqual(credit_remaining(None, now, 320), 0)
        self.assertIsNone(reserve_credit(None, now, 320))
        self.assertEqual(credit_remaining({"date": "2026-11-21", "credits": 0}, now, 320), 0)
        self.assertEqual(credit_remaining({"window_start": now + 60, "credits": 0}, now, 320), 0)

    def test_synthetic_1959_stock_candidate_coverage(self):
        # Synthetic stress test, not a measurement of the user's real cache.
        jobs = [{"symbol": "P%04d" % i, "provider": "stashgamma",
                 "tier": 1, "age_days": 5} for i in range(28)]
        jobs += [{"symbol": "G%04d" % i, "provider": "stashgamma",
                  "tier": 3, "age_days": 10} for i in range(1931)]
        chosen = schedule(jobs, 250)
        self.assertEqual(len(chosen), 250)
        self.assertTrue({"P%04d" % i for i in range(28)}.issubset(
            {j["symbol"] for j in chosen}))
        self.assertGreaterEqual(sum(j["tier"] == 3 for j in chosen), 50)

    def test_history_timestamp_normalized(self):
        history = update_candidate_history(
            {}, {"results": [{"symbol": "ABC"}]}, ["ABC"],
            "2026-11-20T19:30:00Z")
        self.assertEqual(list(history), ["2026-11-20"])

    def test_zero_budget(self):
        self.assertEqual(schedule([{"symbol": "A", "tier": 1}], 0), [])


if __name__ == "__main__":
    unittest.main()
