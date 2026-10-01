from __future__ import annotations
"""Zero-API signal history recorder and outcome evaluator.

This script never calls a market-data API. It only reads:
  1. docs/data/results.json
  2. .cache/market/*_2y.json

Usage:
  python3 scripts/signal_history.py
  python3 scripts/signal_history.py --record-only
  python3 scripts/signal_history.py --evaluate-only

Each MATCH/WATCH is stored once per signal date. Later cached bars are used
to measure pivot breakout, stop, 1R, and price movement after the signal.
"""
import argparse
import glob
import json
import os
from datetime import datetime
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_PATH = os.path.join(ROOT, "docs", "data", "results.json")
HISTORY_PATH = os.path.join(ROOT, "docs", "data", "signal_history.json")
CACHE_DIR = os.path.join(ROOT, ".cache", "market")

WINDOWS = (1, 3, 5, 10)


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value
    except (OSError, ValueError):
        return default


def save_json(path: str, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_cache(symbol: str) -> Optional[dict]:
    path = os.path.join(CACHE_DIR, "{}_2y.json".format(symbol.upper()))
    payload = load_json(path, None)
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    required = ("dates", "high", "low", "close")
    if not all(isinstance(data.get(k), list) for k in required):
        return None
    return data


def parse_date(value: str):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def normalise_signal(row: dict, signal_date: str) -> dict:
    metrics = row.get("metrics") or {}
    stage2_values = (row.get("stage2") or {}).get("values") or {}
    return {
        "signal_date": signal_date,
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "sector": row.get("sector"),
        "grade": row.get("grade"),
        "close": metrics.get("close"),
        "pivot": metrics.get("pivot"),
        "stop_loss": metrics.get("stop_loss"),
        "target_1r": metrics.get("target_1r"),
        "pct_to_pivot": metrics.get("pct_to_pivot"),
        "cup_depth_pct": metrics.get("cup_depth_pct"),
        "cup_days": metrics.get("cup_days"),
        "handle_days": metrics.get("handle_days"),
        "handle_depth_pct": metrics.get("handle_depth_pct"),
        "vcp_range_pct": metrics.get("vcp_range_pct"),
        "vdu_ratio": metrics.get("vdu_ratio"),
        "rs_rating": stage2_values.get("rs_rating"),
        "avg_volume_63d": row.get("avg_volume_63d"),
        "checks": row.get("checks") or {},
        "outcome": {},
    }


def record_current_signals(history: list[dict]) -> int:
    results = load_json(RESULTS_PATH, None)
    if not isinstance(results, dict):
        raise SystemExit("找不到或無法讀取 {}".format(RESULTS_PATH))

    signal_date = results.get("generated_at")
    if not signal_date:
        raise SystemExit("results.json 缺少 generated_at")

    signal_date = str(signal_date)[:10]
    rows = list(results.get("results") or []) + list(results.get("watchlist") or [])

    existing = {
        (str(x.get("signal_date")), str(x.get("symbol")).upper())
        for x in history
        if x.get("signal_date") and x.get("symbol")
    }

    added = 0
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        key = (signal_date, symbol)
        if key in existing:
            continue
        history.append(normalise_signal(row, signal_date))
        existing.add(key)
        added += 1

    history.sort(
        key=lambda x: (
            str(x.get("signal_date") or ""),
            str(x.get("symbol") or ""),
        )
    )
    return added


def first_index_on_or_after(dates: list, target_date: str) -> Optional[int]:
    target = parse_date(target_date)
    if target is None:
        return None
    for i, value in enumerate(dates):
        current = parse_date(value)
        if current is not None and current >= target:
            return i
    return None


def evaluate_signal(signal: dict, data: dict) -> bool:
    dates = data.get("dates") or []
    highs = data.get("high") or []
    lows = data.get("low") or []
    closes = data.get("close") or []

    n = min(len(dates), len(highs), len(lows), len(closes))
    if n == 0:
        return False

    dates = dates[:n]
    highs = highs[:n]
    lows = lows[:n]
    closes = closes[:n]

    signal_idx = first_index_on_or_after(dates, signal.get("signal_date"))
    if signal_idx is None:
        return False

    # We only evaluate completed bars after the signal date.
    future_start = signal_idx + 1
    available = max(0, n - future_start)
    if available <= 0:
        return False

    pivot = signal.get("pivot")
    stop = signal.get("stop_loss")
    target = signal.get("target_1r")

    try:
        pivot = float(pivot) if pivot is not None else None
    except (TypeError, ValueError):
        pivot = None
    try:
        stop = float(stop) if stop is not None else None
    except (TypeError, ValueError):
        stop = None
    try:
        target = float(target) if target is not None else None
    except (TypeError, ValueError):
        target = None

    outcome = signal.setdefault("outcome", {})
    outcome["last_evaluated_date"] = str(dates[-1])[:10]

    # First breakout after the signal. A breakout is based on intraday high
    # crossing the stored pivot, not the closing price.
    breakout_idx = None
    if pivot is not None:
        for i in range(future_start, n):
            if highs[i] >= pivot:
                breakout_idx = i
                break

    if breakout_idx is not None:
        outcome["breakout"] = {
            "date": str(dates[breakout_idx])[:10],
            "bars_after_signal": breakout_idx - signal_idx,
        }
    else:
        outcome["breakout"] = None

    # Signal-date windows: what happened during the first 1/3/5/10 completed
    # trading bars after the signal?
    for window in WINDOWS:
        end = min(n, future_start + window)
        if end <= future_start:
            continue
        hs = highs[future_start:end]
        ls = lows[future_start:end]
        cs = closes[future_start:end]

        item = {
            "bars_available": end - future_start,
            "max_high": round(max(hs), 4),
            "min_low": round(min(ls), 4),
            "last_close": round(cs[-1], 4),
        }
        if pivot is not None:
            item["pivot_hit"] = max(hs) >= pivot
        if stop is not None:
            item["stop_hit"] = min(ls) <= stop
        if target is not None:
            item["target_1r_hit"] = max(hs) >= target

        outcome["signal_window_{}d".format(window)] = item

    # Once a breakout happens, measure the next 1/3/5/10 completed bars from
    # that breakout. This is the more useful view for a trader interested in
    # post-pivot behaviour.
    if breakout_idx is not None:
        for window in WINDOWS:
            start = breakout_idx
            end = min(n, start + window + 1)
            if end <= start:
                continue
            hs = highs[start:end]
            ls = lows[start:end]
            cs = closes[start:end]
            item = {
                "bars_available": end - start,
                "max_high": round(max(hs), 4),
                "min_low": round(min(ls), 4),
                "last_close": round(cs[-1], 4),
            }
            if pivot is not None:
                item["pivot_reclaimed"] = min(ls) >= pivot
            if stop is not None:
                item["stop_hit"] = min(ls) <= stop
            if target is not None:
                item["target_1r_hit"] = max(hs) >= target
            outcome["breakout_window_{}d".format(window)] = item

    return True


def evaluate_all(history: list[dict]) -> int:
    evaluated = 0
    for signal in history:
        symbol = str(signal.get("symbol") or "").upper()
        if not symbol:
            continue
        data = load_cache(symbol)
        if not data:
            continue
        if evaluate_signal(signal, data):
            evaluated += 1
    return evaluated


def summary(history: list[dict]):
    total = len(history)
    by_grade = {}
    for signal in history:
        grade = signal.get("grade") or "unknown"
        by_grade[grade] = by_grade.get(grade, 0) + 1

    print("=== ZERO-API SIGNAL HISTORY ===")
    print("History file:", HISTORY_PATH)
    print("Signals:", total)
    for grade in ("match", "watch"):
        print("  {}: {}".format(grade.upper(), by_grade.get(grade, 0)))

    for window in WINDOWS:
        key = "signal_window_{}d".format(window)
        rows = [
            s.get("outcome", {}).get(key)
            for s in history
            if isinstance(s.get("outcome", {}).get(key), dict)
            and s["outcome"][key].get("bars_available", 0) >= window
        ]
        if not rows:
            continue

        pivot_hit = sum(bool(x.get("pivot_hit")) for x in rows)
        target_hit = sum(bool(x.get("target_1r_hit")) for x in rows)
        stop_hit = sum(bool(x.get("stop_hit")) for x in rows)

        print(
            "Signal +{}d: n={} pivot_hit={:.1f}% 1R_hit={:.1f}% stop_hit={:.1f}%".format(
                window,
                len(rows),
                100.0 * pivot_hit / len(rows),
                100.0 * target_hit / len(rows),
                100.0 * stop_hit / len(rows),
            )
        )

    breakout_rows = [
        s for s in history
        if isinstance(s.get("outcome", {}).get("breakout"), dict)
    ]
    print("Signals with later pivot breakout:", len(breakout_rows))

    for window in WINDOWS:
        key = "breakout_window_{}d".format(window)
        rows = [
            s.get("outcome", {}).get(key)
            for s in breakout_rows
            if isinstance(s.get("outcome", {}).get(key), dict)
            and s["outcome"][key].get("bars_available", 0) >= window
        ]
        if not rows:
            continue

        target_hit = sum(bool(x.get("target_1r_hit")) for x in rows)
        stop_hit = sum(bool(x.get("stop_hit")) for x in rows)
        print(
            "Breakout +{}d: n={} 1R_hit={:.1f}% stop_hit={:.1f}%".format(
                window,
                len(rows),
                100.0 * target_hit / len(rows),
                100.0 * stop_hit / len(rows),
            )
        )


def main(record: bool, evaluate: bool):
    history = load_json(HISTORY_PATH, [])
    if not isinstance(history, list):
        history = []

    added = record_current_signals(history) if record else 0
    evaluated = evaluate_all(history) if evaluate else 0
    save_json(HISTORY_PATH, history)

    print("Added signals:", added)
    print("Evaluated signals:", evaluated)
    summary(history)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--record-only", action="store_true")
    ap.add_argument("--evaluate-only", action="store_true")
    args = ap.parse_args()

    if args.record_only and args.evaluate_only:
        raise SystemExit("--record-only 同 --evaluate-only 不可同時使用")

    record = not args.evaluate_only
    evaluate = not args.record_only
    main(record=record, evaluate=evaluate)
