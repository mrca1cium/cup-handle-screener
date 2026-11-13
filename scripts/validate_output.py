#!/usr/bin/env python3
"""Validate APEX website output without changing data or contacting providers."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path


class ValidationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def date_string(value, field):
    require(isinstance(value, str), f"{field}: expected YYYY-MM-DD string")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field}: invalid date") from exc
    require(parsed.isoformat() == value, f"{field}: invalid date format")
    return parsed


def validate(results, charts):
    require(isinstance(results, dict), "results.json must be an object")
    require(isinstance(charts, dict), "charts.json must be an object")
    for key in ("generated_at", "market", "universe_stats", "total_scanned", "results", "watchlist"):
        require(key in results, f"results.json missing {key}")
    market = results["market"]
    stats = results["universe_stats"]
    require(isinstance(market, dict) and isinstance(stats, dict), "market/universe_stats must be objects")
    require("date" in market, "market.date missing")
    date_string(results["generated_at"], "generated_at")
    date_string(market["date"], "market.date")
    require(results["generated_at"] == market["date"], "generated_at != market.date")
    if "generated_at_utc" in results:
        stamp = results["generated_at_utc"]
        require(isinstance(stamp, str) and stamp.endswith("Z"), "generated_at_utc must be UTC ISO-8601")
        try:
            parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("invalid generated_at_utc") from exc
        require(parsed.utcoffset() == dt.timedelta(0), "generated_at_utc must be UTC")
    if "schema_version" in results:
        require(results["schema_version"] == 2, "unsupported schema_version")

    symbols = set()
    for list_name, expected_grade in (("results", "match"), ("watchlist", "watch")):
        rows = results[list_name]
        require(isinstance(rows, list), f"{list_name} must be a list")
        for i, row in enumerate(rows):
            where = f"{list_name}[{i}]"
            require(isinstance(row, dict), f"{where} must be an object")
            symbol = row.get("symbol")
            require(isinstance(symbol, str) and symbol and symbol == symbol.strip().upper(), f"{where}: invalid symbol")
            require(symbol not in symbols, f"duplicate symbol: {symbol}")
            symbols.add(symbol)
            require(row.get("grade") == expected_grade, f"{where}: wrong grade")

    require(set(charts) == symbols, "charts symbols must equal MATCH + WATCH symbols")
    for symbol, chart in charts.items():
        require(isinstance(chart, dict), f"{symbol}: chart must be object")
        columns = ("dates", "open", "high", "low", "close", "volume")
        for key in columns:
            require(isinstance(chart.get(key), list), f"{symbol}: missing {key} array")
        n = len(chart["dates"])
        require(n > 0 and all(len(chart[k]) == n for k in columns), f"{symbol}: inconsistent OHLCV lengths")
        require(len(set(chart["dates"])) == n, f"{symbol}: duplicate dates")
        require(chart["dates"] == sorted(chart["dates"]), f"{symbol}: dates out of order")
        for i in range(n):
            date_string(chart["dates"][i], f"{symbol}.dates[{i}]")
            o, h, l, c, v = (chart[k][i] for k in ("open", "high", "low", "close", "volume"))
            for key, value in zip(("open", "high", "low", "close", "volume"), (o, h, l, c, v)):
                require(type(value) in (int, float) and math.isfinite(value), f"{symbol}.{key}[{i}]: non-finite number")
            require(l > 0 and l <= min(o, c) <= max(o, c) <= h, f"{symbol}: invalid OHLC at {i}")
            require(v >= 0, f"{symbol}: negative volume at {i}")
        for key in ("pivot",):
            value = chart.get(key)
            require(type(value) in (int, float) and math.isfinite(value) and value > 0, f"{symbol}: invalid {key}")

    for key, expected in (("results", len(results["results"])), ("watchlist", len(results["watchlist"])),
                          ("pattern_matches", len(symbols))):
        require(type(stats.get(key)) is int and stats[key] == expected, f"universe_stats.{key}: count mismatch")
    scanned = results["total_scanned"]
    require(type(scanned) is int and scanned >= len(symbols), "invalid total_scanned")
    if "liquidity_pass" in stats:
        require(stats["liquidity_pass"] == scanned, "liquidity_pass != total_scanned")
    return len(results["results"]), len(results["watchlist"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=Path("docs/data/results.json"))
    parser.add_argument("--charts", type=Path, default=Path("docs/data/charts.json"))
    args = parser.parse_args(argv)
    try:
        with args.results.open(encoding="utf-8") as f:
            results = json.load(f, parse_constant=lambda x: (_ for _ in ()).throw(ValidationError(f"invalid JSON constant {x}")))
        with args.charts.open(encoding="utf-8") as f:
            charts = json.load(f, parse_constant=lambda x: (_ for _ in ()).throw(ValidationError(f"invalid JSON constant {x}")))
        match, watch = validate(results, charts)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"APEX OUTPUT INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"APEX OUTPUT VALID: MATCH={match} WATCH={watch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
