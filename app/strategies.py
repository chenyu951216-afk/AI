from __future__ import annotations
from dataclasses import replace
from .models import Signal, StrategySpec
from .indicators import fib_context, safe

COMMON={"risk_pct":0.012,"leverage":2.0,"max_positions":3,"max_position_notional_pct":0.18,"max_symbol_exposure_pct":0.20,"max_total_exposure_pct":0.80,"tp1_r":1.0,"tp2_r":2.0,"tp3_r":3.2,"tp1_fraction":0.30,"tp2_fraction":0.35,"trail_r":1.2}

def P(**x): p=dict(COMMON); p.update(x); return p

def sig(spec,m,side,score,stop,reason,extra=None):
    f={"rvol":safe(m.candles_15m.iloc[-1].rvol20),"adx":safe(m.candles_15m.iloc[-1].adx14),"rsi":safe(m.candles_15m.iloc[-1].rsi14),"oi_change_30m":m.oi_change_30m,"funding":m.funding,"regime":m.regime}
    if extra:f.update(extra)
    return Signal(spec.name,m.symbol,side,score,m.price,float(stop),reason,f)

class BaseStrategy:
    needs_depth=False; needs_liquidation=False; needs_cg_oi=False
    def __init__(self,spec:StrategySpec): self.spec=spec
    def with_params(self,params:dict): return self.__class__(replace(self.spec,params=params))
    def regime_ok(self,regime:str)->bool: return "ANY" in self.spec.regimes or regime in self.spec.regimes
    def evaluate(self,m): raise NotImplementedError

class SMCLiquidity(BaseStrategy):
    def evaluate(self,m):
        if not self.regime_ok(m.regime): return None
        d=m.candles_15m; x=d.iloc[-1]; p=self.spec.params; atr=safe(x.atr14); swing_hi=safe(d.high.iloc[-22:-2].max()); swing_lo=safe(d.low.iloc[-22:-2].min())
        if x.low<swing_lo and x.close>swing_lo and x.close>x.open and x.rvol20>=p["min_rvol"]:
            stop=min(float(x.low),swing_lo)-p["atr_buffer"]*atr; return sig(self.spec,m,"long",0.80,stop,"Liquidity sweep below prior swing then reclaim/CHoCH confirmation",{"swing":swing_lo})
        if x.high>swing_hi and x.close<swing_hi and x.close<x.open and x.rvol20>=p["min_rvol"]:
            stop=max(float(x.high),swing_hi)+p["atr_buffer"]*atr; return sig(self.spec,m,"short",0.80,stop,"Liquidity sweep above prior swing then bearish reclaim/CHoCH",{"swing":swing_hi})

class OITrendExpansion(BaseStrategy):
    needs_cg_oi=True
    def evaluate(self,m):
        if not self.regime_ok(m.regime): return None
        d=m.candles_15m; x=d.iloc[-1]; p=self.spec.params; atr=safe(x.atr14); oi=max(m.oi_change_30m,m.cg_oi_change_30m/100 if m.cg_oi_change_30m is not None else -99)
        if x.close>x.hh20 and oi>=p["min_oi_change"] and x.rvol20>=p["min_rvol"] and x.adx14>=p["min_adx"]:
            return sig(self.spec,m,"long",0.84,m.price-p["atr_stop"]*atr,"Price breakout confirmed by OI expansion, relative volume and ADX",{"effective_oi":oi})
        if x.close<x.ll20 and oi>=p["min_oi_change"] and x.rvol20>=p["min_rvol"] and x.adx14>=p["min_adx"]:
            return sig(self.spec,m,"short",0.84,m.price+p["atr_stop"]*atr,"Downside breakout confirmed by OI expansion, relative volume and ADX",{"effective_oi":oi})

class FundingSqueeze(BaseStrategy):
    def evaluate(self,m):
        d=m.candles_15m; x=d.iloc[-1]; p=self.spec.params; atr=safe(x.atr14)
        if m.funding>=p["funding_extreme"] and x.rsi14>=p["rsi_extreme"] and x.close<x.open and m.oi_change_30m>0:
            return sig(self.spec,m,"short",0.76,m.price+p["atr_stop"]*atr,"Crowded positive funding plus OI and exhaustion reversal")
        if m.funding<=-p["funding_extreme"] and x.rsi14<=100-p["rsi_extreme"] and x.close>x.open and m.oi_change_30m>0:
            return sig(self.spec,m,"long",0.76,m.price-p["atr_stop"]*atr,"Crowded negative funding plus OI and exhaustion reversal")

