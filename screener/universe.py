from __future__ import annotations
"""股票池來源。

架構：
  1. S&P 500：GitHub static CSV 為主要來源。
  2. S&P 400 / 600：iShares IJH / IJR holdings CSV 為主要來源，Wikipedia 只作後備。
  3. SEC company_tickers.json：提供 S&P 1500 以外嘅長尾候選股。

重要：呢個 module 只負責「候選股票池」，唔做市值/成交量篩選；
data.py 會用 Yahoo quote 做廉價 pre-filter，避免對幾千隻股票逐隻下載 2y 日線。
"""

import csv
import io
import json
import logging
import re
from pathlib import Path

import requests

log = logging.getLogger(__name__)

SP500_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)

# iShares ETF holdings：IJH ≈ S&P MidCap 400；IJR ≈ S&P SmallCap 600。
# 比直接爬 Wikipedia 穩定，而且係由指數 ETF 嘅 holdings 提供 constituent candidates。
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

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_HEADERS = {
    "User-Agent": "cup-handle-screener research (github.com/mrca1cium/cup-handle-screener)"
}

# Yahoo 常見普通股格式；過長/特殊代號先唔入 broad candidate pool。
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
    """轉成 Yahoo 常用 ticker 格式，例如 BRK.B -> BRK-B。"""
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
        out.append({
            "symbol": sym,
            "name": str(row.get("name") or sym),
            "sector": str(row.get("sector") or ""),
        })
    return out


def get_sp500() -> list[dict]:
    """回傳 S&P 500；來源失敗時依次用 Wikipedia，再用內建後備。"""
    try:
        rows = _read_sp500_csv()
        if rows:
            log.info("GitHub CSV 取得 %d 隻 S&P 500 成分股", len(rows))
            return rows
    except Exception as e:  # noqa: BLE001
        log.warning("S&P 500 GitHub CSV 失敗：%s", e)

    try:
        rows = _read_wiki_table(WIKI_URLS["sp500"])
        if rows:
            log.info("Wikipedia 取得 %d 隻 S&P 500 成分股", len(rows))
            return rows
    except Exception as e:  # noqa: BLE001
        log.warning("S&P 500 Wikipedia 失敗：%s", e)

    log.warning("S&P 500 使用內建後備名單 %d 隻", len(FALLBACK))
    return [{"symbol": s, "name": s, "sector": ""} for s in FALLBACK]


def get_sp400() -> list[dict]:
    """回傳 S&P 400 candidates；優先 iShares IJH，Wikipedia 作後備。"""
    return _get_index_candidates("sp400")


def get_sp600() -> list[dict]:
    """回傳 S&P 600 candidates；優先 iShares IJR，Wikipedia 作後備。"""
    return _get_index_candidates("sp600")


def get_sp1500() -> list[dict]:
    """回傳 S&P 500 + 400 + 600 合併池。

    任何一個來源失敗都只會少嗰一個 index；唔會用 60 隻 fallback 代替整個
    S&P 1500，而且會清楚喺 log 顯示各池實際數量。
    """
    groups = {
        "sp500": get_sp500(),
        "sp400": get_sp400(),
        "sp600": get_sp600(),
    }
    merged: dict[str, dict] = {}
    for rows in groups.values():
        for row in rows:
            merged.setdefault(row["symbol"], row)

    log.info(
        "S&P pools：500=%d, 400=%d, 600=%d, 合併去重=%d",
        len(groups["sp500"]), len(groups["sp400"]), len(groups["sp600"]), len(merged),
    )
    return list(merged.values())


def get_sec_candidates() -> list[dict]:
    """SEC 全市場候選代號。

    呢個係 candidate source，並唔代表每一隻都係可交易 US common stock；
    data.py 必須再用 price / volume / market cap pre-filter。
    """
    try:
        rows = _read_sec_tickers()
        log.info("SEC company_tickers.json 取得 %d 隻候選代號", len(rows))
        return rows
    except Exception as e:  # noqa: BLE001
        log.warning("SEC 代號表失敗：%s", e)
        return []


def build_universe() -> tuple[list[dict], list[dict]]:
    """回傳 (core, extra)。

    core = S&P 1500；extra = SEC candidates 扣除 core。
    main.py 再決定邊啲 extra 通過市值/流動性 pre-filter。
    """
    core = get_sp1500()
    seen = {r["symbol"] for r in core}
    extra = [r for r in get_sec_candidates() if r["symbol"] not in seen]
    log.info("Broad candidate pool：core=%d, SEC long-tail=%d, total=%d", len(core), len(extra), len(core) + len(extra))
    return core, extra


def get_broad_market() -> tuple[list[dict], list[dict]]:
    """向下兼容舊 API；正式流程可直接用 build_universe()。"""
    return build_universe()


def get_universe(mode: str = "sp1500") -> list[dict]:
    """統一入口：sp500 / sp1500 / broad。"""
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
        rows = _read_ishares_csv(ISHARES_URLS[index])
        if rows:
            log.info("iShares %s 取得 %d 隻 candidates", index, len(rows))
            return rows
    except Exception as e:  # noqa: BLE001
        log.warning("iShares %s 失敗：%s", index, e)

    try:
        rows = _read_wiki_table(WIKI_URLS[index])
        if rows:
            log.info("Wikipedia %s 取得 %d 隻 candidates", index, len(rows))
            return rows
    except Exception as e:  # noqa: BLE001
        log.warning("Wikipedia %s 失敗：%s", index, e)

    log.warning("%s 今次無法取得 constituents；不自行捏造名單", index)
    return []


def _read_sp500_csv() -> list[dict]:
    resp = requests.get(
        SP500_CSV_URL,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    out = []
    for row in reader:
        sym = _normalise_symbol(row.get("Symbol", ""))
        if not _valid_symbol(sym):
            continue
        out.append({
            "symbol": sym,
            "name": row.get("Security", sym),
            "sector": row.get("GICS Sector", ""),
        })
    return _dedupe(out)


def _read_ishares_csv(url: str) -> list[dict]:
    """解析 iShares holdings CSV；跳過 metadata，保留 Asset Class=Equity。"""
    resp = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
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


def _read_sec_tickers() -> list[dict]:
    resp = requests.get(SEC_TICKERS_URL, timeout=30, headers=SEC_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    out = []
    for row in data.values():
        sym = _normalise_symbol(row.get("ticker", ""))
        if not _valid_symbol(sym):
            continue
        out.append({
            "symbol": sym,
            "name": str(row.get("title", sym)),
            "sector": "",
        })
    return _dedupe(out)


def _read_wiki_table(url: str) -> list[dict]:
    import pandas as pd

    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    dfs = pd.read_html(resp.text)
    if not dfs:
        return []

    # 唔假設第一張 table 永遠係 constituents；搵有 Symbol 欄嗰張。
    target = None
    for df in dfs:
        if "Symbol" in df.columns:
            target = df
            break
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


# 向下兼容舊名；其他腳本如果仲引用 pd_read_wiki，可以繼續運作。
pd_read_wiki = lambda: _read_wiki_table(WIKI_URLS["sp500"])  # noqa: E731


if __name__ == "__main__":
    core, extra = build_universe()
    print(json.dumps({
        "core": len(core),
        "extra": len(extra),
        "total": len(core) + len(extra),
        "sample": (core + extra)[:10],
    }, ensure_ascii=False, indent=2))
