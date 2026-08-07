# AI Crypto Strategy Lab

最終用途：長時間掃描 Bitget USDT 永續合約市場，用多套互相獨立的策略做真實行情 paper trading，讓每套策略在硬風控邊界內持續測試、調整與淘汰，只有通過長期 forward evidence 的策略才會自動取得 `FINAL / live_eligible`，之後仍必須由操作員在網頁手動打開 LIVE 總開關，才可能送出 Bitget 真實訂單。

> 任何策略都不保證獲利。這個系統的設計目標是降低過度最佳化、短期運氣與統計污染，並把真實資金權限和研究權限分離。

## 內建策略

1. SMC 流動性反轉：swing liquidity sweep + reclaim + CHoCH/BOS。
2. OI 趨勢擴張：價格突破 + Bitget OI + CoinGlass 全市場 OI + RVOL + ADX。
3. Funding 擠壓反轉：極端 funding + OI + RSI/exhaustion。
4. 量能壓縮突破：range compression + abnormal relative volume breakout。
5. Fibonacci 回踩：1H impulse + 0.50–0.618 retracement + 15m confirmation。
6. VWAP / EMA 動能：1H trend + 15m VWAP reclaim/rejection + EMA alignment。
7. 區間均值回歸：低 ADX regime + z-score + RSI。
8. 訂單簿失衡：Bitget L2 depth imbalance + spread + momentum。
9. CoinGlass 清算磁鐵：liquidation heatmap cluster + market structure。

每套策略 Champion 都有獨立 `10,000 USDT` paper account。Challenger 是額外影子帳戶，只用來驗證新參數，不會混入 Champion 的績效。

## 不是「輸一單就亂改」：Champion / Challenger

機器人每個 learning cycle 最多只提出少量有界變更。候選參數不會直接覆蓋正式策略，而是建立 Challenger，跟 Champion 同時在真實行情上 paper trade。Challenger 至少累積指定筆數與天數，再比較 PF、expectancy、stressed PF、drawdown、R expectancy 等；只有明顯較穩定才 promoted，否則 rejected。

可學習範圍不只訊號門檻，也包含：

- `risk_pct` 每筆風險比例
- `leverage` 模擬槓桿
- `max_positions` 同策略最大同時持倉
- `max_position_notional_pct` 單筆下單名義金額上限（所以「下多少」本身也在學習）
- `max_symbol_exposure_pct` 單幣曝險
- `max_total_exposure_pct` 總曝險
- `tp1_r / tp2_r / tp3_r`
- `tp1_fraction / tp2_fraction`
- `trail_r`
- 每套策略自己的 OI / volume / ADX / RSI / ATR / Fibo / orderbook / liquidation 等參數

AI 永遠不能突破 `.env` 的 HARD_* 風控 ceiling。升級到 TUNING / FINAL 後，風險與槓桿的 stage cap 也會自動降低。

為避免永遠反覆改參數、永遠累積不到固定版本的 forward evidence，系統有 convergence freeze：當 Champion 已接近下一個升級門檻時，暫停探索一段時間，讓同一組參數累積乾淨的 14/45 天證據；若期限到了仍沒過門檻，再恢復 Challenger 探索。

更重要的是：FINAL 策略若有 Challenger 新參數被 promoted，舊 FINAL/live 資格會立即撤銷，新 Champion 回到 TUNING 並重新累積完整 FINAL evidence。新參數不會繼承舊參數的成績。

## 三階段

### EARLY
較積極收集 forward samples，但仍受 hard risk limits 限制。

### TUNING
至少累積足夠完整 round-trip、天數、PF、正 expectancy、可接受回撤與 rolling robustness 才會進入。

### FINAL
預設要求：

- `FINAL_MIN_TRADES=250`
- `FINAL_MIN_DAYS=45`
- PF >= 1.25
- stressed PF >= 1.10
- max drawdown <= 10%
- rolling positive windows >= 75%
- 正收益不能過度集中單一幣
- 至少跨 5 個交易幣種

FINAL 之後如果最近 60+ 筆交易明顯退化（例如 PF < 0.95、expectancy < 0、回撤惡化），系統會自動 demote 回 TUNING 並立即撤銷 `live_eligible`。

