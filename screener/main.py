from __future__ import annotations
"""入口：跑整個篩選 pipeline，輸出 JSON 到 docs/data/。"""
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


def run(limit: int = 0, pause: float = 0.35, universe_mode: str = "broad",
        prefilter: bool = True, min_price: float = 10.0, min_avg_vol: int = 500_000,
        min_market_cap: int = 2_000_000_000, max_universe: int = 1500):
    os.makedirs(OUT_DIR, exist_ok=True)

    if universe_mode == "broad":
        core, extra = get_broad_market()
    else:
        core, extra = get_universe(universe_mode), []
    if limit:
        core, extra = core[:limit], []
    log.info("核心池（%s，已知板塊）：%d 隻；長尾候選（SEC 全市場，未知板塊）：%d 隻",
              universe_mode, len(core), len(extra))

    # 核心池（sp1500）：流動性初篩 fail-open——冇報價就保留，交返俾後面 Stage 2 把關。
    # 長尾（SEC 全市場，冇經過指數篩選）：一定要用市值+流動性初篩 fail-closed 把關，
    # 攞唔到報價/唔夠市值/唔夠流動就直接剔除，唔會落成千上萬隻未經驗證嘅細價股歷史數據。
    if prefilter:
        core_syms = [u["symbol"] for u in core]
        if core_syms:
            kept = set(data_mod.prefilter_liquidity(core_syms, min_price=min_price,
                                                       min_avg_vol=min_avg_vol, fail_open=True))
            core = [u for u in core if u["symbol"] in kept]
        extra_syms = [u["symbol"] for u in extra]
        if extra_syms:
            kept = set(data_mod.prefilter_liquidity(extra_syms, min_price=min_price,
                                                       min_avg_vol=min_avg_vol,
                                                       min_market_cap=min_market_cap, fail_open=False))
            extra = [u for u in extra if u["symbol"] in kept]
    elif extra:
        log.warning("--no-prefilter 只影響核心池；長尾冇市值/流動性把關，為安全起見直接捨棄 %d 隻", len(extra))
        extra = []

    universe = core + extra
    if max_universe and len(universe) > max_universe:
        log.info("篩後池 %d 隻 > 上限 %d，截取前 %d 隻（核心池優先保留）", len(universe), max_universe, max_universe)
        universe = universe[:max_universe]
    symbols = [u["symbol"] for u in universe]
    log.info("最終下載池：%d 隻", len(symbols))

    log.info("開始下載 %d 隻股票嘅歷史 K 線", len(symbols))
    all_data = data_mod.fetch_many(symbols, pause=pause)
    log.info("成功下載 %d 隻", len(all_data))

    spy = all_data.get("SPY") or data_mod.fetch_daily("SPY", "2y")
    spy_close = spy["close"] if spy else []
    ratings = stage2.rs_ratings(all_data, spy_close)

    # 大市 + 板塊相對強弱要喺掃描前攞好，先可以逐隻股票對應加分
    try:
        market = market_status()
    except Exception as e:  # noqa: BLE001
        log.warning("大市監控失敗: %s", e)
        market = {}
    rel_map = {s["symbol"]: s["rel_vs_spy"] for s in market.get("sectors_all", [])}

    results, watchlist = [], []
    charts = {}
    for u in universe:
        sym = u["symbol"]
        d = all_data.get(sym)
        if not d:
            continue
        s2 = stage2.check_stage2(d, ratings.get(sym, 0))
        if not s2["pass"]:
            continue
        pat = pattern.detect(d)
        if not pat:
            continue
        # Step 1 加分制：板塊強弱唔用嚟剔除股票（Stage 2 + 杯柄照樣獨立篩），
        # 淨係影響 🔥 標記同排序——強股都可以嚟自中性/冇對應 GICS 嘅主題板塊。
        st = sector_tag(u["sector"], rel_map)
        entry = {
            "symbol": sym, "name": u["name"], "sector": u["sector"],
            "sector_strength": st,
            "stage2": s2, **{k: v for k, v in pat.items() if k != "chart_start"},
        }
        if pat["grade"] == "match":
            results.append(entry)
        else:
            watchlist.append(entry)
        # 網頁 K 線圖數據（杯口前 20 日起）
        cs = pat["chart_start"]
        charts[sym] = {
            "dates": d["dates"][cs:], "open": d["open"][cs:], "high": d["high"][cs:],
            "low": d["low"][cs:], "close": d["close"][cs:], "volume": d["volume"][cs:],
            "rim_date": pat["metrics"]["rim_date"],
            "bottom_date": pat["metrics"]["bottom_date"],
            "pivot": pat["metrics"]["pivot"],
        }

    def sort_key(e: dict):
        # 強勢板塊排前，弱勢排後，中性/冇對應板塊企中間；同層再按 RS 評級排。
        return (STRENGTH_ORDER[e["sector_strength"]["tier"]], -e["stage2"]["values"]["rs_rating"])

    results.sort(key=sort_key)
    watchlist.sort(key=sort_key)

    out = {"generated_at": market.get("date"), "market": market,
           "total_scanned": len(all_data), "results": results, "watchlist": watchlist}
    with open(os.path.join(OUT_DIR, "results.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    with open(os.path.join(OUT_DIR, "charts.json"), "w", encoding="utf-8") as f:
        json.dump(charts, f, ensure_ascii=False)
    log.info("完成：match %d 隻，watch %d 隻", len(results), len(watchlist))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只掃前 N 隻（測試用，會停用長尾）")
    ap.add_argument("--pause", type=float, default=0.35, help="每隻下載間隔秒數")
    ap.add_argument("--universe", choices=["sp500", "sp1500", "broad"], default="broad",
                     help="sp500≈500隻 / sp1500=S&P500+400+600≈1400-1500隻(視乎Wikipedia)"
                          " / broad=sp1500 + SEC全市場長尾(市值/流動性把關後)，預設")
    ap.add_argument("--no-prefilter", action="store_true",
                     help="跳過核心池嘅流動性初篩（debug 用）；長尾唔受影響，冇把關一律捨棄")
    ap.add_argument("--min-price", type=float, default=10.0, help="初篩：最低股價")
    ap.add_argument("--min-avg-vol", type=int, default=500_000, help="初篩：最低3個月日均成交量")
    ap.add_argument("--min-market-cap", type=int, default=2_000_000_000,
                     help="長尾初篩：最低市值（美元），預設 20 億")
    ap.add_argument("--max-universe", type=int, default=1500,
                     help="篩後最終下載池嘅隻數上限（核心池優先保留），0 = 不限")
    a = ap.parse_args()
    run(limit=a.limit, pause=a.pause, universe_mode=a.universe,
        prefilter=not a.no_prefilter, min_price=a.min_price, min_avg_vol=a.min_avg_vol,
        min_market_cap=a.min_market_cap, max_universe=a.max_universe)
