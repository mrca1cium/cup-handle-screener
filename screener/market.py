from __future__ import annotations
"""大市監控（手冊 Step 1）：指數回調幅度 + 板塊相對強弱。"""
import datetime

from .data import fetch_daily

INDICES = {
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq",
    "^RUT": "Russell 2000",
}

SECTORS = {
    "XLK": "科技", "XLV": "醫療", "XLF": "金融", "XLY": "可選消費", "XLP": "必需消費",
    "XLE": "能源", "XLI": "工業", "XLU": "公用事業", "XLB": "材料", "XLRE": "房地產",
    "XLC": "通訊", "GLD": "黃金", "ITA": "太空與國防", "XME": "金屬與礦業",
    "XHB": "住宅建築",
}

# GICS Sector（Wikipedia 成分股表嘅字眼）→ 對應嘅板塊 ETF。
# GLD / ITA / XME / XHB 呢啲主題性 ETF 冇對應 GICS Sector，唔喺呢個表——
# 呢啲主題嘅股會跌落 sector_tag() 嘅「neutral」（中性/彈性）分支，唔會被打為弱勢。
GICS_TO_ETF = {
    "Information Technology": "XLK",
    "Health Care": "XLV",
    "Financials": "XLF",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Utilities": "XLU",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}

# 板塊相對強弱（rel_vs_spy，百分點）分級門檻——加分制用，唔係硬篩選。
STRONG_TH = 2.0
WEAK_TH = -2.0


def sector_tag(gics_sector: str, rel_map: dict[str, float]) -> dict:
    """將個股嘅 GICS Sector 對應去板塊 ETF 相對強弱，回傳 tier: strong/neutral/weak。

    冇對應 ETF（主題股、GICS 分類缺失）或者當日冇該 ETF 數據，一律當 neutral——
    唔會因為板塊分類唔齊全而被錯誤打為弱勢，保留「強股可以來自中性板塊」嘅彈性。
    """
    etf = GICS_TO_ETF.get(gics_sector)
    rel = rel_map.get(etf) if etf else None
    if rel is None:
        return {"etf": etf, "rel_vs_spy": None, "tier": "neutral"}
    if rel >= STRONG_TH:
        tier = "strong"
    elif rel <= WEAK_TH:
        tier = "weak"
    else:
        tier = "neutral"
    return {"etf": etf, "rel_vs_spy": round(rel, 1), "tier": tier}


def drawdown(d: dict) -> float:
    """現價距離近期（半年）高點的回調百分比（正數 = 回調中）。"""
    hi = max(d["high"][-126:])
    return round((1 - d["close"][-1] / hi) * 100, 1)


def market_status() -> dict:
    indices = {}
    for sym, name in INDICES.items():
        d = fetch_daily(sym, "1y")
        if d:
            dd = drawdown(d)
            indices[sym] = {
                "name": name, "drawdown_pct": dd,
                "correction": 5 <= dd <= 10, "pullback": 0 < dd < 5,
            }
    sectors = []
    spy = fetch_daily("SPY", "6mo")
    spy_ret = (spy["close"][-1] / spy["close"][0] - 1) * 100 if spy else 0
    for sym, name in SECTORS.items():
        d = fetch_daily(sym, "6mo")
        if not d:
            continue
        c = d["close"]
        ret_1m = (c[-1] / c[-21] - 1) * 100 if len(c) > 21 else 0
        rel = ret_1m - ((spy["close"][-1] / spy["close"][-22] - 1) * 100
                        if spy and len(spy["close"]) > 22 else 0)
        sectors.append({"symbol": sym, "name": name,
                        "ret_1m": round(ret_1m, 1), "rel_vs_spy": round(rel, 1)})
    sectors.sort(key=lambda s: s["rel_vs_spy"], reverse=True)
    return {
        "date": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
        "spy_ret_6mo": round(spy_ret, 1),
        "indices": indices,
        "sectors": sectors[:8],       # 網頁頂部條狀圖只顯示最強/最弱幾個
        "sectors_all": sectors,       # 全部板塊——俾 main.py 逐隻股票對應加分用
    }
