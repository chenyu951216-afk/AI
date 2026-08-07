from __future__ import annotations
import json,math,statistics,time
from copy import deepcopy
from .config import settings
from .db import db
from .risk import COMMON_BOUNDS,risk_manager

SPECIFIC_BOUNDS={
 "smc_liquidity":{"min_rvol":(.75,1.8),"atr_buffer":(.15,.75)},
 "oi_trend":{"min_oi_change":(.001,.03),"min_rvol":(.85,2.8),"min_adx":(12,32),"atr_stop":(.9,2.6)},
 "funding_squeeze":{"funding_extreme":(.0001,.002),"min_oi_change":(0,.02),"rsi_extreme":(62,82),"atr_stop":(.9,2.6)},
 "volume_breakout":{"min_rvol":(.9,3.2),"compression_ratio":(.50,1.05),"atr_stop":(.9,2.5)},
 "fib_pullback":{"zone_tolerance":(.15,.85),"min_rvol":(.70,1.8),"lookback":(40,110),"stop_buffer_atr":(.05,.80)},
 "vwap_ema":{"rsi_long":(46,60),"atr_stop":(.9,2.2)},
 "mean_reversion":{"max_adx":(14,28),"max_context_adx":(16,30),"z":(1.3,2.8),"rsi_low":(24,40),"atr_stop":(1.0,2.7)},
 "orderbook":{"imbalance":(.08,.45),"max_spread":(.0005,.0028),"min_rvol":(.65,1.8),"atr_stop":(.65,2.0)},
 "liquidation_magnet":{"strength_ratio":(1.05,3.0),"max_rsi":(58,78),"atr_stop":(.9,2.4)},
}
STOP_PARAM={"smc_liquidity":"atr_buffer","oi_trend":"atr_stop","funding_squeeze":"atr_stop","volume_breakout":"atr_stop","fib_pullback":"stop_buffer_atr","vwap_ema":"atr_stop","mean_reversion":"atr_stop","orderbook":"atr_stop","liquidation_magnet":"atr_stop"}
SIZING=["risk_pct","leverage","max_positions","max_position_notional_pct","max_symbol_exposure_pct","max_total_exposure_pct"]
EXITS=["tp1_r","tp2_r","tp3_r","tp1_fraction","tp2_fraction","trail_r"]

def completed_roundtrips(strategy,variant="champion",limit=1000,since=None):
    q="""SELECT t.position_id,t.strategy,t.variant,MIN(t.symbol)symbol,SUM(t.gross_pnl)gross_pnl,SUM(t.fees)fees,SUM(t.net_pnl)net_pnl,MAX(t.closed_at)closed_at,MIN(t.opened_at)opened_at,MAX(t.initial_risk_cash)initial_risk_cash FROM trades t WHERE t.strategy=? AND t.variant=? AND NOT EXISTS(SELECT 1 FROM positions p WHERE p.id=t.position_id)""";args=[strategy,variant]
    if since is not None:q+=" AND t.opened_at>=?";args.append(since)
    q+=" GROUP BY t.position_id,t.strategy,t.variant ORDER BY closed_at DESC LIMIT ?";args.append(limit);return db.query(q,tuple(args))[::-1]
def metrics(trades):
    if not trades:return {"n":0,"win_rate":0,"pf":0,"expectancy":0,"max_dd_pct":0,"sharpe":0,"stress_pf":0,"stress_expectancy":0,"avg_r":0,"payoff":0}
    pn=[float(t["net_pnl"]) for t in trades];wins=[x for x in pn if x>0];loss=[x for x in pn if x<0];gw=sum(wins);gl=-sum(loss);eq=settings.initial_paper_equity;peak=eq;dd=0
    for x in pn:eq+=x;peak=max(peak,eq);dd=max(dd,peak-eq)
    mean=statistics.fmean(pn);sd=statistics.pstdev(pn) if len(pn)>1 else 0;stress=[float(t["net_pnl"])-float(t["fees"])*(settings.paper_stress_slippage_bps/max(settings.paper_slippage_bps,1)) for t in trades];sw=sum(x for x in stress if x>0);sl=-sum(x for x in stress if x<0);rs=[float(t["net_pnl"])/max(float(t.get("initial_risk_cash") or 0),1e-9) for t in trades]
    return {"n":len(pn),"win_rate":len(wins)/len(pn),"pf":gw/gl if gl else 9.99,"expectancy":mean,"max_dd_pct":dd/max(peak,1),"sharpe":mean/sd*math.sqrt(len(pn)) if sd else 0,"stress_pf":sw/sl if sl else 9.99,"stress_expectancy":statistics.fmean(stress),"avg_r":statistics.fmean(rs),"payoff":statistics.fmean(wins)/abs(statistics.fmean(loss)) if wins and loss else 0}
