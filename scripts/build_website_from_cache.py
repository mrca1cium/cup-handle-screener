from __future__ import annotations
"""Build docs/data/results.json and charts.json entirely from the local cache.

No market-data history requests are made here. The batch runner already owns
the cache. Current Nasdaq metadata is used only for display names/sectors and
the persistent universe snapshot is authoritative for membership.
"""
import datetime
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, ".cache", "market")
OUT_DIR = os.path.join(ROOT, "docs", "data")
STATE_PATH = os.path.join(CACHE_DIR, "stashgamma_batch_state.json")

sys.path.insert(0, ROOT)

from screener import data as data_mod
from screener import pattern, stage2
from screener.main import _cheap_filter
from screener.market import sector_tag
from screener.universe import get_broad_market


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def load_cache() -> dict[str, dict]:
    out = {}
    for path in glob.glob(os.path.join(CACHE_DIR, "*_2y.json")):
        symbol = os.path.basename(path)[:-8].upper()
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            data = payload.get("data") or {}
            if len(data.get("close", [])) >= data_mod.DEFAULT_MIN_BARS:
                out[symbol] = data
        except Exception:
            continue
    return out


def cache_last_date(all_data: dict[str, dict]) -> str | None:
    dates = []
    for d in all_data.values():
        if d.get("dates"):
            dates.append(str(d["dates"][-1]))
    return max(dates) if dates else None


