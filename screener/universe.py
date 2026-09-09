from __future__ import annotations
"""股票池清單。

Wikipedia 而家會擋 GitHub Actions（同好多雲端）嘅 IP，直接爬佢個表已經唔穩陣——
所以 S&P 500 改用一個持續同步 Wikipedia 內容嘅 GitHub 靜態 CSV（`datasets/s-and-p-500-companies`）
做主要來源，靠 raw.githubusercontent.com（GitHub Actions 本身可以連）而唔係 en.wikipedia.org。
Wikipedia 直接爬仍然keep 做 S&P 400/600 嘅來源（暫時冇同類穩陣嘅 CSV 可用）同埋 S&P 500 嘅後備。

支援兩個池：
  • sp500  —— 約 500 隻
  • sp1500 —— S&P 500（CSV，穩陣） + S&P 400/600（Wikipedia，如果畀擋咗就淨係得 500 隻，
              唔會整個池跌落去得 60 隻嗰個內建後備）
"""
import csv
import io
import json
import logging
import re

import requests

log = logging.getLogger(__name__)

SP500_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

WIKI_URLS = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

# SEC 官方代號表——同 Wikipedia 冇關係，唔會撞正 Wikipedia 嗰個雲端 IP 封鎖。
# 冇 GICS 板塊、冇市值，但覆蓋成個美股市場（~10,000 隻），做「長尾」候選池嘅來源，
# 靠 data.prefilter_liquidity() 嘅市值/流動性初篩把關（唔會不經篩選就落佢哋歷史數據）。
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_HEADERS = {"User-Agent": "cup-handle-screener research (github.com/mrca1cium/cup-handle-screener)"}
_TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z]{1,2})?$")

# 後備名單：CSV 同 Wikipedia 都失敗先用（高流動性大型股 60 隻）
FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "BRK-B", "TSLA", "LLY",
    "AVGO", "JPM", "V", "UNH", "XOM", "MA", "COST", "HD", "PG", "JNJ",
    "ABBV", "MRK", "CVX", "AMD", "NFLX", "CRM", "ADBE", "PEP", "KO", "WMT",
    "BAC", "TMO", "LIN", "ABT", "DIS", "ORCL", "INTC", "CMCSA", "SCHW", "TXN",
    "PFE", "PM", "CAT", "VZ", "RTX", "AMGN", "IBM", "GE", "BA", "AMAT",
    "MU", "LRCX", "KLAC", "NOW", "PANW", "CRWD", "SNPS", "CDNS", "ANET", "SMCI",
]


def get_sp500() -> list[dict]:
    """回傳 [{"symbol", "name", "sector"}]。順序試：GitHub CSV → Wikipedia → 內建後備。"""
    try:
        rows = _read_sp500_csv()
        if rows:
            log.info("GitHub CSV 取得 %d 隻 S&P 500 成分股", len(rows))
            return rows
    except Exception as e:  # noqa: BLE001
        log.warning("GitHub CSV 抓取失敗（%s），試 Wikipedia", e)
    try:
        rows = _read_wiki_table(WIKI_URLS["sp500"])
        if rows:
            log.info("Wikipedia 取得 %d 隻 S&P 500 成分股", len(rows))
            return rows
    except Exception as e:  # noqa: BLE001
        log.warning("Wikipedia 抓取失敗（%s），改用內建後備名單", e)
    return [{"symbol": s, "name": s, "sector": ""} for s in FALLBACK]


