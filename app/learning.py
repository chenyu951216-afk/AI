from __future__ import annotations
import json, math, statistics, time
from copy import deepcopy
from .config import settings
from .db import db
from .risk import COMMON_BOUNDS, risk_manager

SPECIFIC_BOUNDS={
 "smc_liquidity":{"min_rvol":(1.0,1.9),"atr_buffer":(.15,.60)},
 "oi_trend":{"min_oi_change":(.003,.03),"min_rvol":(1.1,2.8),"min_adx":(16,32),"atr_stop":(1.0,2.4)},
 "funding_squeeze":{"funding_extreme":(.0002,.002),"rsi_extreme":(66,82),"atr_stop":(1.0,2.4)},
 "volume_breakout":{"min_rvol":(1.2,3.2),"compression_ratio":(.45,.90),"atr_stop":(1.0,2.3)},
 "fib_pullback":{"zone_tolerance":(.15,.75),"min_rvol":(.9,1.8),"lookback":(40,100),"stop_buffer_atr":(.05,.60)},
 "vwap_ema":{"rsi_long":(48,60),"atr_stop":(1.0,2.0)},
 "mean_reversion":{"max_adx":(14,24),"z":(1.6,2.8),"rsi_low":(22,36),"atr_stop":(1.2,2.5)},
 "orderbook":{"imbalance":(.12,.45),"max_spread":(.0005,.0025),"min_rvol":(.8,1.8),"atr_stop":(.7,1.8)},
 "liquidation_magnet":{"strength_ratio":(1.1,3.0),"max_rsi":(60,76),"atr_stop":(1.0,2.2)},
}

def completed_roundtrips(strategy:str,variant:str="champion",limit:int=1000,since:int|None=None):
    q="""SELECT t.position_id,t.strategy,t.variant,MIN(t.symbol) symbol,SUM(t.gross_pnl) gross_pnl,SUM(t.fees) fees,SUM(t.net_pnl) net_pnl,
      MAX(t.closed_at) closed_at,MIN(t.opened_at) opened_at,MAX(t.initial_risk_cash) initial_risk_cash
      FROM trades t WHERE t.strategy=? AND t.variant=? AND NOT EXISTS(SELECT 1 FROM positions p WHERE p.id=t.position_id)"""
    args=[strategy,variant]
    if since is not None:q+=" AND t.opened_at>=?";args.append(since)
    q+=" GROUP BY t.position_id,t.strategy,t.variant ORDER BY closed_at DESC LIMIT ?";args.append(limit)
    return db.query(q,tuple(args))[::-1]

def metrics(trades):
    if not trades:return {"n":0,"win_rate":0,"pf":0,"expectancy":0,"max_dd_pct":0,"sharpe":0,"stress_pf":0,"stress_expectancy":0,"avg_r":0,"payoff":0}
    pn=[float(t["net_pnl"]) for t in trades]; wins=[x for x in pn if x>0]; losses=[x for x in pn if x<0]; gw=sum(wins); gl=-sum(losses); eq=settings.initial_paper_equity; peak=eq; maxdd=0
    for x in pn:eq+=x;peak=max(peak,eq);maxdd=max(maxdd,peak-eq)
    mean=statistics.fmean(pn); sd=statistics.pstdev(pn) if len(pn)>1 else 0; stress=[float(t["net_pnl"])-float(t["fees"])*(settings.paper_stress_slippage_bps/max(settings.paper_slippage_bps,1)) for t in trades]; sw=sum(x for x in stress if x>0); sl=-sum(x for x in stress if x<0)
    rs=[float(t["net_pnl"])/max(float(t.get("initial_risk_cash") or 0),1e-9) for t in trades]
    return {"n":len(pn),"win_rate":len(wins)/len(pn),"pf":gw/gl if gl else 9.99,"expectancy":mean,"max_dd_pct":maxdd/max(peak,1),"sharpe":mean/sd*math.sqrt(len(pn)) if sd else 0,"stress_pf":sw/sl if sl else 9.99,"stress_expectancy":statistics.fmean(stress),"avg_r":statistics.fmean(rs),"payoff":(statistics.fmean(wins)/abs(statistics.fmean(losses))) if wins and losses else 0}

