from __future__ import annotations
from dataclasses import replace
from .models import Signal, StrategySpec
from .indicators import fib_context, safe

COMMON={"risk_pct":.015,"leverage":2.5,"max_positions":4,"max_position_notional_pct":.24,"max_symbol_exposure_pct":.24,"max_total_exposure_pct":1.10,"sl1_r":.62,"sl1_fraction":.18,"tp1_r":.80,"tp2_r":1.60,"tp3_r":2.80,"tp1_fraction":.25,"tp2_fraction":.30,"breakeven_trigger_r":.85,"breakeven_offset_r":.04,"trail_start_r":1.35,"trail_r":1.00}
def P(**x):p=dict(COMMON);p.update(x);return p

def macro_nudge(m,side):
    if m.macro_regime=="HIGH_VOL":return -.01
    if m.macro_regime=="BULL" and side=="long":return .012
    if m.macro_regime=="BEAR" and side=="short":return .012
    if m.macro_regime=="BULL" and side=="short":return -.008
    if m.macro_regime=="BEAR" and side=="long":return -.008
    return 0.

def sig(spec,m,side,score,stop,reason,tf=None,extra=None):
    tf=tf or spec.signal_tf;x=m.last(tf);f={"rvol":safe(x.rvol20),"adx":safe(x.adx14),"rsi":safe(x.rsi14),"oi_change_30m":m.oi_change_30m,"funding":m.funding,"local_regime":m.regime,"btc_macro":m.macro_regime,"signal_tf":tf,"bar_ts":int(x.ts),"change24h":m.change24h,"gainer_rank":m.gainer_rank,"loser_rank":m.loser_rank}
    if extra:f.update(extra)
    return Signal(spec.name,m.symbol,side,max(0,min(1,score+macro_nudge(m,side))),m.price,float(stop),reason,f,tf,int(x.ts))

class BaseStrategy:
    needs_depth=False;needs_liquidation=False;needs_cg_oi=False
    def __init__(self,spec):self.spec=spec
    def evaluate(self,m):raise NotImplementedError

class SMCLiquidity(BaseStrategy):
    def evaluate(self,m):
        d=m.df("15m");h=m.df("1H");x=d.iloc[-1];p=self.spec.params;atr=safe(x.atr14);lb=int(p["sweep_lookback"]);hi=safe(d.high.iloc[-lb:-2].max());lo=safe(d.low.iloc[-lb:-2].min());body=abs(x.close-x.open)/max(x.high-x.low,1e-12);ht=1 if h.iloc[-1].ema20>h.iloc[-1].ema50 else -1
        if x.low<lo and x.close>lo and x.close>x.open and x.rvol20>=p["min_rvol"] and body>=p["min_reclaim_body"]:return sig(self.spec,m,"long",.74+(.03 if ht>0 else 0),min(float(x.low),lo)-p["atr_buffer"]*atr,"Closed 15m liquidity sweep + reclaim",extra={"swing":lo})
        if x.high>hi and x.close<hi and x.close<x.open and x.rvol20>=p["min_rvol"] and body>=p["min_reclaim_body"]:return sig(self.spec,m,"short",.74+(.03 if ht<0 else 0),max(float(x.high),hi)+p["atr_buffer"]*atr,"Closed 15m liquidity sweep + bearish reclaim",extra={"swing":hi})

