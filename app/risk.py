from __future__ import annotations
import time
from .config import settings
from .db import db

COMMON_BOUNDS={
 "risk_pct":(0.003,0.020),"leverage":(1.0,5.0),"max_positions":(1.0,8.0),
 "max_position_notional_pct":(0.06,0.40),"max_symbol_exposure_pct":(0.08,0.40),"max_total_exposure_pct":(0.35,2.25),
 "tp1_r":(0.55,1.60),"tp2_r":(1.10,3.20),"tp3_r":(1.80,5.50),"tp1_fraction":(0.12,0.48),"tp2_fraction":(0.12,0.48),"trail_r":(0.50,2.60),
}
STAGE_RISK_CAP={"EARLY":0.020,"TUNING":0.013,"FINAL":0.008}
STAGE_LEVERAGE_CAP={"EARLY":5.0,"TUNING":3.5,"FINAL":2.5}

class RiskManager:
    def sanitize(self,params:dict,stage:str)->dict:
        p=dict(params)
        for k,(lo,hi) in COMMON_BOUNDS.items():
            if k in p:p[k]=max(lo,min(hi,float(p[k])))
        p["risk_pct"]=min(float(p.get("risk_pct",.012)),STAGE_RISK_CAP.get(stage,.01),settings.hard_max_risk_per_trade)
        p["leverage"]=min(float(p.get("leverage",2)),STAGE_LEVERAGE_CAP.get(stage,2),settings.hard_max_leverage)
        p["max_positions"]=int(max(1,min(round(float(p.get("max_positions",3))),settings.hard_max_open_positions)))
        p["max_position_notional_pct"]=min(float(p.get("max_position_notional_pct",.20)),settings.hard_max_symbol_exposure_pct)
        p["max_symbol_exposure_pct"]=min(float(p.get("max_symbol_exposure_pct",.22)),settings.hard_max_symbol_exposure_pct)
        p["max_total_exposure_pct"]=min(float(p.get("max_total_exposure_pct",1.0)),settings.hard_max_total_exposure_pct)
        p["tp2_r"]=max(float(p.get("tp2_r",1.8)),float(p.get("tp1_r",.9))+.25);p["tp3_r"]=max(float(p.get("tp3_r",3)),p["tp2_r"]+.45)
        f1=float(p.get("tp1_fraction",.3));f2=float(p.get("tp2_fraction",.35))
        if f1+f2>.85:
            z=.85/(f1+f2);f1*=z;f2*=z
        p["tp1_fraction"]=round(f1,4);p["tp2_fraction"]=round(f2,4);return p
    def equity(self,strategy:str,variant:str,prices:dict[str,float]|None=None)->float:
        acc=db.account(strategy,variant);bal=float(acc["balance"]) if acc else settings.initial_paper_equity;unreal=0.;prices=prices or {}
        for pos in db.query("SELECT * FROM positions WHERE strategy=? AND variant=?",(strategy,variant)):
            px=float(prices.get(pos["symbol"],pos["entry"]));sg=1 if pos["side"]=="long" else -1;unreal+=(px-float(pos["entry"]))*float(pos["remaining_qty"])*sg
        return bal+unreal
    def strategy_daily_pnl(self,strategy:str,variant:str)->float:
        cut=int(time.time()*1000)-86400000;r=db.one("SELECT COALESCE(SUM(net_pnl),0)x FROM trades WHERE strategy=? AND variant=? AND closed_at>=?",(strategy,variant,cut));return float(r["x"] if r else 0)
    def system_daily_pnl(self)->float:
        cut=int(time.time()*1000)-86400000;r=db.one("SELECT COALESCE(SUM(net_pnl),0)x FROM trades WHERE variant='champion' AND closed_at>=?",(cut,));return float(r["x"] if r else 0)
    def exposure(self,strategy:str,variant:str,prices=None):
        total=0.;by={};prices=prices or {}
        for p in db.query("SELECT * FROM positions WHERE strategy=? AND variant=?",(strategy,variant)):
            px=float(prices.get(p["symbol"],p["entry"]));n=abs(float(p["remaining_qty"])*px);total+=n;by[p["symbol"]]=by.get(p["symbol"],0)+n
        return total,by
    def allow_entry(self,strategy,variant,symbol,entry,stop,params,stage,prices=None):
        p=self.sanitize(params,stage);acc=db.account(strategy,variant)
        if not acc:return False,"missing paper account"
        eq=self.equity(strategy,variant,prices);initial=float(acc["initial_balance"])
        if eq<=0:return False,"equity depleted"
        if self.strategy_daily_pnl(strategy,variant)<=-settings.hard_strategy_daily_loss_pct*initial:return False,"strategy daily loss circuit breaker"
        if variant=="champion" and self.system_daily_pnl()<=-settings.hard_system_daily_loss_pct*settings.initial_paper_equity*max(1,len(db.query("SELECT strategy FROM strategy_state"))):return False,"system daily loss circuit breaker"
        if eq<=initial*(1-settings.hard_strategy_drawdown_pct):return False,"strategy hard drawdown circuit breaker"
        sp=abs(entry-stop)/max(entry,1e-12)
        if sp<settings.hard_min_stop_pct:return False,"stop too tight for hard floor"
        if sp>settings.hard_max_stop_pct:return False,"stop too wide for hard ceiling"
        n=int((db.one("SELECT COUNT(*)n FROM positions WHERE strategy=? AND variant=?",(strategy,variant)) or {"n":0})["n"])
        if n>=p["max_positions"]:return False,"learned max positions reached"
        total,by=self.exposure(strategy,variant,prices)
        if total>=eq*p["max_total_exposure_pct"]:return False,"learned total exposure cap reached"
        if by.get(symbol,0)>=eq*p["max_symbol_exposure_pct"]:return False,"symbol exposure cap reached"
        return True,"ok"
    def size_and_targets(self,strategy,variant,symbol,side,entry,stop,params,stage,prices=None):
        p=self.sanitize(params,stage);eq=self.equity(strategy,variant,prices);dist=abs(entry-stop);risk=eq*p["risk_pct"];q_r=risk/max(dist,1e-12);q_lev=eq*p["leverage"]/max(entry,1e-12);total,by=self.exposure(strategy,variant,prices);q_total=max(0,eq*p["max_total_exposure_pct"]-total)/max(entry,1e-12);q_sym=max(0,eq*p["max_symbol_exposure_pct"]-by.get(symbol,0))/max(entry,1e-12);q_pos=eq*p["max_position_notional_pct"]/max(entry,1e-12);qty=max(0,min(q_r,q_lev,q_total,q_sym,q_pos));sg=1 if side=="long" else -1
        return {"qty":qty,"risk_cash":risk,"tp1":entry+sg*dist*p["tp1_r"],"tp2":entry+sg*dist*p["tp2_r"],"tp3":entry+sg*dist*p["tp3_r"],"params":p}

risk_manager=RiskManager()
