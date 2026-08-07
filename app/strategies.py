from __future__ import annotations

from abc import ABC, abstractmethod
import math

from .indicators import fib_context, safe
from .market import MarketSnapshot
from .models import Signal, StrategySpec


def _tp(side: str, entry: float, stop: float, rs=(1.0,2.0,3.0)) -> list[float]:
    r=abs(entry-stop)
    return [entry+r*x if side=="long" else entry-r*x for x in rs]


def _sig(spec: StrategySpec, m: MarketSnapshot, side: str, score: float, stop: float, tps: list[float], reason: str, extras: dict | None=None, trailing: float=1.5):
    c=m.candles_15m.iloc[-1]
    feats={"rsi":safe(c.rsi14),"rvol":safe(c.rvol20),"adx":safe(c.adx14),"atr_pct":safe(c.atr14/c.close),"funding":m.funding,"oi_change":m.oi_change_30m,"volume24h":m.quote_volume}
    feats.update(extras or {})
    if not math.isfinite(stop) or stop<=0 or abs(m.price-stop)/m.price < 0.0015: return None
    if side=="long" and stop>=m.price: return None
    if side=="short" and stop<=m.price: return None
    return Signal(spec.name,m.symbol,side,score,m.price,stop,tps,trailing,reason,feats)


class BaseStrategy(ABC):
    spec: StrategySpec
    needs_depth=False
    needs_liquidation=False
    def __init__(self,spec:StrategySpec): self.spec=spec
    @abstractmethod
    def evaluate(self,m:MarketSnapshot)->Signal|None: ...


class SMCLiquidity(BaseStrategy):
    def evaluate(self,m):
        d=m.candles_15m; x=d.iloc[-1]; p=d.iloc[-2]; atr=safe(x.atr14)
        swept_low = p.low < safe(p.ll20,p.low) and p.close > safe(p.ll20,p.close)
        swept_high = p.high > safe(p.hh20,p.high) and p.close < safe(p.hh20,p.close)
        bull_choch = x.close > d.high.iloc[-8:-2].max() and x.rvol20 > self.spec.params["min_rvol"]
        bear_choch = x.close < d.low.iloc[-8:-2].min() and x.rvol20 > self.spec.params["min_rvol"]
        if swept_low and bull_choch:
            stop=min(p.low,x.low)-self.spec.params["atr_buffer"]*atr
            return _sig(self.spec,m,"long",0.82,stop,_tp("long",m.price,stop,(1,2.2,3.5)),"Liquidity sweep below prior swing + bullish CHoCH/BOS",trailing=1.6)
        if swept_high and bear_choch:
            stop=max(p.high,x.high)+self.spec.params["atr_buffer"]*atr
            return _sig(self.spec,m,"short",0.82,stop,_tp("short",m.price,stop,(1,2.2,3.5)),"Liquidity sweep above prior swing + bearish CHoCH/BOS",trailing=1.6)


class OITrendExpansion(BaseStrategy):
    def evaluate(self,m):
        d=m.candles_15m; x=d.iloc[-1]; atr=safe(x.atr14)
        strong_oi=m.oi_change_30m>=self.spec.params["min_oi_change"]
        vol=x.rvol20>=self.spec.params["min_rvol"]; adx=x.adx14>=self.spec.params["min_adx"]
        if strong_oi and vol and adx and x.close>x.hh20 and x.ema20>x.ema50:
            stop=min(safe(d.low.iloc[-6:-1].min()),m.price-self.spec.params["atr_stop"]*atr)
            return _sig(self.spec,m,"long",0.84,stop,_tp("long",m.price,stop,(1,2,3.2)),"Breakout with rising OI, relative volume and trend strength",{"oi":m.oi},1.8)
        if strong_oi and vol and adx and x.close<x.ll20 and x.ema20<x.ema50:
            stop=max(safe(d.high.iloc[-6:-1].max()),m.price+self.spec.params["atr_stop"]*atr)
            return _sig(self.spec,m,"short",0.84,stop,_tp("short",m.price,stop,(1,2,3.2)),"Breakdown with rising OI, relative volume and trend strength",{"oi":m.oi},1.8)


