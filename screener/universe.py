from __future__ import annotations
"""股票池清單（從 Wikipedia 取得，失敗時用內建後備名單）。

支援兩個池：
  • sp500  —— 原本嘅 S&P 500（約 500 隻）
  • sp1500 —— S&P 500 + S&P 400 (MidCap) + S&P 600 (SmallCap)，約 1,400–1,500 隻，
              市值下限大約對應 Mid/Large Cap（S&P 600 最細嗰批市值都有幾億美元），
              比起漫無目的咁掃全市場 8,000+ 隻，會篩走大量低流動性垃圾股，
              亦避免因為請求量過大而畀 Yahoo Finance 封鎖 GitHub Actions 嘅雲端 IP。
"""
import json
import logging

import requests

log = logging.getLogger(__name__)

WIKI_URLS = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

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
    """回傳 [{"symbol", "name", "sector"}]（原本嘅 S&P 500，向下兼容）。"""
    try:
        tables = _read_wiki_table(WIKI_URLS["sp500"])
        if tables:
            log.info("Wikipedia 取得 %d 隻 S&P 500 成分股", len(tables))
            return tables
    except Exception as e:  # noqa: BLE001
        log.warning("Wikipedia 抓取失敗（%s），改用內建後備名單", e)
    return [{"symbol": s, "name": s, "sector": ""} for s in FALLBACK]


def get_sp1500() -> list[dict]:
    """回傳 S&P 500 + 400 + 600 合併名單（去重，同一代號以先出現嗰個為準）。"""
    merged: dict[str, dict] = {}
    total_ok = 0
    for key in ("sp500", "sp400", "sp600"):
        try:
            rows = _read_wiki_table(WIKI_URLS[key])
            for r in rows:
                merged.setdefault(r["symbol"], r)
            total_ok += 1
            log.info("Wikipedia %s 取得 %d 隻", key, len(rows))
        except Exception as e:  # noqa: BLE001
            log.warning("Wikipedia %s 抓取失敗（%s），跳過呢個池", key, e)
    if not merged:
        log.warning("三個 Wikipedia 池全部抓取失敗，改用內建後備名單")
        return [{"symbol": s, "name": s, "sector": ""} for s in FALLBACK]
    log.info("S&P 1500 合併池共 %d 隻（去重後）", len(merged))
    return list(merged.values())


def get_universe(mode: str = "sp1500") -> list[dict]:
    """統一入口：mode = 'sp500' 或 'sp1500'。"""
    if mode == "sp500":
        return get_sp500()
    return get_sp1500()


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
