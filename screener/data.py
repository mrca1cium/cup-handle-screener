from __future__ import annotations
"""Yahoo Finance data layer: quote pre-screen -> historical daily data."""
import logging
import random
import time
from typing import Optional
import requests

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "Accept": "application/json"}
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
RETRIES = 3
QUOTE_BATCH_SIZE = 50
DEFAULT_MIN_BARS = 252

class QuoteStatus:
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"

def _backoff(attempt: int, base: float, cap: float) -> float:
    return min(cap, base * (2 ** attempt)) + random.uniform(0, 1.5)

def fetch_daily(symbol: str, range_: str = "2y", min_bars: int = DEFAULT_MIN_BARS) -> Optional[dict]:
    params = {"range": range_, "interval": "1d", "events": "div,splits"}
    for attempt in range(RETRIES):
        try:
            r = requests.get(CHART_URL.format(symbol=symbol), params=params, headers=HEADERS, timeout=30)
            if r.status_code == 429:
                wait = _backoff(attempt, 20, 120)
                log.warning("%s 被限流，等 %.1fs 重試", symbol, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            res = (r.json().get("chart", {}).get("result") or [])
            if not res:
                log.info("%s 無歷史數據", symbol)
                return None
            res = res[0]
            ts = res.get("timestamp") or []
            quote = (res.get("indicators", {}).get("quote") or [{}])[0]
            rows = [(t, o, h, l, c, v) for t, o, h, l, c, v in zip(ts, quote.get("open", []), quote.get("high", []), quote.get("low", []), quote.get("close", []), quote.get("volume", [])) if c is not None and v is not None]
            if len(rows) < min_bars:
                log.info("%s 數據不足（%d bars，要求 %d）", symbol, len(rows), min_bars)
                return None
            return {"dates": [d2s(t) for t, *_ in rows], "open": [r[1] for r in rows], "high": [r[2] for r in rows], "low": [r[3] for r in rows], "close": [r[4] for r in rows], "volume": [r[5] for r in rows]}
        except Exception as e:  # noqa: BLE001
            log.warning("%s 第 %d 次失敗: %s", symbol, attempt + 1, e)
            if attempt < RETRIES - 1:
                time.sleep(_backoff(attempt, 5, 30))
    return None

def d2s(ts: int) -> str:
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")

def fetch_quotes(symbols: list[str], pause: float = 0.5) -> dict[str, dict]:
    """批量取得 quote；Yahoo 未返回嘅 ticker 會喺 screen_quotes() 標成 UNKNOWN。"""
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), QUOTE_BATCH_SIZE):
        batch = symbols[i:i + QUOTE_BATCH_SIZE]
        success = False
        for attempt in range(RETRIES):
            try:
                r = requests.get(QUOTE_URL, params={"symbols": ",".join(batch)}, headers=HEADERS, timeout=30)
                if r.status_code == 429:
                    wait = _backoff(attempt, 15, 90)
                    log.warning("quote batch 被限流，等 %.1fs 重試", wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                for q in r.json().get("quoteResponse", {}).get("result", []):
                    sym = str(q.get("symbol") or "").upper()
                    if sym:
                        out[sym] = {"price": q.get("regularMarketPrice"), "avg_vol": q.get("averageDailyVolume3Month") or q.get("averageDailyVolume10Day"), "market_cap": q.get("marketCap")}
                success = True
                break
            except Exception as e:  # noqa: BLE001
                log.warning("quote batch 第 %d 次失敗：%s", attempt + 1, e)
                if attempt < RETRIES - 1:
                    time.sleep(_backoff(attempt, 5, 30))
        if not success:
            log.warning("quote batch 完全失敗：%d 隻標記 UNKNOWN", len(batch))
        if i + QUOTE_BATCH_SIZE < len(symbols):
            time.sleep(pause)
    return out

def screen_quotes(symbols: list[str], min_price: float = 10.0, min_avg_vol: int = 500_000, min_market_cap: int = 0) -> dict:
    """將 symbols 分成 PASS / FAIL / UNKNOWN；UNKNOWN 唔等於 FAIL。"""
    quotes = fetch_quotes(symbols)
    passed, failed, unknown = [], [], []
    quote_map: dict[str, dict] = {}
    for symbol in symbols:
        q = quotes.get(symbol)
        if not q or q.get("price") is None or q.get("avg_vol") is None:
            unknown.append(symbol)
            continue
        quote_map[symbol] = q
        if q["price"] < min_price or q["avg_vol"] < min_avg_vol:
            failed.append(symbol)
        elif min_market_cap and (q.get("market_cap") is None or q.get("market_cap") < min_market_cap):
            failed.append(symbol)
        else:
            passed.append(symbol)
    stats = {"input": len(symbols), "passed": len(passed), "failed": len(failed), "unknown": len(unknown)}
    log.info("quote 初篩：%d → PASS %d / FAIL %d / UNKNOWN %d", len(symbols), len(passed), len(failed), len(unknown))
    return {"passed": passed, "failed": failed, "unknown": unknown, "quotes": quote_map, "stats": stats}

def prefilter_liquidity(symbols: list[str], min_price: float = 10.0, min_avg_vol: int = 500_000, min_market_cap: int = 0, fail_open: bool = True) -> list[str]:
    """舊 API wrapper：fail_open=True 回傳 PASS+UNKNOWN；否則只回傳 PASS。"""
    screened = screen_quotes(symbols, min_price, min_avg_vol, min_market_cap)
    return screened["passed"] + screened["unknown"] if fail_open else screened["passed"]

def fetch_many(symbols: list[str], pause: float = 0.35, range_: str = "2y", min_bars: int = DEFAULT_MIN_BARS) -> dict[str, dict]:
    """逐隻下載歷史日線，保持低頻率以減低 Yahoo rate-limit 風險。"""
    out: dict[str, dict] = {}
    total = len(symbols)
    for i, sym in enumerate(symbols):
        data = fetch_daily(sym, range_, min_bars=min_bars)
        if data:
            out[sym] = data
        if (i + 1) % 50 == 0 or i + 1 == total:
            log.info("歷史數據：%d/%d，成功 %d", i + 1, total, len(out))
        if i + 1 < total:
            time.sleep(pause)
    return out

if __name__ == "__main__":
    d = fetch_daily("AAPL", "1y")
    if d:
        print(d["dates"][-1], d["close"][-1])