def get_sp1500() -> list[dict]:
    """回傳 S&P 500 + 400 + 600 合併名單（去重，同一代號以先出現嗰個為準）。
    S&P 500 用穩陣嘅 CSV 來源做底，即使 Wikipedia 畀擋（400/600 攞唔到），
    仍然可以攞返約 500 隻，而唔係跌落去得 60 隻嗰個極端後備。"""
    merged: dict[str, dict] = {}
    for r in get_sp500():
        merged.setdefault(r["symbol"], r)
    base_count = len(merged)
    for key in ("sp400", "sp600"):
        try:
            rows = _read_wiki_table(WIKI_URLS[key])
            for r in rows:
                merged.setdefault(r["symbol"], r)
            log.info("Wikipedia %s 取得 %d 隻", key, len(rows))
        except Exception as e:  # noqa: BLE001
            log.warning("Wikipedia %s 抓取失敗（%s），跳過呢個池（S&P 1500 池會縮水）", key, e)
    if len(merged) == base_count:
        log.warning("S&P 400/600 兩個都攞唔到，今次 sp1500 池實際上只有 S&P 500 嘅 %d 隻", base_count)
    log.info("S&P 1500 合併池共 %d 隻（去重後）", len(merged))
    return list(merged.values())


def get_universe(mode: str = "sp1500") -> list[dict]:
    """統一入口：mode = 'sp500' / 'sp1500' / 'broad'（sp1500 + SEC 長尾，向下兼容用；
    正式流程請用 get_broad_market()，可以分開核心池同長尾嚴格把關）。"""
    if mode == "sp500":
        return get_sp500()
    if mode == "sp1500":
        return get_sp1500()
    core, extra = get_broad_market()
    return core + extra


def _read_sec_tickers() -> list[dict]:
    resp = requests.get(SEC_TICKERS_URL, timeout=30, headers=SEC_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    out = []
    for row in data.values():
        sym = str(row.get("ticker", "")).strip().upper()
        if not sym or not _TICKER_RE.match(sym):
            continue  # 濾走明顯唔係普通股代號嘅雜訊（基金份額、奇怪代號等）
        out.append({"symbol": sym, "name": str(row.get("title", sym)), "sector": ""})
    return out


def get_broad_market() -> tuple[list[dict], list[dict]]:
    """回傳 (core, extra)：
      • core  —— S&P 1500（有 GICS 板塊資料，如果 Wikipedia 畀擋就淨係 S&P 500）
      • extra —— SEC 全市場代號表當中扣走 core 已有嗰啲之後嘅長尾（冇板塊資料，
                  冇市值資料，靠 main.py 用 data.prefilter_liquidity(min_market_cap=...,
                  fail_open=False) 嚴格篩走冇報價/細市值/低流動性嗰批，先至會落歷史數據）。
    """
    core = get_sp1500()
    seen = {r["symbol"] for r in core}
    try:
        sec_rows = _read_sec_tickers()
        log.info("SEC company_tickers.json 取得 %d 隻候選代號", len(sec_rows))
    except Exception as e:  # noqa: BLE001
        log.warning("SEC 代號表抓取失敗（%s），今次冇長尾，淨係得 sp1500 核心池", e)
        sec_rows = []
    extra = [r for r in sec_rows if r["symbol"] not in seen]
    return core, extra


def _read_sp500_csv() -> list[dict]:
    resp = requests.get(SP500_CSV_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    out = []
    for row in reader:
        sym = row["Symbol"].strip().replace(".", "-")  # BRK.B -> BRK-B (yahoo 格式)
        out.append({
            "symbol": sym,
            "name": row.get("Security", sym),
            "sector": row.get("GICS Sector", ""),
        })
    return out


def _read_wiki_table(url: str) -> list[dict]:
    import pandas as pd

    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    dfs = pd.read_html(resp.text)
    df = dfs[0]
    out = []
    for _, r in df.iterrows():
        sym = str(r["Symbol"]).strip().replace(".", "-")  # BRK.B -> BRK-B (yahoo 格式)
        out.append({
            "symbol": sym,
            "name": str(r.get("Security", r.get("Company", sym))),
            "sector": str(r.get("GICS Sector", "")),
        })
    return out


# 向下兼容舊名（有其他腳本可能仲引用緊呢個名）
pd_read_wiki = lambda: _read_wiki_table(WIKI_URLS["sp500"])  # noqa: E731


if __name__ == "__main__":
    print(json.dumps(get_universe("sp1500")[:5], ensure_ascii=False, indent=2))
