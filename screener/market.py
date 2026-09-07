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
        "sectors": sectors[:8],
    }
