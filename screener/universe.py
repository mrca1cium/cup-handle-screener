from __future__ import annotations
"""股票池來源。

架構：
  1. S&P 500 / 400 / 600：建立核心池。
  2. Nasdaq public screener：補充 S&P1500 以外嘅美股長尾，並提供廉價嘅
     price / current-volume / market-cap metadata。
  3. data.py 只喺通過廉價 metadata filter 後下載 2y 日線，避免對全市場逐隻下載。

SEC company_tickers.json 不再作 long-tail source，因為 GitHub Actions / 本機
環境容易遇到 SEC 403；iShares 亦只作 S&P400/600 fallback。
"""

import csv
import io
import json
import logging
import re
import time

import requests

log = logging.getLogger(__name__)

SP500_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)

ISHARES_URLS = {
    "sp400": (
        "https://www.ishares.com/us/products/239763/ishares-core-sp-midcap-etf/"
        "1467271812596.ajax?fileType=csv&fileName=IJH_holdings&dataType=fund"
    ),
    "sp600": (
        "https://www.ishares.com/us/products/239774/ishares-core-sp-small-cap-etf/"
        "1467271812596.ajax?fileType=csv&fileName=IJR_holdings&dataType=fund"
    ),
}

WIKI_URLS = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

_TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z]{1,2})?$")

FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "BRK-B", "TSLA", "LLY",
    "AVGO", "JPM", "V", "UNH", "XOM", "MA", "COST", "HD", "PG", "JNJ",
    "ABBV", "MRK", "CVX", "AMD", "NFLX", "CRM", "ADBE", "PEP", "KO", "WMT",
    "BAC", "TMO", "LIN", "ABT", "DIS", "ORCL", "INTC", "CMCSA", "SCHW", "TXN",
    "PFE", "PM", "CAT", "VZ", "RTX", "AMGN", "IBM", "GE", "BA", "AMAT",
    "MU", "LRCX", "KLAC", "NOW", "PANW", "CRWD", "SNPS", "CDNS", "ANET", "SMCI",
]


def _normalise_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper().replace(".", "-")


def _valid_symbol(symbol: str) -> bool:
    return bool(_TICKER_RE.match(symbol))


