from __future__ import annotations
"""S&P 500 成分股清單（從 Wikipedia 取得，失敗時用內建後備名單）。"""
import json
import logging

import requests

log = logging.getLogger(__name__)

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# 後備名單：Wikipedia 失敗時用（高流動性大型股 60 隻）
FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "BRK-B", "TSLA", "LLY",
    "AVGO", "JPM", "V", "UNH", "XOM", "MA", "COST", "HD", "PG", "JNJ",
    "ABBV", "MRK", "CVX", "AMD", "NFLX", "CRM", "ADBE", "PEP", "KO", "WMT",
    "BAC", "TMO", "LIN", "ABT", "DIS", "ORCL", "INTC", "CMCSA", "SCHW", "TXN",
    "PFE", "PM", "CAT", "VZ", "RTX", "AMGN", "IBM", "GE", "BA", "AMAT",
    "MU", "LRCX", "KLAC", "NOW", "PANW", "CRWD", "SNPS", "CDNS", "ANET", "SMCI",
]


def get_sp500() -> list[dict]:
    """回傳 [{"symbol", "name", "sector"}]。"""
    try:
        tables = pd_read_wiki()
        if tables:
            log.info("Wikipedia 取得 %d 隻 S&P 500 成分股", len(tables))
            return tables
    except Exception as e:  # noqa: BLE001
        log.warning("Wikipedia 抓取失敗（%s），改用內建後備名單", e)
    return [{"symbol": s, "name": s, "sector": ""} for s in FALLBACK]


def pd_read_wiki() -> list[dict]:
    import pandas as pd

    resp = requests.get(WIKI_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    dfs = pd.read_html(resp.text)
    df = dfs[0]
    out = []
    for _, r in df.iterrows():
        sym = str(r["Symbol"]).strip().replace(".", "-")  # BRK.B -> BRK-B (yahoo 格式)
        out.append({
            "symbol": sym,
            "name": str(r.get("Security", sym)),
            "sector": str(r.get("GICS Sector", "")),
        })
    return out


if __name__ == "__main__":
    print(json.dumps(get_sp500()[:5], ensure_ascii=False, indent=2))
