from __future__ import annotations
import json, os, time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from .config import settings
from .db import db
from .engine import engine
from .learning import completed_roundtrips, metrics, robust_windows, sample_days, profit_concentration, symbol_count, learner
from .live import live_adapter
from .market import market
from .risk import risk_manager

app=FastAPI(title="AI Crypto Strategy Lab",version="2026.08-final")

@app.on_event("startup")
async def startup(): await engine.start()

def admin(request:Request):
    if not settings.admin_token: raise HTTPException(503,"ADMIN_TOKEN is not configured")
    if request.headers.get("x-admin-token","")!=settings.admin_token: raise HTTPException(403,"bad admin token")

def strategy_rows():
    out=[]; prices=market.prices()
    for s in db.query("SELECT * FROM strategy_state ORDER BY strategy"):
        tr=completed_roundtrips(s["strategy"],"champion",100000);m=metrics(tr);eq=risk_manager.equity(s["strategy"],"champion",prices);acc=db.account(s["strategy"],"champion");opens=(db.one("SELECT COUNT(*) n FROM positions WHERE strategy=? AND variant='champion'",(s["strategy"],)) or {"n":0})["n"];c=db.one("SELECT * FROM challengers WHERE strategy=? AND status='ACTIVE'",(s["strategy"],))
        challenger=None
        if c:
            ct=completed_roundtrips(s["strategy"],"challenger",10000,int(c["started_at"]));cm=metrics(ct);challenger={"started_at":c["started_at"],"trades":cm["n"],"pf":round(cm["pf"],2),"expectancy":round(cm["expectancy"],2),"dd_pct":round(cm["max_dd_pct"]*100,2),"params":json.loads(c["params_json"]),"reason":c["reason"]}
        out.append({"strategy":s["strategy"],"name":s["display_name"],"description":s["description"],"stage":s["stage"],"enabled":bool(s["enabled"]),"balance":round(float(acc["balance"] if acc else 0),2),"equity":round(eq,2),"return_pct":round((eq/float(acc["initial_balance"])-1)*100,2) if acc else 0,"open_positions":int(opens),"trades":m["n"],"win_rate":round(m["win_rate"]*100,1),"profit_factor":round(m["pf"],2),"expectancy":round(m["expectancy"],2),"avg_r":round(m["avg_r"],3),"max_dd_pct":round(m["max_dd_pct"]*100,2),"stress_pf":round(m["stress_pf"],2),"robust_windows":round(robust_windows(tr),2),"days":round(sample_days(tr),1),"symbols":symbol_count(tr),"profit_concentration":round(profit_concentration(tr)*100,1) if tr else 0,"live_eligible":bool(s["live_eligible"]),"params":json.loads(s["params_json"]),"challenger":challenger})
    return out

@app.get("/",response_class=HTMLResponse)
def home():
    return open(os.path.join(os.path.dirname(os.path.dirname(__file__)),"templates","index.html"),encoding="utf-8").read()

@app.get("/api/overview")
def overview():
    now=int(time.time()*1000);day=now-86400000;p=(db.one("SELECT COALESCE(SUM(net_pnl),0) x FROM trades WHERE variant='champion' AND closed_at>=?",(day,)) or {"x":0})["x"];reg=db.one("SELECT * FROM regime_history ORDER BY ts DESC LIMIT 1")
    return {"running":engine.running,"last_scan":engine.last_scan,"last_error":engine.last_error,"scan_count":engine.scan_count,"universe_size":len(engine.universe),"daily_realized_pnl":round(float(p),2),"coinglass_connected":bool(settings.coinglass_enabled and settings.coinglass_api_key),"regime":reg or {"regime":"NEUTRAL"},"live":live_adapter.gate_status()}
