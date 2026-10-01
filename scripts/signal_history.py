from __future__ import annotations
"""Zero-API signal history recorder and trading-oriented outcome evaluator.

This script never calls a market-data API. It only reads:
  1. docs/data/results.json
  2. .cache/market/*_2y.json

The history file is append-only by (signal_date, symbol). Existing signals
are preserved when the evaluator is upgraded.

Usage:
  python3 scripts/signal_history.py
  python3 scripts/signal_history.py --record-only
  python3 scripts/signal_history.py --evaluate-only

Outcome model:
  Signal -> Pivot touch -> Touch breakout -> Confirmed breakout
          -> R-multiple / MFE / MAE / false breakout.

A touch breakout is the first future bar whose high reaches the stored pivot.
A confirmed breakout is the first future bar whose close finishes above pivot.

False breakout is evaluated separately for each breakout definition. It means
price later trades below pivot before the stored 1R target is reached.
"""
import argparse
import json
import os
from datetime import datetime
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_PATH = os.path.join(ROOT, "docs", "data", "results.json")
HISTORY_PATH = os.path.join(ROOT, "docs", "data", "signal_history.json")
CACHE_DIR = os.path.join(ROOT, ".cache", "market")

WINDOWS = (1, 3, 5, 10)
R_LEVELS = (0.5, 1.0, 1.5, 2.0)


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
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


def as_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _r_level_map(pivot, risk):
    if risk is None or risk <= 0:
        return {}
    return {
        "{}R".format(r): round(pivot + risk * r, 4)
        for r in R_LEVELS
    }


def _evaluate_breakout(
    dates,
    highs,
    lows,
    closes,
    breakout_idx,
    pivot,
    stop,
    target_1r,
    risk,
):
    """Evaluate one breakout definition from its first breakout bar onward."""
    max_high = max(highs[breakout_idx:])
    min_low = min(lows[breakout_idx:])
    max_high_idx = breakout_idx + highs[breakout_idx:].index(max_high)
    min_low_idx = breakout_idx + lows[breakout_idx:].index(min_low)

    result = {
        "max_high": round(max_high, 4),
        "max_high_date": str(dates[max_high_idx])[:10],
        "min_low": round(min_low, 4),
        "min_low_date": str(dates[min_low_idx])[:10],
        "max_gain_from_pivot_pct": round(
            (max_high / pivot - 1.0) * 100.0, 2
        ),
        "max_drawdown_from_pivot_pct": round(
            (min_low / pivot - 1.0) * 100.0, 2
        ),
    }

    if risk is not None and risk > 0:
        for r in R_LEVELS:
            level = pivot + risk * r
            result["{}R_hit".format(r)] = max_high >= level
            result["{}R_level".format(r)] = round(level, 4)

    if stop is not None:
        result["stop_hit"] = min_low <= stop

    target_level = (
        pivot + risk if risk is not None and risk > 0 else target_1r
    )

    false_breakout = False
    false_breakout_idx = None
    target_hit_idx = None

    for i in range(breakout_idx, len(dates)):
        if target_level is not None and highs[i] >= target_level:
            target_hit_idx = i
            break
        if i > breakout_idx and lows[i] < pivot:
            false_breakout = True
            false_breakout_idx = i
            break

    result["target_1r_hit"] = target_hit_idx is not None
    result["false_breakout"] = false_breakout

    if false_breakout_idx is not None:
        result["false_breakout_date"] = str(dates[false_breakout_idx])[:10]
        result["false_breakout_bars"] = false_breakout_idx - breakout_idx

    if target_hit_idx is not None:
        result["target_1r_date"] = str(dates[target_hit_idx])[:10]
        result["target_1r_bars"] = target_hit_idx - breakout_idx

    return result


