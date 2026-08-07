from __future__ import annotations

import json, os, time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import db
from .engine import engine
from .learning import metrics, learner, completed_roundtrips

app=FastAPI(title="Crypto Strategy Lab",version="1.0.0")

@app.on_event("startup")
async def startup(): await engine.start()

@app.get("/",response_class=HTMLResponse)
def home():
    path=os.path.join(os.path.dirname(os.path.dirname(__file__)),"templates","index.html")
    return open(path,"r",encoding="utf-8").read()


def strategy_rows():
    out=[]
    for s in db.query("SELECT * FROM strategy_state ORDER BY strategy"):
        trades=completed_roundtrips(s["strategy"], 100000)
        m=metrics(trades); eq=engine.equity(s["strategy"])
        opens=(db.one("SELECT COUNT(*) n FROM positions WHERE strategy=?",(s["strategy"],)) or {"n":0})["n"]
        out.append({"strategy":s["strategy"],"name":s["display_name"],"stage":s["stage"],"balance":round(float(s["balance"]),2),"equity":round(eq,2),"return_pct":round((eq/float(s["initial_balance"])-1)*100,2),"open_positions":opens,"trades":m["n"],"win_rate":round(m["win_rate"]*100,1),"profit_factor":round(m["pf"],2),"expectancy":round(m["expectancy"],2),"sharpe_like":round(m["sharpe"],2),"max_dd_pct":round(m["max_dd_pct"]*100,2),"stress_pf":round(m["stress_pf"],2),"live_eligible":bool(s["live_eligible"]),"params":json.loads(s["params_json"])})
    return out

@app.get("/api/overview")
def overview():
    now=int(time.time()*1000); day=now-24*3600*1000
    pnl=(db.one("SELECT COALESCE(SUM(net_pnl),0) x FROM trades WHERE closed_at>=?",(day,)) or {"x":0})["x"]
    return {"running":engine.running,"last_scan":engine.last_scan,"last_error":engine.last_error,"universe_size":len(engine.universe),"paper_accounts":len(strategy_rows()),"daily_realized_pnl":round(float(pnl),2),"live_trading_enabled":settings.live_trading_enabled,"coinglass_connected":bool(settings.coinglass_api_key),"scan_interval_sec":settings.scan_interval_sec}

@app.get("/api/strategies")
def strategies(): return strategy_rows()

@app.get("/api/positions")
def positions(): return db.query("SELECT * FROM positions ORDER BY opened_at DESC")

@app.get("/api/trades")
def trades(limit:int=200): return db.query("SELECT * FROM trades ORDER BY closed_at DESC LIMIT ?",(min(limit,1000),))

@app.get("/api/signals")
def signals(limit:int=100): return db.query("SELECT * FROM signals ORDER BY created_at DESC LIMIT ?",(min(limit,500),))

@app.get("/api/adjustments")
def adjustments(limit:int=100): return db.query("SELECT * FROM adjustments ORDER BY created_at DESC LIMIT ?",(min(limit,500),))

@app.get("/api/daily")
def daily():
    rows=db.query("""
      SELECT strategy,date(closed_at/1000,'unixepoch','+8 hours') day,COUNT(*) trades,
             SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) wins,SUM(net_pnl) pnl
      FROM (
        SELECT t.position_id,t.strategy,MAX(t.closed_at) closed_at,SUM(t.net_pnl) net_pnl
        FROM trades t WHERE NOT EXISTS (SELECT 1 FROM positions p WHERE p.id=t.position_id)
        GROUP BY t.position_id,t.strategy
      ) x GROUP BY strategy,day ORDER BY day DESC,strategy
    """)
    for r in rows: r["win_rate"]=round(100*r["wins"]/r["trades"],1) if r["trades"] else 0
    return rows

@app.post("/api/learning/run")
def learning_run(request:Request):
    token=request.headers.get("x-admin-token","")
    if settings.admin_token and token!=settings.admin_token: raise HTTPException(403,"bad admin token")
    learner.last_run=0
    return {"changes":learner.run()}

@app.get("/health")
def health(): return {"ok":True,"time":int(time.time()*1000),"engine":engine.running}