@app.get("/api/strategies")
def strategies(): return strategy_rows()
@app.get("/api/positions")
def positions(): return db.query("SELECT * FROM positions WHERE variant='champion' ORDER BY opened_at DESC")
@app.get("/api/trades")
def trades(limit:int=200): return db.query("SELECT * FROM trades WHERE variant='champion' ORDER BY closed_at DESC LIMIT ?",(min(limit,1000),))
@app.get("/api/signals")
def signals(limit:int=100): return db.query("SELECT * FROM signals WHERE variant='champion' ORDER BY created_at DESC LIMIT ?",(min(limit,500),))
@app.get("/api/adjustments")
def adjustments(limit:int=100): return db.query("SELECT * FROM adjustments ORDER BY created_at DESC LIMIT ?",(min(limit,500),))
@app.get("/api/risk-events")
def risk_events(limit:int=100): return db.query("SELECT * FROM risk_events ORDER BY created_at DESC LIMIT ?",(min(limit,500),))
@app.get("/api/live/orders")
def live_orders(limit:int=100): return db.query("SELECT * FROM live_orders ORDER BY created_at DESC LIMIT ?",(min(limit,500),))
@app.get("/api/live/status")
def live_status():
    x=live_adapter.gate_status();x.update({"last_reconcile":int(live_adapter.last_sync*1000) if live_adapter.last_sync else None,"reconcile_error":live_adapter.last_sync_error,"exchange_positions":len(live_adapter.last_positions)});return x
@app.get("/api/daily")
def daily():
    rows=db.query("""SELECT strategy,date(closed_at/1000,'unixepoch','+8 hours') day,COUNT(*) trades,SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) wins,SUM(net_pnl) pnl FROM (
      SELECT t.position_id,t.strategy,MAX(t.closed_at) closed_at,SUM(t.net_pnl) net_pnl FROM trades t WHERE t.variant='champion' AND NOT EXISTS(SELECT 1 FROM positions p WHERE p.id=t.position_id) GROUP BY t.position_id,t.strategy
    )x GROUP BY strategy,day ORDER BY day DESC,strategy LIMIT 1000""")
    for r in rows:r["win_rate"]=round(100*r["wins"]/r["trades"],1) if r["trades"] else 0
    return rows

class Toggle(BaseModel): enabled: bool
@app.post("/api/live/toggle")
def live_toggle(body:Toggle,request:Request):
    admin(request)
    if body.enabled:
        if not settings.live_trading_allowed: raise HTTPException(409,"LIVE_TRADING_ALLOWED=false in Zeabur environment")
        if not live_adapter.credentials_ready(): raise HTTPException(409,"Bitget private API credentials are incomplete")
        eligible=(db.one("SELECT COUNT(*) n FROM strategy_state WHERE live_eligible=1 AND stage='FINAL' AND enabled=1") or {"n":0})["n"]
        if int(eligible)<1: raise HTTPException(409,"No strategy has passed FINAL eligibility yet")
    live_adapter.set_web_master(body.enabled);db.event("LIVE_MASTER",f"web live switch -> {body.enabled}","WARN" if body.enabled else "INFO");return live_adapter.gate_status()

@app.post("/api/learning/run")
def learning_run(request:Request):admin(request);learner.last_run=0;return {"changes":learner.run(force=True)}
@app.post("/api/strategy/{name}/toggle")
def strategy_toggle(name:str,body:Toggle,request:Request):
    admin(request);s=db.state(name)
    if not s:raise HTTPException(404,"strategy not found")
    db.update_strategy(name,enabled=body.enabled);return {"strategy":name,"enabled":body.enabled}
@app.get("/api/config/public")
def public_config():
    return {"port":settings.port,"db_path":settings.db_path,"scan_interval_sec":settings.scan_interval_sec,"initial_paper_equity":settings.initial_paper_equity,"hard_max_risk_per_trade":settings.hard_max_risk_per_trade,"hard_max_leverage":settings.hard_max_leverage,"hard_max_open_positions":settings.hard_max_open_positions,"live_trading_allowed":settings.live_trading_allowed,"live_partial_tp_enabled":settings.live_partial_tp_enabled,"admin_token_configured":bool(settings.admin_token),"bitget_credentials_configured":live_adapter.credentials_ready(),"coinglass_configured":bool(settings.coinglass_api_key)}
@app.get("/health")
def health():return {"ok":True,"time":int(time.time()*1000),"engine":engine.running,"last_error":engine.last_error}
