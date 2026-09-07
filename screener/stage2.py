from __future__ import annotations
"""Stage 2 Uptrend 八大條件（手冊 Step 2）。"""


def sma(vals: list[float], n: int) -> list:
    """簡單移動平均，前 n-1 個為 None。"""
    out = []
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def pct(a: float, b: float) -> float:
    return (a - b) / b * 100


def check_stage2(d: dict, rs_rating: float) -> dict:
    """回傳八項條件逐項結果（True/False）+ 關鍵數值。d = data.fetch_daily 輸出。"""
    close, high, low, vol = d["close"], d["high"], d["low"], d["volume"]
    n = len(close)
    if n < 210:
        return {"pass": False, "checks": {}, "reason": "數據不足"}

    ma50 = sma(close, 50)
    ma150 = sma(close, 150)
    ma200 = sma(close, 200)
    c = close[-1]

    # 52 週高低
    hi52 = max(high[-252:]) if n >= 252 else max(high)
    lo52 = min(low[-252:]) if n >= 252 else min(low)

    checks = {
        "1_price_above_150_200": c > ma150[-1] and c > ma200[-1],
        "2_ma150_above_200": ma150[-1] > ma200[-1],
        "3_ma200_rising": ma200[-1] > ma200[-22],  # 約一個月前
        "4_ma50_above_150_200": ma50[-1] > ma150[-1] and ma50[-1] > ma200[-1],
        "5_price_above_50": c > ma50[-1],
        "6_within_25pct_of_52w_high": c >= hi52 * 0.75,
        "7_at_least_25pct_above_52w_low": c >= lo52 * 1.25,
        "8_rs_above_70": rs_rating >= 70,
    }
    avg_vol_3m = sum(vol[-63:]) / min(63, len(vol))
    checks_all = all(checks.values())
    return {
        "pass": checks_all and avg_vol_3m > 500_000,
        "checks": checks,
        "values": {
            "close": round(c, 2),
            "ma50": round(ma50[-1], 2),
            "ma150": round(ma150[-1], 2),
            "ma200": round(ma200[-1], 2),
            "pct_from_52w_high": round(pct(c, hi52), 1),
            "pct_above_52w_low": round(pct(c, lo52), 1),
            "avg_vol_3m": int(avg_vol_3m),
            "rs_rating": round(rs_rating, 1),
        },
    }


def rs_ratings(all_data: dict[str, dict], spy_close: list[float]) -> dict[str, float]:
    """近似 IBD RS Rating：3/6/9/12 個月加權相對回報，轉為 1-99 百分位。"""
    raw = {}
    for sym, d in all_data.items():
        c = d["close"]
        if len(c) < 260 or len(spy_close) < 260:
            continue
        def ret(series, days):
            if len(series) <= days:
                return 0.0
            return (series[-1] / series[-1 - days] - 1) * 100
        rel = (
            2.0 * (ret(c, 63) - ret(spy_close, 63))
            + ret(c, 126) - ret(spy_close, 126)
            + (ret(c, 189) - ret(spy_close, 189))
            + (ret(c, 252) - ret(spy_close, 252))
        ) / 5.0
        raw[sym] = rel
    ranked = sorted(raw.items(), key=lambda kv: kv[1])
    m = len(ranked)
    out = {}
    for i, (sym, _) in enumerate(ranked):
        out[sym] = round(1 + 98 * i / max(m - 1, 1), 1)  # 1-99
    return out
