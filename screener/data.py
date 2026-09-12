from __future__ import annotations
"""Yahoo Finance data layer: historical daily data with cache and rate-limit protection."""
import datetime
import json
import logging
import os
import random
import time
from typing import Optional
import requests

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36", "Accept": "application/json,text/plain,*/*"}
CHART_HOSTS = ["https://query1.finance.yahoo.com/v8/finance/chart/{symbol}", "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"]
RETRIES = 2
DEFAULT_MIN_BARS = 252
CACHE_MAX_AGE_SECONDS = 3 * 24 * 3600
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "yahoo")
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


class YahooRateLimitError(RuntimeError):
    """Yahoo returned 429 on all retry attempts for a request."""


def _backoff(attempt: int, base: float, cap: float) -> float:
    return min(cap, base * (2 ** attempt)) + random.uniform(0, 1.5)


def _cache_path(symbol: str, range_: str) -> str:
    safe = symbol.replace("/", "_").replace("\\", "_")
    return os.path.join(CACHE_DIR, "%s_%s.json" % (safe, range_))


def _load_cache(symbol: str, range_: str, min_bars: int) -> Optional[dict]:
    if range_ != "2y":
        return None
    path = _cache_path(symbol, range_)
    try:
        if not os.path.exists(path):
            return None
        payload = json.load(open(path, "r", encoding="utf-8"))
        fetched_at = float(payload.get("fetched_at", 0))
        data = payload.get("data")
        if not data or len(data.get("dates", [])) < min_bars:
            return None
        age = time.time() - fetched_at
        if age > CACHE_MAX_AGE_SECONDS:
            return None
        log.info("%s 使用本地 2y cache（%.1f 小時前）", symbol, age / 3600)
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("%s cache 讀取失敗：%s", symbol, e)
        return None


def _save_cache(symbol: str, range_: str, data: dict) -> None:
    if range_ != "2y":
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _cache_path(symbol, range_)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "range": range_, "data": data}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log.warning("%s cache 寫入失敗：%s", symbol, e)


def _chart(symbol: str, range_: str, timeout: int = 30) -> Optional[dict]:
    params = {"range": range_, "interval": "1d", "events": "div,splits"}
    last_error = None
    rate_limited = False
    for attempt in range(RETRIES):
        host = CHART_HOSTS[attempt % len(CHART_HOSTS)]
        try:
            r = SESSION.get(host.format(symbol=symbol), params=params, timeout=timeout)
            if r.status_code == 429:
                rate_limited = True
                wait = _backoff(attempt, 25, 90)
                log.warning("%s Yahoo 429（%s），等 %.1fs 再試", symbol, host.split("//")[1].split("/")[0], wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            result = (r.json().get("chart", {}).get("result") or [])
            if not result:
                return None
            return result[0]
        except KeyboardInterrupt:
            raise
        except Exception as e:
            last_error = e
            log.warning("%s chart 第 %d 次失敗: %s", symbol, attempt + 1, e)
            if attempt < RETRIES - 1:
                time.sleep(_backoff(attempt, 5, 30))
    if rate_limited:
        raise YahooRateLimitError("Yahoo 429: %s" % symbol)
    if last_error:
        log.info("%s 歷史 request 放棄：%s", symbol, last_error)
    return None


def _rows_from_result(res: dict) -> list[tuple]:
    ts = res.get("timestamp") or []
    quote = (res.get("indicators", {}).get("quote") or [{}])[0]
    opens, highs, lows = quote.get("open", []), quote.get("high", []), quote.get("low", [])
    closes, volumes = quote.get("close", []), quote.get("volume", [])
    return [(t, o, h, l, c, v) for t, o, h, l, c, v in zip(ts, opens, highs, lows, closes, volumes) if c is not None and v is not None]


def fetch_daily(symbol: str, range_: str = "2y", min_bars: int = DEFAULT_MIN_BARS) -> Optional[dict]:
    cached = _load_cache(symbol, range_, min_bars)
    if cached:
        return cached
    res = _chart(symbol, range_)
    if not res:
        log.info("%s 無歷史數據", symbol)
        return None
    rows = _rows_from_result(res)
    if len(rows) < min_bars:
        log.info("%s 數據不足（%d bars，要求 %d）", symbol, len(rows), min_bars)
        return None
    data = {"dates": [d2s(t) for t, *_ in rows], "open": [r[1] for r in rows], "high": [r[2] for r in rows], "low": [r[3] for r in rows], "close": [r[4] for r in rows], "volume": [r[5] for r in rows]}
    _save_cache(symbol, range_, data)
    return data


def d2s(ts: int) -> str:
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def fetch_short(symbol: str, range_: str = "3mo", min_bars: int = 20) -> Optional[dict]:
    res = _chart(symbol, range_)
    if not res:
        return None
    rows = _rows_from_result(res)
    if len(rows) < min_bars:
        return None
    meta = res.get("meta") or {}
    closes, volumes = [r[4] for r in rows], [r[5] for r in rows]
    return {"price": meta.get("regularMarketPrice") or closes[-1], "avg_vol": sum(volumes) / len(volumes), "market_cap": None, "bars": len(rows)}


def screen_quotes(symbols: list[str], pause: float = 0.35, min_price: float = 10.0, min_avg_vol: int = 500_000, min_market_cap: int = 0) -> dict:
    out, passed, failed, unknown = {}, [], [], []
    for i, symbol in enumerate(symbols):
        q = fetch_short(symbol)
        if not q: unknown.append(symbol)
        elif q["price"] < min_price or q["avg_vol"] < min_avg_vol: failed.append(symbol)
        else: out[symbol] = q; passed.append(symbol)
        if i + 1 < len(symbols): time.sleep(pause)
    return {"passed": passed, "failed": failed, "unknown": unknown, "quotes": out, "stats": {"input": len(symbols), "passed": len(passed), "failed": len(failed), "unknown": len(unknown)}}


def prefilter_liquidity(symbols: list[str], min_price: float = 10.0, min_avg_vol: int = 500_000, min_market_cap: int = 0, fail_open: bool = True) -> list[str]:
    screened = screen_quotes(symbols, min_price=min_price, min_avg_vol=min_avg_vol, min_market_cap=min_market_cap)
    return screened["passed"] + screened["unknown"] if fail_open else screened["passed"]


def average_volume(data: dict, bars: int = 63) -> float:
    vols = [v for v in data.get("volume", []) if v is not None]
    return sum(vols[-bars:]) / min(len(vols), bars) if vols else 0.0


def fetch_many(symbols: list[str], pause: float = 0.75, range_: str = "2y", min_bars: int = DEFAULT_MIN_BARS) -> dict[str, dict]:
    """逐隻下載歷史日線；成功結果即時寫入本地 cache；Yahoo 429 時停止 batch，方便稍後 resume。"""
    out = {}
    total = len(symbols)
    for i, sym in enumerate(symbols):
        try:
            data = fetch_daily(sym, range_, min_bars=min_bars)
        except YahooRateLimitError:
            log.error("Yahoo 連續 429，停止 historical batch（%d/%d）；稍後重跑會自動跳過已 cache 數據。", i, total)
            break
        if data:
            out[sym] = data
        if (i + 1) % 50 == 0 or i + 1 == total:
            log.info("歷史數據：%d/%d，成功 %d", i + 1, total, len(out))
        if i + 1 < total:
            time.sleep(pause)
    return out


if __name__ == "__main__":
    d = fetch_daily("AAPL", "1y")
    if d: print(d["dates"][-1], d["close"][-1], average_volume(d))
