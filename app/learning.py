from __future__ import annotations
import json,math,statistics,time
from copy import deepcopy
from .config import settings
from .db import db
from .risk import COMMON_BOUNDS,risk_manager

SPECIFIC_BOUNDS={
 "smc_liquidity":{"min_rvol":(.70,1.8),"atr_buffer":(.12,.80),"sweep_lookback":(12,44),"min_reclaim_body":(.18,.70)},
 "oi_trend":{"min_oi_change":(.001,.03),"min_rvol":(.80,2.8),"min_adx":(10,32),"atr_stop":(.85,2.6),"breakout_lookback":(10,40),"max_extension_atr":(1.0,4.0)},
 "funding_squeeze":{"funding_extreme":(.00008,.002),"min_oi_change":(0,.02),"rsi_extreme":(60,84),"min_reversal_body":(.18,.75),"atr_stop":(.85,2.6)},
 "volume_breakout":{"min_rvol":(.85,3.2),"compression_ratio":(.50,1.05),"compression_lookback":(12,40),"breakout_lookback":(10,40),"atr_stop":(.85,2.5)},
 "fib_pullback":{"zone_tolerance":(.12,.90),"min_rvol":(.65,1.8),"lookback":(36,120),"confirmation_body":(.12,.70),"stop_buffer_atr":(.04,.90)},
 "vwap_ema":{"rsi_long":(44,62),"retest_lookback":(2,10),"atr_stop":(.75,2.2)},
 "mean_reversion":{"max_adx":(12,30),"max_context_adx":(14,32),"z":(1.15,3.0),"rsi_low":(22,42),"atr_stop":(.90,2.8)},
 "orderbook":{"imbalance":(.06,.48),"max_spread":(.0004,.0032),"min_rvol":(.55,1.9),"atr_stop":(.55,2.1)},
 "liquidation_magnet":{"strength_ratio":(1.02,3.2),"max_rsi":(56,82),"min_cluster_atr":(.05,1.2),"max_cluster_atr":(2.0,12.0),"atr_stop":(.80,2.6)},
 "loser_rebound_cycle":{"rank_max":(5,35),"min_drop_pct":(.06,.30),"base_lookback":(6,28),"min_rvol":(.60,1.8),"rebound_rsi":(28,52),"rejection_rsi":(36,64),"continuation_rvol":(.55,1.8),"atr_stop":(.80,2.6)},
 "gainer_pullback_cycle":{"rank_max":(5,35),"min_gain_pct":(.06,.30),"swing_lookback":(6,28),"exhaustion_rsi":(62,86),"exhaustion_rvol":(.75,2.5),"continuation_rsi":(46,68),"continuation_rvol":(.55,1.8),"atr_stop":(.80,2.6)},
}
STOP_PARAM={k:"atr_stop" for k in SPECIFIC_BOUNDS};STOP_PARAM["smc_liquidity"]="atr_buffer";STOP_PARAM["fib_pullback"]="stop_buffer_atr"
SIZING=["risk_pct","leverage","max_positions","max_position_notional_pct","max_symbol_exposure_pct","max_total_exposure_pct"]
EXITS=["sl1_r","sl1_fraction","tp1_r","tp2_r","tp3_r","tp1_fraction","tp2_fraction","breakeven_trigger_r","breakeven_offset_r","trail_start_r","trail_r"]
INT_KEYS={"max_positions","lookback","min_adx","rsi_long","rsi_low","rsi_extreme","max_adx","max_context_adx","max_rsi","sweep_lookback","breakout_lookback","compression_lookback","retest_lookback","rank_max","base_lookback","rebound_rsi","rejection_rsi","swing_lookback","exhaustion_rsi","continuation_rsi"}

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

def exit_diagnostics(strategy,variant="champion",since=None,limit=400):
    q="SELECT * FROM post_trade_studies WHERE strategy=? AND variant=? AND status='COMPLETE'";args=[strategy,variant]
    if since is not None:q+=" AND closed_at>=?";args.append(since)
    q+=" ORDER BY completed_at DESC LIMIT ?";args.append(limit);rows=db.query(q,tuple(args));flags={};captures=[];mfes=[];actual=[]
    for r in rows:
        try:
            a=json.loads(r["analysis_json"]);captures.append(float(a.get("capture_ratio",0)));mfes.append(float(a.get("in_trade_mfe_r",0)));actual.append(float(a.get("actual_r",0)))
            for f in a.get("flags",[]):flags[f]=flags.get(f,0)+1
        except Exception:pass
    n=len(rows);ratios={k:v/n for k,v in flags.items()} if n else {};return {"n":n,"ratios":ratios,"avg_capture":statistics.fmean(captures) if captures else 0,"avg_mfe_r":statistics.fmean(mfes) if mfes else 0,"avg_actual_r":statistics.fmean(actual) if actual else 0}
