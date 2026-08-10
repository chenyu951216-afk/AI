from __future__ import annotations

import json
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ai_hunter import ai_hunter
from .config import settings
from .daily_report import daily_reporter
from .db import db
from .engine import engine
from .historical_replay import historical_replay
from .learning import (
    completed_roundtrips,
    exit_diagnostics,
    learner,
    learning_progress,
    metrics,
    profit_concentration,
    robust_windows,
    sample_days,
    symbol_count,
)
from .live import live_adapter
from .market import market
from .risk import risk_manager
from .strategies import SPEC_MAP

app = FastAPI(
    title="AI Crypto Strategy Lab",
    version="2026.08.10-nonblocking-bitget-execution",
)
app.mount(
    "/static",
    StaticFiles(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    ),
    name="static",
)


@app.on_event("startup")
async def startup():
    # Exchange workers are deliberately separate from the strategy scanner.
    # Start them first so queued jobs can be recovered immediately after a
    # Zeabur restart. They do no exchange I/O unless credentials are present.
    await live_adapter.start()
    await engine.start()
    await daily_reporter.start()
    await historical_replay.start()


@app.on_event("shutdown")
async def shutdown():
    await engine.stop()
    await live_adapter.stop()
    await market.close()


def admin(r: Request):
    if not settings.admin_token:
        raise HTTPException(503, "ADMIN_TOKEN is not configured")
    if r.headers.get("x-admin-token", "") != settings.admin_token:
        raise HTTPException(403, "bad admin token")


def _exit_profile(p):
    keys = [
        "sl1_r",
        "sl1_fraction",
        "tp1_r",
        "tp2_r",
        "tp3_r",
        "tp1_fraction",
        "tp2_fraction",
        "breakeven_trigger_r",
        "breakeven_offset_r",
        "trail_start_r",
        "trail_r",
    ]
    return {k: p.get(k) for k in keys if k in p}


def strategy_rows():
    out = []
    prices = market.prices()
    cg = market.cg.health()
    for s in db.query("SELECT * FROM strategy_state ORDER BY strategy"):
        since = int(s.get("evidence_since") or 0)
        tr = completed_roundtrips(s["strategy"], "champion", 100000, since)
        m = metrics(tr)
        eq = risk_manager.equity(s["strategy"], "champion", prices)
        acc = db.account(s["strategy"], "champion")
        opens = int(
            (
                db.one(
                    "SELECT COUNT(*)n FROM positions WHERE strategy=? AND variant='champion'",
                    (s["strategy"],),
                )
                or {"n": 0}
            )["n"]
        )
        c = db.one(
            "SELECT * FROM challengers WHERE strategy=? AND status='ACTIVE'",
            (s["strategy"],),
        )
        diag = exit_diagnostics(s["strategy"], "champion", since)
        progress = learning_progress(tr, m, diag)
        challenger = None
        if c:
            ct = completed_roundtrips(
                s["strategy"], "challenger", 10000, int(c["started_at"])
            )
            cm = metrics(ct)
            challenger = {
                "domain": c.get("domain"),
                "started_at": c["started_at"],
                "trades": cm["n"],
                "pf": round(cm["pf"], 2),
                "expectancy": round(cm["expectancy"], 2),
                "dd_pct": round(cm["max_dd_pct"] * 100, 2),
                "params": json.loads(c["params_json"]),
                "reason": c["reason"],
            }
        spec = SPEC_MAP[s["strategy"]]
        params = json.loads(s["params_json"])
        data_health = cg if s["strategy"] == "liquidation_magnet" else None
        ai_model = ai_hunter.status() if s["strategy"] == "ai_extreme_hunter" else None
        rr = db.one(
            "SELECT * FROM replay_runs WHERE strategy=? ORDER BY started_at DESC LIMIT 1",
            (s["strategy"],),
        )
        rp = db.one(
            "SELECT * FROM replay_proposals WHERE strategy=? ORDER BY created_at DESC LIMIT 1",
            (s["strategy"],),
        )
        out.append(
            {
                "strategy": s["strategy"],
                "name": s["display_name"],
                "description": s["description"],
                "signal_tf": spec.signal_tf,
                "context_tfs": spec.context_tfs,
                "stage": s["stage"],
                "enabled": bool(s["enabled"]),
                "balance": round(float(acc["balance"] if acc else 0), 2),
                "equity": round(eq, 2),
                "return_pct": round(
                    (eq / float(acc["initial_balance"]) - 1) * 100, 2
                )
                if acc
                else 0,
                "open_positions": opens,
                "trades": m["n"],
                "win_rate": round(m["win_rate"] * 100, 1),
                "profit_factor": round(m["pf"], 2),
                "expectancy": round(m["expectancy"], 2),
                "avg_r": round(m["avg_r"], 3),
                "max_dd_pct": round(m["max_dd_pct"] * 100, 2),
                "stress_pf": round(m["stress_pf"], 2),
                "robust_windows": round(robust_windows(tr), 2),
                "days": round(sample_days(tr), 1),
                "symbols": symbol_count(tr),
                "profit_concentration": round(profit_concentration(tr) * 100, 1)
                if tr
                else 0,
                "live_eligible": bool(s["live_eligible"]),
                "live_manual_enabled": bool(s.get("live_manual_enabled")),
                "params": params,
                "exit_profile": _exit_profile(params),
                "challenger": challenger,
                "exit_lab": diag,
                "learning_progress": progress,
                "data_health": data_health,
                "ai_model": ai_model,
                "historical_replay": {"latest": rr, "proposal": rp},
            }
        )
    return out


