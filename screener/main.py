from __future__ import annotations
"""入口：cheap metadata filter -> 2y K 線 -> 平均成交量 -> Stage 2 -> Cup/Handle。"""
import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener import data as data_mod  # noqa: E402
from screener import pattern, stage2  # noqa: E402
from screener.market import market_status, sector_tag  # noqa: E402
from screener.universe import get_universe, get_broad_market  # noqa: E402

STRENGTH_ORDER = {"strong": 0, "neutral": 1, "weak": 2}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("screener")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "data")


def _rank(row: dict) -> tuple:
    """超過歷史下載上限時，優先保留市值大、流動性高嘅股票。"""
    return (row.get("market_cap") or 0, row.get("current_volume") or 0)


def _cheap_filter(rows: list[dict], min_price: float, min_current_vol: int,
                  min_market_cap: int) -> tuple[list[dict], dict]:
    passed, failed, unknown = [], [], []
    for row in rows:
        price = row.get("price")
        current_vol = row.get("current_volume")
        market_cap = row.get("market_cap")
        if price is None or current_vol is None or market_cap is None:
            unknown.append(row["symbol"])
            continue
        if price < min_price or current_vol < min_current_vol or market_cap < min_market_cap:
            failed.append(row["symbol"])
        else:
            passed.append(row)
    return passed, {
        "input": len(rows),
        "passed": len(passed),
        "failed": len(failed),
        "unknown": len(unknown),
    }