class FundingSqueeze(BaseStrategy):
    def evaluate(self,m):
        d=m.candles_15m; x=d.iloc[-1]; atr=safe(x.atr14); fr=self.spec.params["funding_extreme"]
        if m.funding>=fr and x.rsi14>=self.spec.params["rsi_extreme"] and x.close<x.ema20 and m.oi_change_30m>0:
            stop=max(safe(d.high.iloc[-8:].max()),m.price+1.2*atr)
            return _sig(self.spec,m,"short",0.78,stop,[safe(x.vwap),m.price-2*abs(m.price-stop),m.price-3*abs(m.price-stop)],"Crowded positive funding + overbought exhaustion + OI expansion",trailing=1.4)
        if m.funding<=-fr and x.rsi14<=100-self.spec.params["rsi_extreme"] and x.close>x.ema20 and m.oi_change_30m>0:
            stop=min(safe(d.low.iloc[-8:].min()),m.price-1.2*atr)
            return _sig(self.spec,m,"long",0.78,stop,[safe(x.vwap),m.price+2*abs(m.price-stop),m.price+3*abs(m.price-stop)],"Crowded negative funding + oversold exhaustion + OI expansion",trailing=1.4)


class VolumeBreakout(BaseStrategy):
    def evaluate(self,m):
        d=m.candles_15m; x=d.iloc[-1]; atr=safe(x.atr14)
        compression=safe(d.range20.iloc[-2]) <= safe(d.range20.iloc[-60:-2].quantile(self.spec.params["compression_q"]),1)
        if compression and x.rvol20>=self.spec.params["min_rvol"] and x.close>x.hh20 and x.close>x.open:
            stop=max(safe(x.hh20)-.2*atr,m.price-self.spec.params["atr_stop"]*atr)
            return _sig(self.spec,m,"long",0.81,stop,_tp("long",m.price,stop,(0.9,2,3)),"Range compression resolved upward on abnormal volume",trailing=1.7)
        if compression and x.rvol20>=self.spec.params["min_rvol"] and x.close<x.ll20 and x.close<x.open:
            stop=min(safe(x.ll20)+.2*atr,m.price+self.spec.params["atr_stop"]*atr)
            return _sig(self.spec,m,"short",0.81,stop,_tp("short",m.price,stop,(0.9,2,3)),"Range compression resolved downward on abnormal volume",trailing=1.7)


class FibonacciPullback(BaseStrategy):
    def evaluate(self,m):
        h=m.candles_1h; d=m.candles_15m; x=d.iloc[-1]; atr=safe(x.atr14); f=fib_context(h,self.spec.params["lookback"])
        tol=self.spec.params["zone_tolerance"]*atr
        if f["dir"]==1 and h.iloc[-1].ema20>h.iloc[-1].ema50:
            in_zone=(f["fib618"]-tol)<=m.price<=(f["fib50"]+tol)
            if in_zone and x.close>x.open and x.rvol20>=self.spec.params["min_rvol"]:
                stop=min(f["fib786"]-.25*atr,d.low.iloc[-6:].min())
                return _sig(self.spec,m,"long",0.79,stop,[f["hi"],f.get("ext1272",f["hi"]),m.price+3*abs(m.price-stop)],"1H impulse retraced into 0.50-0.618 Fibonacci value zone with confirmation",trailing=1.6)
        if f["dir"]==-1 and h.iloc[-1].ema20<h.iloc[-1].ema50:
            in_zone=(f["fib50"]-tol)<=m.price<=(f["fib618"]+tol)
            if in_zone and x.close<x.open and x.rvol20>=self.spec.params["min_rvol"]:
                stop=max(f["fib786"]+.25*atr,d.high.iloc[-6:].max())
                return _sig(self.spec,m,"short",0.79,stop,[f["lo"],f.get("ext1272",f["lo"]),m.price-3*abs(m.price-stop)],"1H down impulse retraced into 0.50-0.618 Fibonacci value zone with confirmation",trailing=1.6)


