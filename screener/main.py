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
from screener.market import market_status  # noqa: E402
from screener.universe import get_sp500  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("screener")

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "data")


def run(limit: int = 0, pause: float = 0.35):
    os.makedirs(OUT_DIR, exist_ok=True)
    universe = get_sp500()
    if limit:
        universe = universe[:limit]
    symbols = [u["symbol"] for u in universe]
    log.info("開始下載 %d 隻股票", len(symbols))

    all_data = data_mod.fetch_many(symbols, pause=pause)
    log.info("成功下載 %d 隻", len(all_data))

    spy = all_data.get("SPY") or data_mod.fetch_daily("SPY", "2y")
    spy_close = spy["close"] if spy else []
    ratings = stage2.rs_ratings(all_data, spy_close)

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
        entry = {
            "symbol": sym, "name": u["name"], "sector": u["sector"],
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

    results.sort(key=lambda e: -e["stage2"]["values"]["rs_rating"])
    watchlist.sort(key=lambda e: -e["stage2"]["values"]["rs_rating"])

    try:
        market = market_status()
    except Exception as e:  # noqa: BLE001
        log.warning("大市監控失敗: %s", e)
        market = {}

    out = {"generated_at": market.get("date"), "market": market,
           "total_scanned": len(all_data), "results": results, "watchlist": watchlist}
    with open(os.path.join(OUT_DIR, "results.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    with open(os.path.join(OUT_DIR, "charts.json"), "w", encoding="utf-8") as f:
        json.dump(charts, f, ensure_ascii=False)
    log.info("完成：match %d 隻，watch %d 隻", len(results), len(watchlist))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只掃前 N 隻（測試用）")
    ap.add_argument("--pause", type=float, default=0.35, help="每隻下載間隔秒數")
    a = ap.parse_args()
    run(limit=a.limit, pause=a.pause)
