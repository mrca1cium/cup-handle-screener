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

import requests

log = logging.getLogger(__name__)

SP500_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

WIKI_URLS = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

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
    """統一入口：mode = 'sp500' 或 'sp1500'。"""
    if mode == "sp500":
        return get_sp500()
    return get_sp1500()


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
