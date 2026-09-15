from __future__ import annotations
"""Market data layer: StashGamma for historical daily data; Yahoo for short quote prefilter."""
import datetime
import json
import logging
import os
import random
import time
from typing import Optional

import requests
import certifi

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
}
CHART_HOSTS = [
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
    "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
]
STASHGAMMA_URL = "https://www.stashgamma.com/api/dataapi/v1/eod/{symbol}"
RETRIES = 2
DEFAULT_MIN_BARS = 252
# StashGamma documents a 300/hour limit. Keep a safety margin so one run
# does not deliberately consume the whole hourly allowance.
DEFAULT_MAX_REQUESTS = 280
CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".cache",
    "market",
)
UNAVAILABLE_PATH = os.path.join(CACHE_DIR, "stashgamma_unavailable.json")
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


class YahooRateLimitError(RuntimeError):
    """Yahoo returned 429 on all retry attempts for a request."""


class StashGammaError(RuntimeError):
    """StashGamma historical-data request failed."""


class StashGammaRateLimitError(StashGammaError):
    """StashGamma returned 429; the caller should stop the current batch."""


class StashGammaUnavailableError(StashGammaError):
    """StashGamma has no usable data for this symbol (for example HTTP 404)."""


def _backoff(attempt: int, base: float, cap: float) -> float:
    return min(cap, base * (2 ** attempt)) + random.uniform(0, 1.5)


def _cache_path(symbol: str, range_: str = "2y") -> str:
    safe = symbol.replace("/", "_").replace("\\", "_")
    return os.path.join(CACHE_DIR, "%s_%s.json" % (safe, range_))


def _load_cache(symbol: str, range_: str, min_bars: int) -> Optional[dict]:
    """Load a historical cache without expiring it automatically.

    Weekly refresh is handled explicitly by fetch_many(refresh=True). This is
    important because the screener should not re-download 2 years of history
    simply because a cache is older than a few days.
    """
    if range_ != "2y":
        return None
    path = _cache_path(symbol, range_)
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        data = payload.get("data")
        if not data or len(data.get("dates", [])) < min_bars:
            return None
        log.info(
            "%s 使用本地 2y cache（來源=%s）",
            symbol,
            payload.get("source", "unknown"),
        )
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("%s cache 讀取失敗：%s", symbol, e)
        return None


def _save_cache(symbol: str, range_: str, data: dict, source: str) -> None:
    if range_ != "2y":
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _cache_path(symbol, range_)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "fetched_at": time.time(),
                    "range": range_,
                    "source": source,
                    "data": data,
                },
                f,
                ensure_ascii=False,
            )
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log.warning("%s cache 寫入失敗：%s", symbol, e)


def _load_unavailable() -> set:
    try:
        if not os.path.exists(UNAVAILABLE_PATH):
            return set()
        with open(UNAVAILABLE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, list):
            return set(str(x).upper() for x in payload)
        if isinstance(payload, dict):
            return set(str(x).upper() for x in payload.get("symbols", []))
    except Exception as e:  # noqa: BLE001
        log.warning("StashGamma unavailable list 讀取失敗：%s", e)
    return set()