def load_previous_output() -> dict:
    path = os.path.join(OUT_DIR, "results.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def build_metadata(state: dict) -> dict[str, dict]:
    metadata = {}
    rows = state.get("universe_rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("symbol"):
                metadata[str(row["symbol"]).upper()] = row

    # Backward-compatible fallback: the batch state before this builder did
    # not persist row metadata. Nasdaq metadata is not historical market data,
    # and this call is only needed until the next batch stores universe_rows.
    if not metadata:
        try:
            core, extra = get_broad_market()
            for row in core + extra:
                if row.get("symbol"):
                    metadata[str(row["symbol"]).upper()] = row
        except Exception as exc:
            print(f"WARNING: unable to refresh Nasdaq metadata: {exc}")

    previous = load_previous_output()
    for entry in (previous.get("results") or []) + (previous.get("watchlist") or []):
        symbol = str(entry.get("symbol", "")).upper()
        if not symbol:
            continue
        row = metadata.setdefault(symbol, {})
        row.setdefault("name", entry.get("name", symbol))
        row.setdefault("sector", entry.get("sector", ""))
        if entry.get("market_cap") is not None:
            row.setdefault("market_cap", entry.get("market_cap"))
    return metadata


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)

    state = load_state()
    snapshot = state.get("universe_symbols")
    if not isinstance(snapshot, list) or not snapshot:
        raise SystemExit("universe_symbols snapshot is missing; run stashgamma_batch.py first")

    snapshot = [str(s).upper() for s in snapshot]
    snapshot_set = set(snapshot)
    all_data = load_cache()
    if "SPY" not in all_data:
        raise SystemExit("SPY_2y.json is missing from local cache")

    metadata = build_metadata(state)
    previous = load_previous_output()

    # Preserve the last known market radar without making extra historical-data
    # API requests. The stock screener itself is fully refreshed from cache.
    market = previous.get("market") or {}
    data_date = cache_last_date(all_data)
    generated_at = data_date or market.get("date") or datetime.datetime.utcnow().strftime("%Y-%m-%d")

    # Use the persistent cheap-filter snapshot as the universe membership.
    candidates = []
    for symbol in snapshot:
        d = all_data.get(symbol)
        row = metadata.get(symbol, {})
        if not d:
            continue
        avg_vol = data_mod.average_volume(d, bars=63)
        if avg_vol < 500_000:
            continue
        item = dict(row)
        item["symbol"] = symbol
        item["name"] = item.get("name") or symbol
        item["sector"] = item.get("sector") or ""
        item["avg_volume_63d"] = avg_vol
        candidates.append((symbol, item, d))

    ratings = stage2.rs_ratings(all_data, all_data["SPY"]["close"])
    rel_map = {
        str(s.get("symbol", "")).upper(): s.get("rel_vs_spy")
        for s in (market.get("sectors_all") or [])
        if s.get("symbol")
    }

    results = []
    watchlist = []
    charts = {}
    stage2_pass = 0
    pattern_candidates = 0

    for symbol, row, d in candidates:
        rating = ratings.get(symbol)
        if rating is None:
            continue

        s2 = stage2.check_stage2(d, rating)
        if not s2["pass"]:
            continue
        stage2_pass += 1

        pat = pattern.detect(d)
        if not pat:
            continue
        pattern_candidates += 1

        entry = {
            "symbol": symbol,
            "name": row["name"],
            "sector": row["sector"],
            "sector_strength": sector_tag(row["sector"], rel_map),
            "avg_volume_63d": row["avg_volume_63d"],
            "market_cap": row.get("market_cap"),
            "stage2": s2,
            **{k: v for k, v in pat.items() if k != "chart_start"},
        }

        if pat["grade"] == "match":
            results.append(entry)
        else:
            watchlist.append(entry)

        cs = pat["chart_start"]
        charts[symbol] = {
            "dates": d["dates"][cs:],
            "open": d["open"][cs:],
            "high": d["high"][cs:],
            "low": d["low"][cs:],
            "close": d["close"][cs:],
            "volume": d["volume"][cs:],
            "rim_date": pat["metrics"]["rim_date"],
            "bottom_date": pat["metrics"]["bottom_date"],
            "pivot": pat["metrics"]["pivot"],
        }

    strength_order = {"strong": 0, "neutral": 1, "weak": 2}

    def sort_key(entry):
        tier = entry.get("sector_strength", {}).get("tier", "neutral")
        return (
            strength_order.get(tier, 1),
            -(entry.get("stage2", {}).get("values", {}).get("rs_rating") or 0),
        )

    results.sort(key=sort_key)
    watchlist.sort(key=sort_key)

    unavailable = data_mod._load_unavailable()
    insufficient_map = state.get("insufficient_symbols")
    if not isinstance(insufficient_map, dict):
        insufficient_map = {}

    universe_stats = {
        "core": state.get("universe_core_count"),
        "sp1500": state.get("universe_core_count"),
        "nasdaq_candidates": state.get("universe_extra_count"),
        "cheap_filter": {
            "input": len(snapshot),
            "passed": len(snapshot),
            "failed": 0,
            "unknown": 0,
        },
        "min_current_volume_gate": 50_000,
        "min_average_volume": 500_000,
        "qualified_before_cap": len(snapshot),
        "historical_selected": len(snapshot),
        "historical_downloaded": len(all_data),
        "liquidity_pass": len(candidates),
        "liquidity_fail": max(0, len(snapshot) - len(candidates)),
        "stage2_pass": stage2_pass,
        "pattern_matches": pattern_candidates,
        "results": len(results),
        "watchlist": len(watchlist),
        "cache_unavailable": len(unavailable & snapshot_set),
        "cache_insufficient": len(insufficient_map.keys() & snapshot_set),
    }

    output = {
        "generated_at": generated_at,
        "market": market,
        "universe_stats": universe_stats,
        "total_scanned": len(candidates),
        "results": results,
        "watchlist": watchlist,
    }

    with open(os.path.join(OUT_DIR, "results.json"), "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    with open(os.path.join(OUT_DIR, "charts.json"), "w", encoding="utf-8") as f:
        json.dump(charts, f, ensure_ascii=False, separators=(",", ":"))

    print(
        "Website build: cache={:,} snapshot={:,} liquidity={:,} Stage2={:,} MATCH={:,} WATCH={:,} generated_at={}".format(
            len(all_data),
            len(snapshot),
            len(candidates),
            stage2_pass,
            len(results),
            len(watchlist),
            generated_at,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