class VWAPEMAMomentum(BaseStrategy):
    def evaluate(self,m):
        d=m.candles_15m; h=m.candles_1h; x=d.iloc[-1]; p=d.iloc[-2]; atr=safe(x.atr14)
        if h.iloc[-1].ema20>h.iloc[-1].ema50 and x.ema20>x.ema50 and p.low<=p.vwap and x.close>x.vwap and x.rsi14>=self.spec.params["rsi_long"]:
            stop=min(d.low.iloc[-8:].min(),m.price-self.spec.params["atr_stop"]*atr)
            return _sig(self.spec,m,"long",0.76,stop,_tp("long",m.price,stop,(1,2,4)),"Higher-timeframe trend + VWAP reclaim + EMA alignment",trailing=1.3)
        if h.iloc[-1].ema20<h.iloc[-1].ema50 and x.ema20<x.ema50 and p.high>=p.vwap and x.close<x.vwap and x.rsi14<=100-self.spec.params["rsi_long"]:
            stop=max(d.high.iloc[-8:].max(),m.price+self.spec.params["atr_stop"]*atr)
            return _sig(self.spec,m,"short",0.76,stop,_tp("short",m.price,stop,(1,2,4)),"Higher-timeframe downtrend + VWAP rejection + EMA alignment",trailing=1.3)


class MeanReversion(BaseStrategy):
    def evaluate(self,m):
        d=m.candles_15m; x=d.iloc[-1]; atr=safe(x.atr14)
        if x.adx14<=self.spec.params["max_adx"] and x.z20<=-self.spec.params["z"] and x.rsi14<=self.spec.params["rsi_low"]:
            stop=m.price-self.spec.params["atr_stop"]*atr
            return _sig(self.spec,m,"long",0.70,stop,[safe(x.bb_mid),safe(x.vwap),m.price+2.2*abs(m.price-stop)],"Low-ADX regime with statistically stretched downside deviation",trailing=1.1)
        if x.adx14<=self.spec.params["max_adx"] and x.z20>=self.spec.params["z"] and x.rsi14>=100-self.spec.params["rsi_low"]:
            stop=m.price+self.spec.params["atr_stop"]*atr
            return _sig(self.spec,m,"short",0.70,stop,[safe(x.bb_mid),safe(x.vwap),m.price-2.2*abs(m.price-stop)],"Low-ADX regime with statistically stretched upside deviation",trailing=1.1)


class OrderbookImbalance(BaseStrategy):
    needs_depth=True
    def evaluate(self,m):
        if m.orderbook_imbalance is None: return None
        d=m.candles_15m; x=d.iloc[-1]; atr=safe(x.atr14); th=self.spec.params["imbalance"]
        spread=(m.ask-m.bid)/m.price if m.price else 1
        if spread>self.spec.params["max_spread"]: return None
        if m.orderbook_imbalance>=th and x.close>x.ema20 and x.rvol20>=self.spec.params["min_rvol"]:
            stop=m.price-self.spec.params["atr_stop"]*atr
            return _sig(self.spec,m,"long",0.74,stop,_tp("long",m.price,stop,(0.8,1.6,2.5)),"Bid-side depth imbalance aligned with short-term momentum",{"book_imbalance":m.orderbook_imbalance,"spread":spread},1.0)
        if m.orderbook_imbalance<=-th and x.close<x.ema20 and x.rvol20>=self.spec.params["min_rvol"]:
            stop=m.price+self.spec.params["atr_stop"]*atr
            return _sig(self.spec,m,"short",0.74,stop,_tp("short",m.price,stop,(0.8,1.6,2.5)),"Ask-side depth imbalance aligned with short-term momentum",{"book_imbalance":m.orderbook_imbalance,"spread":spread},1.0)