## 真實交易：三層 Gate

真實 Bitget 訂單必須同時滿足：

1. 策略機器人自動學習完成：`stage == FINAL && live_eligible == true`
2. Zeabur 部署層允許：`LIVE_TRADING_ALLOWED=true` 且 Bitget private credentials 完整
3. 你本人在 Dashboard 右上角輸入 `ADMIN_TOKEN` 並手動把 LIVE switch 打開

少一項都不下單。網頁開關狀態存 SQLite runtime settings，重新啟動後仍保留，但 FINAL strategy 若退化會自動失去資格。

Bitget live adapter 還會先扣掉該策略現有真實倉位名義金額，並依 Bitget 帳戶 `available` 可用保證金再做最後一次縮倉，避免 paper 倉位直接照搬造成真實帳戶過度曝險；送單前也會重新抓 ticker，如果 paper 觸發價與真實行情偏離超過 `LIVE_MAX_ENTRY_DRIFT_BPS` 就拒絕追價。接著會讀 `/api/v2/mix/market/contracts`，依 `minTradeNum`、`sizeMultiplier`、`volumePlace`、`minTradeUSDT`、`maxMarketOrderQty`、`maxLever` 正規化數量與槓桿，再送 `/api/v2/mix/order/place-order`。Entry 同時帶預設止損；若 `LIVE_PARTIAL_TP_ENABLED=true`，再用 `/api/v2/mix/order/place-tpsl-order` 建立 TP1 / TP2 / TP3 分批 profit plans。

## 市場資料

- Bitget public V2：tickers、contracts、15m / 1H candles、OI、funding、orderbook depth。
- CoinGlass V4：pair liquidation heatmap、全市場 exchange-list OI 30m change（有付費 key 才啟用）；可用 `COINGLASS_TOP_SYMBOLS` 控制每輪只對流動性前段幣種做付費資料 enrichment，避免浪費 API 額度。
- OI 也會在本地持續保存 snapshot，計算 Bitget 自身約 30m OI change。
- BTC 1H EMA / ADX / ATR 自動判定 `BULL_TREND / BEAR_TREND / RANGE / HIGH_VOL / NEUTRAL`，各策略依適合 regime 決定是否工作。

## Dashboard（PORT 8080）

首頁會顯示：

- 掃描狀態 / 幣池 / BTC regime / CoinGlass 狀態
- 每策略 equity、總報酬、完整交易數、勝率、PF、stress PF、Avg R、DD、rolling robustness
- EARLY / TUNING / FINAL
- live eligible 狀態
- Challenger 正在測什麼、交易數與 PF
- Champion 當前學到的 risk / leverage / exposure / TP / trailing 參數
- 模擬持倉、SL / TP1 / TP2 / TP3
- 每日 PnL / 勝率
- 所有參數調整 before → after + 原因
- 風控拒絕原因
- 真實 Bitget order log
- LIVE 手動總開關

## Zeabur

1. 直接用此 GitHub repo 建立 Zeabur service。
2. Port `8080`。
3. 建立 persistent volume，掛到 `/data`。
4. 把 `.env.example` 全部環境變數放到 Zeabur。
5. 初期保持 `LIVE_TRADING_ALLOWED=false`，Bitget private key 可先留空；Bitget public market data 不需要 private key。
6. 填入 `COINGLASS_API_KEY` 後自動啟用 CoinGlass 資料。
7. `ADMIN_TOKEN` 請一定使用長亂數字串。

資料庫預設：`/data/crypto_lab.db`。

## 重要環境變數

完整清單直接看 `.env.example`。不要把真實 `.env` commit 到 GitHub。

## API

- `/health`
- `/api/overview`
- `/api/strategies`
- `/api/positions`
- `/api/trades`
- `/api/signals`
- `/api/daily`
- `/api/adjustments`
- `/api/risk-events`
- `/api/live/status`
- `/api/live/orders`
- `POST /api/live/toggle`（需要 `x-admin-token`）
- `POST /api/learning/run`（需要 `x-admin-token`）
- `POST /api/strategy/{name}/toggle`（需要 `x-admin-token`）

## 執行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```
