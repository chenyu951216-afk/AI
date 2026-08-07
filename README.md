# AI Crypto Strategy Lab

多策略加密貨幣 forward-paper research / learning / Bitget live-execution 平台。部署目標為 Zeabur，網站 Port 8080。

## 核心規則

- **所有 K 線判斷只允許 fully closed candles。** 資料層會先丟掉尚未走完整個 timeframe 的 K；策略、學習、post-trade review 都拿不到未收 K。
- 不同策略使用不同主週期：SMC/OI/Funding/Volume/Fib/Liquidation 主判斷 15m；VWAP/EMA、Mean Reversion、Orderbook 主判斷 5m；各自再讀 15m/1H/4H context。
- BTC 大盤只做很小的 score nudge，不是 hard gate。每顆幣的 local regime 由自己的已收 1H/4H 判斷。
- 每套策略各自 10,000U paper account，資料、交易、post-trade study、Champion/Challenger 完全依 strategy namespace 隔離，不互相借資料。
- EARLY 初始門檻刻意較積極收集樣本；TUNING/FINAL 才逐步收斂與降風險。

## 9 套策略

1. SMC liquidity sweep / reclaim
2. OI trend expansion
3. Funding squeeze reversal
4. Volume compression breakout
5. Fibonacci pullback
6. VWAP / EMA momentum
7. Mean reversion
8. Orderbook imbalance
9. CoinGlass liquidation magnet

## Post-Trade Exit Lab

完整平倉後不停止追蹤。系統會依該策略自己的 `signal_tf` 繼續保存後續已收 K，並比較：

- 持倉內 MFE / MAE
- 出場後延伸幅度
- 實現 R 與 capture ratio
- STOP_TOO_TIGHT_CANDIDATE
- STOP_TOO_WIDE_OR_ENTRY_BAD
- TRAIL_TOO_TIGHT_CANDIDATE
- TP_TOO_EARLY_CANDIDATE
- TP_TOO_FAR_OR_GIVEBACK
- LOW_EXIT_CAPTURE

單筆診斷不會直接改參數。只有同一策略累積足夠 post-trade studies 後，才建立 **exit-domain Challenger**。Challenger 一次只能屬於 `entry`、`exit`、`sizing` 其中一個 domain，避免把多種改動混在一起而學不出因果。

## 真實交易

Paper quantity **不會直接複製到真實帳戶**。FINAL 之後由 Shared Live Portfolio Allocator 讀取 Bitget 共用帳戶：

- account equity / available margin
- 全帳戶目前真實倉位與總 notional
- 該策略自己的近期 PF / sample quality
- 該筆策略自己的 stop distance
- 共用帳戶 max concurrent positions / total notional ceiling / free margin reserve

總機器人重新計算真實 notional；策略仍保有自己的 Entry / SL / TP / trailing 邏輯。

### 真實交易需要全部 Gate

1. Strategy `FINAL`
2. `live_eligible=true`
3. 該策略網頁 **真實交易允許 = ON**（預設 OFF）
4. Zeabur `LIVE_TRADING_ALLOWED=true`
5. Bitget private credentials 完整
6. 網頁頂部 LIVE Master = ON

## Bitget Protection Guardian

入場 market order 先附 `presetStopLossPrice` + `presetStopSurplusPrice`，降低進場後短暫裸倉風險。成交後 Guardian 另外建立可追蹤/可修改的交易所 TP/SL：

- Position STOP
- TP1 partial
- TP2 partial
- Position TP3 runner

並使用 Bitget pending-plan API 定期驗證。缺單會重試補掛；移動止損只在策略主週期 **已收 K** 更新後同步到 Bitget `modify-tpsl-order`。若關鍵 STOP 與至少一個 TP 經重試仍無法驗證，可設定自動 emergency close。

## Zeabur

1. 連接本 repo。
2. 掛 Persistent Volume 到 `/data`。
3. 複製 `.env.example` 到 Zeabur Environment Variables。
4. `DB_PATH=/data/crypto_lab.db`。
5. Paper 階段 `LIVE_TRADING_ALLOWED=false` 最安全；Bitget keys 可先留空。

> 這是研究與自動執行系統，不保證獲利。FINAL 是統計/forward gate，不是獲利保證。