def _evaluate_breakout_windows(
    outcome,
    prefix,
    dates,
    highs,
    lows,
    closes,
    breakout_idx,
    pivot,
    stop,
    risk,
):
    for window in WINDOWS:
        start = breakout_idx
        end = min(len(dates), start + window + 1)
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
            # Intraday definition: price never traded below pivot.
            "pivot_held": min(ls) >= pivot,
        }

        if risk is not None and risk > 0:
            for r in R_LEVELS:
                item["{}R_hit".format(r)] = max(hs) >= pivot + risk * r

        if stop is not None:
            item["stop_hit"] = min(ls) <= stop

        outcome["{}_window_{}d".format(prefix, window)] = item


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

    future_start = signal_idx + 1
    if future_start >= n:
        return False

    pivot = as_float(signal.get("pivot"))
    stop = as_float(signal.get("stop_loss"))
    target_1r = as_float(signal.get("target_1r"))
    entry_close = as_float(signal.get("close"))

    outcome = signal.setdefault("outcome", {})
    outcome["last_evaluated_date"] = str(dates[-1])[:10]

    if pivot is None or entry_close is None:
        return False

    risk = None
    if stop is not None and pivot > stop:
        risk = pivot - stop
    elif target_1r is not None and target_1r > pivot:
        risk = target_1r - pivot

    if risk is not None and risk > 0:
        outcome["risk_per_share"] = round(risk, 4)
        outcome["r_levels"] = _r_level_map(pivot, risk)
    else:
        outcome["risk_per_share"] = None
        outcome["r_levels"] = {}

    # ------------------------------------------------------------------
    # 1) TOUCH BREAKOUT
    # First future bar whose intraday high reaches the pivot.
    # This preserves the original definition for backward compatibility.
    # ------------------------------------------------------------------
    touch_idx = None
    for i in range(future_start, n):
        if highs[i] >= pivot:
            touch_idx = i
            break

    outcome["pivot_touch"] = (
        {
            "date": str(dates[touch_idx])[:10],
            "bars_after_signal": touch_idx - signal_idx,
        }
        if touch_idx is not None
        else None
    )

    if touch_idx is None:
        outcome["breakout"] = None
        outcome["breakout_outcome"] = None
    else:
        outcome["breakout"] = {
            "date": str(dates[touch_idx])[:10],
            "bars_after_signal": touch_idx - signal_idx,
        }
        outcome["breakout_outcome"] = _evaluate_breakout(
            dates,
            highs,
            lows,
            closes,
            touch_idx,
            pivot,
            stop,
            target_1r,
            risk,
        )

        _evaluate_breakout_windows(
            outcome,
            "breakout",
            dates,
            highs,
            lows,
            closes,
            touch_idx,
            pivot,
            stop,
            risk,
        )

    # ------------------------------------------------------------------
    # 2) CONFIRMED BREAKOUT
    # First future bar whose CLOSE finishes above the pivot.
    # This is intentionally separate from the touch definition because a
    # wick through pivot can be rejection rather than a confirmed breakout.
    # ------------------------------------------------------------------
    confirmed_idx = None
    for i in range(future_start, n):
        if closes[i] > pivot:
            confirmed_idx = i
            break

    outcome["confirmed_breakout"] = (
        {
            "date": str(dates[confirmed_idx])[:10],
            "bars_after_signal": confirmed_idx - signal_idx,
        }
        if confirmed_idx is not None
        else None
    )

    if confirmed_idx is None:
        outcome["confirmed_breakout_outcome"] = None
    else:
        outcome["confirmed_breakout_outcome"] = _evaluate_breakout(
            dates,
            highs,
            lows,
            closes,
            confirmed_idx,
            pivot,
            stop,
            target_1r,
            risk,
        )

        _evaluate_breakout_windows(
            outcome,
            "confirmed_breakout",
            dates,
            highs,
            lows,
            closes,
            confirmed_idx,
            pivot,
            stop,
            risk,
        )

    # Signal-date windows are retained because they answer a different
    # question: how quickly did a setup move toward the pivot or invalidate?
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
            "pivot_hit": max(hs) >= pivot,
        }

        if stop is not None:
            item["stop_hit"] = min(ls) <= stop

        if risk is not None and risk > 0:
            for r in R_LEVELS:
                item["{}R_hit".format(r)] = (
                    max(hs) >= pivot + risk * r
                )

        outcome["signal_window_{}d".format(window)] = item

    return True


def evaluate_all(history: list[dict]) -> int:
    evaluated = 0
    for signal in history:
        symbol = str(signal.get("symbol") or "").upper()
        if not symbol:
            continue
        data = load_cache(symbol)
        if data and evaluate_signal(signal, data):
            evaluated += 1
    return evaluated


def pct(numerator, denominator):
    return 100.0 * numerator / denominator if denominator else 0.0