def _mark_unavailable(symbol: str) -> None:
    """Remember permanent/unavailable symbols so they do not waste API calls."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        symbols = _load_unavailable()
        symbols.add(symbol.upper())
        tmp = UNAVAILABLE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(symbols), f, ensure_ascii=False, indent=2)
        os.replace(tmp, UNAVAILABLE_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("%s unavailable list 寫入失敗：%s", symbol, e)


def _chart(symbol: str, range_: str, timeout: int = 30) -> Optional[dict]:
    """Yahoo chart endpoint, retained only for short quote prefiltering."""
    params = {"range": range_, "interval": "1d", "events": "div,splits"}
    last_error = None
    rate_limited = False
    for attempt in range(RETRIES):
        host = CHART_HOSTS[attempt % len(CHART_HOSTS)]
        try:
            r = SESSION.get(host.format(symbol=symbol), params=params, timeout=timeout)
            if r.status_code == 429:
                rate_limited = True
                if attempt < RETRIES - 1:
                    wait = _backoff(attempt, 25, 90)
                    log.warning(
                        "%s Yahoo 429（%s），等 %.1fs 再試",
                        symbol,
                        host.split("//")[1].split("/")[0],
                        wait,
                    )
                    time.sleep(wait)
                else:
                    log.error(
                        "%s Yahoo 429（%s），已達 retry 上限",
                        symbol,
                        host.split("//")[1].split("/")[0],
                    )
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
        log.info("%s Yahoo request 放棄：%s", symbol, last_error)
    return None


def _stashgamma(symbol: str, range_: str, timeout: int = 30) -> Optional[dict]:
    """Fetch daily OHLCV history from StashGamma."""
    api_key = os.environ.get("STASHGAMMA_API_KEY")
    if not api_key:
        raise StashGammaError(
            "找不到 STASHGAMMA_API_KEY；請先在環境變量設定 StashGamma API key"
        )

    if range_ not in ("2y", "1y", "6mo", "3mo"):
        raise StashGammaError("不支援的 StashGamma range: %s" % range_)

    days = {
        "2y": 730,
        "1y": 365,
        "6mo": 183,
        "3mo": 92,
    }[range_]
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)

    url = STASHGAMMA_URL.format(symbol=symbol.upper())
    headers = {"X-Api-Key": api_key}

    try:
        r = SESSION.get(
            url,
            params={
                "from": start.strftime("%Y-%m-%d"),
                "to": end.strftime("%Y-%m-%d"),
            },
            headers=headers,
            timeout=timeout,
            verify=certifi.where(),
        )
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        raise StashGammaError("%s request 失敗：%s" % (symbol, e))

    if r.status_code == 429:
        raise StashGammaRateLimitError("%s StashGamma 429 rate limit" % symbol)
    if r.status_code == 404:
        raise StashGammaUnavailableError("%s StashGamma HTTP 404" % symbol)
    if r.status_code in (401, 403):
        raise StashGammaError(
            "%s StashGamma API key 無效或未獲授權（HTTP %d）"
            % (symbol, r.status_code)
        )
    if r.status_code != 200:
        raise StashGammaError(
            "%s StashGamma HTTP %d: %s" % (symbol, r.status_code, r.text[:300])
        )

    try:
        payload = r.json()
    except ValueError as e:
        raise StashGammaError("%s StashGamma 回傳唔係有效 JSON：%s" % (symbol, e))

    bars = payload.get("bars", []) if isinstance(payload, dict) else []
    if not isinstance(bars, list) or not bars:
        raise StashGammaUnavailableError("%s StashGamma 無 bars" % symbol)

    rows = []
    for bar in bars:
        try:
            if not all(
                bar.get(k) is not None
                for k in ("date", "open", "high", "low", "close", "volume")
            ):
                continue
            rows.append(
                (
                    str(bar["date"]),
                    float(bar["open"]),
                    float(bar["high"]),
                    float(bar["low"]),
                    float(bar["close"]),
                    float(bar["volume"]),
                )
            )
        except (TypeError, ValueError):
            continue

    if not rows:
        raise StashGammaUnavailableError("%s StashGamma 無有效 bars" % symbol)

    rows.sort(key=lambda x: x[0])
    return {
        "dates": [r[0] for r in rows],
        "open": [r[1] for r in rows],
        "high": [r[2] for r in rows],
        "low": [r[3] for r in rows],
        "close": [r[4] for r in rows],
        "volume": [r[5] for r in rows],
    }


def _merge_daily(old: dict, new: dict) -> dict:
    """Merge refreshed bars into the existing 2y cache by date."""
    rows = {}
    for i, date in enumerate(old.get("dates", [])):
        try:
            rows[str(date)] = (
                float(old["open"][i]),
                float(old["high"][i]),
                float(old["low"][i]),
                float(old["close"][i]),
                float(old["volume"][i]),
            )
        except (IndexError, TypeError, ValueError):
            continue

    for i, date in enumerate(new.get("dates", [])):
        try:
            rows[str(date)] = (
                float(new["open"][i]),
                float(new["high"][i]),
                float(new["low"][i]),
                float(new["close"][i]),
                float(new["volume"][i]),
            )
        except (IndexError, TypeError, ValueError):
            continue

    dates = sorted(rows.keys())[-550:]
    return {
        "dates": dates,
        "open": [rows[d][0] for d in dates],
        "high": [rows[d][1] for d in dates],
        "low": [rows[d][2] for d in dates],
        "close": [rows[d][3] for d in dates],
        "volume": [rows[d][4] for d in dates],
    }


def _rows_from_result(res: dict) -> list[tuple]:
    ts = res.get("timestamp") or []
    quote = (res.get("indicators", {}).get("quote") or [{}])[0]
    opens, highs, lows = quote.get("open", []), quote.get("high", []), quote.get("low", [])
    closes, volumes = quote.get("close", []), quote.get("volume", [])
    return [
        (t, o, h, l, c, v)
        for t, o, h, l, c, v in zip(ts, opens, highs, lows, closes, volumes)
        if c is not None and v is not None
    ]


def fetch_daily(
    symbol: str,
    range_: str = "2y",
    min_bars: int = DEFAULT_MIN_BARS,
    refresh: bool = False,
) -> Optional[dict]:
    """Fetch historical daily OHLCV with persistent cache.

    Initial load: download 2 years once.
    Weekly refresh: download only the recent 3 months and merge it into the
    existing 2-year cache, avoiding another full 2-year download.
    """
    symbol = symbol.upper()
    cached = _load_cache(symbol, range_, min_bars)
    if cached and not refresh:
        return cached

    if symbol in _load_unavailable():
        log.info("%s 已列入 StashGamma unavailable list，跳過 API", symbol)
        return None

    if refresh and cached and range_ == "2y":
        data = _stashgamma(symbol, "3mo")
        merged = _merge_daily(cached, data) if data else cached
        if len(merged["dates"]) >= min_bars:
            _save_cache(symbol, "2y", merged, "stashgamma")
            return merged
        return None

    data = _stashgamma(symbol, range_)
    if data and len(data["dates"]) >= min_bars:
        _save_cache(symbol, range_, data, "stashgamma")
        return data

    if data:
        log.info(
            "%s StashGamma 數據不足（%d bars，要求 %d）",
            symbol,
            len(data["dates"]),
            min_bars,
        )
    return None


def d2s(ts: int) -> str:
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def fetch_short(symbol: str, range_: str = "3mo", min_bars: int = 20) -> Optional[dict]:
    """Short quote helper retained for the cheap prefilter; this still uses Yahoo."""
    res = _chart(symbol, range_)
    if not res:
        return None
    rows = _rows_from_result(res)
    if len(rows) < min_bars:
        return None
    meta = res.get("meta") or {}
    closes, volumes = [r[4] for r in rows], [r[5] for r in rows]
    return {
        "price": meta.get("regularMarketPrice") or closes[-1],
        "avg_vol": sum(volumes) / len(volumes),
        "market_cap": None,
        "bars": len(rows),
    }


def screen_quotes(
    symbols: list[str],
    pause: float = 0.35,
    min_price: float = 10.0,
    min_avg_vol: int = 500_000,
    min_market_cap: int = 0,
) -> dict:
    out, passed, failed, unknown = {}, [], [], []
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
    return {
        "passed": passed,
        "failed": failed,
        "unknown": unknown,
        "quotes": out,
        "stats": {
            "input": len(symbols),
            "passed": len(passed),
            "failed": len(failed),
            "unknown": len(unknown),
        },
    }


def prefilter_liquidity(
    symbols: list[str],
    min_price: float = 10.0,
    min_avg_vol: int = 500_000,
    min_market_cap: int = 0,
    fail_open: bool = True,
) -> list[str]:
    screened = screen_quotes(
        symbols,
        min_price=min_price,
        min_avg_vol=min_avg_vol,
        min_market_cap=min_market_cap,
    )
    return screened["passed"] + screened["unknown"] if fail_open else screened["passed"]


def average_volume(data: dict, bars: int = 63) -> float:
    vols = [v for v in data.get("volume", []) if v is not None]
    return sum(vols[-bars:]) / min(len(vols), bars) if vols else 0.0


def fetch_many(
    symbols: list[str],
    pause: float = 0.75,
    range_: str = "2y",
    min_bars: int = DEFAULT_MIN_BARS,
    refresh: bool = False,
    max_requests: int = DEFAULT_MAX_REQUESTS,
) -> dict[str, dict]:
    """Fetch many symbols with cache-first and rate-limit-aware behavior.

    - Existing 2y cache is reused unless refresh=True.
    - refresh=True fetches only 3 months and merges it into the 2y cache.
    - HTTP 404 is permanently recorded in the unavailable list.
    - HTTP 429 stops the current batch immediately instead of hammering the API.
    - max_requests defaults to 280, leaving a safety margin below 300/hour.
    """
    out = {}
    unavailable = _load_unavailable()
    total = len(symbols)
    requests_made = 0
    cache_hits = 0

    for i, sym in enumerate(symbols):
        sym = sym.upper()
        if sym in unavailable:
            log.info("%s 已知 unavailable，跳過（%d/%d）", sym, i + 1, total)
            continue

        # A cache hit does not consume an API request.
        cached = _load_cache(sym, range_, min_bars)
        if cached and not refresh:
            out[sym] = cached
            cache_hits += 1
        else:
            if requests_made >= max_requests:
                log.warning(
                    "已達本批安全 API request 上限 %d；停止本批。下一批再繼續。",
                    max_requests,
                )
                break
            try:
                data = fetch_daily(
                    sym,
                    range_,
                    min_bars=min_bars,
                    refresh=refresh,
                )
                requests_made += 1
            except StashGammaUnavailableError as e:
                requests_made += 1
                _mark_unavailable(sym)
                unavailable.add(sym)
                log.warning("%s unavailable，加入黑名單：%s", sym, e)
                data = None
            except StashGammaRateLimitError as e:
                log.error(
                    "%s 收到 429：立即停止本批，避免繼續浪費 API quota：%s",
                    sym,
                    e,
                )
                break
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                requests_made += 1
                log.error(
                    "%s historical 失敗：%s；跳過並繼續（%d/%d）",
                    sym,
                    e,
                    i + 1,
                    total,
                )
                data = None

            if data:
                out[sym] = data

        if (i + 1) % 50 == 0 or i + 1 == total:
            log.info(
                "歷史數據：%d/%d，成功 %d，cache %d，API requests %d/%d",
                i + 1,
                total,
                len(out),
                cache_hits,
                requests_made,
                max_requests,
            )
        if i + 1 < total and requests_made < max_requests:
            time.sleep(pause)

    log.info(
        "歷史 batch 完成：input=%d success=%d cache_hits=%d API_requests=%d",
        total,
        len(out),
        cache_hits,
        requests_made,
    )
    return out


if __name__ == "__main__":
    d = fetch_daily("AAPL", "2y")
    if d:
        print(d["dates"][-1], d["close"][-1], average_volume(d))