def position_rows():
    out = []
    for x in db.query(
        "SELECT * FROM positions WHERE variant='champion' ORDER BY opened_at DESC"
    ):
        p = json.loads(x.get("params_json") or "{}")
        r = abs(float(x["entry"]) - float(x["initial_stop"]))
        sg = 1 if x["side"] == "long" else -1
        x["sl1"] = float(x["entry"]) - sg * r * float(p.get("sl1_r", 0.62))
        x["sl1_fraction"] = p.get("sl1_fraction", 0.18)
        x["breakeven_trigger_r"] = p.get("breakeven_trigger_r", 0.85)
        x["breakeven_offset_r"] = p.get("breakeven_offset_r", 0.04)
        x["trail_start_r"] = p.get("trail_start_r", 1.35)
        x["trail_r"] = p.get("trail_r", 1.0)
        x["notional"] = round(float(x["remaining_qty"]) * float(x["entry"]), 2)
        out.append(x)
    return out


@app.get("/", response_class=HTMLResponse)
def home():
    return open(
        os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "index.html"
        ),
        encoding="utf-8",
    ).read()


@app.get("/api/overview")
def overview():
    now = int(time.time() * 1000)
    day = now - 86400000
    p = (
        db.one(
            "SELECT COALESCE(SUM(net_pnl),0)x FROM trades WHERE variant='champion' AND closed_at>=?",
            (day,),
        )
        or {"x": 0}
    )["x"]
    reg = db.one("SELECT * FROM regime_history ORDER BY ts DESC LIMIT 1")
    cg = market.cg.health()
    engine_status = engine.status()
    return {
        **engine_status,
        "universe_size": len(engine.universe),
        "strategy_count": len(SPEC_MAP),
        "daily_realized_pnl": round(float(p), 2),
        "coinglass_connected": cg["ok"],
        "coinglass": cg,
        "macro": reg or {"regime": "NEUTRAL"},
        "live": live_adapter.gate_status(),
        "market": market.health(),
        "daily_report": daily_reporter.status(),
        "historical_replay": historical_replay.status(),
        "storage": db.diagnostics(),
    }


@app.get("/api/strategies")
def strategies():
    return strategy_rows()


@app.get("/api/positions")
def positions():
    return position_rows()


@app.get("/api/trades")
def trades(limit: int = 200):
    return db.query(
        "SELECT * FROM trades WHERE variant='champion' ORDER BY closed_at DESC,id DESC LIMIT ?",
        (min(limit, 1000),),
    )


