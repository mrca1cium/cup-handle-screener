# 美股杯柄形態篩選器（Cup & Handle Screener）

根據《美股杯柄形態精準交易手冊》（Minervini 系統）實現的自動化選股工具：
每日美股收市後自動掃描 S&P 500 成分股，找出處於 **Stage 2 上升趨勢** 且形成
**杯柄 / VCP 形態 + 量縮（VDU）** 的股票，並在 GitHub Pages 網頁顯示結果。

## 篩選流程（對應手冊）

| Step | 內容 | 實現 |
|---|---|---|
| 1 | 大市回調 5–10% 監控 + 板塊相對強弱 | `screener/market.py` |
| 2 | Stage 2 八大條件 + 流動性 | `screener/stage2.py` |
| 3 | 杯柄幾何（深度 12–40%、杯 1–6 個月、柄短於杯、VCP ≤10%、higher lows、VDU） | `screener/pattern.py` |
| 4 | Pivot / 止蝕 / 1R 目標價 | `screener/pattern.py` |

數據直接取自 Yahoo Finance v8 chart API（不經 yfinance，避免其限流/兼容問題）。

## 本地運行

```bash
python3 screener/main.py                # 全量 S&P 500
python3 screener/main.py --limit 30     # 測試前 30 隻
python3 -m http.server -d docs 8000     # 本地預覽網頁 http://localhost:8000
```

依賴：`requests`（`pip3 install requests`）。注意 Python ≥3.8 為佳（GitHub Actions 用 3.12）。

## 推上 GitHub + 開通網頁

```bash
cd cup-handle-screener
git init && git add -A && git commit -m "init"
gh repo create cup-handle-screener --public --source=. --push
```

然後到 repo **Settings → Pages → Source: Deploy from a branch → main / /docs**，
幾分鐘後網頁便在 `https://<用戶名>.github.io/cup-handle-screener/`。

GitHub Actions 會每日（週一至五 UTC 21:30，約香港朝早 5:30 夏令）自動掃描並 commit 新結果。
冬日時間（11 月起）如想對準收市後，把 `.github/workflows/scan.yml` 的 cron 改成 `30 22 * * 1-5`。
也可以在 Actions 頁手動 **Run workflow** 立即跑一次。

## 結果解讀

- **完整杯柄**（綠色）：全部關鍵條件通過
- **觀察名單**（黃色）：大部分通過，接近成形
- 每張卡可展開：Stage 2 逐項 ✓/✗、杯柄參數、止蝕/目標價、K 線圖（藍虛線 = Pivot，黃點 = 杯底）
- 按「複製全部代號」貼入 TradingView 設突破警報（手冊 Step 4–5）

## 免責聲明

僅供形態篩選參考，最終請按手冊 Step 3 肉眼審查圖表美觀度，並嚴守 8% 硬止蝕。唔構成投資建議。
