from __future__ import annotations
from dataclasses import replace
from .models import Signal,StrategySpec
from .indicators import fib_context,safe

COMMON={"risk_pct":.015,"leverage":2.5,"max_positions":4,"max_position_notional_pct":.24,"max_symbol_exposure_pct":.24,"max_total_exposure_pct":1.10,"tp1_r":.9,"tp2_r":1.8,"tp3_r":3.0,"tp1_fraction":.28,"tp2_fraction":.32,"trail_r":1.25}
def P(**x):p=dict(COMMON);p.update(x);return p

def macro_nudge(m,side):
    # BTC is deliberately a tiny score nudge, never a hard gate.
    if m.macro_regime=="HIGH_VOL":return -.01
    if m.macro_regime=="BULL" and side=="long":return .015
    if m.macro_regime=="BEAR" and side=="short":return .015
    if m.macro_regime=="BULL" and side=="short":return -.01
    if m.macro_regime=="BEAR" and side=="long":return -.01
    return 0.

def sig(spec,m,side,score,stop,reason,tf=None,extra=None):
    tf=tf or spec.signal_tf;x=m.last(tf);f={"rvol":safe(x.rvol20),"adx":safe(x.adx14),"rsi":safe(x.rsi14),"oi_change_30m":m.oi_change_30m,"funding":m.funding,"local_regime":m.regime,"btc_macro":m.macro_regime,"signal_tf":tf,"bar_ts":int(x.ts)}
    if extra:f.update(extra)
    return Signal(spec.name,m.symbol,side,max(0,min(1,score+macro_nudge(m,side))),m.price,float(stop),reason,f,tf,int(x.ts))

class BaseStrategy:
    needs_depth=False;needs_liquidation=False;needs_cg_oi=False
    def __init__(self,spec):self.spec=spec
    def evaluate(self,m):raise NotImplementedError

class SMCLiquidity(BaseStrategy):
    def evaluate(self,m):
        d=m.df("15m");h=m.df("1H");x=d.iloc[-1];p=self.spec.params;atr=safe(x.atr14);hi=safe(d.high.iloc[-24:-2].max());lo=safe(d.low.iloc[-24:-2].min());htrend=1 if h.iloc[-1].ema20>h.iloc[-1].ema50 else -1
        if x.low<lo and x.close>lo and x.close>x.open and x.rvol20>=p["min_rvol"]:
            return sig(self.spec,m,"long",.76+(.03 if htrend>0 else 0),min(float(x.low),lo)-p["atr_buffer"]*atr,"15m closed liquidity sweep + reclaim; 1H structure only soft-confirmed",extra={"swing":lo})
        if x.high>hi and x.close<hi and x.close<x.open and x.rvol20>=p["min_rvol"]:
            return sig(self.spec,m,"short",.76+(.03 if htrend<0 else 0),max(float(x.high),hi)+p["atr_buffer"]*atr,"15m closed liquidity sweep + bearish reclaim; 1H structure only soft-confirmed",extra={"swing":hi})

class OITrendExpansion(BaseStrategy):
    needs_cg_oi=True
    def evaluate(self,m):
        d=m.df("15m");h=m.df("1H");x=d.iloc[-1];p=self.spec.params;atr=safe(x.atr14);oi=max(m.oi_change_30m,(m.cg_oi_change_30m or -999)/100);up=h.iloc[-1].ema20>=h.iloc[-1].ema50
        if x.close>x.hh20 and oi>=p["min_oi_change"] and x.rvol20>=p["min_rvol"] and x.adx14>=p["min_adx"]:return sig(self.spec,m,"long",.77+(.03 if up else 0),m.price-p["atr_stop"]*atr,"15m closed breakout + OI expansion + RVOL + ADX",extra={"effective_oi":oi})
        if x.close<x.ll20 and oi>=p["min_oi_change"] and x.rvol20>=p["min_rvol"] and x.adx14>=p["min_adx"]:return sig(self.spec,m,"short",.77+(.03 if not up else 0),m.price+p["atr_stop"]*atr,"15m closed downside breakout + OI expansion + RVOL + ADX",extra={"effective_oi":oi})

