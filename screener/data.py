from __future__ import annotations
"""Yahoo Finance data layer: short chart checks + historical daily data.

重要：不再使用 Yahoo v7/finance/quote + crumb/cookie。
Yahoo 目前對該 endpoint 容易回 401/429；v8/finance/chart 則可直接提供
OHLCV，而且不需要 crumb。Broad universe 嘅 price / market-cap / current-volume
metadata 由 universe.py 嘅 Nasdaq screener 提供。
"""

import logging
import random
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
}
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
RETRIES = 3
DEFAULT_MIN_BARS = 252


class QuoteStatus:
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


def _backoff(attempt: int, base: float, cap: float) -> float:
    return min(cap, base * (2 ** attempt)) + random.uniform(0, 1.5)


def _chart(symbol: str, range_: str, timeout: int = 30) -> Optional[dict]:
    params = {"range": range_, "interval": "1d", "events": "div,splits"}
    for attempt in range(RETRIES):
        try:
            r = requests.get(CHART_URL.format(symbol=symbol), params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 429:
                wait = _backoff(attempt, 10, 90)
                log.warning("%s 被限流，等 %.1fs 重試", symbol, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            result = (r.json().get("chart", {}).get("result") or [])
            if not result:
                return None
            return result[0]
        except Exception as e:  # noqa: BLE001
            log.warning("%s chart 第 %d 次失敗: %s", symbol, attempt + 1, e)
            if attempt < RETRIES - 1:
                time.sleep(_backoff(attempt, 4, 30))
    return None


def _rows_from_result(res: dict) -> list[tuple]:
    ts = res.get("timestamp") or []
    quote = (res.get("indicators", {}).get("quote") or [{}])[0]
    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])
    return [
        (t, o, h, l, c, v)
        for t, o, h, l, c, v in zip(ts, opens, highs, lows, closes, volumes)
        if c is not None and v is not None
    ]


def fetch_daily(symbol: str, range_: str = "2y", min_bars: int = DEFAULT_MIN_BARS) -> Optional[dict]:
    res = _chart(symbol, range_)
    if not res:
        log.info("%s 無歷史數據", symbol)
        return None
    rows = _rows_from_result(res)
    if len(rows) < min_bars:
        log.info("%s 數據不足（%d bars，要求 %d）", symbol, len(rows), min_bars)
        return None
    return {
        "dates": [d2s(t) for t, *_ in rows],
        "open": [r[1] for r in rows],
        "high": [r[2] for r in rows],
        "low": [r[3] for r in rows],
        "close": [r[4] for r in rows],
        "volume": [r[5] for r in rows],
    }


def d2s(ts: int) -> str:
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def fetch_short(symbol: str, range_: str = "3mo", min_bars: int = 20) -> Optional[dict]:
    """Compatibility helper：用 chart endpoint 做短期 quote / average-volume 檢查。"""
    res = _chart(symbol, range_)
    if not res:
        return None
    rows = _rows_from_result(res)
    if len(rows) < min_bars:
        return None
    meta = res.get("meta") or {}
    closes = [r[4] for r in rows]
    volumes = [r[5] for r in rows]
    return {
        "price": meta.get("regularMarketPrice") or closes[-1],
        "avg_vol": sum(volumes) / len(volumes),
        "market_cap": None,
        "bars": len(rows),
    }


def screen_quotes(symbols: list[str], pause: float = 0.35, min_price: float = 10.0,
                  min_avg_vol: int = 500_000, min_market_cap: int = 0) -> dict:
    """Compatibility API：用 v8 chart 做短期檢查，不再碰 crumb endpoint。"""
    out: dict[str, dict] = {}
    passed, failed, unknown = [], [], []
    for i, symbol in enumerate(symbols):
        q = fetch_short(symbol)
        if not q:
            unknown.append(symbol)
        elif q["price"] < min_price or q["avg_vol"] < min_avg_vol:
            failed.append(symbol)
        else:
            out[symbol] = q
            passed.append(symbol)
        if i + 1 < len(symbols):
            time.sleep(pause)
    stats = {"input": len(symbols), "passed": len(passed), "failed": len(failed), "unknown": len(unknown)}
    return {"passed": passed, "failed": failed, "unknown": unknown, "quotes": out, "stats": stats}


def prefilter_liquidity(symbols: list[str], min_price: float = 10.0, min_avg_vol: int = 500_000,
                        min_market_cap: int = 0, fail_open: bool = True) -> list[str]:
    screened = screen_quotes(symbols, min_price=min_price, min_avg_vol=min_avg_vol,
                             min_market_cap=min_market_cap)
    return screened["passed"] + screened["unknown"] if fail_open else screened["passed"]


def average_volume(data: dict, bars: int = 63) -> float:
    vols = [v for v in data.get("volume", []) if v is not None]
    if not vols:
        return 0.0
    return sum(vols[-bars:]) / min(len(vols), bars)


def fetch_many(symbols: list[str], pause: float = 0.35, range_: str = "2y",
               min_bars: int = DEFAULT_MIN_BARS) -> dict[str, dict]:
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
        print(d["dates"][-1], d["close"][-1], average_volume(d))