class OITrendExpansion(BaseStrategy):
    needs_cg_oi=True
    def evaluate(self,m):
        d=m.df("15m");h=m.df("1H");x=d.iloc[-1];p=self.spec.params;atr=safe(x.atr14);lb=int(p["breakout_lookback"]);hi=safe(d.high.shift(1).rolling(lb).max().iloc[-1]);lo=safe(d.low.shift(1).rolling(lb).min().iloc[-1]);oi=max(m.oi_change_30m,(m.cg_oi_change_30m or -999)/100);up=h.iloc[-1].ema20>=h.iloc[-1].ema50;ext=abs(x.close-x.ema20)/max(atr,1e-12)
        if x.close>hi and oi>=p["min_oi_change"] and x.rvol20>=p["min_rvol"] and x.adx14>=p["min_adx"] and ext<=p["max_extension_atr"]:return sig(self.spec,m,"long",.75+(.03 if up else 0),m.price-p["atr_stop"]*atr,"Closed 15m breakout + OI expansion + RVOL/ADX",extra={"effective_oi":oi})
        if x.close<lo and oi>=p["min_oi_change"] and x.rvol20>=p["min_rvol"] and x.adx14>=p["min_adx"] and ext<=p["max_extension_atr"]:return sig(self.spec,m,"short",.75+(.03 if not up else 0),m.price+p["atr_stop"]*atr,"Closed 15m downside breakout + OI expansion + RVOL/ADX",extra={"effective_oi":oi})

class FundingSqueeze(BaseStrategy):
    def evaluate(self,m):
        d=m.df("15m");h=m.df("1H");x=d.iloc[-1];p=self.spec.params;atr=safe(x.atr14);stretch=abs(safe(x.close/h.iloc[-1].ema20-1));body=abs(x.close-x.open)/max(x.high-x.low,1e-12)
        if m.funding>=p["funding_extreme"] and x.rsi14>=p["rsi_extreme"] and x.close<x.open and body>=p["min_reversal_body"] and m.oi_change_30m>=p["min_oi_change"]:return sig(self.spec,m,"short",.71+min(.05,stretch*3),m.price+p["atr_stop"]*atr,"Closed 15m exhaustion vs crowded positive funding/OI")
        if m.funding<=-p["funding_extreme"] and x.rsi14<=100-p["rsi_extreme"] and x.close>x.open and body>=p["min_reversal_body"] and m.oi_change_30m>=p["min_oi_change"]:return sig(self.spec,m,"long",.71+min(.05,stretch*3),m.price-p["atr_stop"]*atr,"Closed 15m exhaustion vs crowded negative funding/OI")

class VolumeBreakout(BaseStrategy):
    def evaluate(self,m):
        d=m.df("15m");h=m.df("1H");x=d.iloc[-1];p=self.spec.params;atr=safe(x.atr14);cl=int(p["compression_lookback"]);bl=int(p["breakout_lookback"]);med=safe(d.range20.iloc[-max(90,cl*3):-2].median(),1);compressed=safe(d.range20.iloc[-2])<=med*p["compression_ratio"];hi=safe(d.high.shift(1).rolling(bl).max().iloc[-1]);lo=safe(d.low.shift(1).rolling(bl).min().iloc[-1])
        if compressed and x.close>hi and x.rvol20>=p["min_rvol"]:return sig(self.spec,m,"long",.73+(.025 if h.iloc[-1].ema20>=h.iloc[-1].ema50 else 0),m.price-p["atr_stop"]*atr,"Closed 15m compression release + abnormal volume")
        if compressed and x.close<lo and x.rvol20>=p["min_rvol"]:return sig(self.spec,m,"short",.73+(.025 if h.iloc[-1].ema20<=h.iloc[-1].ema50 else 0),m.price+p["atr_stop"]*atr,"Closed 15m compression release downward + abnormal volume")

