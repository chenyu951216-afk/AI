from __future__ import annotations
import json,time
from .config import settings
from .db import db
from .risk import risk_manager

class PaperBroker:
    def enter(self,signal,variant,params,stage,regime,prices):
        ok,why=risk_manager.allow_entry(signal.strategy,variant,signal.symbol,signal.entry,signal.stop,params,stage,prices)
        if not ok:db.risk_event("PAPER_ENTRY_BLOCKED",why,signal.strategy,variant,signal.symbol);return None
        plan=risk_manager.size_and_targets(signal.strategy,variant,signal.symbol,signal.side,signal.entry,signal.stop,params,stage,prices);qty=float(plan["qty"])
        if qty<=0:db.risk_event("ZERO_SIZE","risk sizing returned zero",signal.strategy,variant,signal.symbol);return None
        slip=settings.paper_slippage_bps/10000;fill=signal.entry*(1+slip if signal.side=="long" else 1-slip);dist=abs(fill-signal.stop);sg=1 if signal.side=="long" else -1;p=plan["params"]
        sl1=fill-sg*dist*p["sl1_r"];tp1=fill+sg*dist*p["tp1_r"];tp2=fill+sg*dist*p["tp2_r"];tp3=fill+sg*dist*p["tp3_r"];now=int(time.time()*1000)
        cur=db.execute("""INSERT INTO positions(strategy,variant,symbol,side,qty,entry,initial_stop,stop,tp1,tp2,tp3,tp1_fraction,tp2_fraction,remaining_qty,initial_risk_cash,leverage,opened_at,stage,regime,signal_tf,signal_bar_ts,params_json,last_observed_bar_ts,max_favorable,min_favorable,sl1_hit)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",(signal.strategy,variant,signal.symbol,signal.side,qty,fill,signal.stop,signal.stop,tp1,tp2,tp3,p["tp1_fraction"],p["tp2_fraction"],qty,plan["risk_cash"],p["leverage"],now,stage,regime,signal.signal_tf,signal.bar_ts,json.dumps(p),signal.bar_ts,fill,fill))
        db.event("PAPER_ENTRY",f"{signal.strategy} {signal.symbol} {signal.side} entry={fill:.10g} sl1={sl1:.10g} stop={signal.stop:.10g} tp={tp1:.10g}/{tp2:.10g}/{tp3:.10g}")
        return int(cur.lastrowid)
    def _sl1_price(self,pos,p):
        r=abs(float(pos["entry"])-float(pos["initial_stop"]));sg=1 if pos["side"]=="long" else -1;return float(pos["entry"])-sg*r*float(p.get("sl1_r",.62))
    def manage(self,prices):
        for pos in db.query("SELECT * FROM positions ORDER BY opened_at"):
            px=float(prices.get(pos["symbol"]) or 0)
            if px<=0:continue
            side=pos["side"];p=json.loads(pos.get("params_json") or "{}")
            if (side=="long" and px<=pos["stop"]) or (side=="short" and px>=pos["stop"]):self.close_piece(pos,px,float(pos["remaining_qty"]),"STOP");continue
            sl1=self._sl1_price(pos,p)
            if not pos.get("sl1_hit") and ((side=="long" and px<=sl1) or (side=="short" and px>=sl1)):
                q=min(float(pos["remaining_qty"]),float(pos["qty"])*float(p.get("sl1_fraction",.18)));self.close_piece(pos,px,q,"SL1",True);db.execute("UPDATE positions SET sl1_hit=1 WHERE id=?",(pos["id"],))
            pos=db.one("SELECT * FROM positions WHERE id=?",(pos["id"],))
            if not pos:continue
            if not pos["tp1_hit"] and ((side=="long" and px>=pos["tp1"]) or (side=="short" and px<=pos["tp1"])):
                q=min(float(pos["remaining_qty"]),float(pos["qty"])*float(pos["tp1_fraction"]));self.close_piece(pos,px,q,"TP1",True);db.execute("UPDATE positions SET tp1_hit=1 WHERE id=?",(pos["id"],))
            pos=db.one("SELECT * FROM positions WHERE id=?",(pos["id"],))
            if not pos:continue
            if not pos["tp2_hit"] and ((side=="long" and px>=pos["tp2"]) or (side=="short" and px<=pos["tp2"])):
                q=min(float(pos["remaining_qty"]),float(pos["qty"])*float(pos["tp2_fraction"]));self.close_piece(pos,px,q,"TP2",True);db.execute("UPDATE positions SET tp2_hit=1 WHERE id=?",(pos["id"],))
            pos=db.one("SELECT * FROM positions WHERE id=?",(pos["id"],))
            if not pos:continue
            if (side=="long" and px>=pos["tp3"]) or (side=="short" and px<=pos["tp3"]):self.close_piece(pos,px,float(pos["remaining_qty"]),"TP3")
    def observe_snapshot(self,snap):
        for pos in db.query("SELECT * FROM positions WHERE symbol=?",(snap.symbol,)):
            tf=pos["signal_tf"];d=snap.df(tf);rows=d[d.ts>int(pos["last_observed_bar_ts"])]
            for _,bar in rows.iterrows():self._observe_open_bar(pos,bar,tf);pos=db.one("SELECT * FROM positions WHERE id=?",(pos["id"],)) or pos
        if settings.post_trade_enabled:
            for st in db.query("SELECT * FROM post_trade_studies WHERE symbol=? AND status='FOLLOWING'",(snap.symbol,)):
                tf=st["signal_tf"];d=snap.df(tf);rows=d[d.ts>int(st["last_bar_ts"])]
                for _,bar in rows.iterrows():
                    db.execute("INSERT OR IGNORE INTO trade_path_bars(position_id,strategy,variant,symbol,phase,tf,ts,open,high,low,close)VALUES(?,?,?,?,?,?,?,?,?,?,?)",(st["position_id"],st["strategy"],st["variant"],st["symbol"],"AFTER",tf,int(bar.ts),float(bar.open),float(bar.high),float(bar.low),float(bar.close)));db.execute("UPDATE post_trade_studies SET last_bar_ts=?,bars_after=bars_after+1 WHERE position_id=?",(int(bar.ts),st["position_id"]));st=db.one("SELECT * FROM post_trade_studies WHERE position_id=?",(st["position_id"],)) or st
                    if int(st["bars_after"])>=settings.post_trade_follow_bars:self._finish_study(st);break
    def _observe_open_bar(self,pos,bar,tf):
        db.execute("INSERT OR IGNORE INTO trade_path_bars(position_id,strategy,variant,symbol,phase,tf,ts,open,high,low,close)VALUES(?,?,?,?,?,?,?,?,?,?,?)",(pos["id"],pos["strategy"],pos["variant"],pos["symbol"],"IN_TRADE",tf,int(bar.ts),float(bar.open),float(bar.high),float(bar.low),float(bar.close)))
        mx=max(float(pos["max_favorable"]),float(bar.high));mn=min(float(pos["min_favorable"]),float(bar.low));p=json.loads(pos.get("params_json") or "{}");r=max(abs(float(pos["entry"])-float(pos["initial_stop"])),1e-12);sg=1 if pos["side"]=="long" else -1;close_r=(float(bar.close)-float(pos["entry"]))*sg/r;newstop=float(pos["stop"])
        if close_r>=float(p.get("breakeven_trigger_r",.85)):
            be=float(pos["entry"])+sg*r*float(p.get("breakeven_offset_r",.04));newstop=max(newstop,be) if sg>0 else min(newstop,be)
        if close_r>=float(p.get("trail_start_r",1.35)):
            trail=r*float(p.get("trail_r",1.0));candidate=float(bar.close)-sg*trail;newstop=max(newstop,candidate) if sg>0 else min(newstop,candidate)
        db.execute("UPDATE positions SET max_favorable=?,min_favorable=?,stop=?,last_observed_bar_ts=? WHERE id=?",(mx,mn,newstop,int(bar.ts),pos["id"]))
    def close_piece(self,pos,price,qty,reason,keep=False):
        if qty<=0:return
        slip=settings.paper_slippage_bps/10000;fill=price*(1-slip if pos["side"]=="long" else 1+slip);sg=1 if pos["side"]=="long" else -1;gross=(fill-float(pos["entry"]))*qty*sg;fees=(float(pos["entry"])*qty+fill*qty)*settings.paper_taker_fee;net=gross-fees;db.adjust_balance(pos["strategy"],pos["variant"],net);now=int(time.time()*1000)
        db.execute("INSERT INTO trades(position_id,strategy,variant,symbol,side,entry,exit,qty,gross_pnl,fees,net_pnl,reason,initial_risk_cash,opened_at,closed_at)VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(pos["id"],pos["strategy"],pos["variant"],pos["symbol"],pos["side"],pos["entry"],fill,qty,gross,fees,net,reason,pos["initial_risk_cash"],pos["opened_at"],now));rem=max(0,float(pos["remaining_qty"])-qty);full=rem<1e-10 or (not keep and qty>=float(pos["remaining_qty"])-1e-10)
        if full:db.execute("DELETE FROM positions WHERE id=?",(pos["id"],));self._start_study(pos,fill,reason,now)
        else:db.execute("UPDATE positions SET remaining_qty=?,realized_pnl=realized_pnl+? WHERE id=?",(rem,net,pos["id"]))
    def _start_study(self,pos,final_exit,reason,closed_at):
        if not settings.post_trade_enabled:return
        db.execute("INSERT OR REPLACE INTO post_trade_studies(position_id,strategy,variant,symbol,side,signal_tf,entry,initial_stop,final_exit,exit_reason,initial_risk_cash,opened_at,closed_at,last_bar_ts,bars_after,status,analysis_json)VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'FOLLOWING','{}')",(pos["id"],pos["strategy"],pos["variant"],pos["symbol"],pos["side"],pos["signal_tf"],pos["entry"],pos["initial_stop"],final_exit,reason,pos["initial_risk_cash"],pos["opened_at"],closed_at,int(pos.get("last_observed_bar_ts") or pos["signal_bar_ts"])))
    def _finish_study(self,st):
        bars=db.query("SELECT * FROM trade_path_bars WHERE position_id=? ORDER BY ts",(st["position_id"],));inside=[x for x in bars if x["phase"]=="IN_TRADE"];after=[x for x in bars if x["phase"]=="AFTER"];r=max(abs(float(st["entry"])-float(st["initial_stop"])),1e-12);side=st["side"]
        def mfe(rows,anchor):return max([((float(x["high"])-anchor)/r if side=="long" else (anchor-float(x["low"]))/r) for x in rows] or [0.])
        def mae(rows,anchor):return max([((anchor-float(x["low"]))/r if side=="long" else (float(x["high"])-anchor)/r) for x in rows] or [0.])
        in_mfe=max(0,mfe(inside,float(st["entry"])));in_mae=max(0,mae(inside,float(st["entry"])));after_entry_mfe=max(0,mfe(after,float(st["entry"])));after_ext=max(0,mfe(after,float(st["final_exit"])));after_mae=max(0,mae(after,float(st["final_exit"])))
        pieces=db.query("SELECT reason,net_pnl FROM trades WHERE position_id=? ORDER BY closed_at,id",(st["position_id"],));actual_r=sum(float(x["net_pnl"]) for x in pieces)/max(float(st["initial_risk_cash"]),1e-12);had_sl1=any(x["reason"]=="SL1" for x in pieces);had1=any(x["reason"]=="TP1" for x in pieces);had2=any(x["reason"]=="TP2" for x in pieces);had3=any(x["reason"]=="TP3" for x in pieces);capture=max(0,min(2,actual_r/max(in_mfe,.15))) if in_mfe>0 else 0;flags=[]
        if had_sl1 and (had2 or had3 or actual_r>.7):flags.append("PARTIAL_SL_TOO_EARLY_CANDIDATE")
        if st["exit_reason"]=="STOP" and not had1 and after_entry_mfe>=1.0:flags.append("STOP_TOO_TIGHT_CANDIDATE")
        if st["exit_reason"]=="STOP" and not had1 and in_mfe<.30 and in_mae>=.85 and after_entry_mfe<.45:flags.append("STOP_TOO_WIDE_OR_ENTRY_BAD")
        if had1 and st["exit_reason"]=="STOP" and after_ext>=.65:flags.append("TRAIL_OR_BE_TOO_TIGHT_CANDIDATE")
        if st["exit_reason"]=="TP3" and after_ext>=.75:flags.append("TP_TOO_EARLY_CANDIDATE")
        if in_mfe>=1.15 and actual_r<=.10:flags.append("TP_TOO_FAR_OR_GIVEBACK")
        if in_mfe>=1.5 and capture<.35:flags.append("LOW_EXIT_CAPTURE")
        reason=f"持倉 MFE={in_mfe:.2f}R/MAE={in_mae:.2f}R，實現={actual_r:.2f}R；出場後 {settings.post_trade_follow_bars} 根已收 {st['signal_tf']} K 延伸={after_ext:.2f}R、相對進場最佳={after_entry_mfe:.2f}R。";reason+=(" 診斷："+", ".join(flags)) if flags else " 未形成足夠證據判定出場明顯失真。"
        a={"in_trade_mfe_r":round(in_mfe,4),"in_trade_mae_r":round(in_mae,4),"after_entry_mfe_r":round(after_entry_mfe,4),"after_extension_r":round(after_ext,4),"after_mae_r":round(after_mae,4),"actual_r":round(actual_r,4),"capture_ratio":round(capture,4),"had_sl1":had_sl1,"had_tp1":had1,"had_tp2":had2,"flags":flags,"reason":reason};db.execute("UPDATE post_trade_studies SET status='COMPLETE',analysis_json=?,completed_at=? WHERE position_id=?",(json.dumps(a,ensure_ascii=False),int(time.time()*1000),st["position_id"]))

paper_broker=PaperBroker()