class VolumeBreakout(BaseStrategy):
    def evaluate(self,m):
        if not self.regime_ok(m.regime): return None
        d=m.candles_15m; x=d.iloc[-1]; p=self.spec.params; atr=safe(x.atr14); med=safe(d.range20.iloc[-80:-1].median(),1)
        compressed=safe(d.range20.iloc[-2])<=med*p["compression_ratio"]
        if compressed and x.close>x.hh20 and x.rvol20>=p["min_rvol"]: return sig(self.spec,m,"long",0.79,m.price-p["atr_stop"]*atr,"Compression released upward with abnormal relative volume")
        if compressed and x.close<x.ll20 and x.rvol20>=p["min_rvol"]: return sig(self.spec,m,"short",0.79,m.price+p["atr_stop"]*atr,"Compression released downward with abnormal relative volume")

class FibonacciPullback(BaseStrategy):
    def evaluate(self,m):
        if not self.regime_ok(m.regime): return None
        d=m.candles_15m; h=m.candles_1h; x=d.iloc[-1]; p=self.spec.params; f=fib_context(h,int(p["lookback"])); atr=safe(x.atr14); tol=p["zone_tolerance"]*atr
        if f["dir"]==1 and h.iloc[-1].ema20>h.iloc[-1].ema50 and f["fib618"]-tol<=m.price<=f["fib50"]+tol and x.close>x.open and x.rvol20>=p["min_rvol"]:
            return sig(self.spec,m,"long",0.81,min(f["fib786"]-p["stop_buffer_atr"]*atr,float(d.low.iloc[-6:].min())),"1H impulse retraced into 0.50-0.618 Fibonacci zone with bullish confirmation",{"fib_target":f["hi"]})
        if f["dir"]==-1 and h.iloc[-1].ema20<h.iloc[-1].ema50 and f["fib50"]-tol<=m.price<=f["fib618"]+tol and x.close<x.open and x.rvol20>=p["min_rvol"]:
            return sig(self.spec,m,"short",0.81,max(f["fib786"]+p["stop_buffer_atr"]*atr,float(d.high.iloc[-6:].max())),"1H impulse retraced into 0.50-0.618 Fibonacci zone with bearish confirmation",{"fib_target":f["lo"]})

class VWAPEMAMomentum(BaseStrategy):
    def evaluate(self,m):
        if not self.regime_ok(m.regime): return None
        d=m.candles_15m; h=m.candles_1h; x=d.iloc[-1]; q=d.iloc[-2]; p=self.spec.params; atr=safe(x.atr14)
        if h.iloc[-1].ema20>h.iloc[-1].ema50 and x.ema20>x.ema50 and q.low<=q.vwap and x.close>x.vwap and x.rsi14>=p["rsi_long"]:
            return sig(self.spec,m,"long",0.77,min(float(d.low.iloc[-8:].min()),m.price-p["atr_stop"]*atr),"Higher-timeframe trend with VWAP reclaim and EMA alignment")
        if h.iloc[-1].ema20<h.iloc[-1].ema50 and x.ema20<x.ema50 and q.high>=q.vwap and x.close<x.vwap and x.rsi14<=100-p["rsi_long"]:
            return sig(self.spec,m,"short",0.77,max(float(d.high.iloc[-8:].max()),m.price+p["atr_stop"]*atr),"Higher-timeframe downtrend with VWAP rejection and EMA alignment")

class MeanReversion(BaseStrategy):
    def evaluate(self,m):
        if not self.regime_ok(m.regime): return None
        d=m.candles_15m; x=d.iloc[-1]; p=self.spec.params; atr=safe(x.atr14)
        if x.adx14<=p["max_adx"] and x.z20<=-p["z"] and x.rsi14<=p["rsi_low"]: return sig(self.spec,m,"long",0.71,m.price-p["atr_stop"]*atr,"Range regime downside statistical stretch")
        if x.adx14<=p["max_adx"] and x.z20>=p["z"] and x.rsi14>=100-p["rsi_low"]: return sig(self.spec,m,"short",0.71,m.price+p["atr_stop"]*atr,"Range regime upside statistical stretch")

class OrderbookImbalance(BaseStrategy):
    needs_depth=True
    def evaluate(self,m):
        if m.orderbook_imbalance is None:return None
        d=m.candles_15m; x=d.iloc[-1]; p=self.spec.params; atr=safe(x.atr14); spread=(m.ask-m.bid)/max(m.price,1e-12)
        if spread>p["max_spread"]:return None
        if m.orderbook_imbalance>=p["imbalance"] and x.close>x.ema20 and x.rvol20>=p["min_rvol"]: return sig(self.spec,m,"long",0.75,m.price-p["atr_stop"]*atr,"Bid depth imbalance aligned with momentum",{"book_imbalance":m.orderbook_imbalance,"spread":spread})
        if m.orderbook_imbalance<=-p["imbalance"] and x.close<x.ema20 and x.rvol20>=p["min_rvol"]: return sig(self.spec,m,"short",0.75,m.price+p["atr_stop"]*atr,"Ask depth imbalance aligned with momentum",{"book_imbalance":m.orderbook_imbalance,"spread":spread})