class FundingSqueeze(BaseStrategy):
    def evaluate(self,m):
        d=m.df("15m");h=m.df("1H");x=d.iloc[-1];p=self.spec.params;atr=safe(x.atr14);stretch=abs(safe(x.close/h.iloc[-1].ema20-1))
        if m.funding>=p["funding_extreme"] and x.rsi14>=p["rsi_extreme"] and x.close<x.open and m.oi_change_30m>=p["min_oi_change"]:return sig(self.spec,m,"short",.72+min(.05,stretch*3),m.price+p["atr_stop"]*atr,"Closed 15m exhaustion against crowded positive funding/OI")
        if m.funding<=-p["funding_extreme"] and x.rsi14<=100-p["rsi_extreme"] and x.close>x.open and m.oi_change_30m>=p["min_oi_change"]:return sig(self.spec,m,"long",.72+min(.05,stretch*3),m.price-p["atr_stop"]*atr,"Closed 15m exhaustion against crowded negative funding/OI")

class VolumeBreakout(BaseStrategy):
    def evaluate(self,m):
        d=m.df("15m");h=m.df("1H");x=d.iloc[-1];p=self.spec.params;atr=safe(x.atr14);med=safe(d.range20.iloc[-90:-2].median(),1);compressed=safe(d.range20.iloc[-2])<=med*p["compression_ratio"]
        if compressed and x.close>x.hh20 and x.rvol20>=p["min_rvol"]:return sig(self.spec,m,"long",.75+(.025 if h.iloc[-1].ema20>=h.iloc[-1].ema50 else 0),m.price-p["atr_stop"]*atr,"Closed 15m compression release with abnormal volume")
        if compressed and x.close<x.ll20 and x.rvol20>=p["min_rvol"]:return sig(self.spec,m,"short",.75+(.025 if h.iloc[-1].ema20<=h.iloc[-1].ema50 else 0),m.price+p["atr_stop"]*atr,"Closed 15m compression release downward with abnormal volume")

class FibonacciPullback(BaseStrategy):
    def evaluate(self,m):
        d=m.df("15m");h=m.df("1H");h4=m.df("4H");x=d.iloc[-1];p=self.spec.params;f=fib_context(h,int(p["lookback"]));atr=safe(x.atr14);tol=p["zone_tolerance"]*atr;bias=1 if h4.iloc[-1].ema20>=h4.iloc[-1].ema50 else -1
        if f["dir"]==1 and h.iloc[-1].ema20>h.iloc[-1].ema50 and f["fib618"]-tol<=x.close<=f["fib50"]+tol and x.close>x.open and x.rvol20>=p["min_rvol"]:return sig(self.spec,m,"long",.77+(.02 if bias>0 else 0),min(f["fib786"]-p["stop_buffer_atr"]*atr,float(d.low.iloc[-7:].min())),"1H Fibonacci pullback + closed 15m bullish confirmation; 4H is soft bias",extra={"fib_target":f["hi"]})
        if f["dir"]==-1 and h.iloc[-1].ema20<h.iloc[-1].ema50 and f["fib50"]-tol<=x.close<=f["fib618"]+tol and x.close<x.open and x.rvol20>=p["min_rvol"]:return sig(self.spec,m,"short",.77+(.02 if bias<0 else 0),max(f["fib786"]+p["stop_buffer_atr"]*atr,float(d.high.iloc[-7:].max())),"1H Fibonacci pullback + closed 15m bearish confirmation; 4H is soft bias",extra={"fib_target":f["lo"]})

