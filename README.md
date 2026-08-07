# Crypto Strategy Lab

一套可直接部署到 Zeabur 的多策略加密貨幣研究、掃描、紙上交易與安全調參平台。預設 **不會真實下單**。每個策略都有獨立 10,000 USDT 模擬帳戶、獨立進場模型、止損、三段止盈、移動止損、交易紀錄、每日勝率/PnL 與調整紀錄。

## 內建 9 個策略

1. **SMC Liquidity Reversal**：swing liquidity sweep + reclaim + CHoCH/BOS；結構外加 ATR buffer 停損。
2. **OI Trend Expansion**：20-bar breakout + 30m OI expansion + relative volume + ADX；避免只看 OI 單一訊號。
3. **Funding Squeeze Reversal**：極端 funding + OI expansion + RSI/exhaustion；做 crowded positioning 反轉。
4. **Volume Compression Breakout**：區間壓縮後異常相對成交量突破。
5. **Fibonacci Pullback**：1H impulse + 0.50–0.618 pullback + 15m confirmation；0.786/structure 外止損。
6. **VWAP / EMA Momentum**：1H trend + 15m VWAP retest/reclaim + EMA alignment。
7. **Mean Reversion**：只在低 ADX regime 使用 z-score + RSI，避免拿均值回歸硬扛趨勢。
8. **Orderbook Imbalance**：Bitget 深度買賣盤失衡 + spread + volume/trend filter。
9. **CoinGlass Liquidation Magnet**：CoinGlass liquidation heatmap 找主要清算密集區，再用結構方向確認；沒有 CoinGlass key 時策略自動無訊號，不會亂用假資料。

## 三階段與「不學歪」設計

### EARLY — 初期激進紙上測試
- 每筆風險約 1.5%（個別較反轉/微結構策略更低）。
- 允許較多樣本，目的不是宣告獲利，而是收集 forward paper evidence。
- 最多 4 個同策略同時持倉，paper notional 有 3x equity cap。

### TUNING — 中期調整
- 80+ 筆**完整 round-trip** paper trades、至少 14 天、正 expectancy、PF >= 1.05、最大回撤 <= 18%，且多個時間窗不能只靠單一時段賺錢才會升級。
- 每次學習最多只改 **一個參數**，且只在預先設定的合理 bounds 中移動約 5% 範圍。
- 每 24 小時最多調整一次，不會每輸一單就亂改。
- 所有 before / after / reason 都寫進 `adjustments`。

### FINAL — 最後測試完成
- 200+ **完整 round-trip** paper trades、至少 45 天、PF >= 1.20、positive expectancy、Sharpe-like >= 0.8、最大回撤 <= 12%、至少 75% rolling windows 為正，才會自動標記 FINAL。
- FINAL 還要求把交易成本再加壓後仍為正（stressed PF >= 1.05），且單一幣種不能貢獻超過 40% 的正收益，降低「其實只靠某一顆幣碰巧賺到」的假 edge。
- FINAL 只是 `live_eligible=true`，**仍然無法真實下單**。
- 真實 Bitget 下單還需要人工在 Zeabur 同時設定：`LIVE_TRADING_ENABLED=true` + `FINAL_LIVE_UNLOCK_TOKEN` + Bitget private API credentials。
- `BitgetLiveAdapter` 程式本身再次檢查 `stage == FINAL`，非 FINAL 直接拒絕。

這樣做是刻意避免典型 backtest overfitting：大量嘗試後只留下歷史最漂亮參數，很容易產生假的 Sharpe/PF。此版本採 forward paper evidence、rolling robustness、bounded one-change-at-a-time governor；未來可再加 CSCV / Deflated Sharpe / double out-of-sample，但不應讓 optimizer 直接碰真實資金。

## 市場資料

- Bitget Futures public REST：all tickers、15m/1H candles、current OI、funding、orderbook depth。
- CoinGlass v4：付費 key 可用 liquidation heatmap；只在該策略需要時才呼叫並快取 5 分鐘，避免浪費額度。
- OI 30m change 由程式自己持續保存 Bitget OI snapshots 算，不依賴外部歷史資料供應商。
- 掃描先以 24h USDT volume 過濾，再輪轉掃描，不是固定 watchlist。持倉價格管理使用 Bitget batch ticker 每輪刷新。

## 紙上成交模型

Paper fill 不是用 signal price 當作零成本神成交：預設計入 0.06% taker fee 與 4 bps slippage；TP/SL 出場也計入費用/滑價。TP1 平 30%、TP2 平 35%、TP3/stop 處理剩餘部位；TP1 後 stop 拉到 BE，TP2 後再鎖利並啟用 trailing。

## Zeabur 部署

1. 把整個 repo 上傳 GitHub。
2. Zeabur 新增 Service → GitHub repository。
3. Port 使用 `8080`（Zeabur 若注入 `PORT` 也會自動使用）。
4. 若要保留 SQLite 歷史，掛 persistent volume 到 `/data`；或至少保持 `DB_PATH=/data/crypto_lab.db`。
5. 複製 `.env.example` 的環境變數。紙上研究階段 Bitget private keys 全部留空。
6. CoinGlass key 填 `COINGLASS_API_KEY` 即可啟用清算磁鐵策略。
7. 打開網站首頁就是 dashboard；健康檢查為 `/health`。

## Dashboard / API

- `/`：策略總覽、Equity、勝率、PF、Expectancy、持倉、最近訊號、每日統計、調整紀錄。
- `/api/overview`
- `/api/strategies`
- `/api/positions`
- `/api/trades`
- `/api/signals`
- `/api/adjustments`
- `/api/daily`
- `/api/learning/run`：可搭 `ADMIN_TOKEN` 手動觸發一次 governor。

## 重要限制

任何策略都不能保證「真正會賺錢」。這個專案的目標是把錯誤策略快速淘汰、把資料洩漏/過度最佳化/零成本回測等假象壓低，再用長時間 forward paper results 挑出值得進一步驗證的模型。真實資金啟用前仍應至少再加入：exchange position reconciliation、API idempotency、circuit breaker、最大日損、總 portfolio exposure、停機復原與更完整的交易所 precision/min-size 處理。