@app.get("/api/signals")
def signals(limit: int = 100):
    return db.query(
        "SELECT * FROM signals WHERE variant='champion' ORDER BY created_at DESC LIMIT ?",
        (min(limit, 500),),
    )


@app.get("/api/adjustments")
def adjustments(limit: int = 100):
    return db.query(
        "SELECT * FROM adjustments ORDER BY created_at DESC LIMIT ?",
        (min(limit, 500),),
    )


@app.get("/api/post-trade")
def post_trade(limit: int = 100):
    return db.query(
        "SELECT * FROM post_trade_studies WHERE variant='champion' ORDER BY closed_at DESC LIMIT ?",
        (min(limit, 500),),
    )


@app.get("/api/risk-events")
def risk_events(limit: int = 100):
    return db.query(
        "SELECT * FROM risk_events ORDER BY created_at DESC LIMIT ?",
        (min(limit, 500),),
    )


@app.get("/api/live/orders")
def live_orders(limit: int = 100):
    return db.query(
        "SELECT * FROM live_orders ORDER BY created_at DESC LIMIT ?",
        (min(limit, 500),),
    )


@app.get("/api/live/protections")
def live_protections(limit: int = 200):
    return db.query(
        "SELECT p.*,o.strategy,o.symbol FROM live_protections p "
        "JOIN live_orders o ON o.id=p.live_order_id ORDER BY p.updated_at DESC LIMIT ?",
        (min(limit, 1000),),
    )


@app.get("/api/live/jobs")
def live_jobs(limit: int = 100):
    return db.query(
        "SELECT * FROM live_execution_jobs ORDER BY updated_at DESC LIMIT ?",
        (min(limit, 500),),
    )


@app.get("/api/live/status")
def live_status():
    x = live_adapter.gate_status()
    x.update(
        {
            "last_reconcile": int(live_adapter.last_sync * 1000)
            if live_adapter.last_sync
            else None,
            "reconcile_error": live_adapter.last_sync_error,
            "exchange_positions": len(live_adapter.last_positions),
        }
    )
    return x


@app.get("/api/storage-status")
def storage_status():
    return db.diagnostics()


@app.get("/api/ai-hunter/status")
def ai_hunter_status():
    return ai_hunter.status()


@app.get("/api/daily-report/status")
def daily_report_status():
    return daily_reporter.status()


@app.post("/api/daily-report/run")
async def daily_report_run(r: Request):
    admin(r)
    return await daily_reporter.run_latest(force=True)


@app.get("/api/historical-replay/status")
def historical_replay_status():
    return historical_replay.status()


@app.get("/api/historical-replay/runs")
def historical_replay_runs(limit: int = 100):
    return db.query(
        "SELECT * FROM replay_runs ORDER BY started_at DESC LIMIT ?",
        (min(limit, 500),),
    )


@app.get("/api/historical-replay/proposals")
def historical_replay_proposals(limit: int = 100):
    return db.query(
        "SELECT * FROM replay_proposals ORDER BY created_at DESC LIMIT ?",
        (min(limit, 500),),
    )


@app.post("/api/historical-replay/run/{name}")
async def historical_replay_run(name: str, r: Request):
    admin(r)
    if name not in SPEC_MAP:
        raise HTTPException(404, "strategy not found")
    return await historical_replay.run_strategy(name)


@app.get("/api/daily")
def daily():
    rows = db.query(
        """SELECT strategy,date(closed_at/1000,'unixepoch','+8 hours')day,
        COUNT(*)trades,SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END)wins,SUM(net_pnl)pnl
        FROM (SELECT t.position_id,t.strategy,MAX(t.closed_at)closed_at,SUM(t.net_pnl)net_pnl
        FROM trades t WHERE t.variant='champion'
        AND NOT EXISTS(SELECT 1 FROM positions p WHERE p.id=t.position_id)
        GROUP BY t.position_id,t.strategy)x
        GROUP BY strategy,day ORDER BY day DESC,strategy LIMIT 1000"""
    )
    for r in rows:
        r["win_rate"] = (
            round(100 * r["wins"] / r["trades"], 1) if r["trades"] else 0
        )
    return rows


