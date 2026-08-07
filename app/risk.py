from __future__ import annotations
import json, math, time
from .config import settings
from .db import db

COMMON_BOUNDS={
 "risk_pct":(0.0025,0.018), "leverage":(1.0,4.0), "max_positions":(1.0,6.0),
 "max_position_notional_pct":(0.05,0.35), "max_symbol_exposure_pct":(0.08,0.35), "max_total_exposure_pct":(0.30,2.00),
 "tp1_r":(0.60,1.50), "tp2_r":(1.20,3.00), "tp3_r":(2.00,5.00),
 "tp1_fraction":(0.15,0.45), "tp2_fraction":(0.15,0.45), "trail_r":(0.60,2.20),
}
STAGE_RISK_CAP={"EARLY":0.018,"TUNING":0.012,"FINAL":0.0075}
STAGE_LEVERAGE_CAP={"EARLY":4.0,"TUNING":3.0,"FINAL":2.5}

class RiskManager:
    def sanitize(self,params:dict,stage:str)->dict:
        p=dict(params)
        for k,(lo,hi) in COMMON_BOUNDS.items():
            if k not in p: continue
            v=float(p[k]); p[k]=max(lo,min(hi,v))
        p["risk_pct"]=min(float(p.get("risk_pct",0.01)),STAGE_RISK_CAP.get(stage,0.01),settings.hard_max_risk_per_trade)
        p["leverage"]=min(float(p.get("leverage",1.0)),STAGE_LEVERAGE_CAP.get(stage,2.0),settings.hard_max_leverage)
        p["max_positions"]=int(max(1,min(int(round(float(p.get("max_positions",3)))),settings.hard_max_open_positions)))
        p["max_position_notional_pct"]=min(float(p.get("max_position_notional_pct",0.18)),settings.hard_max_symbol_exposure_pct)
        p["max_symbol_exposure_pct"]=min(float(p.get("max_symbol_exposure_pct",0.20)),settings.hard_max_symbol_exposure_pct)
        p["max_total_exposure_pct"]=min(float(p.get("max_total_exposure_pct",1.0)),settings.hard_max_total_exposure_pct)
        p["tp2_r"]=max(float(p.get("tp2_r",1.8)),float(p.get("tp1_r",1.0))+0.25)
        p["tp3_r"]=max(float(p.get("tp3_r",3.0)),p["tp2_r"]+0.50)
        f1=float(p.get("tp1_fraction",0.30)); f2=float(p.get("tp2_fraction",0.35))
        if f1+f2>0.85:
            scale=0.85/(f1+f2); f1*=scale; f2*=scale
        p["tp1_fraction"]=round(f1,4); p["tp2_fraction"]=round(f2,4)
        return p

    def equity(self,strategy:str,variant:str,prices:dict[str,float]|None=None)->float:
        acc=db.account(strategy,variant); bal=float(acc["balance"]) if acc else settings.initial_paper_equity
        unreal=0.0; prices=prices or {}
        for pos in db.query("SELECT * FROM positions WHERE strategy=? AND variant=?",(strategy,variant)):
            px=float(prices.get(pos["symbol"],pos["entry"])); sign=1 if pos["side"]=="long" else -1
            unreal+=(px-float(pos["entry"]))*float(pos["remaining_qty"])*sign
        return bal+unreal

    def strategy_daily_pnl(self,strategy:str,variant:str="champion") -> float:
        cutoff=int(time.time()*1000)-24*3600*1000
        r=db.one("SELECT COALESCE(SUM(net_pnl),0) x FROM trades WHERE strategy=? AND variant=? AND closed_at>=?",(strategy,variant,cutoff))
        return float(r["x"] if r else 0)

    def system_daily_pnl(self)->float:
        cutoff=int(time.time()*1000)-24*3600*1000
        r=db.one("SELECT COALESCE(SUM(net_pnl),0) x FROM trades WHERE variant='champion' AND closed_at>=?",(cutoff,)); return float(r["x"] if r else 0)

    def exposure(self,strategy:str,variant:str,prices:dict[str,float]|None=None)->tuple[float,dict[str,float]]:
        total=0.0; by={}; prices=prices or {}
        for pos in db.query("SELECT * FROM positions WHERE strategy=? AND variant=?",(strategy,variant)):
            px=float(prices.get(pos["symbol"],pos["entry"])); n=abs(float(pos["remaining_qty"])*px); total+=n; by[pos["symbol"]]=by.get(pos["symbol"],0)+n
        return total,by

    def allow_entry(self,strategy:str,variant:str,symbol:str,entry:float,stop:float,params:dict,stage:str,prices:dict[str,float]|None=None)->tuple[bool,str]:
        p=self.sanitize(params,stage); acc=db.account(strategy,variant)
        if not acc: return False,"missing paper account"
        equity=self.equity(strategy,variant,prices); initial=float(acc["initial_balance"])
        if equity<=0: return False,"equity depleted"
        if self.strategy_daily_pnl(strategy,variant)<=-settings.hard_strategy_daily_loss_pct*initial: return False,"strategy daily loss circuit breaker"
        if variant=="champion" and self.system_daily_pnl()<=-settings.hard_system_daily_loss_pct*settings.initial_paper_equity*max(1,len(db.query("SELECT strategy FROM strategy_state"))): return False,"system daily loss circuit breaker"
        if equity<=initial*(1-settings.hard_strategy_drawdown_pct): return False,"strategy hard drawdown circuit breaker"
        dist=abs(entry-stop); stop_pct=dist/max(entry,1e-12)
        if stop_pct<settings.hard_min_stop_pct: return False,"stop too tight for hard floor"
        if stop_pct>settings.hard_max_stop_pct: return False,"stop too wide for hard ceiling"
        n=int((db.one("SELECT COUNT(*) n FROM positions WHERE strategy=? AND variant=?",(strategy,variant)) or {"n":0})["n"])
        if n>=int(p["max_positions"]): return False,"learned max positions reached"
        total,by=self.exposure(strategy,variant,prices)
        if total>=equity*p["max_total_exposure_pct"]: return False,"learned total exposure cap reached"
        if by.get(symbol,0)>=equity*p["max_symbol_exposure_pct"]: return False,"symbol exposure cap reached"
        return True,"ok"

    def size_and_targets(self,strategy:str,variant:str,symbol:str,side:str,entry:float,stop:float,params:dict,stage:str,prices:dict[str,float]|None=None)->dict:
        p=self.sanitize(params,stage); equity=self.equity(strategy,variant,prices); dist=abs(entry-stop)
        risk_cash=equity*p["risk_pct"]; qty_by_risk=risk_cash/max(dist,1e-12)
        max_by_leverage=(equity*p["leverage"])/max(entry,1e-12)
        total,by=self.exposure(strategy,variant,prices)
        max_total=max(0,equity*p["max_total_exposure_pct"]-total)/max(entry,1e-12)
        max_symbol=max(0,equity*p["max_symbol_exposure_pct"]-by.get(symbol,0))/max(entry,1e-12)
        max_position=(equity*p["max_position_notional_pct"])/max(entry,1e-12)
        qty=max(0,min(qty_by_risk,max_by_leverage,max_total,max_symbol,max_position))
        sign=1 if side=="long" else -1
        t1=entry+sign*dist*p["tp1_r"]; t2=entry+sign*dist*p["tp2_r"]; t3=entry+sign*dist*p["tp3_r"]
        return {"qty":qty,"risk_cash":risk_cash,"tp1":t1,"tp2":t2,"tp3":t3,"params":p}

risk_manager=RiskManager()
