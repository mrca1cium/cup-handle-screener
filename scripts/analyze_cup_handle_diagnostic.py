from __future__ import annotations
"""Zero-API diagnostic for Cup & Handle / VCP filtering.

This script deliberately mirrors screener.pattern.detect() and stage2.check_stage2()
instead of inventing a second set of rules.

It reports how many Stage 2 candidates survive each sequential pattern gate, including
the early-return geometry checks that the normal pattern.detect() function does not
expose in its final checks dictionary.

No network/API calls are made.
"""
import argparse
import glob
import json
import os

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


def geometry_diagnostics(d):
    """Return sequential Cup/Handle gate results, mirroring pattern.detect()."""
    high = d["high"]
    low = d["low"]
    close = d["close"]
    vol = d["volume"]
    n = len(close)

    result = {
        "data_260": n >= 260,
        "cup_depth": False,
        "cup_length": False,
        "handle_rebound": False,
        "handle_length": False,
        "handle_shorter_than_cup": False,
        "handle_near_rim": False,
        "vcp_range_le_10": False,
        "higher_lows": False,
        "no_lower_lows": False,
        "volume_dry_up": False,
        "not_too_sharp": False,
        "hard_fail": False,
        "complete": False,
    }

    if n < 260:
        return result

    # 1. Left cup rim: exactly as pattern.detect()
    lookback = min(150, n - 5)
    seg_high = high[n - lookback - 5: n - 5]
    rim_i = n - lookback - 5 + max(range(lookback), key=lambda i: seg_high[i])
    rim = high[rim_i]

    # 2. Cup bottom/depth
    bot_i = min(range(rim_i, n), key=lambda i: low[i])
    bot = low[bot_i]
    depth_pct = (rim - bot) / rim * 100
    result["cup_depth"] = 12 <= depth_pct <= 40
    if not result["cup_depth"]:
        return result

    # 3. Cup duration
    cup_days = bot_i - rim_i
    result["cup_length"] = 20 <= cup_days <= 130
    if not result["cup_length"]:
        return result

    # 4. Rebound to >=90% of rim
    handle_start = None
    for i in range(bot_i, n):
        if close[i] >= rim * 0.90:
            handle_start = i
            break
    result["handle_rebound"] = handle_start is not None
    if handle_start is None:
        return result

    # 5. Handle duration
    handle_days = n - 1 - handle_start
    result["handle_length"] = 4 <= handle_days <= 42
    if not result["handle_length"]:
        return result

    # 6. Handle shorter than cup
    result["handle_shorter_than_cup"] = handle_days <= cup_days
    if not result["handle_shorter_than_cup"]:
        return result

    hh = high[handle_start:n]
    ll = low[handle_start:n]
    handle_high = max(hh)
    handle_low = min(ll)

    # 7. Handle high near rim
    result["handle_near_rim"] = (
        handle_high >= rim * 0.95 and handle_high <= rim * 1.02
    )
    if not result["handle_near_rim"]:
        return result

    # 8. VCP
    vcp_win = min(10, handle_days + 1)
    vcp_high = max(high[n - vcp_win:])
    vcp_low = min(low[n - vcp_win:])
    vcp_range_pct = (vcp_high - vcp_low) / vcp_high * 100
    result["vcp_range_le_10"] = vcp_range_pct <= 10

    # 9. Higher lows / lower lows
    seg = max(1, handle_days // 3)
    thirds = [
        min(low[handle_start + k * seg: handle_start + (k + 1) * seg])
        for k in range(3)
    ]
    higher_lows = thirds[0] < thirds[1] < thirds[2] if handle_days >= 9 else True
    lower_lows = thirds[0] > thirds[1] > thirds[2] if handle_days >= 9 else False
    result["higher_lows"] = higher_lows
    result["no_lower_lows"] = not lower_lows

    # 10. VDU
    avg3 = sum(vol[-3:]) / 3
    avg50 = sum(vol[-50:]) / 50
    result["volume_dry_up"] = avg3 < avg50 * 0.5

    # 11. Sharp recent rise hard fail
    recent_gain = (close[-1] / close[max(0, n - 6)] - 1) * 100
    too_sharp = recent_gain > 12
    result["not_too_sharp"] = not too_sharp
    result["hard_fail"] = lower_lows or too_sharp

    # Pattern grade, exactly matching pattern.detect()
    checks = {
        "cup_depth_ok": True,
        "cup_length_ok": True,
        "handle_length_ok": True,
        "handle_shorter_than_cup": True,
        "handle_near_rim": True,
        "vcp_range_le_10": result["vcp_range_le_10"],
        "higher_lows": result["higher_lows"],
        "no_lower_lows": result["no_lower_lows"],
        "volume_dry_up": result["volume_dry_up"],
        "not_too_sharp": result["not_too_sharp"],
    }
    passed = sum(checks.values())

    result["complete"] = not result["hard_fail"] and passed >= 7

    return result


def main(min_avg_vol=500_000):
    all_data = load_cache()
    spy = all_data.get("SPY")
    if not spy or len(spy.get("close", [])) < 260:
        raise SystemExit("SPY_2y.json is missing or has fewer than 260 bars")

    ratings = stage2.rs_ratings(all_data, spy["close"])

    liquidity = []
    stage2_rows = []

    for sym, d in all_data.items():
        if sym == "SPY":
            continue
        avg_vol = data_mod.average_volume(d, bars=63)
        if avg_vol < min_avg_vol:
            continue

        rating = ratings.get(sym)
        if rating is None:
            continue

        liquidity.append((sym, d, avg_vol, rating))
        s2 = stage2.check_stage2(d, rating)
        if s2["pass"]:
            stage2_rows.append((sym, d, avg_vol, rating, s2))

    # Sequential funnel. A stock only reaches the next gate if it passed the prior one.
    gates = [
        ("data_260", "At least 260 bars"),
        ("cup_depth", "Cup depth 12–40%"),
        ("cup_length", "Cup duration 20–130d"),
        ("handle_rebound", "Close rebounds to >=90% of rim"),
        ("handle_length", "Handle duration 4–42d"),
        ("handle_shorter_than_cup", "Handle shorter than cup"),
        ("handle_near_rim", "Handle high 95–102% of rim"),
        ("vcp_range_le_10", "Recent 10d VCP range <=10%"),
        ("higher_lows", "Higher lows"),
        ("no_lower_lows", "No lower lows"),
        ("volume_dry_up", "VDU: 3d avg < 50% of 50d avg"),
        ("not_too_sharp", "No recent 5d gain >12%"),
    ]

    survivors = stage2_rows
    funnel = []

    for key, label in gates:
        passed = []
        for sym, d, avg_vol, rating, s2 in survivors:
            diag = geometry_diagnostics(d)
            if diag[key]:
                passed.append((sym, d, avg_vol, rating, s2))
        funnel.append((key, label, len(survivors), len(passed), len(survivors) - len(passed)))
        survivors = passed

    # Final pattern.detect() result on all Stage 2 rows, so the reported MATCH/WATCH
    # remains the canonical screener result.
    matches = []
    watch = []
    for sym, d, avg_vol, rating, s2 in stage2_rows:
        pat = pattern.detect(d)
        if pat:
            row = (sym, avg_vol, rating, pat)
            if pat["grade"] == "match":
                matches.append(row)
            elif pat["grade"] == "watch":
                watch.append(row)

    print("=== ZERO-API CUP & HANDLE DIAGNOSTIC ===")
    print("Cache directory:", CACHE_DIR)
    print()
    print("Cached stocks (including SPY): {:,}".format(len(all_data)))
    print("Liquidity-qualified (63D Avg Volume >= {:,}): {:,}".format(
        min_avg_vol, len(liquidity)
    ))
    print("Stage 2 pass: {:,}".format(len(stage2_rows)))
    print()

    print("=== SEQUENTIAL CUP/HANDLE FUNNEL ===")
    print("{:<42} {:>8} {:>8} {:>8}".format(
        "Condition", "Entered", "Passed", "Failed"
    ))
    print("-" * 72)
    for _, label, entered, passed, failed in funnel:
        pct = 100.0 * passed / entered if entered else 0.0
        print("{:<42} {:>8} {:>8} {:>8}  ({:5.1f}% pass)".format(
            label, entered, passed, failed, pct
        ))

    print()
    print("=== FINAL PATTERN RESULT ===")
    print("MATCH: {:,}".format(len(matches)))
    print("WATCH: {:,}".format(len(watch)))
    print()

    # The early geometry gates explain most None results. Also show the post-geometry
    # quality checks independently among stocks that make it through handle geometry.
    geometry_survivors = []
    for sym, d, avg_vol, rating, s2 in stage2_rows:
        diag = geometry_diagnostics(d)
        if (
            diag["data_260"]
            and diag["cup_depth"]
            and diag["cup_length"]
            and diag["handle_rebound"]
            and diag["handle_length"]
            and diag["handle_shorter_than_cup"]
            and diag["handle_near_rim"]
        ):
            geometry_survivors.append((sym, d, avg_vol, rating))

    print("=== QUALITY CHECKS AMONG HANDLE-GEOMETRY SURVIVORS ===")
    print("Geometry survivors: {:,}".format(len(geometry_survivors)))
    quality_keys = [
        ("vcp_range_le_10", "VCP range <=10%"),
        ("higher_lows", "Higher lows"),
        ("no_lower_lows", "No lower lows"),
        ("volume_dry_up", "VDU"),
        ("not_too_sharp", "Not too sharp"),
    ]
    for key, label in quality_keys:
        passed = sum(geometry_diagnostics(d)[key] for _, d, _, _ in geometry_survivors)
        total = len(geometry_survivors)
        print("  {:30s} {:4d}/{:<4d} ({:5.1f}%)".format(
            label, passed, total, 100.0 * passed / total if total else 0.0
        ))

    def show(title, rows):
        print()
        print(title)
        if not rows:
            print("  (none)")
            return
        rows = sorted(rows, key=lambda x: (-x[2], -x[1]))
        for sym, avg_vol, rating, pat in rows:
            m = pat["metrics"]
            print(
                "  {:6s} RS={:4.1f} AvgVol={:>12,} depth={:4.1f}% "
                "handle={:2d}d VCP={:4.1f}% pivot={:>9.2f}".format(
                    sym, rating, int(avg_vol), m["cup_depth_pct"],
                    m["handle_days"], m["vcp_range_pct"], m["pivot"]
                )
            )

    show("Cup/Handle MATCH:", matches)
    show("Cup/Handle WATCH:", watch)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-avg-vol", type=int, default=500_000)
    args = ap.parse_args()
    main(args.min_avg_vol)