def run(limit: int = 0, pause: float = 0.35, universe_mode: str = "broad",
        prefilter: bool = True, min_price: float = 10.0, min_avg_vol: int = 500_000,
        min_market_cap: int = 2_000_000_000, max_universe: int = 3000,
        min_current_vol: int = 50_000):
    os.makedirs(OUT_DIR, exist_ok=True)

    if universe_mode == "broad":
        core, extra = get_broad_market()
    else:
        core, extra = get_universe(universe_mode), []

    raw_counts = {
        "core": len(core), "sp500": 0, "sp400": 0, "sp600": 0,
        "sp1500": 0, "nasdaq_candidates": len(extra),
    }
    if universe_mode == "sp1500":
        raw_counts["sp1500"] = len(core)
    elif universe_mode == "broad":
        # get_broad_market() 已經以 S&P500/400/600 建立 core；
        # 這裡唔再呼叫 get_universe("sp400") / ("sp600")，避免 invalid mode warning。
        raw_counts["sp1500"] = len(core)
        raw_counts["sp500"] = 0
        raw_counts["sp400"] = 0
        raw_counts["sp600"] = 0
    else:
        raw_counts[universe_mode] = len(core)

    if limit:
        core, extra = core[:limit], []

    all_candidates = core + extra
    log.info("候選池：core=%d；Nasdaq long-tail=%d；合計=%d", len(core), len(extra), len(all_candidates))

    cheap_stats = {"input": len(all_candidates), "passed": 0, "failed": 0, "unknown": 0}
    if prefilter:
        # current volume 只係「防止大量細流動性股票進入 historical」嘅廉價 gate；
        # 真正嘅 500k average daily volume 會用 2y Yahoo bars 計算，避免多一次 Yahoo request。
        cheap, cheap_stats = _cheap_filter(
            all_candidates, min_price=min_price,
            min_current_vol=min_current_vol,
            min_market_cap=min_market_cap,
        )
    else:
        log.warning("--no-prefilter：停用 cheap metadata 初篩")
        cheap = all_candidates

    cheap.sort(key=_rank, reverse=True)
    qualified_before_cap = len(cheap)
    if max_universe and len(cheap) > max_universe:
        log.info("cheap pool %d 隻 > historical 上限 %d，按市值/現時成交量保留 %d 隻",
                 len(cheap), max_universe, max_universe)
        universe = cheap[:max_universe]
    else:
        universe = cheap

    symbols = [u["symbol"] for u in universe]
    log.info("Cheap filter 後：%d 隻；最終 historical pool：%d 隻", qualified_before_cap, len(symbols))
    log.info("開始下載 %d 隻股票嘅 2y 歷史 K 線", len(symbols))
    all_data = data_mod.fetch_many(symbols, pause=pause)
    log.info("成功下載 %d 隻", len(all_data))

    # 2y data 已經下載；喺同一批資料計算真正嘅 63 trading-day average volume，
    # 唔需要額外 Yahoo quote endpoint。
    liquid_universe = []
    liquidity_failed = 0
    for row in universe:
        d = all_data.get(row["symbol"])
        if not d:
            continue
        avg_vol = data_mod.average_volume(d, bars=63)
        row["avg_volume_63d"] = avg_vol
        if avg_vol >= min_avg_vol:
            liquid_universe.append(row)
        else:
            liquidity_failed += 1

    log.info("63日平均成交量 >= %d：%d 隻；未達：%d 隻", min_avg_vol, len(liquid_universe), liquidity_failed)

    spy = all_data.get("SPY") or data_mod.fetch_daily("SPY", "2y")
    spy_close = spy["close"] if spy else []
    ratings = stage2.rs_ratings(all_data, spy_close)

    try:
        market = market_status()
    except Exception as e:  # noqa: BLE001
        log.warning("大市監控失敗: %s", e)
        market = {}
    rel_map = {s["symbol"]: s["rel_vs_spy"] for s in market.get("sectors_all", [])}

    results, watchlist = [], []
    charts = {}
    stage2_pass = 0
    pattern_matches = 0
    for u in liquid_universe:
        sym = u["symbol"]
        d = all_data.get(sym)
        if not d:
            continue
        s2 = stage2.check_stage2(d, ratings.get(sym, 0))
        if not s2["pass"]:
            continue
        stage2_pass += 1
        pat = pattern.detect(d)
        if not pat:
            continue
        pattern_matches += 1
        st = sector_tag(u["sector"], rel_map)
        entry = {
            "symbol": sym, "name": u["name"], "sector": u["sector"],
            "sector_strength": st, "avg_volume_63d": u["avg_volume_63d"],
            "market_cap": u.get("market_cap"),
            "stage2": s2, **{k: v for k, v in pat.items() if k != "chart_start"},
        }
        if pat["grade"] == "match":
            results.append(entry)
        else:
            watchlist.append(entry)
        cs = pat["chart_start"]
        charts[sym] = {
            "dates": d["dates"][cs:], "open": d["open"][cs:], "high": d["high"][cs:],
            "low": d["low"][cs:], "close": d["close"][cs:], "volume": d["volume"][cs:],
            "rim_date": pat["metrics"]["rim_date"],
            "bottom_date": pat["metrics"]["bottom_date"],
            "pivot": pat["metrics"]["pivot"],
        }

    def sort_key(e: dict):
        return (STRENGTH_ORDER[e["sector_strength"]["tier"]], -e["stage2"]["values"]["rs_rating"])

    results.sort(key=sort_key)
    watchlist.sort(key=sort_key)

    universe_stats = {
        **raw_counts,
        "cheap_filter": cheap_stats,
        "min_current_volume_gate": min_current_vol,
        "min_average_volume": min_avg_vol,
        "qualified_before_cap": qualified_before_cap,
        "historical_selected": len(universe),
        "historical_downloaded": len(all_data),
        "liquidity_pass": len(liquid_universe),
        "liquidity_fail": liquidity_failed,
        "stage2_pass": stage2_pass,
        "pattern_matches": pattern_matches,
        "results": len(results),
        "watchlist": len(watchlist),
    }

    out = {
        "generated_at": market.get("date"),
        "market": market,
        "universe_stats": universe_stats,
        "total_scanned": len(liquid_universe),
        "results": results,
        "watchlist": watchlist,
    }
    with open(os.path.join(OUT_DIR, "results.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    with open(os.path.join(OUT_DIR, "charts.json"), "w", encoding="utf-8") as f:
        json.dump(charts, f, ensure_ascii=False)
    log.info("完成：match %d 隻，watch %d 隻", len(results), len(watchlist))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只掃前 N 隻（測試用，會停用長尾）")
    ap.add_argument("--pause", type=float, default=0.35, help="每隻歷史下載間隔秒數")
    ap.add_argument("--universe", choices=["sp500", "sp1500", "broad"], default="broad",
                    help="sp500≈500 / sp1500=S&P500+400+600 / broad=sp1500 + Nasdaq 全市場")
    ap.add_argument("--no-prefilter", action="store_true", help="跳過 cheap metadata 初篩（debug 用）")
    ap.add_argument("--min-price", type=float, default=10.0, help="初篩：最低股價")
    ap.add_argument("--min-avg-vol", type=int, default=500_000, help="真正篩選：63日平均成交量")
    ap.add_argument("--min-current-vol", type=int, default=50_000,
                    help="廉價 gate：單日成交量最低值；只用於減少 historical request")
    ap.add_argument("--min-market-cap", type=int, default=2_000_000_000,
                    help="初篩：最低市值（美元），預設 20 億")
    ap.add_argument("--max-universe", type=int, default=3000,
                    help="最終 2y historical pool 上限；按市值/現時成交量排序，0=不限")
    a = ap.parse_args()
    run(limit=a.limit, pause=a.pause, universe_mode=a.universe,
        prefilter=not a.no_prefilter, min_price=a.min_price,
        min_avg_vol=a.min_avg_vol, min_market_cap=a.min_market_cap,
        max_universe=a.max_universe, min_current_vol=a.min_current_vol)