class Toggle(BaseModel):
    enabled: bool


@app.post("/api/live/toggle")
def live_toggle(body: Toggle, r: Request):
    admin(r)
    if body.enabled:
        if not settings.live_trading_allowed:
            raise HTTPException(409, "LIVE_TRADING_ALLOWED=false in Zeabur")
        if not live_adapter.credentials_ready():
            raise HTTPException(409, "Bitget private API credentials incomplete")
        n = int(
            (
                db.one(
                    "SELECT COUNT(*)n FROM strategy_state WHERE live_eligible=1 "
                    "AND live_manual_enabled=1 AND stage='FINAL' AND enabled=1"
                )
                or {"n": 0}
            )["n"]
        )
        if n < 1:
            raise HTTPException(
                409, "No FINAL strategy has been manually approved for live trading"
            )
    live_adapter.set_web_master(body.enabled)
    db.event(
        "LIVE_MASTER",
        f"web live switch -> {body.enabled}",
        "WARN" if body.enabled else "INFO",
    )
    return live_adapter.gate_status()


@app.post("/api/learning/run")
def learning_run(r: Request):
    admin(r)
    learner.last_run = 0
    return {"changes": learner.run(True)}


@app.post("/api/strategy/{name}/toggle")
def strategy_toggle(name: str, body: Toggle, r: Request):
    admin(r)
    s = db.state(name)
    if not s:
        raise HTTPException(404, "strategy not found")
    db.update_strategy(
        name,
        enabled=body.enabled,
        **({"live_manual_enabled": False} if not body.enabled else {}),
    )
    return {"strategy": name, "enabled": body.enabled}


@app.post("/api/strategy/{name}/live-toggle")
def strategy_live_toggle(name: str, body: Toggle, r: Request):
    admin(r)
    s = db.state(name)
    if not s:
        raise HTTPException(404, "strategy not found")
    if body.enabled and (
        s["stage"] != "FINAL" or not s["live_eligible"] or not s["enabled"]
    ):
        raise HTTPException(
            409, "Only enabled FINAL/live-eligible strategies can be manually approved"
        )
    db.update_strategy(name, live_manual_enabled=body.enabled)
    db.event(
        "STRATEGY_LIVE_APPROVAL",
        f"{name} -> {body.enabled}",
        "WARN" if body.enabled else "INFO",
    )
    return {"strategy": name, "live_manual_enabled": body.enabled}


@app.get("/api/config/public")
def public_config():
    return {
        "scan_interval_sec": settings.scan_interval_sec,
        "scan_concurrency": settings.scan_concurrency,
        "initial_paper_equity": settings.initial_paper_equity,
        "closed_candle_safety_ms": settings.closed_candle_safety_ms,
        "post_trade_follow_bars": settings.post_trade_follow_bars,
        "live_trading_allowed": settings.live_trading_allowed,
        "live_base_risk_pct": settings.live_base_risk_pct,
        "live_max_total_notional_multiple": settings.live_max_total_notional_multiple,
        "admin_token_configured": bool(settings.admin_token),
        "bitget_credentials_configured": live_adapter.credentials_ready(),
        "coinglass": market.cg.health(),
        "ai_hunter": ai_hunter.status(),
        "daily_report": {
            "enabled": settings.discord_daily_report_enabled,
            "configured": bool(settings.discord_webhook_url),
            "hour": settings.daily_report_hour,
            "minute": settings.daily_report_minute,
            "timezone": settings.timezone,
        },
        "historical_replay": historical_replay.status(),
        "storage": db.diagnostics(),
        "live": live_adapter.gate_status(),
        "market": market.health(),
    }


@app.get("/health")
def health():
    live = live_adapter.gate_status()
    return {
        "ok": True,
        "time": int(time.time() * 1000),
        "engine": engine.status(),
        "coinglass": market.cg.health(),
        "market": market.health(),
        "live": live,
        "ai_hunter_ready": ai_hunter.status()["ready"],
        "daily_report": daily_reporter.status(),
        "historical_replay": historical_replay.status(),
        "storage": db.diagnostics(),
    }
