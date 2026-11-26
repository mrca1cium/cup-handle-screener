"""Exercise real orchestration with fake providers, clocks and isolated state."""
import copy
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import stashgamma_batch as batch
from scripts.candidate_priority import initialize_credit_ledger, credit_remaining
from scripts.initialize_twelvedata_ledger import main as initialize


class BatchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "docs/data").mkdir(parents=True)
        (root / "docs/data/results.json").write_text(json.dumps({
            "generated_at": "2026-11-20", "results": [{"symbol": "TD1"}],
            "watchlist": [], "refresh_priority": ["TD2"]}))
        self.now = 1800000000
        self.state = {"universe_symbols": ["TD1", "TD2", "SG", "NEW"],
                      "twelvedata_fallback_symbols": [],
                      "twelvedata_daily_ledger": initialize_credit_ledger(self.now - 90000, 320, True)}
        self.caches = {s: {"source": "twelvedata" if s.startswith("TD") else "stashgamma",
                           "fetched_at": 0, "data": {"dates": ["2026-11-13"]}}
                       for s in ("TD1", "TD2", "SG")}
        for s in ("SPY", "QQQ", "IWM"):
            self.caches[s] = {"source": "stashgamma", "fetched_at": 0,
                              "data": {"dates": ["2026-11-20"]}}
        self.calls = []
        self.cost = 1
        self.failure = None
        for p in (patch.dict(os.environ, STASHGAMMA_API_KEY="offline", TWELVEDATA_API_KEY="offline"),
                  patch.object(batch, "ROOT", str(root)),
                  patch.object(batch, "CACHE_DIR", str(root)),
                  patch.object(batch, "LOG_DIR", str(root)),
                  patch.object(batch, "candidate_rows", return_value=[]),
                  patch.object(batch, "load_state", side_effect=lambda: copy.deepcopy(self.state)),
                  patch.object(batch, "save_state", side_effect=lambda **kw: self.state.update(copy.deepcopy(kw))),
                  patch.object(batch, "load_cache_meta", side_effect=lambda s: self.caches.get(s)),
                  patch.object(batch, "latest_session", return_value="2026-11-20"),
                  patch.object(batch, "SECTORS", {}), patch.object(batch, "INDICES", {}),
                  patch.object(batch.data_mod, "_load_unavailable", return_value=set()),
                  patch.object(batch.data_mod, "_mark_unavailable"),
                  patch.object(batch.data_mod, "fetch_daily", side_effect=self.sg),
                  patch.object(batch.data_mod, "refresh_twelvedata", side_effect=self.td),
                  patch.object(batch.data_mod, "fetch_twelvedata", side_effect=self.td),
                  patch.object(batch.time, "time", return_value=self.now),
                  patch.object(batch.time, "monotonic", return_value=100),
                  patch.object(batch.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def sg(self, symbol, *args, **kwargs):
        self.calls.append(("SG", symbol))
        return {"dates": ["2026-11-20"]}

    def td(self, symbol, *args, **kwargs):
        # Reservation must be durable before any mock HTTP response.
        self.assertGreaterEqual(self.state["twelvedata_daily_ledger"]["credits"], 8)
        self.calls.append(("TD", symbol))
        batch.data_mod.TWELVEDATA_LAST_REQUEST_CREDITS = self.cost
        if self.failure:
            raise self.failure
        return {"dates": ["2026-11-20"]}

    def test_entire_batch_runs_and_reserves_before_requests(self):
        self.assertEqual(batch.run(4), 0)
        self.assertEqual(self.calls[0], ("TD", "TD1"))
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.state["twelvedata_daily_ledger"]["credits"], 16)
        self.assertEqual(self.state["updated_current"], 4)
        batch.time.sleep.assert_called_with(65.0)

    def test_missing_ledger_skips_twelve_data_and_backfills_stashgamma(self):
        self.state.pop("twelvedata_daily_ledger")
        self.assertEqual(batch.run(4), 0)
        self.assertTrue(self.calls)
        self.assertTrue(all(provider == "SG" for provider, _ in self.calls))
        self.assertEqual(self.state["data_diagnostics"]["TD1"]["status"], "CREDIT_BLOCKED")

    def test_unknown_cost_stops_and_quarantines_across_batches(self):
        self.cost = None
        self.assertEqual(batch.run(4), 2)
        self.assertEqual(self.calls, [("TD", "TD1")])
        self.assertEqual(credit_remaining(self.state["twelvedata_daily_ledger"], self.now + 90000, 320), 0)
        self.calls.clear()
        self.assertEqual(batch.run(4), 0)
        self.assertTrue(all(p == "SG" for p, _ in self.calls))

    def test_cost_above_reserve_stops_and_records_observed_cost(self):
        self.cost = 12
        self.assertEqual(batch.run(4), 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.state["twelvedata_daily_ledger"]["credits"], 12)

    def test_error_paths_never_refund_and_check_headers(self):
        for failure in (batch.data_mod.TwelveDataUnavailableError("unknown symbol"),
                        batch.data_mod.TwelveDataError("bad JSON"),
                        batch.data_mod.TwelveDataRateLimitError("429")):
            with self.subTest(failure=type(failure).__name__):
                self.state["twelvedata_daily_ledger"] = initialize_credit_ledger(self.now - 90000, 320, True)
                self.calls.clear()
                self.state.pop("twelvedata_unavailable_symbols", None)
                self.failure, self.cost = failure, None
                self.assertEqual(batch.run(4), 2)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.state["twelvedata_daily_ledger"]["credits"], 8)
                self.assertIn("blocked_reason", self.state["twelvedata_daily_ledger"])

    def test_none_response_also_checks_credit_headers(self):
        with patch.object(batch.data_mod, "refresh_twelvedata", return_value=None):
            self.assertEqual(batch.run(4), 2)
        self.assertIn("blocked_reason", self.state["twelvedata_daily_ledger"])

    def test_sequential_batches_share_one_budget(self):
        self.assertEqual(batch.run(1), 0)
        self.assertEqual(batch.run(1), 0)
        self.assertEqual(self.state["twelvedata_daily_ledger"]["credits"], 16)

    def test_concurrent_batch_does_not_send_requests(self):
        with (Path(self.temp.name) / "stashgamma_batch.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(batch.run(4), 2)
        self.assertEqual(self.calls, [])

    def test_p0_instruments_share_stock_request_cap(self):
        self.caches["SPY"]["data"]["dates"] = ["2026-11-13"]
        self.assertEqual(batch.run(1), 0)
        self.assertEqual(self.calls, [("SG", "SPY")])

    def test_missing_twelve_data_key_does_not_block_stashgamma(self):
        with patch.dict(os.environ, {"TWELVEDATA_API_KEY": ""}):
            self.assertEqual(batch.run(4), 0)
        self.assertTrue(self.calls)
        self.assertTrue(all(p == "SG" for p, _ in self.calls))

    def test_unmetered_type_metadata_request_is_never_made(self):
        self.state.pop("twelvedata_fallback_symbols")
        with patch.object(batch.data_mod, "TWELVEDATA_STOCKS_CACHE", str(Path(self.temp.name) / "missing")), \
             patch.object(batch.data_mod, "_load_twelvedata_stock_types", side_effect=AssertionError("unmetered request")):
            self.assertEqual(batch.run(4), 0)

    def test_state_write_failure_returns_retryable_before_request(self):
        with patch.object(batch, "save_state", side_effect=OSError("disk full")):
            self.assertEqual(batch.run(4), 2)
        self.assertEqual(self.calls, [])

    def test_native_index_codes_never_reach_market_provider(self):
        with patch.object(batch, "INDICES", {"^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^RUT": "Russell"}):
            self.assertEqual(batch.run(20), 0)
        self.assertTrue(all(not symbol.startswith("^") for _, symbol in self.calls))

    def test_known_cost_unavailable_does_not_block_other_updates_or_publication(self):
        self.failure = batch.data_mod.TwelveDataUnavailableError("HTTP 404: data not found")
        self.assertEqual(batch.run(4), 0)
        self.assertEqual(self.state["failed"], 0)
        self.assertEqual(self.state["unavailable"], 2)
        self.assertEqual(self.state["success"], 2)
        self.assertEqual(self.state["twelvedata_daily_ledger"]["credits"], 16)
        self.assertIn("TD1", self.state["twelvedata_unavailable_symbols"])
        self.calls.clear()
        self.assertEqual(batch.run(4), 0)
        self.assertTrue(all(provider == "SG" for provider, _ in self.calls))

    def test_old_provider_response_is_not_marked_current(self):
        with patch.object(batch.data_mod, "refresh_twelvedata", side_effect=lambda *a: (
                setattr(batch.data_mod, "TWELVEDATA_LAST_REQUEST_CREDITS", 1) or {"dates": ["2026-11-13"]})):
            self.assertEqual(batch.run(1), 0)
        self.assertEqual(self.state["updated_current"], 0)
        self.assertEqual(self.state["data_diagnostics"]["TD1"]["status"], "STALE")


class BootstrapTests(unittest.TestCase):
    def test_inspection_never_creates_ledger_or_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "state.json"
            p.write_text('{"other":123}')
            self.assertEqual(initialize(["--state", str(p)]), 0)
            self.assertEqual(p.read_text(), '{"other":123}')
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_bootstrap_preserves_other_state_and_enforces_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "state.json"
            p.write_text('{"other":123}')
            with patch("scripts.initialize_twelvedata_ledger.time.time", return_value=1000):
                self.assertEqual(initialize(["--state", str(p), "--initialize", "--exclusive-key"]), 0)
            state = json.loads(p.read_text())
            self.assertEqual(state["other"], 123)
            self.assertEqual(credit_remaining(state["twelvedata_daily_ledger"], 87399, 320), 0)
            self.assertEqual(credit_remaining(state["twelvedata_daily_ledger"], 87400, 320), 40)

    def test_bootstrap_cannot_overwrite_active_batch_state(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "state.json"
            p.write_text('{"other":123}')
            with (Path(directory) / "stashgamma_batch.lock").open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(initialize(["--state", str(p), "--initialize", "--exclusive-key"]), 1)
            self.assertEqual(p.read_text(), '{"other":123}')