class FibonacciPullback(BaseStrategy):
    def evaluate(self,m):
        d=m.df("15m");h=m.df("1H");h4=m.df("4H");x=d.iloc[-1];p=self.spec.params;f=fib_context(h,int(p["lookback"]));atr=safe(x.atr14);tol=p["zone_tolerance"]*atr;bias=1 if h4.iloc[-1].ema20>=h4.iloc[-1].ema50 else -1;body=abs(x.close-x.open)/max(x.high-x.low,1e-12)
        if f["dir"]==1 and h.iloc[-1].ema20>h.iloc[-1].ema50 and f["fib618"]-tol<=x.close<=f["fib50"]+tol and x.close>x.open and body>=p["confirmation_body"] and x.rvol20>=p["min_rvol"]:return sig(self.spec,m,"long",.75+(.02 if bias>0 else 0),min(f["fib786"]-p["stop_buffer_atr"]*atr,float(d.low.iloc[-7:].min())),"1H Fibonacci pullback + closed 15m confirmation",extra={"fib_target":f["hi"]})
        if f["dir"]==-1 and h.iloc[-1].ema20<h.iloc[-1].ema50 and f["fib50"]-tol<=x.close<=f["fib618"]+tol and x.close<x.open and body>=p["confirmation_body"] and x.rvol20>=p["min_rvol"]:return sig(self.spec,m,"short",.75+(.02 if bias<0 else 0),max(f["fib786"]+p["stop_buffer_atr"]*atr,float(d.high.iloc[-7:].max())),"1H Fibonacci pullback + closed 15m bearish confirmation",extra={"fib_target":f["lo"]})

class VWAPEMAMomentum(BaseStrategy):
    def evaluate(self,m):
        d=m.df("5m");m15=m.df("15m");h=m.df("1H");x=d.iloc[-1];p=self.spec.params;atr=safe(x.atr14);lb=int(p["retest_lookback"]);recent=d.iloc[-lb:-1];trend_up=h.iloc[-1].ema20>h.iloc[-1].ema50 and m15.iloc[-1].ema20>=m15.iloc[-1].ema50;touched=recent.low.min()<=recent.vwap.max() if len(recent) else False;touched_s=recent.high.max()>=recent.vwap.min() if len(recent) else False
        if trend_up and x.ema20>x.ema50 and touched and x.close>x.vwap and x.rsi14>=p["rsi_long"]:return sig(self.spec,m,"long",.72,min(float(d.low.iloc[-10:].min()),m.price-p["atr_stop"]*atr),"Closed 5m VWAP reclaim in aligned 15m/1H trend","5m")
        if (not trend_up) and h.iloc[-1].ema20<h.iloc[-1].ema50 and m15.iloc[-1].ema20<=m15.iloc[-1].ema50 and x.ema20<x.ema50 and touched_s and x.close<x.vwap and x.rsi14<=100-p["rsi_long"]:return sig(self.spec,m,"short",.72,max(float(d.high.iloc[-10:].max()),m.price+p["atr_stop"]*atr),"Closed 5m VWAP rejection in aligned downtrend","5m")

class MeanReversion(BaseStrategy):
    def evaluate(self,m):
        d=m.df("5m");m15=m.df("15m");x=d.iloc[-1];c=m15.iloc[-1];p=self.spec.params;atr=safe(x.atr14);range_ok=c.adx14<=p["max_context_adx"] or m.regime in {"RANGE","NEUTRAL"}
        if range_ok and x.adx14<=p["max_adx"] and x.z20<=-p["z"] and x.rsi14<=p["rsi_low"]:return sig(self.spec,m,"long",.68,m.price-p["atr_stop"]*atr,"Closed 5m downside statistical stretch in non-trending context","5m")
        if range_ok and x.adx14<=p["max_adx"] and x.z20>=p["z"] and x.rsi14>=100-p["rsi_low"]:return sig(self.spec,m,"short",.68,m.price+p["atr_stop"]*atr,"Closed 5m upside statistical stretch in non-trending context","5m")

class OrderbookImbalance(BaseStrategy):
    needs_depth=True
    def evaluate(self,m):
        if m.orderbook_imbalance is None:return None
        d=m.df("5m");x=d.iloc[-1];p=self.spec.params;atr=safe(x.atr14);spread=(m.ask-m.bid)/max(m.price,1e-12)
        if spread>p["max_spread"]:return None
        if m.orderbook_imbalance>=p["imbalance"] and x.close>x.ema20 and x.rvol20>=p["min_rvol"]:return sig(self.spec,m,"long",.69,m.price-p["atr_stop"]*atr,"L2 bid imbalance + closed 5m momentum","5m",{"book_imbalance":m.orderbook_imbalance,"spread":spread})
        if m.orderbook_imbalance<=-p["imbalance"] and x.close<x.ema20 and x.rvol20>=p["min_rvol"]:return sig(self.spec,m,"short",.69,m.price+p["atr_stop"]*atr,"L2 ask imbalance + closed 5m momentum","5m",{"book_imbalance":m.orderbook_imbalance,"spread":spread})