def _dedupe(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        sym = _normalise_symbol(row.get("symbol", ""))
        if not _valid_symbol(sym) or sym in seen:
            continue
        seen.add(sym)
        item = dict(row)
        item["symbol"] = sym
        item["name"] = str(row.get("name") or sym)
        item["sector"] = str(row.get("sector") or "")
        out.append(item)
    return out


def _parse_number(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("$", "")
    if not s or s in {"-", "N/A", "NA"}:
        return None
    multiplier = 1.0
    suffix = s[-1:].upper()
    if suffix in {"K", "M", "B", "T"}:
        multiplier = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[suffix]
        s = s[:-1]
    try:
        return float(s) * multiplier
    except ValueError:
        return None


def get_sp500() -> list[dict]:
    try:
        rows = _read_sp500_csv()
        if rows:
            log.info("GitHub CSV 取得 %d 隻 S&P 500 成分股", len(rows))
            return rows
    except Exception as e:
        log.warning("S&P 500 GitHub CSV 失敗：%s", e)
    try:
        rows = _read_wiki_table(WIKI_URLS["sp500"])
        if rows:
            log.info("Wikipedia 取得 %d 隻 S&P 500 成分股", len(rows))
            return rows
    except Exception as e:
        log.warning("S&P 500 Wikipedia 失敗：%s", e)
    log.warning("S&P 500 使用內建後備名單 %d 隻", len(FALLBACK))
    return [{"symbol": s, "name": s, "sector": ""} for s in FALLBACK]


def get_sp400() -> list[dict]:
    return _get_index_candidates("sp400")


def get_sp600() -> list[dict]:
    return _get_index_candidates("sp600")


def get_sp1500() -> list[dict]:
    groups = {"sp500": get_sp500(), "sp400": get_sp400(), "sp600": get_sp600()}
    merged: dict[str, dict] = {}
    for rows in groups.values():
        for row in rows:
            merged.setdefault(row["symbol"], row)
    log.info(
        "S&P pools：500=%d, 400=%d, 600=%d, 合併去重=%d",
        len(groups["sp500"]), len(groups["sp400"]), len(groups["sp600"]), len(merged),
    )
    return list(merged.values())


def get_nasdaq_candidates() -> list[dict]:
    """取得 Nasdaq public stock screener；回傳 ticker + metadata。

    Nasdaq screener 本身已提供 price / current volume / market cap，因此呢層
    可以先做非常便宜嘅市場篩選；真正嘅平均成交量仍會喺 historical stage
    用 2y bars 計算。
    """
    rows: list[dict] = []
    limit = 5000
    offset = 0
    max_pages = 3
    for _ in range(max_pages):
        params = {
            "tableonly": "true",
            "limit": str(limit),
            "offset": str(offset),
            "download": "true",
        }
        last_error = None
        for attempt in range(3):
            try:
                resp = requests.get(NASDAQ_SCREENER_URL, params=params, headers=NASDAQ_HEADERS, timeout=30)
                resp.raise_for_status()
                data = resp.json().get("data") or {}
                batch = data.get("rows") or []
                if not batch:
                    return _dedupe(rows)
                rows.extend(_nasdaq_row(r) for r in batch)
                log.info("Nasdaq screener：offset=%d 取得 %d 隻", offset, len(batch))
                if len(batch) < limit:
                    return _dedupe(rows)
                offset += limit
                break
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt < 2:
                    time.sleep(2 + attempt * 3)
        else:
            raise RuntimeError(f"Nasdaq screener failed at offset {offset}: {last_error}")
    return _dedupe(rows)


def _nasdaq_row(row: dict) -> dict:
    sym = _normalise_symbol(row.get("symbol", ""))
    return {
        "symbol": sym,
        "name": str(row.get("name") or sym),
        "sector": str(row.get("sector") or ""),
        "price": _parse_number(row.get("lastsale")),
        "current_volume": _parse_number(row.get("volume")),
        "market_cap": _parse_number(row.get("marketCap")),
        "exchange": str(row.get("exchange") or ""),
        "industry": str(row.get("industry") or ""),
    }


def get_sec_candidates() -> list[dict]:
    """Backward-compatible stub; SEC is no longer used for broad universe."""
    log.info("SEC long-tail source 已停用；改用 Nasdaq screener")
    return []


def build_universe() -> tuple[list[dict], list[dict]]:
    core = get_sp1500()
    seen = {r["symbol"] for r in core}
    market = get_nasdaq_candidates()
    extra = [r for r in market if r["symbol"] not in seen]
    log.info(
        "Broad candidate pool：core=%d, Nasdaq long-tail=%d, total=%d",
        len(core), len(extra), len(core) + len(extra),
    )
    return core, extra


def get_broad_market() -> tuple[list[dict], list[dict]]:
    return build_universe()


def get_universe(mode: str = "sp1500") -> list[dict]:
    if mode == "sp500":
        return get_sp500()
    if mode == "sp1500":
        return get_sp1500()
    if mode == "broad":
        core, extra = build_universe()
        return core + extra
    raise ValueError(f"unknown universe mode: {mode}")


def _get_index_candidates(index: str) -> list[dict]:
    try:
        rows = _read_wiki_table(WIKI_URLS[index])
        if rows:
            log.info("Wikipedia %s 取得 %d 隻 candidates", index, len(rows))
            return rows
    except Exception as e:
        log.warning("Wikipedia %s 失敗：%s", index, e)
    try:
        rows = _read_ishares_csv(ISHARES_URLS[index])
        if rows:
            log.info("iShares %s 取得 %d 隻 candidates", index, len(rows))
            return rows
    except Exception as e:
        log.warning("iShares %s 失敗：%s", index, e)
    log.warning("%s 今次無法取得 constituents；不自行捏造名單", index)
    return []


def _read_sp500_csv() -> list[dict]:
    resp = requests.get(SP500_CSV_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    out = []
    for row in reader:
        sym = _normalise_symbol(row.get("Symbol", ""))
        if not _valid_symbol(sym):
            continue
        out.append({"symbol": sym, "name": row.get("Security", sym), "sector": row.get("GICS Sector", "")})
    return _dedupe(out)


def _read_ishares_csv(url: str) -> list[dict]:
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    lines = resp.text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        first = line.split(",", 1)[0].strip().strip('"')
        if first == "Ticker":
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("iShares response does not contain holdings CSV header")
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    out = []
    for row in reader:
        sym = _normalise_symbol(row.get("Ticker", ""))
        asset_class = str(row.get("Asset Class") or "").strip()
        if asset_class != "Equity" or not _valid_symbol(sym):
            continue
        out.append({"symbol": sym, "name": sym, "sector": ""})
    return _dedupe(out)


def _read_wiki_table(url: str) -> list[dict]:
    import pandas as pd
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    dfs = pd.read_html(resp.text)
    if not dfs:
        return []
    target = next((df for df in dfs if "Symbol" in df.columns), None)
    if target is None:
        return []
    out = []
    for _, row in target.iterrows():
        sym = _normalise_symbol(row.get("Symbol", ""))
        if not _valid_symbol(sym):
            continue
        out.append({
            "symbol": sym,
            "name": str(row.get("Security", row.get("Company", sym))),
            "sector": str(row.get("GICS Sector", "")),
        })
    return _dedupe(out)


pd_read_wiki = lambda: _read_wiki_table(WIKI_URLS["sp500"])  # noqa: E731


if __name__ == "__main__":
    core, extra = build_universe()
    print(json.dumps({
        "core": len(core), "extra": len(extra), "total": len(core) + len(extra),
        "sample": (core + extra)[:10],
    }, ensure_ascii=False, indent=2))
