from __future__ import annotations
"""Zero-API Cup & Handle threshold distribution analysis.

Uses the current cached market data and mirrors screener.pattern.detect().
No network/API calls are made.

The goal is to inspect the actual distributions around each current threshold
before changing any Cup/Handle/VCP rule.
"""
import argparse
import glob
import json
import os
import statistics

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


def calc_metrics(d):
    high, low, close, vol = d["high"], d["low"], d["close"], d["volume"]
    n = len(close)
    if n < 260:
        return None

    lookback = min(150, n - 5)
    seg_high = high[n - lookback - 5:n - 5]
    rim_i = n - lookback - 5 + max(range(lookback), key=lambda i: seg_high[i])
    rim = high[rim_i]

    bot_i = min(range(rim_i, n), key=lambda i: low[i])
    bot = low[bot_i]
    depth_pct = (rim - bot) / rim * 100
    cup_days = bot_i - rim_i

    handle_start = None
    for i in range(bot_i, n):
        if close[i] >= rim * 0.90:
            handle_start = i
            break
    if handle_start is None:
        return {
            "depth_pct": depth_pct,
            "cup_days": cup_days,
            "handle_rebound": False,
        }

    handle_days = n - 1 - handle_start
    hh = high[handle_start:n]
    ll = low[handle_start:n]
    handle_high = max(hh)
    handle_low = min(ll)

    handle_high_vs_rim_pct = (handle_high / rim - 1) * 100
    handle_low_vs_rim_pct = (handle_low / rim - 1) * 100
    handle_depth_pct = (handle_high - handle_low) / handle_high * 100

    vcp_win = min(10, handle_days + 1)
    vcp_high = max(high[n - vcp_win:])
    vcp_low = min(low[n - vcp_win:])
    vcp_range_pct = (vcp_high - vcp_low) / vcp_high * 100

    seg = max(1, handle_days // 3)
    thirds = [
        min(low[handle_start + k * seg:handle_start + (k + 1) * seg])
        for k in range(3)
    ]
    higher_lows = thirds[0] < thirds[1] < thirds[2] if handle_days >= 9 else True
    lower_lows = thirds[0] > thirds[1] > thirds[2] if handle_days >= 9 else False

    avg3 = sum(vol[-3:]) / 3
    avg50 = sum(vol[-50:]) / 50
    vdu_ratio = avg3 / avg50 if avg50 else None

    recent_gain = (close[-1] / close[max(0, n - 6)] - 1) * 100

    return {
        "depth_pct": depth_pct,
        "cup_days": cup_days,
        "handle_rebound": True,
        "handle_days": handle_days,
        "handle_to_cup_ratio": handle_days / cup_days if cup_days else None,
        "handle_high_vs_rim_pct": handle_high_vs_rim_pct,
        "handle_low_vs_rim_pct": handle_low_vs_rim_pct,
        "handle_depth_pct": handle_depth_pct,
        "vcp_range_pct": vcp_range_pct,
        "higher_lows": higher_lows,
        "lower_lows": lower_lows,
        "vdu_ratio": vdu_ratio,
        "recent_gain_pct": recent_gain,
    }


def pctile(values, p):
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    frac = k - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def fmt(v, digits=1):
    if v is None:
        return "-"
    return f"{v:.{digits}f}"


def distribution(label, values, digits=1):
    values = [v for v in values if v is not None]
    if not values:
        print(f"{label}: no data")
        return
    print(
        f"{label:<30} n={len(values):4d} "
        f"min={fmt(min(values), digits):>8} "
        f"p10={fmt(pctile(values, 10), digits):>8} "
        f"p25={fmt(pctile(values, 25), digits):>8} "
        f"median={fmt(statistics.median(values), digits):>8} "
        f"p75={fmt(pctile(values, 75), digits):>8} "
        f"p90={fmt(pctile(values, 90), digits):>8} "
        f"max={fmt(max(values), digits):>8}"
    )


def gate_counts(rows, predicate):
    passed = sum(1 for r in rows if predicate(r))
    return passed, len(rows) - passed


def main(min_avg_vol=500_000):
    all_data = load_cache()
    spy = all_data.get("SPY")
    if not spy or len(spy.get("close", [])) < 260:
        raise SystemExit("SPY_2y.json is missing or has fewer than 260 bars")

    ratings = stage2.rs_ratings(all_data, spy["close"])

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
        s2 = stage2.check_stage2(d, rating)
        if s2["pass"]:
            metrics = calc_metrics(d)
            if metrics is not None:
                stage2_rows.append((sym, d, avg_vol, rating, metrics))

    # Sequential cohorts mirror the current pattern funnel.
    cohorts = {
        "stage2": stage2_rows,
        "depth": [r for r in stage2_rows if 12 <= r[4]["depth_pct"] <= 40],
    }
    cohorts["cup_length"] = [
        r for r in cohorts["depth"]
        if 20 <= r[4]["cup_days"] <= 130
    ]
    cohorts["rebound"] = [
        r for r in cohorts["cup_length"] if r[4]["handle_rebound"]
    ]
    cohorts["handle_length"] = [
        r for r in cohorts["rebound"] if 4 <= r[4]["handle_days"] <= 42
    ]
    cohorts["handle_shorter"] = [
        r for r in cohorts["handle_length"]
        if r[4]["handle_days"] <= r[4]["cup_days"]
    ]
    cohorts["near_rim"] = [
        r for r in cohorts["handle_shorter"]
        if 95 <= 100 + r[4]["handle_high_vs_rim_pct"] <= 102
    ]
    cohorts["vcp"] = [
        r for r in cohorts["near_rim"] if r[4]["vcp_range_pct"] <= 10
    ]
    cohorts["higher_lows"] = [
        r for r in cohorts["vcp"] if r[4]["higher_lows"]
    ]

    print("=== ZERO-API CUP & HANDLE THRESHOLD DISTRIBUTION ===")
    print("Cache directory:", CACHE_DIR)
    print(f"Cached stocks (including SPY): {len(all_data):,}")
    print(f"Stage 2 + liquidity qualified: {len(stage2_rows):,}")
    print()

    print("=== 1. CORE GEOMETRY DISTRIBUTIONS ===")
    distribution("Cup depth %", [r[4]["depth_pct"] for r in stage2_rows])
    distribution("Cup duration (days)", [r[4]["cup_days"] for r in cohorts["depth"]])
    distribution("Handle duration (days)", [r[4]["handle_days"] for r in cohorts["rebound"]])
    distribution("Handle / cup ratio", [r[4]["handle_to_cup_ratio"] for r in cohorts["handle_length"]], 2)
    distribution("Handle high vs rim %", [r[4]["handle_high_vs_rim_pct"] for r in cohorts["handle_shorter"]])
    distribution("Handle low vs rim %", [r[4]["handle_low_vs_rim_pct"] for r in cohorts["handle_shorter"]])
    distribution("Handle depth %", [r[4]["handle_depth_pct"] for r in cohorts["handle_shorter"]])
    print()

    print("=== 2. QUALITY DISTRIBUTIONS ===")
    distribution("Recent 10d VCP range %", [r[4]["vcp_range_pct"] for r in cohorts["near_rim"]])
    distribution("VDU ratio (3d / 50d)", [r[4]["vdu_ratio"] for r in cohorts["near_rim"]], 3)
    distribution("Recent 5d gain %", [r[4]["recent_gain_pct"] for r in cohorts["near_rim"]])
    print()

    print("=== 3. CURRENT THRESHOLD BOUNDARIES ===")
    checks = [
        ("Cup depth 12–40%", stage2_rows, lambda m: 12 <= m["depth_pct"] <= 40),
        ("Cup duration 20–130d", cohorts["depth"], lambda m: 20 <= m["cup_days"] <= 130),
        ("Rebound >=90% rim", cohorts["cup_length"], lambda m: m["handle_rebound"]),
        ("Handle duration 4–42d", cohorts["rebound"], lambda m: 4 <= m["handle_days"] <= 42),
        ("Handle <= cup", cohorts["handle_length"], lambda m: m["handle_days"] <= m["cup_days"]),
        ("Handle high 95–102% rim", cohorts["handle_shorter"],
         lambda m: 95 <= 100 + m["handle_high_vs_rim_pct"] <= 102),
        ("VCP range <=10%", cohorts["near_rim"], lambda m: m["vcp_range_pct"] <= 10),
        ("Higher lows", cohorts["vcp"], lambda m: m["higher_lows"]),
        ("VDU <0.50", cohorts["near_rim"], lambda m: m["vdu_ratio"] is not None and m["vdu_ratio"] < 0.50),
        ("Recent gain <=12%", cohorts["near_rim"], lambda m: m["recent_gain_pct"] <= 12),
    ]
    for label, rows, pred in checks:
        p, f = gate_counts(rows, lambda r: pred(r[4]))
        pct = 100 * p / len(rows) if rows else 0
        print(f"{label:<30} passed={p:4d} failed={f:4d} pass={pct:5.1f}%")

    print()
    print("=== 4. COUNTS CLOSE TO EACH THRESHOLD ===")

    depth_vals = [r[4]["depth_pct"] for r in stage2_rows]
    print("Cup depth:")
    for lo, hi in [(0, 8), (8, 12), (12, 15), (15, 20), (20, 30), (30, 40), (40, 45), (45, 60)]:
        print(f"  {lo:>2}–{hi:<2}%: {sum(lo <= x < hi for x in depth_vals):4d}")

    cup_vals = [r[4]["cup_days"] for r in cohorts["depth"]]
    print("Cup duration:")
    for lo, hi in [(0, 20), (20, 30), (30, 60), (60, 90), (90, 120), (120, 130), (130, 160), (160, 220)]:
        print(f"  {lo:>3}–{hi:<3}d: {sum(lo <= x < hi for x in cup_vals):4d}")

    handle_vals = [r[4]["handle_days"] for r in cohorts["rebound"]]
    print("Handle duration:")
    for lo, hi in [(0, 4), (4, 7), (7, 14), (14, 21), (21, 28), (28, 35), (35, 42), (42, 56)]:
        print(f"  {lo:>2}–{hi:<2}d: {sum(lo <= x < hi for x in handle_vals):4d}")

    vcp_vals = [r[4]["vcp_range_pct"] for r in cohorts["near_rim"]]
    print("VCP range:")
    for lo, hi in [(0, 5), (5, 8), (8, 10), (10, 12), (12, 15), (15, 20), (20, 30)]:
        print(f"  {lo:>2}–{hi:<2}%: {sum(lo <= x < hi for x in vcp_vals):4d}")

    vdu_vals = [x for x in (r[4]["vdu_ratio"] for r in cohorts["near_rim"]) if x is not None]
    print("VDU ratio:")
    for lo, hi in [(0, 0.25), (0.25, 0.50), (0.50, 0.75), (0.75, 1.00), (1.00, 1.50), (1.50, 3.00)]:
        print(f"  {lo:>4.2f}–{hi:<4.2f}: {sum(lo <= x < hi for x in vdu_vals):4d}")

    print()
    print("=== 5. CURRENT MATCH / WATCH FOR REFERENCE ===")
    matches = []
    watch = []
    for sym, d, avg_vol, rating, _ in stage2_rows:
        pat = pattern.detect(d)
        if pat:
            row = (sym, avg_vol, rating, pat)
            if pat["grade"] == "match":
                matches.append(row)
            elif pat["grade"] == "watch":
                watch.append(row)

    for title, rows in [("MATCH", matches), ("WATCH", watch)]:
        print(f"{title}: {len(rows)}")
        for sym, avg_vol, rating, pat in sorted(rows, key=lambda x: -x[2]):
            m = pat["metrics"]
            print(
                f"  {sym:6s} RS={rating:4.1f} depth={m['cup_depth_pct']:4.1f}% "
                f"cup={m['cup_days']:3d}d handle={m['handle_days']:2d}d "
                f"VCP={m['vcp_range_pct']:4.1f}% pivot={m['pivot']:.2f}"
            )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-avg-vol", type=int, default=500_000)
    args = ap.parse_args()
    main(args.min_avg_vol)