class VWAPEMAMomentum(BaseStrategy):
    def evaluate(self,m):
        d=m.df("5m");m15=m.df("15m");h=m.df("1H");x=d.iloc[-1];q=d.iloc[-2];p=self.spec.params;atr=safe(x.atr14);trend_up=h.iloc[-1].ema20>h.iloc[-1].ema50 and m15.iloc[-1].ema20>=m15.iloc[-1].ema50
        if trend_up and x.ema20>x.ema50 and q.low<=q.vwap and x.close>x.vwap and x.rsi14>=p["rsi_long"]:return sig(self.spec,m,"long",.74,min(float(d.low.iloc[-10:].min()),m.price-p["atr_stop"]*atr),"Closed 5m VWAP reclaim in 15m/1H aligned trend","5m")
        if (not trend_up) and h.iloc[-1].ema20<h.iloc[-1].ema50 and m15.iloc[-1].ema20<=m15.iloc[-1].ema50 and x.ema20<x.ema50 and q.high>=q.vwap and x.close<x.vwap and x.rsi14<=100-p["rsi_long"]:return sig(self.spec,m,"short",.74,max(float(d.high.iloc[-10:].max()),m.price+p["atr_stop"]*atr),"Closed 5m VWAP rejection in 15m/1H aligned downtrend","5m")

class MeanReversion(BaseStrategy):
    def evaluate(self,m):
        d=m.df("5m");m15=m.df("15m");x=d.iloc[-1];c=m15.iloc[-1];p=self.spec.params;atr=safe(x.atr14);range_ok=c.adx14<=p["max_context_adx"] or m.regime in {"RANGE","NEUTRAL"}
        if range_ok and x.adx14<=p["max_adx"] and x.z20<=-p["z"] and x.rsi14<=p["rsi_low"]:return sig(self.spec,m,"long",.69,m.price-p["atr_stop"]*atr,"Closed 5m statistical downside stretch inside non-trending 15m context","5m")
        if range_ok and x.adx14<=p["max_adx"] and x.z20>=p["z"] and x.rsi14>=100-p["rsi_low"]:return sig(self.spec,m,"short",.69,m.price+p["atr_stop"]*atr,"Closed 5m statistical upside stretch inside non-trending 15m context","5m")

class OrderbookImbalance(BaseStrategy):
    needs_depth=True
    def evaluate(self,m):
        if m.orderbook_imbalance is None:return None
        d=m.df("5m");x=d.iloc[-1];p=self.spec.params;atr=safe(x.atr14);spread=(m.ask-m.bid)/max(m.price,1e-12)
        if spread>p["max_spread"]:return None
        if m.orderbook_imbalance>=p["imbalance"] and x.close>x.ema20 and x.rvol20>=p["min_rvol"]:return sig(self.spec,m,"long",.70,m.price-p["atr_stop"]*atr,"Current L2 bid imbalance confirmed by fully closed 5m momentum","5m",{"book_imbalance":m.orderbook_imbalance,"spread":spread})
        if m.orderbook_imbalance<=-p["imbalance"] and x.close<x.ema20 and x.rvol20>=p["min_rvol"]:return sig(self.spec,m,"short",.70,m.price+p["atr_stop"]*atr,"Current L2 ask imbalance confirmed by fully closed 5m momentum","5m",{"book_imbalance":m.orderbook_imbalance,"spread":spread})

class LiquidationMagnet(BaseStrategy):
    needs_liquidation=True
    def evaluate(self,m):
        if not (m.liquidation_above or m.liquidation_below):return None
        d=m.df("15m");h=m.df("1H");x=d.iloc[-1];p=self.spec.params;atr=safe(x.atr14);a=m.liquidation_above_strength;b=m.liquidation_below_strength
        if m.liquidation_above and a>b*p["strength_ratio"] and x.close>x.ema20 and x.rsi14<p["max_rsi"]:return sig(self.spec,m,"long",.73+(.02 if h.iloc[-1].ema20>=h.iloc[-1].ema50 else 0),min(float(d.low.iloc[-8:].min()),m.price-p["atr_stop"]*atr),"CoinGlass liquidation cluster above + closed 15m bullish structure",extra={"liq_target":m.liquidation_above,"liq_ratio":a/max(b,1)})
        if m.liquidation_below and b>a*p["strength_ratio"] and x.close<x.ema20 and x.rsi14>100-p["max_rsi"]:return sig(self.spec,m,"short",.73+(.02 if h.iloc[-1].ema20<=h.iloc[-1].ema50 else 0),max(float(d.high.iloc[-8:].max()),m.price+p["atr_stop"]*atr),"CoinGlass liquidation cluster below + closed 15m bearish structure",extra={"liq_target":m.liquidation_below,"liq_ratio":b/max(a,1)})

