from __future__ import annotations
"""Yahoo Finance v8 chart API 數據下載（不經 yfinance，更穩定、兼容舊 Python）。"""
import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# 注意：唔好用 yfinance 嗰種「Chrome/xxx」UA——Yahoo 已對該指紋回 429
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json",
}
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"

# 失敗重試次數
RETRIES = 3

# 流動性初篩：每次 batch 幾多隻代號（URL 長度同 Yahoo 單次回應量嘅折衷）
QUOTE_BATCH_SIZE = 50


def fetch_daily(symbol: str, range_: str = "2y", min_bars: int = None) -> Optional[dict]:
    """下載日線。回傳 {"dates":[...], "open":[], "high":[], "low":[], "close":[], "volume":[]} 或 None。"""
    if min_bars is None:
        min_bars = 250 if range_ in ("1y", "2y") else 20
    params = {"range": range_, "interval": "1d", "events": "div,splits"}
    for attempt in range(RETRIES):
        try:
            r = requests.get(CHART_URL.format(symbol=symbol), params=params,
                             headers=HEADERS, timeout=30)
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                log.warning("%s 被限流，等 %ds 重試", symbol, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            res = r.json()["chart"]["result"]
            if not res:
                log.warning("%s 無數據", symbol)
                return None
            res = res[0]
            ts = res.get("timestamp") or []
            quote = res["indicators"]["quote"][0]
            rows = [
                (t, o, h, l, c, v)
                for t, o, h, l, c, v in zip(
                    ts, quote["open"], quote["high"], quote["low"],
                    quote["close"], quote["volume"])
                if c is not None and v is not None
            ]
            if len(rows) < min_bars:  # 唔夠數據（1y/2y 需 ≥250 bar 計 200MA/52週）
                log.info("%s 數據不足（%d bars）", symbol, len(rows))
                return None
            return {
                "dates": [d2s(t) for t, *_ in rows],
                "open": [r[1] for r in rows],
                "high": [r[2] for r in rows],
                "low": [r[3] for r in rows],
                "close": [r[4] for r in rows],
                "volume": [r[5] for r in rows],
            }
        except Exception as e:  # noqa: BLE001
            log.warning("%s 第 %d 次失敗: %s", symbol, attempt + 1, e)
            time.sleep(5 * (attempt + 1))
    return None


def d2s(ts: int) -> str:
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def fetch_quotes(symbols: list[str], pause: float = 0.5) -> dict[str, dict]:
    """批量攞現價 + 3 個月日均成交量 + 市值（v7/finance/quote 一次可以問幾十隻），
    用嚟喺落全歷史 K 線之前，平價咁剔除低價/低量/細市值嘅垃圾股（見 prefilter_liquidity）。
    單一 batch 失敗只影響嗰 batch，唔會累街坊（會唔會 fail-open 由 prefilter_liquidity
    嗰邊嘅 fail_open 參數決定，呢個 function 淨係負責攞數）。"""
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), QUOTE_BATCH_SIZE):
        batch = symbols[i:i + QUOTE_BATCH_SIZE]
        for attempt in range(RETRIES):
            try:
                r = requests.get(QUOTE_URL, params={"symbols": ",".join(batch)},
                                  headers=HEADERS, timeout=30)
                if r.status_code == 429:
                    wait = 20 * (attempt + 1)
                    log.warning("quote batch 被限流，等 %ds 重試", wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                results = r.json().get("quoteResponse", {}).get("result", [])
                for q in results:
                    sym = q.get("symbol")
                    if not sym:
                        continue
                    out[sym] = {
                        "price": q.get("regularMarketPrice"),
                        "avg_vol": q.get("averageDailyVolume3Month") or q.get("averageDailyVolume10Day"),
                        "market_cap": q.get("marketCap"),
                    }
                break
            except Exception as e:  # noqa: BLE001
                log.warning("quote batch 第 %d 次失敗: %s", attempt + 1, e)
                time.sleep(5 * (attempt + 1))
        time.sleep(pause)
    return out


def prefilter_liquidity(symbols: list[str], min_price: float = 10.0,
                         min_avg_vol: int = 500_000, min_market_cap: int = 0,
                         fail_open: bool = True) -> list[str]:
    """用批量報價剔除股價 < min_price、3 個月日均成交量 < min_avg_vol，
    或者（當 min_market_cap > 0 時）市值 < min_market_cap 嘅代號。

    fail_open=True（用於已知嘅核心池，例如 S&P 1500）：冇報價（batch 失敗/代號有問題）
    嘅一律保留，交由後面完整嘅 Stage 2 流動性檢查把關，寧願多落一次歷史數據都唔好誤刪好股。

    fail_open=False（用於冇 GICS 板塊資料嘅 SEC 全市場長尾）：冇報價嘅一律剔除——
    呢批代號本身就冇經過指數篩選，唯一把關嘅就係呢個市值/流動性初篩，如果攞唔到
    報價就冇辦法確認佢係唔係垂青嘅 mid/large cap，寧願唔落佢歷史數據，
    好過落成千上萬隻冇經過驗證嘅細價/垂死股，浪費時間又可能拖冇個 workflow。"""
    quotes = fetch_quotes(symbols)
    kept = []
    dropped = 0
    for s in symbols:
        q = quotes.get(s)
        missing_cap = min_market_cap and q and q.get("market_cap") is None
        if not q or q.get("price") is None or q.get("avg_vol") is None or missing_cap:
            if fail_open:
                kept.append(s)
            else:
                dropped += 1
            continue
        ok = q["price"] >= min_price and q["avg_vol"] >= min_avg_vol
        if min_market_cap:
            ok = ok and (q.get("market_cap") or 0) >= min_market_cap
        if ok:
            kept.append(s)
        else:
            dropped += 1
    log.info("流動性初篩（fail_open=%s%s）：%d → %d 隻（剔除 %d 隻，-%.0f%%）",
              fail_open, f", min_cap=${min_market_cap:,}" if min_market_cap else "",
              len(symbols), len(kept), dropped,
              (dropped / len(symbols) * 100) if symbols else 0)
    return kept


def fetch_many(symbols: list[str], pause: float = 0.35, range_: str = "2y") -> dict[str, dict]:
    """逐隻下載（避免限流）。"""
    out = {}
    for i, sym in enumerate(symbols):
        data = fetch_daily(sym, range_)
        if data:
            out[sym] = data
        if (i + 1) % 50 == 0:
            log.info("已下載 %d/%d", i + 1, len(symbols))
        time.sleep(pause)
    return out


if __name__ == "__main__":
    d = fetch_daily("AAPL", "1y")
    print(d["dates"][-1], d["close"][-1])
