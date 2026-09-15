from __future__ import annotations
"""Zero-API analysis of all locally cached 2y data.

Pipeline:
  cache -> 63D liquidity -> RS Rating -> Stage 2 -> Cup/Handle/VCP/VDU.

This script never calls fetch_daily/fetch_many or market_status, so it will not
consume StashGamma quota. It expects SPY_2y.json to exist locally.
"""
import argparse
import json
import os
import glob

from screener import data as data_mod
from screener import pattern, stage2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, ".cache", "market")


def load_cache():
    out = {}
    for path in glob.glob(os.path.join(CACHE_DIR, "*_2y.json")):
        sym = os.path.basename(path)[:-8]
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            d = payload.get("data")
            if d and len(d.get("close", [])) >= 210:
                out[sym] = d
        except Exception:
            continue
    return out


def main(min_avg_vol=500_000, min_rs=70):
    all_data = load_cache()
    spy = all_data.get("SPY")
    if not spy or len(spy.get("close", [])) < 260:
        raise SystemExit("SPY_2y.json is missing or has fewer than 260 bars")

    candidates = []
    for sym, d in all_data.items():
        if sym == "SPY":
            continue
        avg_vol = data_mod.average_volume(d, bars=63)
        if avg_vol >= min_avg_vol:
            candidates.append((sym, d, avg_vol))

    ratings = stage2.rs_ratings(all_data, spy["close"])

    stage2_rows = []
    check_counts = {}
    pattern_matches = []
    pattern_watch = []

    for sym, d, avg_vol in candidates:
        rating = ratings.get(sym)
        if rating is None:
            continue
        s2 = stage2.check_stage2(d, rating)
        for name, ok in s2.get("checks", {}).items():
            check_counts[name] = check_counts.get(name, 0) + int(bool(ok))
        if s2["pass"]:
            stage2_rows.append((sym, d, avg_vol, s2))
            pat = pattern.detect(d)
            if pat:
                row = (sym, avg_vol, rating, pat)
                if pat["grade"] == "match":
                    pattern_matches.append(row)
                elif pat["grade"] == "watch":
                    pattern_watch.append(row)

    print("=== ZERO-API CACHED SCREENER ANALYSIS ===")
    print("Cache directory:", CACHE_DIR)
    print()
    print("Cached stocks (including SPY):", len(all_data))
    print("Stocks with 63D Avg Volume >= {:,}: {:,}".format(min_avg_vol, len(candidates)))
    print("Stage 2 pass: {:,}".format(len(stage2_rows)))
    print("Cup/Handle MATCH: {:,}".format(len(pattern_matches)))
    print("Cup/Handle WATCH: {:,}".format(len(pattern_watch)))
    print()

    print("Stage 2 individual checks among liquidity-qualified stocks:")
    denom = len([x for x in candidates if x[0] in ratings])
    for name in sorted(check_counts):
        n = check_counts[name]
        pct = 100.0 * n / denom if denom else 0.0
        print("  {:38s} {:4d} ({:5.1f}%)".format(name, n, pct))
    print()

    def show(title, rows):
        print(title)
        if not rows:
            print("  (none)")
            return
        rows = sorted(rows, key=lambda x: (-x[2], -x[1]))
        for sym, avg_vol, rating, pat in rows:
            m = pat["metrics"]
            print("  {:6s} RS={:4.1f} AvgVol={:>12,} depth={:4.1f}% handle={:2d}d VCP={:4.1f}% pivot={:>9.2f}".format(
                sym, rating, int(avg_vol), m["cup_depth_pct"], m["handle_days"],
                m["vcp_range_pct"], m["pivot"]))

    show("Cup/Handle MATCH:", pattern_matches)
    print()
    show("Cup/Handle WATCH:", pattern_watch)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-avg-vol", type=int, default=500_000)
    ap.add_argument("--min-rs", type=float, default=70)
    args = ap.parse_args()
    main(args.min_avg_vol, args.min_rs)
