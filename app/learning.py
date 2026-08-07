from __future__ import annotations

import json, math, statistics, time
from copy import deepcopy

from .config import settings
from .db import db


BOUNDS = {
 "smc_liquidity":{"min_rvol":(1.0,1.8),"atr_buffer":(0.15,0.55)},
 "oi_trend":{"min_oi_change":(0.003,0.03),"min_rvol":(1.1,2.8),"min_adx":(16,30),"atr_stop":(1.1,2.2)},
 "funding_squeeze":{"funding_extreme":(0.0002,0.002),"rsi_extreme":(66,82)},
 "volume_breakout":{"min_rvol":(1.2,3.2),"compression_q":(0.2,0.55),"atr_stop":(1.0,2.2)},
 "fib_pullback":{"zone_tolerance":(0.15,0.7),"min_rvol":(0.9,1.8)},
 "vwap_ema":{"rsi_long":(48,60),"atr_stop":(1.0,2.0)},
 "mean_reversion":{"max_adx":(14,24),"z":(1.6,2.8),"rsi_low":(22,36),"atr_stop":(1.2,2.4)},
 "orderbook":{"imbalance":(0.12,0.45),"max_spread":(0.0005,0.0025),"min_rvol":(0.8,1.8),"atr_stop":(0.7,1.6)},
 "liquidation_magnet":{"strength_ratio":(1.1,3.0),"max_rsi":(60,75)},
}



def completed_roundtrips(strategy: str, limit: int = 500):
    return db.query("""
      SELECT t.position_id, t.strategy, MIN(t.symbol) symbol, SUM(t.gross_pnl) gross_pnl, SUM(t.fees) fees, SUM(t.net_pnl) net_pnl, MAX(t.closed_at) closed_at, MIN(t.opened_at) opened_at
      FROM trades t
      WHERE t.strategy=? AND NOT EXISTS (SELECT 1 FROM positions p WHERE p.id=t.position_id)
      GROUP BY t.position_id,t.strategy ORDER BY closed_at DESC LIMIT ?
    """, (strategy, limit))[::-1]

def metrics(trades):
    if not trades: return {"n":0,"win_rate":0,"pf":0,"expectancy":0,"max_dd":0,"max_dd_pct":0,"sharpe":0,"stress_expectancy":0,"stress_pf":0}
    pnls=[float(t["net_pnl"]) for t in trades]; wins=sum(x>0 for x in pnls)
    gross_win=sum(x for x in pnls if x>0); gross_loss=-sum(x for x in pnls if x<0)
    eq=settings.initial_paper_equity; peak=eq; maxdd=0
    for x in pnls:
        eq+=x; peak=max(peak,eq); maxdd=max(maxdd,peak-eq)
    mean=statistics.fmean(pnls); sd=statistics.pstdev(pnls) if len(pnls)>1 else 0
    stress=[float(t["net_pnl"])-float(t.get("fees",0.0)) for t in trades]
    sw=sum(x for x in stress if x>0); sl=-sum(x for x in stress if x<0)
    return {"n":len(pnls),"win_rate":wins/len(pnls),"pf":gross_win/gross_loss if gross_loss else 9.99,"expectancy":mean,"max_dd":maxdd,"max_dd_pct":maxdd/max(peak,1),"sharpe":mean/sd*math.sqrt(len(pnls)) if sd else 0,"stress_expectancy":statistics.fmean(stress),"stress_pf":sw/sl if sl else 9.99}


