"""Offline integration tests for the cache-to-website builder.

All market data and metadata are synthetic. No network calls or API credits.
"""
import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_website_from_cache as builder


def bars(last_date, volume=600_000, count=300):
    end = datetime.date.fromisoformat(last_date)
    dates = [(end - datetime.timedelta(days=count - 1 - i)).isoformat()
             for i in range(count)]
    prices = [100.0 + i * 0.1 for i in range(count)]
    return {
        "dates": dates, "open": prices[:], "close": prices[:],
        "high": [p + 1 for p in prices],
        "low": [p - 1 for p in prices],
        "volume": [volume] * count,
    }


def fake_pattern(data):
    return {
        "grade": "match", "chart_start": 280,
        "metrics": {"rim_date": data["dates"][240],
                    "bottom_date": data["dates"][250], "pivot": 125.0},
        "checks": {"example": True},
    }


class WebsiteBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output_dir = self.temp.name
        self.today = "2026-10-08"
        self.data = {
            "SPY": bars(self.today),
            "QQQ": bars(self.today),
            "IWM": bars(self.today),
            "XLK": bars(self.today),
            "XLV": bars("2026-09-20"),
            "FRESH": bars(self.today),
            "STALE": bars("2026-09-20"),
            "THIN": bars(self.today, volume=100_000),
        }
        self.state = {
            "universe_symbols": ["FRESH", "STALE", "THIN", "MISSING"],
            "universe_core_count": 1500,
            "universe_extra_count": 5000,
            "universe_broad_count": 6500,
            "universe_cheap_stats": {"input": 6500, "passed": 1900,
                                      "failed": 4600, "unknown": 0},
        }
        self.patches = [
            patch.object(builder, "OUT_DIR", self.output_dir),
            patch.object(builder, "load_state", return_value=self.state),
            patch.object(builder, "load_cache", return_value=self.data),
            patch.object(builder, "build_metadata", return_value={
                s: {"name": s, "sector": "Information Technology"}
                for s in self.state["universe_symbols"]
            }),
            patch.object(builder, "load_previous_output", return_value={
                "market": {"indices": {"OLD": {"drawdown_pct": 99}},
                           "sectors_all": [{"symbol": "OLD"}]}
            }),
            patch.object(builder.stage2, "rs_ratings", return_value={
                "FRESH": 90, "STALE": 90, "THIN": 90
            }),
            patch.object(builder.stage2, "check_stage2", return_value={
                "pass": True, "values": {"rs_rating": 90}
            }),
            patch.object(builder.pattern, "detect", side_effect=fake_pattern),
            patch.object(builder.data_mod, "_load_unavailable", return_value=set()),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(builder.main(), 0)
        with open(os.path.join(self.output_dir, "results.json"), encoding="utf-8") as f:
            self.results = json.load(f)
        with open(os.path.join(self.output_dir, "charts.json"), encoding="utf-8") as f:
            self.charts = json.load(f)

    def test_stale_missing_and_illiquid_stocks_excluded(self):
        self.assertEqual([r["symbol"] for r in self.results["results"]], ["FRESH"])
        self.assertEqual(self.results["watchlist"], [])
        self.assertEqual(list(self.charts), ["FRESH"])
        self.assertEqual(self.results["total_scanned"], 1)
        self.assertEqual(self.results["universe_stats"]["liquidity_pass"], 1)

    def test_market_radar_no_old_values_and_proxy_provenance(self):
        market = self.results["market"]
        self.assertEqual(market["date"], self.today)
        self.assertNotIn("OLD", market["indices"])
        self.assertEqual(market["indices"]["^IXIC"]["source_symbol"], "QQQ")
        self.assertTrue(market["indices"]["^IXIC"]["is_proxy"])
        self.assertEqual(market["sector_coverage"]["available"], 1)
        self.assertEqual(market["sector_coverage"]["expected"], 15)
        self.assertIn("XLV", market["sector_coverage"]["missing"])

    def test_output_contract_and_universe_diagnostics(self):
        self.assertEqual(self.results["generated_at"], self.today)
        self.assertEqual(self.results["universe_stats"]["broad_input"], 6500)
        self.assertEqual(self.results["universe_stats"]["cheap_filter_original"]["passed"], 1900)
        self.assertEqual(self.results["universe_stats"]["historical_selected"], 4)
        self.assertEqual(self.results["universe_stats"]["historical_downloaded"], 3)
        self.assertEqual(self.results["universe_stats"]["excluded_missing_or_stale"], 2)
        self.assertEqual(len(self.charts["FRESH"]["close"]), 20)
        self.assertEqual(self.charts["FRESH"]["pivot"], 125.0)


if __name__ == "__main__":
    unittest.main()
