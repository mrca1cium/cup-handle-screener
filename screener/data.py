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

# 失敗重試次數
RETRIES = 3


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
