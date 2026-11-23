"""Offline contract tests: no API calls, no writes to production files."""
import copy
import datetime
import json
import tempfile
import unittest
from pathlib import Path

from scripts.validate_output import ValidationError, main, validate


def fixture():
    dates = [(datetime.date(2026, 11, 1) + datetime.timedelta(days=i)).isoformat() for i in range(3)]
    results = {
        "generated_at": "2026-11-03",
        "generated_at_utc": "2026-11-04T01:02:03Z",
        "schema_version": 2,
        "market": {"date": "2026-11-03"},
        "universe_stats": {"results": 1, "watchlist": 1, "pattern_matches": 2, "liquidity_pass": 4},
        "total_scanned": 4,
        "results": [{"symbol": "AAA", "grade": "match"}],
        "watchlist": [{"symbol": "BBB", "grade": "watch"}],
    }
    bar = {
        "dates": dates,
        "open": [10, 11, 12],
        "high": [11, 12, 13],
        "low": [9, 10, 11],
        "close": [10.5, 11.5, 12.5],
        "volume": [100, 200, 300],
        "pivot": 13,
    }
    return results, {"AAA": copy.deepcopy(bar), "BBB": copy.deepcopy(bar)}


class OutputContractTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate(*fixture()), (1, 1))

    def test_legacy_timestamp_optional(self):
        results, charts = fixture()
        del results["generated_at_utc"]
        del results["schema_version"]
        self.assertEqual(validate(results, charts), (1, 1))

    def test_invalid_date(self):
        results, charts = fixture()
        results["market"]["date"] = "2026-11-31"
        with self.assertRaises(ValidationError):
            validate(results, charts)

    def test_wrong_grade(self):
        results, charts = fixture()
        results["watchlist"][0]["grade"] = "match"
        with self.assertRaises(ValidationError):
            validate(results, charts)

    def test_duplicate_symbol(self):
        results, charts = fixture()
        results["watchlist"][0]["symbol"] = "AAA"
        with self.assertRaises(ValidationError):
            validate(results, charts)

    def test_missing_chart(self):
        results, charts = fixture()
        del charts["AAA"]
        with self.assertRaises(ValidationError):
            validate(results, charts)

    def test_misaligned_ohlcv(self):
        results, charts = fixture()
        charts["AAA"]["volume"].pop()
        with self.assertRaises(ValidationError):
            validate(results, charts)

    def test_nonfinite_price(self):
        results, charts = fixture()
        charts["AAA"]["high"][0] = float("nan")
        with self.assertRaises(ValidationError):
            validate(results, charts)

    def test_count_mismatch(self):
        results, charts = fixture()
        results["universe_stats"]["pattern_matches"] = 3
        with self.assertRaises(ValidationError):
            validate(results, charts)

    def test_empty_valid(self):
        results, _ = fixture()
        results["results"] = []
        results["watchlist"] = []
        results["universe_stats"].update(results=0, watchlist=0, pattern_matches=0)
        self.assertEqual(validate(results, {}), (0, 0))

    def test_weekend_date_allowed(self):
        results, charts = fixture()
        results["generated_at"] = results["market"]["date"] = "2026-11-01"
        self.assertEqual(validate(results, charts), (1, 1))

    def test_cli_rejects_bad_json(self):
        with tempfile.TemporaryDirectory() as folder:
            results = Path(folder) / "results.json"
            charts = Path(folder) / "charts.json"
            results.write_text("{invalid", encoding="utf-8")
            charts.write_text("{}", encoding="utf-8")
            self.assertEqual(main(["--results", str(results), "--charts", str(charts)]), 1)

    def test_cli_accepts_valid_json(self):
        results_data, charts_data = fixture()
        with tempfile.TemporaryDirectory() as folder:
            results = Path(folder) / "results.json"
            charts = Path(folder) / "charts.json"
            results.write_text(json.dumps(results_data), encoding="utf-8")
            charts.write_text(json.dumps(charts_data), encoding="utf-8")
            self.assertEqual(main(["--results", str(results), "--charts", str(charts)]), 0)


if __name__ == "__main__":
    unittest.main()
