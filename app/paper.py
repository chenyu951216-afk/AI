from __future__ import annotations
import json, time
from .config import settings
from .db import db
from .risk import risk_manager

class PaperBroker:
    def enter(self,signal,variant:str,params:dict,stage:str,regime:str,prices:dict[str,float]):
        ok,why=risk_manager.allow_entry(signal.strategy,variant,signal.symbol,signal.entry,signal.stop,params,stage,prices)
        if not ok:
            db.risk_event("PAPER_ENTRY_BLOCKED",why,signal.strategy,variant,signal.symbol); return None
        plan=risk_manager.size_and_targets(signal.strategy,variant,signal.symbol,signal.side,signal.entry,signal.stop,params,stage,prices)
        qty=float(plan["qty"])
        if qty<=0: db.risk_event("ZERO_SIZE","risk sizing returned zero",signal.strategy,variant,signal.symbol); return None
        slip=settings.paper_slippage_bps/10000; fill=signal.entry*(1+slip if signal.side=="long" else 1-slip)
        # Re-anchor R targets to simulated fill while preserving structural stop.
        dist=abs(fill-signal.stop); sign=1 if signal.side=="long" else -1; p=plan["params"]
        tp1=fill+sign*dist*p["tp1_r"]; tp2=fill+sign*dist*p["tp2_r"]; tp3=fill+sign*dist*p["tp3_r"]
        now=int(time.time()*1000)
        cur=db.execute("""INSERT INTO positions(strategy,variant,symbol,side,qty,entry,initial_stop,stop,tp1,tp2,tp3,tp1_fraction,tp2_fraction,remaining_qty,initial_risk_cash,leverage,opened_at,stage,regime,max_favorable,min_favorable)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (signal.strategy,variant,signal.symbol,signal.side,qty,fill,signal.stop,signal.stop,tp1,tp2,tp3,p["tp1_fraction"],p["tp2_fraction"],qty,plan["risk_cash"],p["leverage"],now,stage,regime,fill,fill))
        return int(cur.lastrowid)

    def manage(self,prices:dict[str,float]):
        for pos in db.query("SELECT * FROM positions ORDER BY opened_at"):
            px=float(prices.get(pos["symbol"]) or 0)
            if px<=0: continue
            side=pos["side"]; db.execute("UPDATE positions SET max_favorable=?,min_favorable=? WHERE id=?",(max(float(pos["max_favorable"]),px),min(float(pos["min_favorable"]),px),pos["id"]))
            if (side=="long" and px<=pos["stop"]) or (side=="short" and px>=pos["stop"]): self.close_piece(pos,px,float(pos["remaining_qty"]),"STOP"); continue
            pos=db.one("SELECT * FROM positions WHERE id=?",(pos["id"],)) or pos
            if not pos["tp1_hit"] and ((side=="long" and px>=pos["tp1"]) or (side=="short" and px<=pos["tp1"])):
                q=min(float(pos["remaining_qty"]),float(pos["qty"])*float(pos["tp1_fraction"])); self.close_piece(pos,px,q,"TP1",keep=True)
                db.execute("UPDATE positions SET tp1_hit=1,stop=? WHERE id=?",(pos["entry"],pos["id"]))
            pos=db.one("SELECT * FROM positions WHERE id=?",(pos["id"],))
            if not pos: continue
            if not pos["tp2_hit"] and ((side=="long" and px>=pos["tp2"]) or (side=="short" and px<=pos["tp2"])):
                q=min(float(pos["remaining_qty"]),float(pos["qty"])*float(pos["tp2_fraction"])); self.close_piece(pos,px,q,"TP2",keep=True)
                initial_r=abs(float(pos["entry"])-float(pos["initial_stop"])); lock=float(pos["entry"])+(0.35*initial_r if side=="long" else -0.35*initial_r)
                db.execute("UPDATE positions SET tp2_hit=1,stop=? WHERE id=?",(lock,pos["id"]))
            pos=db.one("SELECT * FROM positions WHERE id=?",(pos["id"],))
            if not pos: continue
            if ((side=="long" and px>=pos["tp3"]) or (side=="short" and px<=pos["tp3"])): self.close_piece(pos,px,float(pos["remaining_qty"]),"TP3"); continue
            if pos["tp1_hit"]:
                initial_r=abs(float(pos["entry"])-float(pos["initial_stop"])); params=self._params(pos["strategy"],pos["variant"]); trail=initial_r*float(params.get("trail_r",1.2)); candidate=px-trail if side=="long" else px+trail
                newstop=max(float(pos["stop"]),candidate) if side=="long" else min(float(pos["stop"]),candidate); db.execute("UPDATE positions SET stop=? WHERE id=?",(newstop,pos["id"]))

    def _params(self,strategy,variant):
        if variant=="challenger":
            c=db.one("SELECT params_json FROM challengers WHERE strategy=? AND status='ACTIVE'",(strategy,))
            if c:return json.loads(c["params_json"])
        s=db.state(strategy); return json.loads(s["params_json"]) if s else {}

    def close_piece(self,pos,price:float,qty:float,reason:str,keep:bool=False):
        if qty<=0:return
        slip=settings.paper_slippage_bps/10000; fill=price*(1-slip if pos["side"]=="long" else 1+slip); sign=1 if pos["side"]=="long" else -1
        gross=(fill-float(pos["entry"]))*qty*sign; fees=(float(pos["entry"])*qty+fill*qty)*settings.paper_taker_fee; net=gross-fees
        db.adjust_balance(pos["strategy"],pos["variant"],net); now=int(time.time()*1000)
        db.execute("""INSERT INTO trades(position_id,strategy,variant,symbol,side,entry,exit,qty,gross_pnl,fees,net_pnl,reason,initial_risk_cash,opened_at,closed_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(pos["id"],pos["strategy"],pos["variant"],pos["symbol"],pos["side"],pos["entry"],fill,qty,gross,fees,net,reason,pos["initial_risk_cash"],pos["opened_at"],now))
        rem=max(0,float(pos["remaining_qty"])-qty)
        if rem<1e-10 or (not keep and qty>=float(pos["remaining_qty"])-1e-10): db.execute("DELETE FROM positions WHERE id=?",(pos["id"],))
        else: db.execute("UPDATE positions SET remaining_qty=?,realized_pnl=realized_pnl+? WHERE id=?",(rem,net,pos["id"]))

paper_broker=PaperBroker()