class LiquidationMagnet(BaseStrategy):
    needs_liquidation=True
    def evaluate(self,m):
        if not (m.liquidation_above or m.liquidation_below):return None
        d=m.candles_15m; x=d.iloc[-1]; p=self.spec.params; atr=safe(x.atr14); a=m.liquidation_above_strength; b=m.liquidation_below_strength
        if m.liquidation_above and a>b*p["strength_ratio"] and x.close>x.ema20 and x.rsi14<p["max_rsi"]: return sig(self.spec,m,"long",0.78,min(float(d.low.iloc[-8:].min()),m.price-p["atr_stop"]*atr),"Dominant CoinGlass liquidation cluster above price with bullish structure",{"liq_target":m.liquidation_above,"liq_ratio":a/max(b,1)})
        if m.liquidation_below and b>a*p["strength_ratio"] and x.close<x.ema20 and x.rsi14>100-p["max_rsi"]: return sig(self.spec,m,"short",0.78,max(float(d.high.iloc[-8:].max()),m.price+p["atr_stop"]*atr),"Dominant CoinGlass liquidation cluster below price with bearish structure",{"liq_target":m.liquidation_below,"liq_ratio":b/max(a,1)})

SPECS=[
 StrategySpec("smc_liquidity","SMC 流動性反轉","Liquidity sweep + reclaim + CHoCH/BOS",P(min_rvol=1.15,atr_buffer=.25,tp3_r=3.6),("BULL_TREND","BEAR_TREND","RANGE","NEUTRAL")),
 StrategySpec("oi_trend","OI 趨勢擴張","Breakout + OI expansion + volume + ADX",P(min_oi_change=.008,min_rvol=1.5,min_adx=20,atr_stop=1.5,risk_pct=.013),("BULL_TREND","BEAR_TREND","HIGH_VOL","NEUTRAL")),
 StrategySpec("funding_squeeze","Funding 擠壓反轉","Funding crowding + OI + exhaustion",P(funding_extreme=.0005,rsi_extreme=72,atr_stop=1.5,risk_pct=.010,tp1_r=.8,tp2_r=1.6,tp3_r=2.8),("ANY",)),
 StrategySpec("volume_breakout","量能壓縮突破","Compression + abnormal volume breakout",P(min_rvol=1.8,compression_ratio=.70,atr_stop=1.4,risk_pct=.013), ("BULL_TREND","BEAR_TREND","HIGH_VOL","NEUTRAL")),
 StrategySpec("fib_pullback","Fibonacci 回踩","1H impulse + 0.50-0.618 retracement",P(lookback=60,zone_tolerance=.35,min_rvol=1.05,stop_buffer_atr=.20,risk_pct=.011,tp1_r=1.0,tp2_r=2.2,tp3_r=3.8),("BULL_TREND","BEAR_TREND")),
 StrategySpec("vwap_ema","VWAP / EMA 動能","Trend continuation after VWAP retest",P(rsi_long=52,atr_stop=1.35,risk_pct=.011),("BULL_TREND","BEAR_TREND")),
 StrategySpec("mean_reversion","區間均值回歸","Low-ADX z-score + RSI mean reversion",P(max_adx=19,z=2.0,rsi_low=30,atr_stop=1.6,risk_pct=.008,leverage=1.5,tp1_r=.8,tp2_r=1.5,tp3_r=2.3),("RANGE","NEUTRAL")),
 StrategySpec("orderbook","訂單簿失衡","L2 depth imbalance + spread + momentum",P(imbalance=.22,max_spread=.0012,min_rvol=1.1,atr_stop=1.0,risk_pct=.008,leverage=1.5,tp1_r=.8,tp2_r=1.5,tp3_r=2.5),("ANY",)),
 StrategySpec("liquidation_magnet","CoinGlass 清算磁鐵","Liquidation heatmap cluster + structure",P(strength_ratio=1.5,max_rsi=68,atr_stop=1.4,risk_pct=.009,tp1_r=1.0,tp2_r=1.8,tp3_r=3.0),("ANY",)),
]
CLASSES=[SMCLiquidity,OITrendExpansion,FundingSqueeze,VolumeBreakout,FibonacciPullback,VWAPEMAMomentum,MeanReversion,OrderbookImbalance,LiquidationMagnet]
SPEC_MAP={s.name:s for s in SPECS}; CLASS_MAP={s.name:c for s,c in zip(SPECS,CLASSES)}
def build_strategy(name:str,params:dict): return CLASS_MAP[name](replace(SPEC_MAP[name],params=params))
