#!/usr/bin/env python3
"""Analyze existing market cache without making any network/API requests."""
from __future__ import annotations

import argparse
import json
import os
import statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, ".cache", "market")


def load_caches():
    rows = []
    if not os.path.isdir(CACHE_DIR):
        return rows
    for name in sorted(os.listdir(CACHE_DIR)):
        if not name.endswith("_2y.json"):
            continue
        path = os.path.join(CACHE_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            data = payload.get("data") or {}
            dates = data.get("dates") or []
            volumes = data.get("volume") or []
            closes = data.get("close") or []
            if not dates or not volumes or not closes:
                continue
            symbol = name[:-8]
            n = min(63, len(volumes))
            avg = sum(float(v) for v in volumes[-n:] if v is not None) / n
            rows.append({
                "symbol": symbol,
                "bars": len(dates),
                "start": dates[0],
                "end": dates[-1],
                "avg_volume_63d": avg,
                "price": float(closes[-1]),
                "fetched_at": payload.get("fetched_at"),
            })
        except Exception as e:
            print("skip", name, e)
    return rows


def fmt(n):
    return format(n, ",.0f")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-avg-vol", type=float, default=500_000)
    args = ap.parse_args()
    rows = load_caches()
    print("=== ZERO-API CACHE ANALYSIS ===")
    print("Cache directory:", CACHE_DIR)
    print("2y cache files:", len(rows))
    if not rows:
        print("No usable 2y cache found.")
        return

    liquid = [r for r in rows if r["avg_volume_63d"] >= args.min_avg_vol]
    illiquid = [r for r in rows if r["avg_volume_63d"] < args.min_avg_vol]
    avgs = [r["avg_volume_63d"] for r in rows]
    print("63D Avg Volume >= %s: %d (%.1f%%)" % (fmt(args.min_avg_vol), len(liquid), 100.0 * len(liquid) / len(rows)))
    print("63D Avg Volume <  %s: %d (%.1f%%)" % (fmt(args.min_avg_vol), len(illiquid), 100.0 * len(illiquid) / len(rows)))
    print("Average 63D volume: %s" % fmt(statistics.mean(avgs)))
    print("Median  63D volume: %s" % fmt(statistics.median(avgs)))
    print("Min / Max 63D volume: %s / %s" % (fmt(min(avgs)), fmt(max(avgs))))
    print("\nTop 20 by 63D average volume:")
    for r in sorted(rows, key=lambda x: x["avg_volume_63d"], reverse=True)[:20]:
        print("%-7s %12s  price=%8.2f  bars=%d" % (r["symbol"], fmt(r["avg_volume_63d"]), r["price"], r["bars"]))
    print("\nBelow 500K:")
    for r in sorted(illiquid, key=lambda x: x["avg_volume_63d"]):
        print("%-7s %12s  price=%8.2f  bars=%d" % (r["symbol"], fmt(r["avg_volume_63d"]), r["price"], r["bars"]))


if __name__ == "__main__":
    main()
