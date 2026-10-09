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
from screener.market import INDICES, SECTORS, sector_tag
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

    # Rebuild the sector-relative-strength radar entirely from local cache.
    # The batch runner refreshes these ETF caches alongside the stock cache.
    market = {}
    data_date = cache_last_date(all_data)
    # Relative freshness guard; the reference date is the newest cached bar.
    freshness_cutoff = (datetime.date.fromisoformat(data_date) - datetime.timedelta(days=7)).isoformat() if data_date else None
    generated_at = data_date or market.get("date") or datetime.datetime.utcnow().strftime("%Y-%m-%d")

    # Rebuild market radar from local cache as well; do not preserve stale
    # market data from a previous website build.
    market["date"] = data_date
    market["indices"] = {}
    index_proxies = {"^GSPC": "SPY", "^IXIC": "QQQ", "^RUT": "IWM"}
    for index_symbol, name in INDICES.items():
        source = index_symbol if index_symbol in all_data else index_proxies.get(index_symbol, "")
        d = all_data.get(source)
        if not d or len(d.get("close", [])) < 126 or not d.get("dates") or (freshness_cutoff and str(d["dates"][-1]) < freshness_cutoff):
            continue
        hi = max(d["high"][-126:])
        dd = round((1 - d["close"][-1] / hi) * 100, 1)
        market["indices"][index_symbol] = {
            "name": name,
            "source_symbol": source,
            "is_proxy": source != index_symbol,
            "data_date": str(d["dates"][-1]),
            "drawdown_pct": dd,
            "correction": 5 <= dd <= 10,
            "pullback": 0 < dd < 5,
        }

    spy = all_data.get("SPY")
    sectors_all = []
    if spy and len(spy.get("close", [])) >= 22 and spy.get("dates") and (not freshness_cutoff or str(spy["dates"][-1]) >= freshness_cutoff):
        spy_close = spy["close"]
        spy_ret_1m = (spy_close[-1] / spy_close[-22] - 1) * 100
        for etf, name in SECTORS.items():
            d = all_data.get(etf)
            if not d or len(d.get("close", [])) < 22 or not d.get("dates") or (freshness_cutoff and str(d["dates"][-1]) < freshness_cutoff):
                continue
            c = d["close"]
            ret_1m = (c[-1] / c[-22] - 1) * 100
            sectors_all.append({
                "symbol": etf,
                "name": name,
                "data_date": str(d["dates"][-1]),
                "ret_1m": round(ret_1m, 1),
                "rel_vs_spy": round(ret_1m - spy_ret_1m, 1),
            })
        sectors_all.sort(key=lambda s: s["rel_vs_spy"], reverse=True)
        if spy and len(spy_close) >= 126:
            market["spy_ret_6mo"] = round((spy_close[-1] / spy_close[-126] - 1) * 100, 1)

    market["sectors_all"] = sectors_all
    market["sectors"] = sectors_all[:8]
    market["sector_coverage"] = {"available": len(sectors_all), "expected": len(SECTORS), "missing": [s for s in SECTORS if s not in {x["symbol"] for x in sectors_all}]}

    # Use the persistent cheap-filter snapshot as the universe membership.
    candidates = []
    for symbol in snapshot:
        d = all_data.get(symbol)
        row = metadata.get(symbol, {})
        if not d:
            continue
        # A recent site-wide date must not conceal a stale individual stock.
        if not d.get("dates") or (freshness_cutoff is not None and str(d["dates"][-1]) < freshness_cutoff):
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
        "universe_stats_date": state.get("universe_stats_date"),
        "broad_input": state.get("universe_broad_count"),
        "cheap_filter_original": state.get("universe_cheap_stats"),
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
        "historical_downloaded": sum(1 for s in snapshot_set if s in all_data),
        "liquidity_pass": len(candidates),
        "liquidity_fail": sum(1 for s in snapshot if s in all_data and data_mod.average_volume(all_data[s], bars=63) < 500_000),
        "excluded_missing_or_stale": len(snapshot) - len(candidates) - sum(1 for s in snapshot if s in all_data and data_mod.average_volume(all_data[s], bars=63) < 500_000),
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
