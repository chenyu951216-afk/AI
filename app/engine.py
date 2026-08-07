from __future__ import annotations

import asyncio, json, math, time, traceback

from .config import settings
from .db import db
from .learning import learner
from .market import market
from .models import Position
from .strategies import build_strategies, SPECS


class Engine:
    def __init__(self):
        self.strategies=build_strategies(); self.running=False; self.last_scan=None; self.last_error=None; self.universe=[]
        for s in SPECS: db.ensure_strategy(s.name,s.display_name,s.stage,s.params,settings.initial_paper_equity)

    def _state(self,name): return db.one("SELECT * FROM strategy_state WHERE strategy=?",(name,))

    async def start(self):
        if self.running: return
        self.running=True
        asyncio.create_task(self.loop())

    async def loop(self):
        while self.running:
            try:
                self.universe=await market.refresh_tickers(); await self.manage_positions()
                batch=market.batch_for_cycle(self.universe)
                for symbol in batch:
                    await self.scan_symbol(symbol)
                    await asyncio.sleep(0.08)
                learner.run(); self.last_scan=int(time.time()*1000); self.last_error=None
            except Exception as e:
                self.last_error=f"{type(e).__name__}: {e}"; traceback.print_exc()
            await asyncio.sleep(settings.scan_interval_sec)

    async def scan_symbol(self,symbol):
        # First build base snapshot. Depth and CoinGlass are fetched only when their specific strategy is evaluated.
        base=await market.snapshot(symbol)
        if not base: return
        for st in self.strategies:
            state=self._state(st.spec.name)
            if not state: continue
            params=json.loads(state["params_json"]); st.spec.params=params; st.spec.stage=state["stage"]
            if self._position_exists(st.spec.name,symbol): continue
            if self._open_count(st.spec.name)>=settings.max_open_positions_per_strategy: continue
            snap=base
            if st.needs_depth:
                try: base.orderbook_imbalance=await market.bitget.depth_imbalance(symbol)
                except Exception: base.orderbook_imbalance=None
            if st.needs_liquidation:
                liq=await market.cg.liquidation_clusters(symbol,base.price)
                base.liquidation_above=liq.get("above"); base.liquidation_below=liq.get("below"); base.liquidation_above_strength=liq.get("above_strength",0); base.liquidation_below_strength=liq.get("below_strength",0)
            sig=st.evaluate(snap)
            if sig and sig.score>=st.spec.min_score:
                self.log_signal(sig); self.paper_enter(sig,state)

    def log_signal(self,s):
        db.execute("INSERT INTO signals(strategy,symbol,side,score,entry,stop,tps_json,reason,features_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
          (s.strategy,s.symbol,s.side,s.score,s.entry,s.stop,json.dumps(s.take_profits),s.reason,json.dumps(s.features),s.created_at))

    def _open_count(self,strategy): return int((db.one("SELECT COUNT(*) n FROM positions WHERE strategy=?",(strategy,)) or {"n":0})["n"])
    def _position_exists(self,strategy,symbol): return bool(db.one("SELECT id FROM positions WHERE strategy=? AND symbol=?",(strategy,symbol)))

    def paper_enter(self,s,state):
        equity=self.equity(s.strategy)
        day_ago=int(time.time()*1000)-24*3600*1000
        daily=(db.one("SELECT COALESCE(SUM(net_pnl),0) x FROM trades WHERE strategy=? AND closed_at>=?",(s.strategy,day_ago)) or {"x":0})["x"]
        if float(daily) <= -0.03*float(state["initial_balance"]):
            return
        risk_pct={"EARLY":0.015,"TUNING":0.010,"FINAL":0.006}.get(state["stage"],0.01)
        risk_cash=equity*risk_pct; dist=abs(s.entry-s.stop)
        if dist<=0: return
        qty=risk_cash/dist
        max_notional=equity*settings.paper_max_leverage
        qty=min(qty,max_notional/s.entry)
        if qty<=0: return
        slip=settings.paper_slippage_bps/10000; fill=s.entry*(1+slip if s.side=="long" else 1-slip)
        tps=(s.take_profits+[s.take_profits[-1]]*3)[:3]
        db.execute("INSERT INTO positions(strategy,symbol,side,qty,entry,stop,tp1,tp2,tp3,remaining_qty,opened_at,trailing_atr,stage) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (s.strategy,s.symbol,s.side,qty,fill,s.stop,tps[0],tps[1],tps[2],qty,int(time.time()*1000),s.trailing_atr,state["stage"]))

    def equity(self,strategy):
        s=self._state(strategy); bal=float(s["balance"]) if s else settings.initial_paper_equity
        unreal=0
        for p in db.query("SELECT * FROM positions WHERE strategy=?",(strategy,)):
            px=market.ticker_price(p["symbol"]) or p["entry"]
            unreal+=(px-p["entry"])*p["remaining_qty"]*(1 if p["side"]=="long" else -1)
        return bal+unreal

    async def manage_positions(self):
        for p in db.query("SELECT * FROM positions"):
            px=market.ticker_price(p["symbol"])
            if px<=0: continue
            side=p["side"]; hit_stop=(px<=p["stop"] if side=="long" else px>=p["stop"])
            if hit_stop:
                self.close_piece(p,px,p["remaining_qty"],"STOP"); continue
            if not p["tp1_hit"] and (px>=p["tp1"] if side=="long" else px<=p["tp1"]):
                q=p["qty"]*0.30; self.close_piece(p,px,min(q,p["remaining_qty"]),"TP1",keep=True)
                db.execute("UPDATE positions SET tp1_hit=1, stop=? WHERE id=?",(p["entry"],p["id"]))
                p=db.one("SELECT * FROM positions WHERE id=?",(p["id"],)) or p
            if p and not p["tp2_hit"] and (px>=p["tp2"] if side=="long" else px<=p["tp2"]):
                q=p["qty"]*0.35; self.close_piece(p,px,min(q,p["remaining_qty"]),"TP2",keep=True)
                r=abs(p["entry"]-p["stop"]); newstop=p["entry"]+0.5*r if side=="long" else p["entry"]-0.5*r
                db.execute("UPDATE positions SET tp2_hit=1, stop=? WHERE id=?",(newstop,p["id"]))
                p=db.one("SELECT * FROM positions WHERE id=?",(p["id"],)) or p
            if p and (px>=p["tp3"] if side=="long" else px<=p["tp3"]):
                self.close_piece(p,px,p["remaining_qty"],"TP3"); continue
            # ATR-style trailing after TP1 uses entry risk as a stable proxy to avoid extra API calls.
            p=db.one("SELECT * FROM positions WHERE id=?",(p["id"],))
            if p and p["tp1_hit"]:
                initial_r=abs(p["tp1"]-p["entry"])
                trail=initial_r*float(p["trailing_atr"])
                candidate=px-trail if side=="long" else px+trail
                newstop=max(p["stop"],candidate) if side=="long" else min(p["stop"],candidate)
                db.execute("UPDATE positions SET stop=? WHERE id=?",(newstop,p["id"]))

    def close_piece(self,p,price,qty,reason,keep=False):
        if qty<=0: return
        slip=settings.paper_slippage_bps/10000; fill=price*(1-slip if p["side"]=="long" else 1+slip)
        gross=(fill-p["entry"])*qty*(1 if p["side"]=="long" else -1)
        fee=(p["entry"]*qty+fill*qty)*settings.paper_taker_fee; net=gross-fee
        risk=max(abs(p["entry"]-p["stop"])*qty,1e-9); rmult=net/risk
        db.adjust_balance(p["strategy"],net)
        db.execute("INSERT INTO trades(position_id,strategy,symbol,side,entry,exit,qty,gross_pnl,fees,net_pnl,r_multiple,reason,opened_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (p["id"],p["strategy"],p["symbol"],p["side"],p["entry"],fill,qty,gross,fee,net,rmult,reason,p["opened_at"],int(time.time()*1000)))
        rem=max(0,float(p["remaining_qty"])-qty)
        if rem<1e-12 or not keep and qty>=float(p["remaining_qty"])-1e-12:
            db.execute("DELETE FROM positions WHERE id=?",(p["id"],))
        else:
            db.execute("UPDATE positions SET remaining_qty=?, realized_pnl=realized_pnl+? WHERE id=?",(rem,net,p["id"]))

engine=Engine()