def robust_windows(trades, windows=4):
    n=len(trades)
    if n<40: return 0
    size=max(10,n//windows); positives=0; used=0
    for i in range(0,n,size):
        chunk=trades[i:i+size]
        if len(chunk)<8: continue
        used+=1
        if metrics(chunk)["expectancy"]>0 and metrics(chunk)["pf"]>1: positives+=1
    return positives/used if used else 0


def profit_concentration(trades):
    by={}
    for t in trades:
        pnl=float(t["net_pnl"]); sym=t.get("symbol") or "?"
        if pnl>0: by[sym]=by.get(sym,0.0)+pnl
    total=sum(by.values())
    return max(by.values(), default=0.0)/total if total>0 else 1.0

def sample_days(trades):
    if not trades: return 0.0
    return (max(t["closed_at"] for t in trades)-min(t["opened_at"] for t in trades))/86400000


class LearningGovernor:
    """Conservative online governor: bounded, auditable, one-parameter-at-a-time changes only."""
    def __init__(self): self.last_run=0

    def due(self): return settings.learning_enabled and time.time()-self.last_run>=settings.learning_interval_hours*3600

    def run(self):
        if not self.due(): return []
        self.last_run=time.time(); out=[]
        states=db.query("SELECT * FROM strategy_state")
        for s in states:
            trades=completed_roundtrips(s["strategy"], settings.learning_recent_window)
            m=metrics(trades)
            if m["n"]<settings.learning_min_trades:
                continue
            params=json.loads(s["params_json"]); before=deepcopy(params); bounds=BOUNDS.get(s["strategy"],{})
            # Diagnose recent failure shape. Tighten signal selectivity if expectancy/PF are poor;
            # relax minimally only if robustly profitable but too sparse. Never alter >1 parameter per cycle.
            key=None; direction=0; why=""
            if m["pf"]<0.95 or m["expectancy"]<0:
                for k in ("min_rvol","min_oi_change","min_adx","z","imbalance","strength_ratio","funding_extreme"):
                    if k in bounds and k in params: key=k; direction=1; break
                why=f"Recent robustness weak (PF {m['pf']:.2f}, expectancy {m['expectancy']:.2f}); tighten one confirmation threshold."
            elif m["pf"]>1.35 and m["expectancy"]>0 and robust_windows(trades)>=0.75 and m["n"]>=100:
                for k in ("min_rvol","min_oi_change","min_adx","z","imbalance","strength_ratio","funding_extreme"):
                    if k in bounds and k in params: key=k; direction=-1; break
                why=f"Stable positive windows with PF {m['pf']:.2f}; cautiously relax one threshold to test generalization."
            if key:
                lo,hi=bounds[key]; span=hi-lo; step=span*0.05*direction
                params[key]=round(max(lo,min(hi,float(params[key])+step)),8)
                db.update_strategy(s["strategy"],params=params)
                db.log_adjustment(s["strategy"],"bounded_parameter",before,params,why,True)
                out.append({"strategy":s["strategy"],"before":before,"after":params,"reason":why})
            self._promotion(s,trades,m)
        return out

    def _promotion(self,s,trades,m):
        stage=s["stage"]; rw=robust_windows(trades)
        # Promotion is forward-paper evidence only. No historical optimizer can directly unlock live trading.
        days=sample_days(trades); conc=profit_concentration(trades)
        if stage=="EARLY" and m["n"]>=80 and days>=14 and m["pf"]>=1.05 and m["expectancy"]>0 and m["max_dd_pct"]<=0.18 and rw>=0.5:
            db.update_strategy(s["strategy"],stage="TUNING")
            db.log_adjustment(s["strategy"],"stage",{"stage":"EARLY"},{"stage":"TUNING"},f"80+ completed paper trades over {days:.0f}d, PF={m['pf']:.2f}, DD={m['max_dd_pct']:.1%}, robust windows={rw:.2f}",True)
        elif stage=="TUNING" and m["n"]>=200 and days>=45 and m["pf"]>=1.20 and m["expectancy"]>0 and m["stress_expectancy"]>0 and m["stress_pf"]>=1.05 and m["max_dd_pct"]<=0.12 and m["sharpe"]>=0.8 and rw>=0.75 and conc<=0.40:
            # Final still remains globally LIVE-LOCKED until the operator explicitly enables env vars.
            db.update_strategy(s["strategy"],stage="FINAL",live_eligible=True)
            db.log_adjustment(s["strategy"],"stage",{"stage":"TUNING"},{"stage":"FINAL","live_eligible":True},f"200+ completed paper trades over {days:.0f}d; PF={m['pf']:.2f}; stressed PF={m['stress_pf']:.2f}; DD={m['max_dd_pct']:.1%}; robust windows={rw:.2f}; best-symbol profit share={conc:.1%}",True)

learner=LearningGovernor()
