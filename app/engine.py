from __future__ import annotations

import asyncio
import json
import time
import traceback

from .config import settings
from .db import db
from .historical_replay import historical_replay
from .learning import learner
from .live import live_adapter
from .market import market
from .paper import paper_broker
from .strategies import SPECS, SPEC_MAP, build_strategy, min_score

# Version changes are non-destructive in DB.reset_research_epoch().
MODEL_VERSION = "2026-08-10-12strategies-nonblocking-live-execution"


class Engine:
    def __init__(self):
        self.running = False
        self.last_scan = None
        self.last_error = None
        self.universe = []
        self.scan_count = 0
        self.last_cycle_seconds = 0.0
        self.last_batch_size = 0
        self._task: asyncio.Task | None = None
        self._eval_cache: dict[tuple[str, str, str, str], int] = {}
        self._scan_sem = asyncio.Semaphore(max(1, settings.scan_concurrency))

        for s in SPECS:
            db.ensure_strategy(
                s.name, s.display_name, s.description, s.params, s.signal_tf
            )
        db.reset_research_epoch(SPECS, MODEL_VERSION)
        if db.runtime_get("live_master_enabled", "") == "":
            db.runtime_set("live_master_enabled", "false")

    async def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self.loop(), name="strategy-engine")

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def status(self):
        return {
            "running": self.running,
            "last_scan": self.last_scan,
            "last_error": self.last_error,
            "scan_count": self.scan_count,
            "last_cycle_seconds": round(self.last_cycle_seconds, 3),
            "last_batch_size": self.last_batch_size,
            "scan_concurrency": max(1, settings.scan_concurrency),
        }

    async def loop(self):
        while self.running:
            cycle_started = time.monotonic()
            try:
                self.universe = await market.refresh_universe()
                prices = market.prices()

                # Paper bookkeeping is local work. No Bitget private API call is
                # allowed on the scanner critical path.
                await asyncio.to_thread(paper_broker.manage, prices)

                states = db.query(
                    "SELECT * FROM strategy_state WHERE enabled=1 ORDER BY strategy"
                )
                challengers = {
                    x["strategy"]: x
                    for x in db.query(
                        "SELECT * FROM challengers WHERE status='ACTIVE'"
                    )
                }
                batch = market.batch_for_cycle(self.universe)
                self.last_batch_size = len(batch)

                async def run_symbol(symbol):
                    async with self._scan_sem:
                        try:
                            await self.scan_symbol(symbol, states, challengers)
                        except Exception as e:
                            db.event(
                                "SYMBOL_SCAN_FAILED",
                                f"{symbol}: {type(e).__name__}: {e}",
                                "WARN",
                            )

                if batch:
                    await asyncio.gather(*(run_symbol(sym) for sym in batch))

                # Learning can be CPU/DB heavy and should not freeze FastAPI's
                # event loop. DB has its own lock, so run it in a worker thread.
                await asyncio.to_thread(learner.run)
                self.last_scan = int(time.time() * 1000)
                self.last_error = None
                self.scan_count += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                db.event("ENGINE_ERROR", self.last_error, "ERROR")
                traceback.print_exc()
            finally:
                self.last_cycle_seconds = time.monotonic() - cycle_started

            await asyncio.sleep(settings.scan_interval_sec)

    async def scan_symbol(self, symbol, states, challengers):
        cg_allowed = symbol in set(
            self.universe[: max(0, settings.coinglass_top_symbols)]
        )
        need_depth = any(s["strategy"] == "orderbook" for s in states)
        need_liq = cg_allowed and any(
            s["strategy"] == "liquidation_magnet" for s in states
        )
        need_cg = cg_allowed and any(
            s["strategy"] == "oi_trend" for s in states
        )
        snap = await market.snapshot(symbol, need_depth, need_liq, need_cg)
        if not snap:
            return

        historical_replay.archive_snapshot(snap)
        paper_broker.observe_snapshot(snap)

        # This is queue-only. The Bitget worker performs network I/O in the
        # background, so a slow exchange can never stall scanning.
        live_adapter.queue_symbol_sync(symbol)

        prices = market.prices()
        prices[symbol] = snap.price
        for state in states:
            await self._evaluate_variant(
                state,
                snap,
                "champion",
                json.loads(state["params_json"]),
                prices,
            )
            c = challengers.get(state["strategy"])
            if c:
                await self._evaluate_variant(
                    state,
                    snap,
                    "challenger",
                    json.loads(c["params_json"]),
                    prices,
                )

    async def _evaluate_variant(self, state, snap, variant, params, prices):
        name = state["strategy"]
        spec = SPEC_MAP[name]
        tf = spec.signal_tf
        bar_ts = snap.closed_ts(tf)
        key = (name, variant, snap.symbol, tf)

        # First-level in-memory guard avoids a SQLite read on every repeated
        # scan of the same already-closed candle. DB remains the restart-safe
        # source of truth.
        if self._eval_cache.get(key) == bar_ts:
            return
        if db.one(
            "SELECT 1 FROM strategy_eval_bars WHERE strategy=? AND variant=? "
            "AND symbol=? AND tf=? AND bar_ts=?",
            (name, variant, snap.symbol, tf, bar_ts),
        ):
            self._eval_cache[key] = bar_ts
            return

        db.execute(
            "INSERT OR IGNORE INTO strategy_eval_bars(strategy,variant,symbol,tf,bar_ts)"
            "VALUES(?,?,?,?,?)",
            (name, variant, snap.symbol, tf, bar_ts),
        )
        self._eval_cache[key] = bar_ts

        if db.one(
            "SELECT id FROM positions WHERE strategy=? AND variant=? AND symbol=?",
            (name, variant, snap.symbol),
        ):
            return

        strategy = build_strategy(name, params)
        sig = strategy.evaluate(snap)
        if not sig or sig.score < min_score(name, state["stage"]):
            return

        db.execute(
            "INSERT INTO signals(strategy,variant,symbol,side,score,entry,stop,reason,"
            "features_json,regime,signal_tf,bar_ts,created_at)"
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                variant,
                snap.symbol,
                sig.side,
                sig.score,
                sig.entry,
                sig.stop,
                sig.reason,
                json.dumps(sig.features),
                snap.regime,
                sig.signal_tf,
                sig.bar_ts,
                sig.created_at,
            ),
        )
        pid = paper_broker.enter(
            sig, variant, params, state["stage"], snap.regime, prices
        )

        if (
            pid
            and variant == "champion"
            and state["stage"] == "FINAL"
            and state["live_eligible"]
            and state.get("live_manual_enabled")
            and live_adapter.gate_status()["ready"]
        ):
            pos = db.one("SELECT * FROM positions WHERE id=?", (pid,))
            if pos:
                try:
                    # queue_entry performs only local validation/DB/queue work.
                    # Actual Bitget order submission belongs to Entry Worker.
                    await live_adapter.queue_entry(pos, params)
                except Exception as e:
                    db.risk_event(
                        "LIVE_SUBMIT_QUEUE_FAILED",
                        str(e),
                        name,
                        "champion",
                        snap.symbol,
                    )


engine = Engine()
