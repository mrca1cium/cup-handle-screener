"""Independent, offline Minervini-inspired VCP detector (B balanced mode).

No I/O, API calls, orders, or changes to legacy Cup & Handle classification.
Pivots are usable only after their right-hand confirmation bars have closed.
"""
from __future__ import annotations

from .vcp_config import BALANCED, VCPConfig

ALGORITHM_VERSION = "vcp-b-1.0"


def validate_ohlcv(d: dict) -> bool:
    keys = ("dates", "open", "high", "low", "close", "volume")
    try:
        cols = [d[k] for k in keys]
        n = len(cols[0])
        if n < 260 or any(len(c) != n for c in cols):
            return False
        if any(not isinstance(x, str) or not x for x in d["dates"]):
            return False
        if any(d["dates"][i] >= d["dates"][i + 1] for i in range(n - 1)):
            return False
        for o, h, l, c, v in zip(d["open"], d["high"], d["low"], d["close"], d["volume"]):
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (o, h, l, c, v)):
                return False
            if not all(float("-inf") < x < float("inf") for x in (o, h, l, c, v)):
                return False
            if l <= 0 or v < 0 or not (l <= o <= h and l <= c <= h):
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def confirmed_pivots(d: dict, start: int, end: int, wing: int = 3) -> list[dict]:
    """Return strictly confirmed pivots within [start,end); ties are not pivots.

    end is exclusive and may be a historical as-of boundary.
    """
    high, low = d["high"], d["low"]
    out = []
    for i in range(max(start + wing, wing), min(end - wing, len(high) - wing)):
        hs = high[i - wing:i + wing + 1]
        ls = low[i - wing:i + wing + 1]
        is_high = high[i] == max(hs) and hs.count(high[i]) == 1
        is_low = low[i] == min(ls) and ls.count(low[i]) == 1
        if is_high == is_low:
            continue
        out.append({
            "kind": "H" if is_high else "L",
            "index": i,
            "confirmed_index": i + wing,
            "date": d["dates"][i],
            "confirmed_date": d["dates"][i + wing],
            "price": high[i] if is_high else low[i],
        })
    # Keep the most extreme pivot of any adjacent same-kind run.
    reduced = []
    for p in out:
        if reduced and reduced[-1]["kind"] == p["kind"]:
            old = reduced[-1]
            better = p["price"] > old["price"] if p["kind"] == "H" else p["price"] < old["price"]
            if better:
                reduced[-1] = p
        else:
            reduced.append(p)
    return reduced


def detect(d: dict, rs_rating: float, stage2_pass: bool, config: VCPConfig = BALANCED,
           as_of: int | None = None) -> dict | None:
    """Classify pre-breakout VCP as confirmed/watch; None means not shortlisted.

    A historical as_of is an exclusive bar count. Caller must supply RS and
    Stage2 calculated using the *same* historical cutoff.
    """
    if as_of is not None:
        if not isinstance(as_of, int) or as_of < 0 or as_of > len(d.get("close", [])):
            return None
        d = {k: v[:as_of] if isinstance(v, list) else v for k, v in d.items()}
    if not validate_ohlcv(d) or not stage2_pass or rs_rating is None or rs_rating < config.min_rs:
        return None
    n = len(d["close"])
    if sum(d["volume"][-63:]) / 63 <= config.min_avg_volume_63d:
        return None
    # All swing points must be confirmed by the as-of date.
    pivots = confirmed_pivots(d, max(0, n - config.max_base_days), n, config.pivot_wing)
    # Select the latest qualifying contiguous contraction chain; never mix
    # disjoint chains or use a future swing to validate an earlier signal.
    pairs = []
    for a, b in zip(pivots, pivots[1:]):
        if a["kind"] != "H" or b["kind"] != "L":
            continue
        if b["index"] - a["index"] < config.min_pivot_spacing:
            continue
        depth = (a["price"] - b["price"]) / a["price"] * 100
        if depth >= config.min_pullback_pct:
            pairs.append({"high": a, "low": b, "depth": depth})
    if len(pairs) < config.min_contractions:
        return None

    chains = []
    for i in range(len(pairs)):
        chain = [pairs[i]]
        for p in pairs[i + 1:]:
            prev = chain[-1]
            if p["high"]["index"] <= prev["low"]["index"]:
                break
            # Do not jump over a non-contracting pair.
            if p["depth"] > prev["depth"] * config.contraction_ratio_max:
                break
            chain.append(p)
        if len(chain) >= config.min_contractions:
            chains.append(chain)
    if not chains:
        # A potentially valid WATCH can have two real pullbacks without
        # monotonic contraction, but only if the most recent two are connected.
        a, b = pairs[-2:]
        if b["high"]["index"] <= a["low"]["index"]:
            return None
        chains = [[a, b]]

    def rank(chain):
        return (chain[-1]["low"]["index"], len(chain), -chain[-1]["depth"])
    chain = max(chains, key=rank)
    first, last = chain[0], chain[-1]
    base_days = n - 1 - first["high"]["index"]
    if not config.min_base_days <= base_days <= config.max_base_days:
        return None
    # A pivot is the latest confirmed resistance of the final contraction.
    pivot = last["high"]["price"]
    close = d["close"][-1]
    distance = (pivot - close) / pivot * 100
    if distance < 0 or distance > config.watch_pivot_distance_max:
        return None
    if close < last["low"]["price"]:
        return None
    last_depth = last["depth"]
    if last_depth > config.watch_last_depth_max:
        return None
    avg3 = sum(d["volume"][-3:]) / 3
    avg50 = sum(d["volume"][-50:]) / 50
    if avg50 <= 0:
        return None
    vdu_ratio = avg3 / avg50
    # Last ten sessions vs preceding twenty; both windows exclude no bars.
    prev20 = sum(d["volume"][-30:-10]) / 20
    last10 = sum(d["volume"][-10:]) / 10
    volume_trend_ok = prev20 > 0 and last10 <= prev20
    contracting = all(
        chain[i]["depth"] <= chain[i - 1]["depth"] * config.contraction_ratio_max
        for i in range(1, len(chain))
    )
    confirmed = (contracting
                 and last_depth <= config.confirmed_last_depth_max
                 and distance <= config.confirmed_pivot_distance_max
                 and vdu_ratio <= config.vdu_ratio_max
                 and volume_trend_ok)
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "as_of_date": d["dates"][-1],
        "signal_date": d["dates"][-1],
        "grade": "confirmed" if confirmed else "watch",
        "rs_rating": round(rs_rating, 1),
        "base_days": base_days,
        "contraction_depths_pct": [round(p["depth"], 2) for p in chain],
        "contraction_pivots": [
            {"high_date": p["high"]["date"], "high_confirmed_date": p["high"]["confirmed_date"],
             "low_date": p["low"]["date"], "low_confirmed_date": p["low"]["confirmed_date"],
             "depth_pct": round(p["depth"], 2)}
            for p in chain
        ],
        "pivot": round(pivot, 4),
        "close": round(close, 4),
        "distance_to_pivot_pct": round(distance, 2),
        "vdu_ratio": round(vdu_ratio, 3),
        "volume_trend_ok": volume_trend_ok,
        "progressive_contraction": contracting,
        "breakout": False,
        "checks": {
            "stage2": True,
            "rs_80": True,
            "progressive_contraction": contracting,
            "last_depth_8": last_depth <= config.confirmed_last_depth_max,
            "vdu_060": vdu_ratio <= config.vdu_ratio_max,
            "volume_trend": volume_trend_ok,
            "pivot_distance_5": distance <= config.confirmed_pivot_distance_max,
        },
    }