class LiquidationMagnet(BaseStrategy):
    needs_liquidation=True
    def evaluate(self,m):
        d=m.df("15m");h=m.df("1H");x=d.iloc[-1];p=self.spec.params;atr=safe(x.atr14)
        if m.liquidation_mode=="HEATMAP" and (m.liquidation_above or m.liquidation_below):
            a=m.liquidation_above_strength;b=m.liquidation_below_strength;da=(m.liquidation_above-m.price)/max(atr,1e-12) if m.liquidation_above else 999;dd=(m.price-m.liquidation_below)/max(atr,1e-12) if m.liquidation_below else 999
            if m.liquidation_above and p["min_cluster_atr"]<=da<=p["max_cluster_atr"] and a>b*p["strength_ratio"] and x.close>x.ema20 and x.rsi14<p["max_rsi"]:return sig(self.spec,m,"long",.72+(.02 if h.iloc[-1].ema20>=h.iloc[-1].ema50 else 0),min(float(d.low.iloc[-8:].min()),m.price-p["atr_stop"]*atr),"CoinGlass heatmap cluster above + closed 15m structure",extra={"liq_source":"heatmap","liq_target":m.liquidation_above,"liq_ratio":a/max(b,1),"liq_distance_atr":da})
            if m.liquidation_below and p["min_cluster_atr"]<=dd<=p["max_cluster_atr"] and b>a*p["strength_ratio"] and x.close<x.ema20 and x.rsi14>100-p["max_rsi"]:return sig(self.spec,m,"short",.72+(.02 if h.iloc[-1].ema20<=h.iloc[-1].ema50 else 0),max(float(d.high.iloc[-8:].max()),m.price+p["atr_stop"]*atr),"CoinGlass heatmap cluster below + closed 15m structure",extra={"liq_source":"heatmap","liq_target":m.liquidation_below,"liq_ratio":b/max(a,1),"liq_distance_atr":dd})
        if m.liquidation_mode=="FLOW_4H":
            l=max(m.liquidation_long_usd,0);sh=max(m.liquidation_short_usd,0);sp=m.liquidation_spike_ratio
            if l>sh*p["strength_ratio"] and sp>=p["min_liq_spike"] and x.close>x.open and x.close>x.ema20 and x.rsi14<p["max_rsi"]:return sig(self.spec,m,"long",.70+min(.05,(sp-1)*.02),min(float(d.low.iloc[-8:].min()),m.price-p["atr_stop"]*atr),"CoinGlass 4H long-liquidation flush + closed 15m reclaim",extra={"liq_source":"history4h","long_liq":l,"short_liq":sh,"liq_spike":sp})
            if sh>l*p["strength_ratio"] and sp>=p["min_liq_spike"] and x.close<x.open and x.close<x.ema20 and x.rsi14>100-p["max_rsi"]:return sig(self.spec,m,"short",.70+min(.05,(sp-1)*.02),max(float(d.high.iloc[-8:].max()),m.price+p["atr_stop"]*atr),"CoinGlass 4H short-liquidation squeeze + closed 15m rejection",extra={"liq_source":"history4h","long_liq":l,"short_liq":sh,"liq_spike":sp})

