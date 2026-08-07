from __future__ import annotations
import asyncio, json, time, traceback
from .config import settings
from .db import db
from .learning import learner
from .live import live_adapter
from .market import market
from .paper import paper_broker
from .strategies import SPECS, build_strategy

class Engine:
    def __init__(self):
        self.running=False; self.last_scan=None; self.last_error=None; self.universe=[]; self.scan_count=0
        for s in SPECS: db.ensure_strategy(s.name,s.display_name,s.description,s.params)
        if db.runtime_get("live_master_enabled","")=="": db.runtime_set("live_master_enabled","false")
    async def start(self):
        if self.running:return
        self.running=True; asyncio.create_task(self.loop())
    async def loop(self):
        while self.running:
            try:
                self.universe=await market.refresh_universe(); prices=market.prices(); paper_broker.manage(prices)
                if settings.live_trading_allowed and live_adapter.credentials_ready(): await live_adapter.reconcile()
                batch=market.batch_for_cycle(self.universe)
                for sym in batch:
                    await self.scan_symbol(sym); await asyncio.sleep(.05)
                learner.run(); self.last_scan=int(time.time()*1000); self.last_error=None; self.scan_count+=1
            except Exception as e:
                self.last_error=f"{type(e).__name__}: {e}"; db.event("ENGINE_ERROR",self.last_error,"ERROR"); traceback.print_exc()
            await asyncio.sleep(settings.scan_interval_sec)
    async def scan_symbol(self,symbol:str):
        states=db.query("SELECT * FROM strategy_state WHERE enabled=1")
        need_depth=any(s["strategy"]=="orderbook" for s in states)
        # CoinGlass enrichments are reserved for the most liquid slice to avoid burning paid API quota.
        # OI Trend still works on every symbol with Bitget-native OI when CoinGlass enrichment is skipped.
        cg_allowed=symbol in set(self.universe[:max(0,settings.coinglass_top_symbols)])
        need_liq=cg_allowed and any(s["strategy"]=="liquidation_magnet" for s in states)
        need_cg=cg_allowed and any(s["strategy"]=="oi_trend" for s in states)
        snap=await market.snapshot(symbol,need_depth,need_liq,need_cg)
        if not snap:return
        prices=market.prices(); prices[symbol]=snap.price
        for state in states:
            name=state["strategy"]; champion=json.loads(state["params_json"])
            await self._evaluate_variant(state,snap,"champion",champion,prices)
            c=db.one("SELECT * FROM challengers WHERE strategy=? AND status='ACTIVE'",(name,))
            if c: await self._evaluate_variant(state,snap,"challenger",json.loads(c["params_json"]),prices)
    async def _evaluate_variant(self,state,snap,variant,params,prices):
        name=state["strategy"]
        if db.one("SELECT id FROM positions WHERE strategy=? AND variant=? AND symbol=?",(name,variant,snap.symbol)):return
        st=build_strategy(name,params); sig=st.evaluate(snap)
        if not sig or sig.score<st.spec.min_score:return
        db.execute("INSERT INTO signals(strategy,variant,symbol,side,score,entry,stop,reason,features_json,regime,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(name,variant,snap.symbol,sig.side,sig.score,sig.entry,sig.stop,sig.reason,json.dumps(sig.features),snap.regime,sig.created_at))
        pid=paper_broker.enter(sig,variant,params,state["stage"],snap.regime,prices)
        if pid and variant=="champion" and state["stage"]=="FINAL" and state["live_eligible"] and live_adapter.gate_status()["ready"]:
            pos=db.one("SELECT * FROM positions WHERE id=?",(pid,))
            if pos:
                try: await live_adapter.place_from_paper(pos,params)
                except Exception as e: db.risk_event("LIVE_SUBMIT_FAILED",str(e),name,"champion",snap.symbol)

engine=Engine()
