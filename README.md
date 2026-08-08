# AI Crypto Strategy Lab

多策略加密貨幣 forward-paper research / learning / Bitget live-execution 平台。部署目標 Zeabur，網站 Port 8080。

## 核心規則

- **所有 K 線判斷與 AI 訓練樣本只允許 fully closed candles。** 未收 K 在資料層就被排除。
- 不同策略使用自己的最適週期；BTC 只做很小的 score nudge，不是 hard gate。
- 每套策略各自 10,000U paper account，交易、Post-Trade、Champion/Challenger 依 strategy namespace 隔離。
- 每套一般策略都有 `ENTRY / EXIT / SIZING` 三個獨立學習域；一次只測一個領域。
- 真實交易共用同一 Bitget 帳戶，但實際 notional 由 Shared Live Portfolio Allocator 統一決定；策略仍保留自己的進場、SL/TP/BE/trailing。

## 12 套策略

1. SMC liquidity sweep / reclaim
2. OI trend expansion
3. Funding squeeze reversal
4. Volume compression breakout
5. Fibonacci pullback
6. VWAP / EMA momentum
7. Mean reversion
8. Orderbook imbalance
9. CoinGlass liquidation flow / magnet
10. 跌幅榜反彈循環：極端跌幅止跌做多，反彈失敗後可續空
11. 漲幅榜回踩循環：極端漲幅衰竭做空，回踩守住後可續多
12. **AI 十倍極端行情獵手**：獨立 Online Tail Model，從全市場已收 15m K 自己學極端上漲與極端下跌前兆，多空模型分開更新

## AI 十倍極端行情獵手

這套策略的方向與候選訊號不依賴固定 RSI/EMA 進場規則。系統從每個已收 15m K 保存當下市場特徵，等後續 24 小時完整走完後再建立 future label，避免未來資料洩漏。

Long target 使用未來最大上漲的 log-tail；價格約 10x 時 target 約為 1。Short target 使用未來最大下跌的 log-tail；價格約跌 90% 時 target 約為 1。真正的 2x/5x/10x 或極端崩跌樣本權重最高，但模型也會從 15%/30%/50% 等較常見的爆發行情先學到前兆，不必等第一顆 literal 10x 才開始學。

- Long / Short 模型完全分開。
- 初次運行會從已經有完整未來路徑的歷史已收 K 做 bootstrap，因此不是從 0 樣本空等一天。
- 新的已收 K 先成為 `PENDING`，滿 24h 後才正式 `LABELED` 並更新模型。
- 模型權重持續在線更新；極端尾端樣本具有更高訓練權重。
- AI 的入場信心門檻 `ai_confidence_floor` 仍由這套策略自己的 ENTRY Challenger 驗證，防止模型過度積極。
- SL/SL1/TP1/TP2/TP3/BE/trailing 仍由這套策略自己的 EXIT learning 與 Post-Trade Lab 檢討。
- SIZING 仍由自己的 Paper learning 驗證；進真錢後下多少 U 交給共用 Live Portfolio Allocator。
- AI 模型資料表只屬於 `ai_extreme_hunter`，不借其他 11 套策略的交易成果。

> 目標是尋找「10x-class tail opportunity」，不是保證能抓到十倍報酬。做空標的是極端下跌尾端，因標的價格本身最低只能跌到 0。

## Post-Trade Exit Lab

完整平倉後不停止追蹤。系統依該策略自己的主週期保存後續已收 K，檢查：部分 SL 是否太早、Full Stop 是否太緊/太寬、TP 是否太早/太遠、BE/trailing 是否反覆被洗掉、實際行情 capture 是否過低。只有累積足夠同策略證據才建立 EXIT Challenger。

## 真實交易 Gate

真實下單必須全部成立：

1. Strategy `FINAL`
2. `live_eligible=true`
3. 該策略網頁「真實交易允許」= ON
4. Zeabur `LIVE_TRADING_ALLOWED=true`
5. Bitget private credentials 完整
6. 網頁頂部 LIVE Master = ON

## Bitget Protection Guardian

真實下單後持續維護 Position STOP、部分 SL1、TP1、TP2、TP3，並讀 pending/history plan 驗證。已執行的部分保護單標記 `FILLED`，不會誤補掛；缺失或取消才修復。保本與 trailing 只在策略主週期已收 K 後更新。

## Zeabur

1. 連接本 repo。
2. Persistent Volume 掛到 `/data`。
3. 使用 `.env.example` 對應 Environment Variables。
4. `DB_PATH=/data/crypto_lab.db`。

> 研究與自動執行系統不保證獲利。FINAL 代表通過目前 forward evidence gate，不代表未來必然獲利。