def exit_quality(d):
    if not d["n"]:return 0
    bad=sum(d["ratios"].get(k,0) for k in ["PARTIAL_SL_TOO_EARLY_CANDIDATE","STOP_TOO_TIGHT_CANDIDATE","TRAIL_OR_BE_TOO_TIGHT_CANDIDATE","TP_TOO_EARLY_CANDIDATE","TP_TOO_FAR_OR_GIVEBACK","LOW_EXIT_CAPTURE"]);return d["avg_capture"]-.20*bad

def learning_progress(trades,m,diag):
    """Live-readiness maturity recalculated from current evidence; it intentionally can move backwards."""
    n=m["n"];days=sample_days(trades);rw=robust_windows(trades);syms=symbol_count(trades);conc=profit_concentration(trades) if trades else 1.
    def cap(x):return max(0.,min(1.,x))
    sample=cap(n/max(settings.final_min_trades,1));age=cap(days/max(settings.final_min_days,1));pf=cap(m["pf"]/max(settings.final_min_pf,1e-9));spf=cap(m["stress_pf"]/max(settings.final_min_stress_pf,1e-9));exp=cap(.5+m["avg_r"]/.5) if n else 0;dd=cap(1-m["max_dd_pct"]/max(settings.final_max_dd_pct*1.8,.01)) if n>=10 else 0.;rob=cap(rw/max(settings.final_min_robust_windows,1e-9));div=cap(syms/max(settings.final_min_symbols,1));con=cap((1-conc)/max(1-settings.final_max_symbol_profit_share,1e-9)) if n>=20 else 0.;post=cap(diag["n"]/max(settings.post_trade_min_studies*2,1))
    parts={"樣本":sample,"天數":age,"PF":pf,"StressPF":spf,"期望值":exp,"回撤":dd,"穩定窗":rob,"幣種分散":div,"收益分散":con,"出場檢討":post};weights={"樣本":.16,"天數":.10,"PF":.14,"StressPF":.10,"期望值":.10,"回撤":.11,"穩定窗":.11,"幣種分散":.05,"收益分散":.05,"出場檢討":.08};pct=round(100*sum(parts[k]*weights[k] for k in weights),1)
    if n<10:pct=min(pct,22.)
    if n>=20 and m["expectancy"]<=0:pct=min(pct,58.)
    if n>=40 and m["pf"]<.85:pct=min(pct,48.)
    weakest=sorted(parts.items(),key=lambda kv:kv[1])[:3];return {"pct":pct,"components":{k:round(v*100,1) for k,v in parts.items()},"weakest":[{"name":k,"pct":round(v*100,1)} for k,v in weakest]}

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
            p=risk_manager.sanitize(json.loads(s["params_json"]),"TUNING");now=int(time.time()*1000);db.update_strategy(s["strategy"],stage="TUNING",params_json=p,evidence_since=now);db.log_adjustment(s["strategy"],"stage",{"stage":"EARLY"},{"stage":"TUNING"},f"closed-K forward gate n={m['n']} {days:.1f}d PF={m['pf']:.2f} DD={m['max_dd_pct']:.1%} windows={rw:.2f}",True);out.append({"strategy":s["strategy"],"stage":"TUNING"})
        if s["stage"]=="TUNING" and m["n"]>=settings.final_min_trades and days>=settings.final_min_days and m["pf"]>=settings.final_min_pf and m["stress_pf"]>=settings.final_min_stress_pf and m["expectancy"]>0 and m["stress_expectancy"]>0 and m["max_dd_pct"]<=settings.final_max_dd_pct and rw>=settings.final_min_robust_windows and profit_concentration(tr)<=settings.final_max_symbol_profit_share and symbol_count(tr)>=settings.final_min_symbols:
            p=risk_manager.sanitize(json.loads(s["params_json"]),"FINAL");now=int(time.time()*1000);db.update_strategy(s["strategy"],stage="FINAL",params_json=p,live_eligible=True,final_since=now,evidence_since=now);db.log_adjustment(s["strategy"],"stage",{"stage":"TUNING"},{"stage":"FINAL","live_eligible":True},f"FINAL gate n={m['n']} {days:.1f}d PF={m['pf']:.2f} stressPF={m['stress_pf']:.2f} DD={m['max_dd_pct']:.1%} windows={rw:.2f}",True);out.append({"strategy":s["strategy"],"stage":"FINAL"})
        if s["stage"]=="FINAL":
            rm=metrics(completed_roundtrips(s["strategy"],"champion",100))
            if rm["n"]>=60 and (rm["pf"]<.95 or rm["expectancy"]<0 or rm["max_dd_pct"]>.15):
                now=int(time.time()*1000);db.update_strategy(s["strategy"],stage="TUNING",params_json=risk_manager.sanitize(json.loads(s["params_json"]),"TUNING"),live_eligible=False,live_manual_enabled=False,final_since=None,evidence_since=now);db.log_adjustment(s["strategy"],"demotion",{"stage":"FINAL"},{"stage":"TUNING"},"recent degradation; live eligibility/manual approval revoked",True);out.append({"strategy":s["strategy"],"stage":"TUNING","demoted":True})
        return out
    def _create(self,s):
        tr=completed_roundtrips(s["strategy"],"champion",settings.learning_recent_window);m=metrics(tr)
        if m["n"]<settings.learning_min_trades:return []
        days=sample_days(tr);rw=robust_windows(tr)
        if s["stage"]=="EARLY" and 7<=days<14 and m["n"]>=60 and m["pf"]>=1 and m["expectancy"]>0 and m["max_dd_pct"]<=.20 and rw>=.4:return []
        if s["stage"]=="TUNING" and 21<=days<settings.final_min_days and m["pf"]>=settings.final_min_pf*.90 and m["stress_pf"]>=1.05 and m["expectancy"]>0 and m["max_dd_pct"]<=settings.final_max_dd_pct*1.2 and rw>=.65:return []
        before=json.loads(s["params_json"]);after=deepcopy(before);bounds={**COMMON_BOUNDS,**SPECIFIC_BOUNDS.get(s["strategy"],{})};diag=exit_diagnostics(s["strategy"],"champion");issue=max(diag["ratios"].values(),default=0);attempts=int((db.one("SELECT COUNT(*)n FROM adjustments WHERE strategy=? AND kind='challenger_started'",(s["strategy"],)) or {"n":0})["n"])
        if attempts%3==0:domain="entry"
        elif diag["n"]>=settings.post_trade_min_studies and issue>=settings.post_trade_min_signal_ratio:domain="exit"
        elif m["max_dd_pct"]>.10 or (m["pf"]>1.35 and rw>=.65):domain="sizing"
        else:domain="entry"
        reason=[];keys=[];directions={}
        if domain=="exit":
            r=diag["ratios"];sp=STOP_PARAM.get(s["strategy"])
            if r.get("PARTIAL_SL_TOO_EARLY_CANDIDATE",0)>=settings.post_trade_min_signal_ratio:keys+=["sl1_r","sl1_fraction"];directions.update({"sl1_r":1,"sl1_fraction":-1})
            if sp and r.get("STOP_TOO_TIGHT_CANDIDATE",0)>=settings.post_trade_min_signal_ratio:keys.append(sp);directions[sp]=1
            elif sp and r.get("STOP_TOO_WIDE_OR_ENTRY_BAD",0)>=settings.post_trade_min_signal_ratio:keys.append(sp);directions[sp]=-1
            if r.get("TRAIL_OR_BE_TOO_TIGHT_CANDIDATE",0)>=settings.post_trade_min_signal_ratio:keys+=["breakeven_trigger_r","trail_start_r","trail_r"];directions.update({"breakeven_trigger_r":1,"trail_start_r":1,"trail_r":1})
            if r.get("TP_TOO_EARLY_CANDIDATE",0)>=settings.post_trade_min_signal_ratio:keys+=["tp2_r","tp3_r"];directions.update({"tp2_r":1,"tp3_r":1})
            if r.get("TP_TOO_FAR_OR_GIVEBACK",0)>=settings.post_trade_min_signal_ratio or r.get("LOW_EXIT_CAPTURE",0)>=settings.post_trade_min_signal_ratio:keys+=["tp1_fraction","tp1_r","trail_start_r"];directions.update({"tp1_fraction":1,"tp1_r":-1,"trail_start_r":-1})
            if not keys:domain="entry"
        if domain=="sizing":
            keys=list(SIZING);down=m["max_dd_pct"]>.10 or m["pf"]<1.1
            for k in keys:directions[k]=-1 if down else 1
        if domain=="entry":
            sp=STOP_PARAM.get(s["strategy"]);keys=[k for k in SPECIFIC_BOUNDS.get(s["strategy"],{}) if k!=sp]
            if not keys:return []
            bad=m["pf"]<1 or m["expectancy"]<0;direction=1 if (bad or attempts%2==0) else -1;looser={"max_spread","compression_ratio","zone_tolerance","max_adx","max_context_adx","max_rsi","max_extension_atr","max_cluster_atr","rank_max","base_lookback","swing_lookback","retest_lookback","sweep_lookback","breakout_lookback","compression_lookback"}
            for k in keys:directions[k]=(-direction if k in looser else direction)
        keys=[k for k in dict.fromkeys(keys) if k in bounds and k in after]
        if keys:rot=attempts%len(keys);keys=keys[rot:]+keys[:rot]
        changed=0
        for k in keys:
            if changed>=settings.max_learning_changes:break
            lo,hi=bounds[k];v=float(after[k]);step=(hi-lo)*(.035 if domain=="entry" else .04 if domain=="exit" else .05)*directions.get(k,1);nv=max(lo,min(hi,v+step));nv=int(round(nv)) if k in INT_KEYS else round(nv,8)
            if nv==after[k]:continue
            after[k]=nv;reason.append(f"{k}: {v} -> {nv}");changed+=1
        after=risk_manager.sanitize(after,s["stage"])
        if after==before:return []
        now=int(time.time()*1000);why=f"domain={domain}; own-strategy closed-K challenger; "+"; ".join(reason)
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