def _print_breakout_summary(
    history,
    breakout_key,
    window_prefix,
    title,
):
    signals = [
        s for s in history
        if isinstance(s.get("outcome", {}).get(breakout_key), dict)
    ]

    print("{}: {}".format(title, len(signals)))

    completed = [
        s for s in signals
        if isinstance(
            s.get("outcome", {}).get(window_prefix),
            dict,
        )
    ]

    if completed:
        print("{} outcomes evaluated: {}".format(
            title, len(completed)
        ))

        for r in R_LEVELS:
            key = "{}R_hit".format(r)
            hits = sum(
                bool(
                    s["outcome"][window_prefix].get(key)
                )
                for s in completed
            )
            print(
                "  +{}R reached: {}/{} ({:.1f}%)".format(
                    r,
                    hits,
                    len(completed),
                    pct(hits, len(completed)),
                )
            )

        false = sum(
            bool(
                s["outcome"][window_prefix].get(
                    "false_breakout"
                )
            )
            for s in completed
        )
        stops = sum(
            bool(
                s["outcome"][window_prefix].get("stop_hit")
            )
            for s in completed
        )

        print(
            "  False breakout: {}/{} ({:.1f}%)".format(
                false,
                len(completed),
                pct(false, len(completed)),
            )
        )
        print(
            "  Stop hit after breakout: {}/{} ({:.1f}%)".format(
                stops,
                len(completed),
                pct(stops, len(completed)),
            )
        )

        for window in WINDOWS:
            key = "{}_window_{}d".format(
                window_prefix, window
            )
            rows = [
                s["outcome"].get(key)
                for s in completed
                if isinstance(
                    s["outcome"].get(key),
                    dict,
                )
                and s["outcome"][key].get(
                    "bars_available", 0
                ) >= window
            ]

            if not rows:
                continue

            r1 = sum(
                bool(x.get("1.0R_hit"))
                for x in rows
            )
            stop = sum(
                bool(x.get("stop_hit"))
                for x in rows
            )
            held = sum(
                bool(x.get("pivot_held"))
                for x in rows
            )

            print(
                "{} +{}d: n={} 1R_hit={:.1f}% "
                "stop_hit={:.1f}% pivot_held={:.1f}%".format(
                    title,
                    window,
                    len(rows),
                    pct(r1, len(rows)),
                    pct(stop, len(rows)),
                    pct(held, len(rows)),
                )
            )


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
        print(
            "  {}: {}".format(
                grade.upper(),
                by_grade.get(grade, 0),
            )
        )

    for window in WINDOWS:
        key = "signal_window_{}d".format(window)
        rows = [
            s.get("outcome", {}).get(key)
            for s in history
            if isinstance(
                s.get("outcome", {}).get(key),
                dict,
            )
            and s["outcome"][key].get(
                "bars_available", 0
            ) >= window
        ]

        if not rows:
            continue

        pivot_hit = sum(
            bool(x.get("pivot_hit"))
            for x in rows
        )
        stop_hit = sum(
            bool(x.get("stop_hit"))
            for x in rows
        )
        r1_hit = sum(
            bool(x.get("1.0R_hit"))
            for x in rows
        )

        print(
            "Signal +{}d: n={} pivot_hit={:.1f}% "
            "1R_hit={:.1f}% stop_hit={:.1f}%".format(
                window,
                len(rows),
                pct(pivot_hit, len(rows)),
                pct(r1_hit, len(rows)),
                pct(stop_hit, len(rows)),
            )
        )

    print()
    print("--- TOUCH BREAKOUT: high >= pivot ---")
    _print_breakout_summary(
        history,
        "breakout",
        "breakout_outcome",
        "Touch breakout",
    )

    print()
    print("--- CONFIRMED BREAKOUT: close > pivot ---")
    _print_breakout_summary(
        history,
        "confirmed_breakout",
        "confirmed_breakout_outcome",
        "Confirmed breakout",
    )


def main(record: bool, evaluate: bool):
    history = load_json(HISTORY_PATH, [])

    if not isinstance(history, list):
        history = []

    added = (
        record_current_signals(history)
        if record
        else 0
    )
    evaluated = (
        evaluate_all(history)
        if evaluate
        else 0
    )

    save_json(HISTORY_PATH, history)

    print("Added signals:", added)
    print("Evaluated signals:", evaluated)
    summary(history)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--record-only",
        action="store_true",
    )
    ap.add_argument(
        "--evaluate-only",
        action="store_true",
    )
    args = ap.parse_args()

    if args.record_only and args.evaluate_only:
        raise SystemExit(
            "--record-only 同 --evaluate-only 不可同時使用"
        )

    main(
        record=not args.evaluate_only,
        evaluate=not args.record_only,
    )
