from __future__ import annotations
"""入口：quote 初篩 -> 排序限額 -> 歷史 K 線 -> Stage 2 -> Cup/Handle，輸出 JSON。"""
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


def _quote_rank(symbol: str, quotes: dict) -> tuple:
    """歷史下載名額不足時，優先保留流動性高、市值大嘅股票。"""
    q = quotes.get(symbol, {})
    market_cap = q.get("market_cap") or 0
    avg_vol = q.get("avg_vol") or 0
    return (market_cap, avg_vol)


def run(limit: int = 0, pause: float = 0.35, universe_mode: str = "broad",
        prefilter: bool = True, min_price: float = 10.0, min_avg_vol: int = 500_000,
        min_market_cap: int = 2_000_000_000, max_universe: int = 3000):
    os.makedirs(OUT_DIR, exist_ok=True)

    if universe_mode == "broad":
        core, extra = get_broad_market()
    else:
        core, extra = get_universe(universe_mode), []

    raw_counts = {
        "core": len(core),
        "sp500": 0,
        "sp400": 0,
        "sp600": 0,
        "sp1500": 0,
        "sec_candidates": len(extra),
    }
    if universe_mode in {"sp1500", "broad"}:
        raw_counts["sp1500"] = len(core)
        # get_broad_market() returns the three index components merged into core;
        # get_universe() has already normalised/deduped them, so these are diagnostic only.
        if universe_mode == "broad":
            try:
                raw_counts["sp500"] = len(get_universe("sp500"))
                raw_counts["sp400"] = len(get_universe("sp400"))
                raw_counts["sp600"] = len(get_universe("sp600"))
            except Exception as e:  # noqa: BLE001
                log.warning("建立 S&P component 診斷數字失敗: %s", e)
    else:
        raw_counts[universe_mode] = len(core)

    if limit:
        core, extra = core[:limit], []

    log.info("核心池（%s）：%d 隻；長尾候選（SEC 全市場）：%d 隻",
              universe_mode, len(core), len(extra))

    quote_stats = {
        "core": {"input": len(core), "passed": 0, "failed": 0, "unknown": 0},
        "extra": {"input": len(extra), "passed": 0, "failed": 0, "unknown": 0},
    }
    quote_map = {}

    if prefilter:
        # 核心池：冇 quote 時 fail-open，避免 Yahoo 暫時漏報導致 S&P1500 成員被誤刪。
        core_syms = [u["symbol"] for u in core]
        core_screen = data_mod.screen_quotes(
            core_syms, min_price=min_price, min_avg_vol=min_avg_vol, min_market_cap=0
        ) if core_syms else {"passed": [], "failed": [], "unknown": [], "quotes": {},
                              "stats": quote_stats["core"]}
        quote_map.update(core_screen["quotes"])
        quote_stats["core"] = core_screen["stats"]
        core_kept = set(core_screen["passed"]) | set(core_screen["unknown"])
        core = [u for u in core if u["symbol"] in core_kept]

        # 長尾：必須 quote PASS + 市值 >= 門檻，先攔住 SEC 全市場大量細價股。
        extra_syms = [u["symbol"] for u in extra]
        extra_screen = data_mod.screen_quotes(
            extra_syms, min_price=min_price, min_avg_vol=min_avg_vol,
            min_market_cap=min_market_cap
        ) if extra_syms else {"passed": [], "failed": [], "unknown": [], "quotes": {},
                              "stats": quote_stats["extra"]}
        quote_map.update(extra_screen["quotes"])
        quote_stats["extra"] = extra_screen["stats"]
        extra_kept = set(extra_screen["passed"])
        extra = [u for u in extra if u["symbol"] in extra_kept]
    else:
        # Debug 模式仍然只允許核心池；否則會把 SEC 全市場候選直接送入歷史 API。
        log.warning("--no-prefilter：停用 quote 初篩，長尾候選一律捨棄")
        extra = []

    qualified = core + extra
    qualified_before_cap = len(qualified)

    # 唔再用「list 順序」硬截 1500。先按 quote 市值/流動性排序，再決定歷史下載名額。
    # 這樣 broad 模式即使有 2,000-3,000 隻合資格股票，都會優先保留真正活躍的大型股票。
    qualified.sort(key=lambda u: _quote_rank(u["symbol"], quote_map), reverse=True)
    if max_universe and len(qualified) > max_universe:
        log.info("合資格池 %d 隻 > 歷史下載上限 %d，按市值/流動性排序後保留 %d 隻",
                 len(qualified), max_universe, max_universe)
        universe = qualified[:max_universe]
    else:
        universe = qualified

    symbols = [u["symbol"] for u in universe]
    log.info("quote 初篩後：%d 隻；最終歷史下載池：%d 隻", qualified_before_cap, len(symbols))

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
    stage2_pass = 0
    pattern_matches = 0
    for u in universe:
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

    universe_stats = {
        **raw_counts,
        "quote_core": quote_stats["core"],
        "quote_extra": quote_stats["extra"],
        "qualified_before_cap": qualified_before_cap,
        "historical_selected": len(universe),
        "historical_downloaded": len(all_data),
        "stage2_pass": stage2_pass,
        "pattern_matches": pattern_matches,
        "results": len(results),
        "watchlist": len(watchlist),
    }

    out = {
        "generated_at": market.get("date"),
        "market": market,
        "universe_stats": universe_stats,
        "total_scanned": len(all_data),
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
    ap.add_argument("--pause", type=float, default=0.35, help="每隻下載間隔秒數")
    ap.add_argument("--universe", choices=["sp500", "sp1500", "broad"], default="broad",
                     help="sp500≈500隻 / sp1500=S&P500+400+600 / broad=sp1500 + SEC全市場長尾，預設")
    ap.add_argument("--no-prefilter", action="store_true",
                     help="跳過核心池嘅 quote 初篩（debug 用）；長尾一律捨棄")
    ap.add_argument("--min-price", type=float, default=10.0, help="初篩：最低股價")
    ap.add_argument("--min-avg-vol", type=int, default=500_000, help="初篩：最低3個月日均成交量")
    ap.add_argument("--min-market-cap", type=int, default=2_000_000_000,
                     help="長尾初篩：最低市值（美元），預設 20 億")
    ap.add_argument("--max-universe", type=int, default=3000,
                     help="篩後最終下載池嘅隻數上限（按市值/流動性排序），0 = 不限")
    a = ap.parse_args()
    run(limit=a.limit, pause=a.pause, universe_mode=a.universe,
        prefilter=not a.no_prefilter, min_price=a.min_price, min_avg_vol=a.min_avg_vol,
        min_market_cap=a.min_market_cap, max_universe=a.max_universe)
