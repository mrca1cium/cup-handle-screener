from __future__ import annotations
"""杯柄形態（Cup & Handle）+ VCP + VDU 偵測（手冊 Step 3–4）。

思路：在最近約 7 個月找「左杯口高點 → 杯底 → 反彈近杯口 → 柄部整理」。
輸出各項幾何/量能指標，grade = "match"（完整杯柄）/ "watch"（接近）/ None（唔符）。
最終形態美觀度由用戶在網頁 K 線圖肉眼把關。
"""
from .stage2 import sma


def detect(d: dict) -> dict | None:
    high, low, close, vol, dates = d["high"], d["low"], d["close"], d["volume"], d["dates"]
    n = len(close)
    if n < 260:
        return None

    # ---- 1. 找左杯口：最近 150 個交易日（排除最後 5 日）內的最高點 ----
    lookback = min(150, n - 5)
    seg_high = high[n - lookback - 5: n - 5]
    rim_i = n - lookback - 5 + max(range(lookback), key=lambda i: seg_high[i])
    rim = high[rim_i]

    # ---- 2. 杯底：杯口之後的最低點，深度 12%–40% ----
    bot_i = min(range(rim_i, n), key=lambda i: low[i])
    bot = low[bot_i]
    depth_pct = (rim - bot) / rim * 100
    if not (12 <= depth_pct <= 40):
        return None
    cup_days = bot_i - rim_i  # 下跌段 + 反彈段 = 杯身長度（~21 至 126 個交易日 = 1-6 個月）
    if not (20 <= cup_days <= 130):
        return None

    # ---- 3. 柄部：杯底反彈至杯口附近（≥ 杯口 90%）之後到現在 ----
    handle_start = None
    for i in range(bot_i, n):
        if close[i] >= rim * 0.90:
            handle_start = i
            break
    if handle_start is None:
        return None  # 未反彈到杯口，唔係ready嘅杯柄
    handle_days = n - 1 - handle_start
    if handle_days < 4 or handle_days > 42:  # 1 星期至 2 個月
        return None
    if handle_days > cup_days:  # 柄不可長過杯（手冊劣質柄特徵 3）
        return None

    hh = high[handle_start:n]
    ll = low[handle_start:n]
    handle_high = max(hh)
    handle_low = min(ll)
    handle_depth_pct = (handle_high - handle_low) / handle_high * 100

    # 柄部高位應接近杯口（容許略低 5%，高出 2% 內當未突破）
    if handle_high < rim * 0.95 or handle_high > rim * 1.02:
        return None

    # ---- 4. VCP：最近 10 日波幅 ≤ 10% ----
    vcp_win = min(10, handle_days + 1)
    vcp_high = max(high[n - vcp_win:])
    vcp_low = min(low[n - vcp_win:])
    vcp_range_pct = (vcp_high - vcp_low) / vcp_high * 100

    # ---- 5. Higher Lows / Lower Lows（柄內分段比較）----
    seg = max(1, handle_days // 3)
    thirds = [min(low[handle_start + k * seg: handle_start + (k + 1) * seg]) for k in range(3)]
    higher_lows = thirds[0] < thirds[1] < thirds[2] if handle_days >= 9 else True
    lower_lows = thirds[0] > thirds[1] > thirds[2] if handle_days >= 9 else False

    # ---- 6. VDU 量縮：近 3 日均量 < 50 日均量嘅 50% ----
    avg3 = sum(vol[-3:]) / 3
    avg50 = sum(vol[-50:]) / 50
    vdu = avg3 < avg50 * 0.5

    # ---- 7. 劣質柄：突破前直拉（最近 5 日升幅 > 12%）----
    recent_gain = (close[-1] / close[max(0, n - 6)] - 1) * 100
    too_sharp = recent_gain > 12

    # ---- 8. Pivot 與風控位 ----
    pivot = handle_high
    c = close[-1]
    stop_8pct = pivot * 0.92
    ma20 = sma(close, 20)[-1]
    # 技術支撐：柄部低點同 20MA 取較高者（較接近買入價）
    support = max(handle_low, ma20) if ma20 else handle_low
    stop_tech = support * 0.98
    # 手冊：支撐位距離買入點 >10% 屬太波動
    stop = stop_8pct if stop_tech < pivot * 0.90 else stop_tech

    checks = {
        "cup_depth_ok": True,           # 已在上面過濾
        "cup_length_ok": True,
        "handle_length_ok": True,
        "handle_shorter_than_cup": True,
        "handle_near_rim": True,
        "vcp_range_le_10": vcp_range_pct <= 10,
        "higher_lows": higher_lows,
        "no_lower_lows": not lower_lows,
        "volume_dry_up": vdu,
        "not_too_sharp": not too_sharp,
    }
    hard_fail = lower_lows or too_sharp
    passed = sum(checks.values())
    grade = None
    if not hard_fail and passed >= 9:
        grade = "match"
    elif not hard_fail and passed >= 7:
        grade = "watch"

    if grade is None:
        return None

    # 剩低幾多日 K 線俾網頁畫圖（杯口前留 20 日）
    chart_start = max(0, rim_i - 20)
    return {
        "grade": grade,
        "checks": checks,
        "metrics": {
            "rim_date": dates[rim_i], "rim_price": round(rim, 2),
            "bottom_date": dates[bot_i], "bottom_price": round(bot, 2),
            "cup_depth_pct": round(depth_pct, 1),
            "cup_days": cup_days,
            "handle_days": handle_days,
            "handle_high": round(handle_high, 2),
            "handle_low": round(handle_low, 2),
            "handle_depth_pct": round(handle_depth_pct, 1),
            "vcp_range_pct": round(vcp_range_pct, 1),
            "vdu_ratio": round(avg3 / avg50, 3) if avg50 else None,
            "pivot": round(pivot, 2),
            "close": round(c, 2),
            "pct_to_pivot": round((pivot / c - 1) * 100, 2),
            "stop_loss": round(stop, 2),
            "target_1r": round(pivot + (pivot - stop), 2),
        },
        "chart_start": chart_start,
        "rim_i": rim_i,
        "bot_i": bot_i,
        "handle_start": handle_start,
    }