class LiquidationMagnet(BaseStrategy):
    needs_liquidation=True
    def evaluate(self,m):
        if not (m.liquidation_above or m.liquidation_below): return None
        d=m.candles_15m; x=d.iloc[-1]; atr=safe(x.atr14)
        a=m.liquidation_above_strength; b=m.liquidation_below_strength
        if m.liquidation_above and a>b*self.spec.params["strength_ratio"] and m.liquidation_above>m.price and x.close>x.ema20 and x.rsi14<self.spec.params["max_rsi"]:
            stop=min(d.low.iloc[-8:].min(),m.price-1.4*atr)
            t1=min(m.liquidation_above,m.price+1.5*abs(m.price-stop)); t2=m.liquidation_above; t3=m.price+3*abs(m.price-stop)
            return _sig(self.spec,m,"long",0.77,stop,[t1,t2,t3],"Dominant CoinGlass liquidation cluster above price with bullish structure",{"liq_strength_ratio":a/max(b,1)},1.5)
        if m.liquidation_below and b>a*self.spec.params["strength_ratio"] and m.liquidation_below<m.price and x.close<x.ema20 and x.rsi14>100-self.spec.params["max_rsi"]:
            stop=max(d.high.iloc[-8:].max(),m.price+1.4*atr)
            t1=max(m.liquidation_below,m.price-1.5*abs(m.price-stop)); t2=m.liquidation_below; t3=m.price-3*abs(m.price-stop)
            return _sig(self.spec,m,"short",0.77,stop,[t1,t2,t3],"Dominant CoinGlass liquidation cluster below price with bearish structure",{"liq_strength_ratio":b/max(a,1)},1.5)


SPECS = [
 StrategySpec("smc_liquidity","SMC 流動性反轉","EARLY",0.015,"Sweep + CHoCH/BOS + structure stop",{"min_rvol":1.15,"atr_buffer":0.25}),
 StrategySpec("oi_trend","OI 趨勢擴張","EARLY",0.015,"Breakout + OI expansion + volume + ADX",{"min_oi_change":0.008,"min_rvol":1.5,"min_adx":20,"atr_stop":1.5}),
 StrategySpec("funding_squeeze","Funding 擠壓反轉","EARLY",0.012,"Crowding reversal using funding/OI/exhaustion",{"funding_extreme":0.0005,"rsi_extreme":72}),
 StrategySpec("volume_breakout","量能壓縮突破","EARLY",0.015,"Compression breakout with abnormal volume",{"min_rvol":1.8,"compression_q":0.35,"atr_stop":1.4}),
 StrategySpec("fib_pullback","Fibonacci 回踩","EARLY",0.012,"1H impulse + 0.50-0.618 retracement confirmation",{"lookback":60,"zone_tolerance":0.35,"min_rvol":1.05}),
 StrategySpec("vwap_ema","VWAP / EMA 動能","EARLY",0.012,"Trend continuation after VWAP retest",{"rsi_long":52,"atr_stop":1.35}),
 StrategySpec("mean_reversion","區間均值回歸","EARLY",0.010,"Low-ADX z-score/RSI mean reversion",{"max_adx":19,"z":2.0,"rsi_low":30,"atr_stop":1.6}),
 StrategySpec("orderbook","訂單簿失衡","EARLY",0.010,"Depth imbalance + spread + momentum filter",{"imbalance":0.22,"max_spread":0.0012,"min_rvol":1.1,"atr_stop":1.0}),
 StrategySpec("liquidation_magnet","CoinGlass 清算磁鐵","EARLY",0.010,"Trade toward dominant liquidation cluster with structure filter",{"strength_ratio":1.5,"max_rsi":68}),
]

STRATEGY_CLASSES=[SMCLiquidity,OITrendExpansion,FundingSqueeze,VolumeBreakout,FibonacciPullback,VWAPEMAMomentum,MeanReversion,OrderbookImbalance,LiquidationMagnet]

def build_strategies(): return [cls(spec) for cls,spec in zip(STRATEGY_CLASSES,SPECS)]