SPECS=[
 StrategySpec("smc_liquidity","SMC 流動性反轉","15m liquidity sweep/reclaim + 1H structure",P(min_rvol=.95,atr_buffer=.28,tp3_r=3.5),"15m",("15m","1H"),.60,.68,.72),
 StrategySpec("oi_trend","OI 趨勢擴張","15m breakout + OI/RVOL/ADX + soft 1H trend",P(min_oi_change=.004,min_rvol=1.15,min_adx=16,atr_stop=1.45,risk_pct=.017),"15m",("15m","1H"),.61,.68,.73),
 StrategySpec("funding_squeeze","Funding 擠壓反轉","15m exhaustion + funding/OI crowding + 1H stretch",P(funding_extreme=.00025,min_oi_change=.0015,rsi_extreme=67,atr_stop=1.45,risk_pct=.014,tp1_r=.75,tp2_r=1.5,tp3_r=2.6),"15m",("15m","1H"),.60,.67,.72),
 StrategySpec("volume_breakout","量能壓縮突破","15m compression + relative-volume breakout",P(min_rvol=1.25,compression_ratio=.88,atr_stop=1.35,risk_pct=.017),"15m",("15m","1H"),.61,.68,.73),
 StrategySpec("fib_pullback","Fibonacci 回踩","1H impulse/Fibonacci + 15m confirmation + soft 4H bias",P(lookback=55,zone_tolerance=.48,min_rvol=.90,stop_buffer_atr=.18,risk_pct=.015,tp1_r=.9,tp2_r=2.0,tp3_r=3.6),"15m",("15m","1H","4H"),.61,.68,.73),
 StrategySpec("vwap_ema","VWAP / EMA 動能","5m VWAP retest + 15m/1H trend alignment",P(rsi_long=49,atr_stop=1.30,risk_pct=.015),"5m",("5m","15m","1H"),.60,.67,.72),
 StrategySpec("mean_reversion","區間均值回歸","5m z-score/RSI + 15m range context",P(max_adx=22,max_context_adx=22,z=1.65,rsi_low=35,atr_stop=1.45,risk_pct=.012,leverage=2,tp1_r=.7,tp2_r=1.35,tp3_r=2.2),"5m",("5m","15m"),.59,.66,.71),
 StrategySpec("orderbook","訂單簿失衡","Current L2 imbalance + fully closed 5m confirmation",P(imbalance=.14,max_spread=.0015,min_rvol=.85,atr_stop=.95,risk_pct=.011,leverage=2,tp1_r=.7,tp2_r=1.4,tp3_r=2.4),"5m",("5m",),.58,.66,.71),
 StrategySpec("liquidation_magnet","CoinGlass 清算磁鐵","Liquidation heatmap + closed 15m structure + soft 1H trend",P(strength_ratio=1.20,max_rsi=72,atr_stop=1.35,risk_pct=.013,tp1_r=.9,tp2_r=1.7,tp3_r=2.9),"15m",("15m","1H"),.60,.67,.72),
]
CLASSES=[SMCLiquidity,OITrendExpansion,FundingSqueeze,VolumeBreakout,FibonacciPullback,VWAPEMAMomentum,MeanReversion,OrderbookImbalance,LiquidationMagnet]
SPEC_MAP={s.name:s for s in SPECS};CLASS_MAP={s.name:c for s,c in zip(SPECS,CLASSES)}
def build_strategy(name,params):return CLASS_MAP[name](replace(SPEC_MAP[name],params=params))
def min_score(name,stage):
    s=SPEC_MAP[name];return s.min_score_early if stage=="EARLY" else s.min_score_tuning if stage=="TUNING" else s.min_score_final