class LoserReboundCycle(BaseStrategy):
    def evaluate(self,m):
        p=self.spec.params
        if m.loser_rank<=0 or m.loser_rank>int(p["rank_max"]) or m.change24h>-p["min_drop_pct"]:return None
        d=m.df("15m");h=m.df("1H");x=d.iloc[-1];q=d.iloc[-2];atr=safe(x.atr14);base=d.iloc[-int(p["base_lookback"]):];stopped=x.low>base.low.iloc[:-1].min() and x.close>x.open and x.close>x.ema20 and q.close<=q.ema20 and x.rvol20>=p["min_rvol"] and x.rsi14>=p["rebound_rsi"]
        if stopped:return sig(self.spec,m,"long",.73,min(float(base.low.min()),m.price-p["atr_stop"]*atr),"Extreme loser: closed 15m base + EMA20 reclaim for rebound long",extra={"rank_mode":"LOSER_REBOUND_LONG"})
        trend_down=h.iloc[-1].ema20<h.iloc[-1].ema50 and d.iloc[-1].ema20<d.iloc[-1].ema50;rejection=q.close>q.ema20 and x.close<x.ema20 and x.close<x.open and x.rsi14>=p["rejection_rsi"] and x.rvol20>=p["continuation_rvol"]
        if trend_down and rejection:return sig(self.spec,m,"short",.75,max(float(d.high.iloc[-8:].max()),m.price+p["atr_stop"]*atr),"Extreme loser: rebound rejected; resume dominant downtrend",extra={"rank_mode":"LOSER_CONTINUATION_SHORT"})

class GainerPullbackCycle(BaseStrategy):
    def evaluate(self,m):
        p=self.spec.params
        if m.gainer_rank<=0 or m.gainer_rank>int(p["rank_max"]) or m.change24h<p["min_gain_pct"]:return None
        d=m.df("15m");h=m.df("1H");x=d.iloc[-1];q=d.iloc[-2];atr=safe(x.atr14);w=d.iloc[-int(p["swing_lookback"]):];blowoff=x.high>=w.high.max() and x.close<x.open and x.rsi14>=p["exhaustion_rsi"] and x.rvol20>=p["exhaustion_rvol"] and x.close<x.ema20
        if blowoff:return sig(self.spec,m,"short",.72,max(float(x.high),m.price+p["atr_stop"]*atr),"Extreme gainer: closed 15m blow-off rejection for pullback short",extra={"rank_mode":"GAINER_PULLBACK_SHORT"})
        trend_up=h.iloc[-1].ema20>h.iloc[-1].ema50 and d.iloc[-1].ema20>d.iloc[-1].ema50;reclaimed=q.low<=q.ema20 and x.close>x.ema20 and x.close>x.open and x.rsi14>=p["continuation_rsi"] and x.rvol20>=p["continuation_rvol"]
        if trend_up and reclaimed:return sig(self.spec,m,"long",.76,min(float(d.low.iloc[-8:].min()),m.price-p["atr_stop"]*atr),"Extreme gainer: pullback held; closed 15m trend continuation long",extra={"rank_mode":"GAINER_CONTINUATION_LONG"})

class AIExtremeMoveHunter(BaseStrategy):
    def evaluate(self,m):
        from .ai_hunter import ai_hunter
        p=self.spec.params;z=ai_hunter.predict(m)
        if not z or not z.get("ready") or not z.get("side"):return None
        x=m.df("15m").iloc[-1];atr=safe(x.atr14);side=z["side"];sg=1 if side=="long" else -1
        stop=m.price-sg*p["atr_stop"]*atr;score=.58+.36*float(z.get("confidence") or 0)
        reason=f"AI extreme-tail {side.upper()} · predicted L={z['long_pred']:.4f} S={z['short_pred']:.4f} · adaptive tail threshold"
        return sig(self.spec,m,side,score,stop,reason,"15m",{"ai_tail_long":z["long_pred"],"ai_tail_short":z["short_pred"],"ai_confidence":z["confidence"],"ai_long_threshold":z["long_threshold"],"ai_short_threshold":z["short_threshold"],"ai_model_n":min(z["long_n"],z["short_n"])})