def robust_windows(trades,windows=5):
    if len(trades)<40:return 0.0
    size=max(10,len(trades)//windows); good=used=0
    for i in range(0,len(trades),size):
        c=trades[i:i+size]
        if len(c)<8:continue
        used+=1;m=metrics(c);good+=int(m["expectancy"]>0 and m["pf"]>1)
    return good/used if used else 0

def sample_days(trades): return (max((t["closed_at"] for t in trades),default=0)-min((t["opened_at"] for t in trades),default=0))/86400000 if trades else 0

def profit_concentration(trades):
    by={}
    for t in trades:
        if float(t["net_pnl"])>0:by[t["symbol"]]=by.get(t["symbol"],0)+float(t["net_pnl"])
    total=sum(by.values());return max(by.values(),default=0)/total if total>0 else 1.0

def symbol_count(trades):return len({t["symbol"] for t in trades})

def score(m):
    if not m["n"]:return -999
    return math.log(max(m["pf"],.05))*1.4 + m["avg_r"]*1.2 + min(m["sharpe"],3)*.2 - m["max_dd_pct"]*4 + math.log(max(m["stress_pf"],.05))*.6

class LearningGovernor:
    def __init__(self):self.last_run=0
    def due(self):return settings.learning_enabled and time.time()-self.last_run>=settings.learning_interval_hours*3600
    def run(self,force=False):
        if not force and not self.due():return []
        self.last_run=time.time();out=[]
        for s in db.query("SELECT * FROM strategy_state WHERE enabled=1"):
            out.extend(self._stage_and_degrade(s)); c=db.one("SELECT * FROM challengers WHERE strategy=? AND status='ACTIVE'",(s["strategy"],))
            if c:out.extend(self._review_challenger(s,c))
            else:out.extend(self._maybe_create_challenger(s))
        return out
    def _stage_and_degrade(self,s):
        since=int(s.get("evidence_since") or 0);tr=completed_roundtrips(s["strategy"],"champion",2000,since if since else None);m=metrics(tr);rw=robust_windows(tr);days=sample_days(tr);out=[]
        if s["stage"]=="EARLY" and m["n"]>=90 and days>=14 and m["pf"]>=1.05 and m["expectancy"]>0 and m["max_dd_pct"]<=.18 and rw>=.5:
            p=risk_manager.sanitize(json.loads(s["params_json"]),"TUNING");now=int(time.time()*1000);db.update_strategy(s["strategy"],stage="TUNING",params_json=p,evidence_since=now);db.log_adjustment(s["strategy"],"stage",{"stage":"EARLY"},{"stage":"TUNING"},f"forward paper passed: n={m['n']}, {days:.1f}d, PF={m['pf']:.2f}, DD={m['max_dd_pct']:.1%}, windows={rw:.2f}",True);out.append({"strategy":s["strategy"],"stage":"TUNING"})
        if s["stage"]=="TUNING" and m["n"]>=settings.final_min_trades and days>=settings.final_min_days and m["pf"]>=settings.final_min_pf and m["stress_pf"]>=settings.final_min_stress_pf and m["expectancy"]>0 and m["stress_expectancy"]>0 and m["max_dd_pct"]<=settings.final_max_dd_pct and rw>=settings.final_min_robust_windows and profit_concentration(tr)<=settings.final_max_symbol_profit_share and symbol_count(tr)>=settings.final_min_symbols:
            p=risk_manager.sanitize(json.loads(s["params_json"]),"FINAL");now=int(time.time()*1000);db.update_strategy(s["strategy"],stage="FINAL",params_json=p,live_eligible=True,final_since=now,evidence_since=now);db.log_adjustment(s["strategy"],"stage",{"stage":"TUNING"},{"stage":"FINAL","live_eligible":True},f"FINAL gate passed: n={m['n']}, {days:.1f}d, PF={m['pf']:.2f}, stressPF={m['stress_pf']:.2f}, DD={m['max_dd_pct']:.1%}, windows={rw:.2f}, symbols={symbol_count(tr)}",True);out.append({"strategy":s["strategy"],"stage":"FINAL"})
        if s["stage"]=="FINAL":
            recent=completed_roundtrips(s["strategy"],"champion",100);rm=metrics(recent)
            if rm["n"]>=60 and (rm["pf"]<.95 or rm["expectancy"]<0 or rm["max_dd_pct"]>.15):
                p=risk_manager.sanitize(json.loads(s["params_json"]),"TUNING");now=int(time.time()*1000);db.update_strategy(s["strategy"],stage="TUNING",params_json=p,live_eligible=False,final_since=None,evidence_since=now);db.log_adjustment(s["strategy"],"demotion",{"stage":"FINAL"},{"stage":"TUNING","live_eligible":False},f"recent degradation revoked live eligibility: PF={rm['pf']:.2f}, exp={rm['expectancy']:.2f}, DD={rm['max_dd_pct']:.1%}",True);out.append({"strategy":s["strategy"],"stage":"TUNING","demoted":True})
        return out
    def _maybe_create_challenger(self,s):
        tr=completed_roundtrips(s["strategy"],"champion",settings.learning_recent_window);m=metrics(tr)
        if m["n"]<settings.learning_min_trades:return []
        days=sample_days(tr); rw=robust_windows(tr)
        # Convergence windows: once a champion is close to the next promotion gate, freeze
        # exploratory mutations long enough to collect uncontaminated forward evidence.
        # If it reaches the deadline without passing the next stage, exploration resumes.
        if s["stage"]=="EARLY" and 7<=days<14 and m["n"]>=60 and m["pf"]>=1.0 and m["expectancy"]>0 and m["max_dd_pct"]<=.20 and rw>=.4:
            return []
        if s["stage"]=="TUNING" and 21<=days<settings.final_min_days and m["pf"]>=settings.final_min_pf*.90 and m["stress_pf"]>=1.05 and m["expectancy"]>0 and m["max_dd_pct"]<=settings.final_max_dd_pct*1.2 and rw>=.65:
            return []
        before=json.loads(s["params_json"]);after=deepcopy(before);reason=[]; bounds={**COMMON_BOUNDS,**SPECIFIC_BOUNDS.get(s["strategy"],{})}

        # Every execution-management parameter is learnable, but only inside hard risk bounds.
        sizing=["risk_pct","leverage","max_positions","max_position_notional_pct","max_symbol_exposure_pct","max_total_exposure_pct"]
        exits=["tp1_r","tp2_r","tp3_r","tp1_fraction","tp2_fraction","trail_r"]
        entry_specific=[k for k in SPECIFIC_BOUNDS.get(s["strategy"],{}) if k in after]

        if m["max_dd_pct"]>.10:
            keys=sizing + [k for k in entry_specific if k in {"atr_stop","atr_buffer"}] + exits
            mode="de_risk"
        elif m["pf"]<1 or m["expectancy"]<0:
            keys=entry_specific + ["risk_pct","leverage","max_positions"] + exits
            mode="tighten_edge"
        elif m["payoff"]<1:
            keys=["tp1_fraction","tp2_fraction","tp1_r","tp2_r","tp3_r","trail_r"] + entry_specific + sizing
            mode="improve_payoff"
        elif m["win_rate"]<.42:
            keys=entry_specific + ["tp1_fraction","tp1_r","trail_r"] + sizing + exits
            mode="improve_selectivity"
        elif m["pf"]>1.3 and robust_windows(tr)>=.65:
            keys=sizing + exits + entry_specific
            mode="careful_scale"
        else:
            keys=entry_specific + exits + sizing
            mode="explore"

        keys=[k for k in keys if k in bounds and k in after]
        # Rotate the candidate list across learning cycles so leverage, size, exposure, TP fractions,
        # trailing and signal parameters all actually receive shadow tests over time.
        attempts=int((db.one("SELECT COUNT(*) n FROM adjustments WHERE strategy=?",(s["strategy"],)) or {"n":0})["n"])
        if keys:
            rot=attempts%len(keys);keys=keys[rot:]+keys[:rot]

        tighten_up={"min_rvol","min_oi_change","min_adx","z","imbalance","strength_ratio","funding_extreme","rsi_long","rsi_extreme"}
        tighten_down={"max_spread","compression_ratio","zone_tolerance","max_adx","rsi_low","max_rsi"}
        changed=0
        for k in keys:
            if changed>=settings.max_learning_changes:break
            lo,hi=bounds[k];v=float(after[k]);span=hi-lo
            if span<=0:continue

            if k in sizing:
                # Drawdown/weak edge scales down. Only robust strong edge may challenge upward sizing.
                up = mode=="careful_scale"
                step=span*.05*(1 if up else -1)
            elif k in {"tp1_fraction","tp2_fraction"}:
                # Poor payoff tests keeping more runner; low win-rate tests locking a little more early.
                if mode=="improve_payoff": direction=-1
                elif mode=="improve_selectivity": direction=1
                else: direction=1 if attempts%2==0 else -1
                step=span*.04*direction
            elif k in {"tp1_r","tp2_r","tp3_r","trail_r"}:
                if mode=="improve_payoff": direction=1
                elif mode=="de_risk": direction=-1 if k=="trail_r" else 1
                else: direction=1 if attempts%2==0 else -1
                step=span*.04*direction
            elif k in {"atr_stop","atr_buffer","stop_buffer_atr"}:
                # Alternate tighter/wider structural protection; challenger data decides.
                direction=-1 if mode=="de_risk" else (1 if attempts%2==0 else -1)
                step=span*.04*direction
            elif k in tighten_up:
                step=span*.04*(1 if mode in {"tighten_edge","improve_selectivity"} else (1 if attempts%2==0 else -1))
            elif k in tighten_down:
                step=span*.04*(-1 if mode in {"tighten_edge","improve_selectivity"} else (1 if attempts%2==0 else -1))
            else:
                step=span*.04*(1 if attempts%2==0 else -1)

            nv=max(lo,min(hi,v+step))
            nv=int(round(nv)) if k in {"max_positions","lookback","min_adx","rsi_long","rsi_low","rsi_extreme","max_adx","max_rsi"} else round(nv,8)
            if nv==after[k]:continue
            after[k]=nv;reason.append(f"{k}: {v} -> {after[k]}");changed+=1

        after=risk_manager.sanitize(after,s["stage"])
        if after==before:return []
        now=int(time.time()*1000);db.execute("INSERT OR REPLACE INTO challengers(strategy,status,params_json,baseline_json,reason,started_at,updated_at) VALUES(?,?,?,?,?,?,?)",(s["strategy"],"ACTIVE",json.dumps(after),json.dumps(before),f"{mode}; "+"; ".join(reason),now,now));db.execute("DELETE FROM positions WHERE strategy=? AND variant='challenger'",(s["strategy"],));db.ensure_account(s["strategy"],"challenger",reset=True);db.log_adjustment(s["strategy"],"challenger_started",before,after,f"Shadow test started ({mode}). Recent PF={m['pf']:.2f}, DD={m['max_dd_pct']:.1%}. "+"; ".join(reason),False)
        return [{"strategy":s["strategy"],"challenger":"started","mode":mode,"changes":reason}]

    def _review_challenger(self,s,c):
        since=int(c["started_at"]);ct=completed_roundtrips(s["strategy"],"challenger",1000,since);days=sample_days(ct)
        if len(ct)<settings.challenger_min_trades or days<settings.challenger_min_days:return []
        bt=completed_roundtrips(s["strategy"],"champion",1000,since);cm=metrics(ct);bm=metrics(bt)
        accept=cm["n"]>=settings.challenger_min_trades and cm["expectancy"]>0 and cm["stress_pf"]>=1.0 and cm["max_dd_pct"]<=max(.18,bm["max_dd_pct"]+.03) and score(cm)>=score(bm)+.08
        before=json.loads(c["baseline_json"]);after=json.loads(c["params_json"]);why=f"challenger n={cm['n']} PF={cm['pf']:.2f} stressPF={cm['stress_pf']:.2f} DD={cm['max_dd_pct']:.1%} score={score(cm):.2f}; champion same-window n={bm['n']} PF={bm['pf']:.2f} DD={bm['max_dd_pct']:.1%} score={score(bm):.2f}"
        if accept:
            now=int(time.time()*1000); promoted=risk_manager.sanitize(after,"TUNING" if s["stage"]=="FINAL" else s["stage"])
            if s["stage"]=="FINAL":
                # A newly promoted parameter set must never inherit the old FINAL/live status.
                # Re-validate the exact new champion from a fresh TUNING evidence window.
                db.update_strategy(s["strategy"],stage="TUNING",params_json=promoted,live_eligible=False,final_since=None,evidence_since=now)
                why += "; FINAL revoked because champion parameters changed; fresh TUNING/FINAL evidence required"
            else:
                db.update_strategy(s["strategy"],params_json=promoted,evidence_since=now)
            db.log_adjustment(s["strategy"],"challenger_promoted",before,after,why,True)
        else:db.log_adjustment(s["strategy"],"challenger_rejected",before,after,why,False)
        db.execute("UPDATE challengers SET status=?,updated_at=? WHERE strategy=?",("PROMOTED" if accept else "REJECTED",int(time.time()*1000),s["strategy"]));db.execute("DELETE FROM positions WHERE strategy=? AND variant='challenger'",(s["strategy"],));db.ensure_account(s["strategy"],"challenger",reset=True)
        return [{"strategy":s["strategy"],"challenger":"promoted" if accept else "rejected","reason":why}]

learner=LearningGovernor()