def robust_windows(trades,windows=5):
    if len(trades)<40:return 0.
    size=max(10,len(trades)//windows);good=used=0
    for i in range(0,len(trades),size):
        c=trades[i:i+size]
        if len(c)<8:continue
        used+=1;m=metrics(c);good+=int(m["expectancy"]>0 and m["pf"]>1)
    return good/used if used else 0
def sample_days(trades):return (max((t["closed_at"] for t in trades),default=0)-min((t["opened_at"] for t in trades),default=0))/86400000 if trades else 0
def profit_concentration(trades):
    by={}
    for t in trades:
        if float(t["net_pnl"])>0:by[t["symbol"]]=by.get(t["symbol"],0)+float(t["net_pnl"])
    z=sum(by.values());return max(by.values(),default=0)/z if z>0 else 1.
def symbol_count(trades):return len({t["symbol"] for t in trades})
def score(m):
    if not m["n"]:return -999
    return math.log(max(m["pf"],.05))*1.4+m["avg_r"]*1.2+min(m["sharpe"],3)*.2-m["max_dd_pct"]*4+math.log(max(m["stress_pf"],.05))*.6

def exit_diagnostics(strategy,variant="champion",since=None,limit=300):
    q="SELECT * FROM post_trade_studies WHERE strategy=? AND variant=? AND status='COMPLETE'";args=[strategy,variant]
    if since is not None:q+=" AND closed_at>=?";args.append(since)
    q+=" ORDER BY completed_at DESC LIMIT ?";args.append(limit);rows=db.query(q,tuple(args));flags={};captures=[];mfes=[];actual=[]
    for r in rows:
        try:a=json.loads(r["analysis_json"]);captures.append(float(a.get("capture_ratio",0)));mfes.append(float(a.get("in_trade_mfe_r",0)));actual.append(float(a.get("actual_r",0)));[flags.__setitem__(f,flags.get(f,0)+1) for f in a.get("flags",[])]
        except Exception:pass
    n=len(rows);ratios={k:v/n for k,v in flags.items()} if n else {};return {"n":n,"ratios":ratios,"avg_capture":statistics.fmean(captures) if captures else 0,"avg_mfe_r":statistics.fmean(mfes) if mfes else 0,"avg_actual_r":statistics.fmean(actual) if actual else 0}
def exit_quality(d):
    if not d["n"]:return 0
    bad=sum(d["ratios"].get(k,0) for k in ["STOP_TOO_TIGHT_CANDIDATE","TRAIL_TOO_TIGHT_CANDIDATE","TP_TOO_EARLY_CANDIDATE","TP_TOO_FAR_OR_GIVEBACK","LOW_EXIT_CAPTURE"]);return d["avg_capture"]-.25*bad

class LearningGovernor:
    def __init__(self):self.last_run=0
    def due(self):return settings.learning_enabled and time.time()-self.last_run>=settings.learning_interval_hours*3600
    def run(self,force=False):
        if not force and not self.due():return []
        self.last_run=time.time();out=[]
        for s in db.query("SELECT * FROM strategy_state WHERE enabled=1"):
            out+=self._stage(s);c=db.one("SELECT * FROM challengers WHERE strategy=? AND status='ACTIVE'",(s["strategy"],));out+=self._review(s,c) if c else self._create(s)
        return out
    def _stage(self,s):
        since=int(s.get("evidence_since") or 0);tr=completed_roundtrips(s["strategy"],"champion",2500,since);m=metrics(tr);rw=robust_windows(tr);days=sample_days(tr);out=[]
        if s["stage"]=="EARLY" and m["n"]>=90 and days>=14 and m["pf"]>=1.05 and m["expectancy"]>0 and m["max_dd_pct"]<=.18 and rw>=.5:
            p=risk_manager.sanitize(json.loads(s["params_json"]),"TUNING");now=int(time.time()*1000);db.update_strategy(s["strategy"],stage="TUNING",params_json=p,evidence_since=now);db.log_adjustment(s["strategy"],"stage",{"stage":"EARLY"},{"stage":"TUNING"},f"closed-K forward paper passed n={m['n']} {days:.1f}d PF={m['pf']:.2f} DD={m['max_dd_pct']:.1%} windows={rw:.2f}",True);out.append({"strategy":s["strategy"],"stage":"TUNING"})
        if s["stage"]=="TUNING" and m["n"]>=settings.final_min_trades and days>=settings.final_min_days and m["pf"]>=settings.final_min_pf and m["stress_pf"]>=settings.final_min_stress_pf and m["expectancy"]>0 and m["stress_expectancy"]>0 and m["max_dd_pct"]<=settings.final_max_dd_pct and rw>=settings.final_min_robust_windows and profit_concentration(tr)<=settings.final_max_symbol_profit_share and symbol_count(tr)>=settings.final_min_symbols:
            p=risk_manager.sanitize(json.loads(s["params_json"]),"FINAL");now=int(time.time()*1000);db.update_strategy(s["strategy"],stage="FINAL",params_json=p,live_eligible=True,final_since=now,evidence_since=now);db.log_adjustment(s["strategy"],"stage",{"stage":"TUNING"},{"stage":"FINAL","live_eligible":True},f"FINAL gate n={m['n']} {days:.1f}d PF={m['pf']:.2f} stressPF={m['stress_pf']:.2f} DD={m['max_dd_pct']:.1%} windows={rw:.2f}",True);out.append({"strategy":s["strategy"],"stage":"FINAL"})
        if s["stage"]=="FINAL":
            rm=metrics(completed_roundtrips(s["strategy"],"champion",100))
            if rm["n"]>=60 and (rm["pf"]<.95 or rm["expectancy"]<0 or rm["max_dd_pct"]>.15):
                now=int(time.time()*1000);db.update_strategy(s["strategy"],stage="TUNING",params_json=risk_manager.sanitize(json.loads(s["params_json"]),"TUNING"),live_eligible=False,live_manual_enabled=False,final_since=None,evidence_since=now);db.log_adjustment(s["strategy"],"demotion",{"stage":"FINAL"},{"stage":"TUNING"},"recent degradation; live eligibility and manual approval revoked",True);out.append({"strategy":s["strategy"],"stage":"TUNING","demoted":True})
        return out
    def _create(self,s):
        tr=completed_roundtrips(s["strategy"],"champion",settings.learning_recent_window);m=metrics(tr)
        if m["n"]<settings.learning_min_trades:return []
        days=sample_days(tr);rw=robust_windows(tr)
        if s["stage"]=="EARLY" and 7<=days<14 and m["n"]>=60 and m["pf"]>=1 and m["expectancy"]>0 and m["max_dd_pct"]<=.20 and rw>=.4:return []
        if s["stage"]=="TUNING" and 21<=days<settings.final_min_days and m["pf"]>=settings.final_min_pf*.90 and m["stress_pf"]>=1.05 and m["expectancy"]>0 and m["max_dd_pct"]<=settings.final_max_dd_pct*1.2 and rw>=.65:return []
        before=json.loads(s["params_json"]);after=deepcopy(before);bounds={**COMMON_BOUNDS,**SPECIFIC_BOUNDS.get(s["strategy"],{})};diag=exit_diagnostics(s["strategy"],"champion");issue=max(diag["ratios"].values(),default=0)
        attempts=int((db.one("SELECT COUNT(*)n FROM adjustments WHERE strategy=?",(s["strategy"],)) or {"n":0})["n"])
        domain="exit" if diag["n"]>=settings.post_trade_min_studies and issue>=settings.post_trade_min_signal_ratio else ("sizing" if m["max_dd_pct"]>.10 or (m["pf"]>1.35 and rw>=.65 and attempts%3==1) else "entry")
        reason=[];keys=[];directions={}
        if domain=="exit":
            r=diag["ratios"];sp=STOP_PARAM.get(s["strategy"])
            if sp and r.get("STOP_TOO_TIGHT_CANDIDATE",0)>=settings.post_trade_min_signal_ratio:keys.append(sp);directions[sp]=1
            elif sp and r.get("STOP_TOO_WIDE_OR_ENTRY_BAD",0)>=settings.post_trade_min_signal_ratio:keys.append(sp);directions[sp]=-1
            if r.get("TRAIL_TOO_TIGHT_CANDIDATE",0)>=settings.post_trade_min_signal_ratio:keys.append("trail_r");directions["trail_r"]=1
            if r.get("TP_TOO_EARLY_CANDIDATE",0)>=settings.post_trade_min_signal_ratio:keys+=["tp3_r","tp2_r"];directions.update({"tp3_r":1,"tp2_r":1})
            if r.get("TP_TOO_FAR_OR_GIVEBACK",0)>=settings.post_trade_min_signal_ratio or r.get("LOW_EXIT_CAPTURE",0)>=settings.post_trade_min_signal_ratio:keys+=["tp1_fraction","trail_r","tp1_r"];directions.update({"tp1_fraction":1,"trail_r":-1,"tp1_r":-1})
            if not keys:domain="entry"
        if domain=="sizing":
            keys=list(SIZING);down=m["max_dd_pct"]>.10 or m["pf"]<1.1
            for k in keys:directions[k]=-1 if down else 1
        if domain=="entry":
            sp=STOP_PARAM.get(s["strategy"]);keys=[k for k in SPECIFIC_BOUNDS.get(s["strategy"],{}) if k!=sp]
            if not keys:return []
            tighten_up={"min_rvol","min_oi_change","min_adx","z","imbalance","strength_ratio","funding_extreme","rsi_long","rsi_extreme"};tighten_down={"max_spread","compression_ratio","zone_tolerance","max_adx","max_context_adx","rsi_low","max_rsi"};bad=m["pf"]<1 or m["expectancy"]<0
            for k in keys:directions[k]=(1 if k in tighten_up else -1 if k in tighten_down else (1 if attempts%2==0 else -1)) if bad else (1 if attempts%2==0 else -1)
        keys=[k for k in dict.fromkeys(keys) if k in bounds and k in after]
        if keys:rot=attempts%len(keys);keys=keys[rot:]+keys[:rot]
        changed=0
        for k in keys:
            if changed>=settings.max_learning_changes:break
            lo,hi=bounds[k];v=float(after[k]);step=(hi-lo)*(.04 if domain!="sizing" else .05)*directions.get(k,1);nv=max(lo,min(hi,v+step));nv=int(round(nv)) if k in {"max_positions","lookback","min_adx","rsi_long","rsi_low","rsi_extreme","max_adx","max_context_adx","max_rsi"} else round(nv,8)
            if nv==after[k]:continue
            after[k]=nv;reason.append(f"{k}: {v} -> {nv}");changed+=1
        after=risk_manager.sanitize(after,s["stage"])
        if after==before:return []
        now=int(time.time()*1000);why=f"domain={domain}; strategy-isolated closed-K challenger; "+"; ".join(reason)
        if domain=="exit":why+=f"; post-trade n={diag['n']} capture={diag['avg_capture']:.2f} ratios={diag['ratios']}"
        db.execute("INSERT OR REPLACE INTO challengers(strategy,status,domain,params_json,baseline_json,reason,started_at,updated_at)VALUES(?,?,?,?,?,?,?,?)",(s["strategy"],"ACTIVE",domain,json.dumps(after),json.dumps(before),why,now,now));db.execute("DELETE FROM positions WHERE strategy=? AND variant='challenger'",(s["strategy"],));db.ensure_account(s["strategy"],"challenger",reset=True);db.log_adjustment(s["strategy"],"challenger_started",before,after,why,False);return [{"strategy":s["strategy"],"challenger":"started","domain":domain,"changes":reason}]
    def _review(self,s,c):
        since=int(c["started_at"]);ct=completed_roundtrips(s["strategy"],"challenger",1200,since);days=sample_days(ct)
        if len(ct)<settings.challenger_min_trades or days<settings.challenger_min_days:return []
        bt=completed_roundtrips(s["strategy"],"champion",1200,since);cm=metrics(ct);bm=metrics(bt);domain=c.get("domain") or "entry";delta=score(cm)-score(bm);accept=cm["expectancy"]>0 and cm["stress_pf"]>=1 and cm["max_dd_pct"]<=max(.18,bm["max_dd_pct"]+.03) and delta>=.08
        if domain=="exit":
            cd=exit_diagnostics(s["strategy"],"challenger",since);bd=exit_diagnostics(s["strategy"],"champion",since);accept=accept and cd["n"]>=max(10,settings.post_trade_min_studies//2) and exit_quality(cd)>=exit_quality(bd)+.04
        before=json.loads(c["baseline_json"]);after=json.loads(c["params_json"]);why=f"{domain} challenger n={cm['n']} PF={cm['pf']:.2f} stressPF={cm['stress_pf']:.2f} DD={cm['max_dd_pct']:.1%} scoreΔ={delta:.2f}"
        if accept:
            now=int(time.time()*1000);prom=risk_manager.sanitize(after,"TUNING" if s["stage"]=="FINAL" else s["stage"])
            if s["stage"]=="FINAL":db.update_strategy(s["strategy"],stage="TUNING",params_json=prom,live_eligible=False,live_manual_enabled=False,final_since=None,evidence_since=now);why+="; FINAL/live/manual approval revoked because champion parameters changed"
            else:db.update_strategy(s["strategy"],params_json=prom,evidence_since=now)
            db.log_adjustment(s["strategy"],"challenger_promoted",before,after,why,True)
        else:db.log_adjustment(s["strategy"],"challenger_rejected",before,after,why,False)
        db.execute("UPDATE challengers SET status=?,updated_at=? WHERE strategy=?",("PROMOTED" if accept else "REJECTED",int(time.time()*1000),s["strategy"]));db.execute("DELETE FROM positions WHERE strategy=? AND variant='challenger'",(s["strategy"],));db.ensure_account(s["strategy"],"challenger",reset=True);return [{"strategy":s["strategy"],"challenger":"promoted" if accept else "rejected","domain":domain,"reason":why}]

learner=LearningGovernor()