SPECS=[
 StrategySpec("smc_liquidity","SMC 流動性反轉","15m sweep/reclaim + soft 1H structure",P(min_rvol=.95,atr_buffer=.28,sweep_lookback=24,min_reclaim_body=.35,sl1_r=.72,sl1_fraction=.15,tp1_r=1.0,tp2_r=2.0,tp3_r=3.6,tp1_fraction=.24,tp2_fraction=.30,breakeven_trigger_r=1.0,breakeven_offset_r=.05,trail_start_r=1.7,trail_r=1.30),"15m",("15m","1H"),.59,.67,.72),
 StrategySpec("oi_trend","OI 趨勢擴張","15m breakout + OI/RVOL/ADX + soft 1H trend",P(min_oi_change=.004,min_rvol=1.12,min_adx=16,atr_stop=1.45,breakout_lookback=20,max_extension_atr=2.2,risk_pct=.017,sl1_r=.60,sl1_fraction=.18,tp1_r=.9,tp2_r=1.8,tp3_r=3.4,breakeven_trigger_r=.9,trail_start_r=1.4,trail_r=1.15),"15m",("15m","1H"),.59,.67,.72),
 StrategySpec("funding_squeeze","Funding 擠壓反轉","15m exhaustion + funding/OI crowding",P(funding_extreme=.00025,min_oi_change=.0015,rsi_extreme=67,min_reversal_body=.40,atr_stop=1.45,risk_pct=.014,sl1_r=.55,sl1_fraction=.22,tp1_r=.70,tp2_r=1.35,tp3_r=2.4,tp1_fraction=.30,tp2_fraction=.30,breakeven_trigger_r=.75,trail_start_r=1.2,trail_r=.90),"15m",("15m","1H"),.58,.66,.71),
 StrategySpec("volume_breakout","量能壓縮突破","15m compression + relative-volume breakout",P(min_rvol=1.22,compression_ratio=.88,compression_lookback=20,breakout_lookback=20,atr_stop=1.35,risk_pct=.017,sl1_r=.62,sl1_fraction=.18,tp1_r=.85,tp2_r=1.7,tp3_r=3.2,breakeven_trigger_r=.9,trail_start_r=1.35,trail_r=1.0),"15m",("15m","1H"),.59,.67,.72),
 StrategySpec("fib_pullback","Fibonacci 回踩","1H impulse/Fibonacci + 15m confirmation",P(lookback=55,zone_tolerance=.48,min_rvol=.88,confirmation_body=.28,stop_buffer_atr=.18,risk_pct=.015,sl1_r=.70,sl1_fraction=.15,tp1_r=.9,tp2_r=1.9,tp3_r=3.8,breakeven_trigger_r=1.0,trail_start_r=1.6,trail_r=1.25),"15m",("15m","1H","4H"),.59,.67,.72),
 StrategySpec("vwap_ema","VWAP / EMA 動能","5m VWAP retest + 15m/1H trend alignment",P(rsi_long=49,retest_lookback=4,atr_stop=1.30,risk_pct=.015,sl1_r=.58,sl1_fraction=.20,tp1_r=.70,tp2_r=1.4,tp3_r=2.6,tp1_fraction=.28,tp2_fraction=.32,breakeven_trigger_r=.75,trail_start_r=1.15,trail_r=.85),"5m",("5m","15m","1H"),.58,.66,.71),
 StrategySpec("mean_reversion","區間均值回歸","5m z-score/RSI + 15m range context",P(max_adx=22,max_context_adx=22,z=1.65,rsi_low=35,atr_stop=1.45,risk_pct=.012,leverage=2,sl1_r=.50,sl1_fraction=.25,tp1_r=.55,tp2_r=1.0,tp3_r=1.8,tp1_fraction=.34,tp2_fraction=.30,breakeven_trigger_r=.60,trail_start_r=.90,trail_r=.65),"5m",("5m","15m"),.57,.65,.70),
 StrategySpec("orderbook","訂單簿失衡","L2 imbalance + fully closed 5m confirmation",P(imbalance=.14,max_spread=.0015,min_rvol=.82,atr_stop=.95,risk_pct=.011,leverage=2,sl1_r=.45,sl1_fraction=.25,tp1_r=.50,tp2_r=.95,tp3_r=1.6,tp1_fraction=.35,tp2_fraction=.30,breakeven_trigger_r=.55,trail_start_r=.80,trail_r=.55),"5m",("5m",),.56,.64,.70),
 StrategySpec("liquidation_magnet","CoinGlass 爆倉流 / 清算磁鐵","Pro 用 heatmap；低階方案自動改用 4H 爆倉歷史 + closed 15m structure",P(strength_ratio=1.16,max_rsi=74,min_cluster_atr=.25,max_cluster_atr=7.0,min_liq_spike=1.05,atr_stop=1.35,risk_pct=.013,sl1_r=.65,sl1_fraction=.18,tp1_r=.9,tp2_r=1.65,tp3_r=3.0,breakeven_trigger_r=.9,trail_start_r=1.35,trail_r=1.0),"15m",("15m","1H"),.56,.64,.70),
 StrategySpec("loser_rebound_cycle","跌幅榜反彈循環","Top losers: rebound long → rejection short continuation",P(rank_max=18,min_drop_pct=.10,base_lookback=12,min_rvol=.95,rebound_rsi=38,rejection_rsi=44,continuation_rvol=.90,atr_stop=1.35,risk_pct=.016,sl1_r=.55,sl1_fraction=.22,tp1_r=.65,tp2_r=1.25,tp3_r=2.2,tp1_fraction=.32,tp2_fraction=.30,breakeven_trigger_r=.70,trail_start_r=1.0,trail_r=.75),"15m",("15m","1H"),.57,.65,.71),
 StrategySpec("gainer_pullback_cycle","漲幅榜回踩循環","Top gainers: exhaustion short → pullback continuation long",P(rank_max=18,min_gain_pct=.10,swing_lookback=12,exhaustion_rsi=72,exhaustion_rvol=1.25,continuation_rsi=54,continuation_rvol=.90,atr_stop=1.35,risk_pct=.016,sl1_r=.55,sl1_fraction=.22,tp1_r=.65,tp2_r=1.30,tp3_r=2.4,tp1_fraction=.30,tp2_fraction=.30,breakeven_trigger_r=.72,trail_start_r=1.05,trail_r=.78),"15m",("15m","1H"),.57,.65,.71),
 StrategySpec("ai_extreme_hunter","AI 十倍極端行情獵手","Online tail model learns precursor patterns from fully closed 15m bars; long 10x / short crash objective",P(atr_stop=1.55,risk_pct=.014,max_positions=3,sl1_r=.68,sl1_fraction=.16,tp1_r=.9,tp2_r=2.2,tp3_r=5.0,tp1_fraction=.20,tp2_fraction=.25,breakeven_trigger_r=1.0,breakeven_offset_r=.06,trail_start_r=1.8,trail_r=1.40),"15m",("15m","1H","4H"),.56,.64,.70),
]
CLASSES=[SMCLiquidity,OITrendExpansion,FundingSqueeze,VolumeBreakout,FibonacciPullback,VWAPEMAMomentum,MeanReversion,OrderbookImbalance,LiquidationMagnet,LoserReboundCycle,GainerPullbackCycle,AIExtremeMoveHunter]
SPEC_MAP={s.name:s for s in SPECS};CLASS_MAP={s.name:c for s,c in zip(SPECS,CLASSES)}
def build_strategy(name,params):return CLASS_MAP[name](replace(SPEC_MAP[name],params=params))
def min_score(name,stage):
    s=SPEC_MAP[name];return s.min_score_early if stage=="EARLY" else s.min_score_tuning if stage=="TUNING" else s.min_score_final
