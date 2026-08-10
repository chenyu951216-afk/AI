from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import random
import time
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from math import isfinite
from urllib.parse import urlencode

import httpx

from .config import settings
from .db import db


def _num(value, default=0.0):
    try:
        x = float(value)
        return x if isfinite(x) else default
    except Exception:
        return default


def _text(value) -> str:
    d = Decimal(str(value))
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


class AsyncPacer:
    def __init__(self, rps: float):
        self.interval = 1.0 / max(float(rps), 0.1)
        self.lock = asyncio.Lock()
        self.next_at = 0.0

    async def acquire(self):
        async with self.lock:
            now = time.monotonic()
            if self.next_at > now:
                await asyncio.sleep(self.next_at - now)
            self.next_at = max(self.next_at, time.monotonic()) + self.interval


class CircuitBreaker:
    def __init__(self, threshold: int, recovery_sec: float):
        self.threshold = max(1, int(threshold))
        self.recovery_sec = max(1.0, float(recovery_sec))
        self.failures = 0
        self.opened_at = 0.0

    def allow(self) -> bool:
        if not self.opened_at:
            return True
        if time.monotonic() - self.opened_at >= self.recovery_sec:
            self.failures = 0
            self.opened_at = 0.0
            return True
        return False

    def success(self):
        self.failures = 0
        self.opened_at = 0.0

    def failure(self):
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()

    def status(self):
        remain = 0.0
        if self.opened_at:
            remain = max(0.0, self.recovery_sec - (time.monotonic() - self.opened_at))
        return {
            "open": bool(self.opened_at and remain > 0),
            "failures": self.failures,
            "recovery_in_sec": round(remain, 2),
        }


class BitgetLiveAdapter:
    """Bitget exchange execution layer only.

    Strategy direction, entry thesis and each strategy's own SL/TP/BE/trailing
    calculations remain outside this module. This layer only validates and
    normalizes exchange parameters, controls shared live capital, submits
    orders and keeps the already-decided protection prices alive on Bitget.
    """

    TERMINAL_JOB = {"DONE", "FAILED", "EXPIRED", "CANCELLED"}

    def __init__(self):
        limits = httpx.Limits(
            max_connections=max(4, settings.bitget_max_concurrency * 3),
            max_keepalive_connections=max(2, settings.bitget_max_concurrency * 2),
            keepalive_expiry=30.0,
        )
        self.client = httpx.AsyncClient(
            base_url=settings.bitget_base_url.rstrip("/"),
            timeout=settings.bitget_request_timeout_sec,
            limits=limits,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        self.pacer = AsyncPacer(settings.bitget_requests_per_second)
        self.semaphore = asyncio.Semaphore(max(1, settings.bitget_max_concurrency))
        self.breaker = CircuitBreaker(
            settings.bitget_circuit_failure_threshold,
            settings.bitget_circuit_recovery_sec,
        )
        self.contract_cache: dict[str, dict] = {}
        self.contract_cache_at = 0.0
        self.account_cache: dict | None = None
        self.account_cache_at = 0.0
        self.position_cache: list[dict] = []
        self.position_cache_at = 0.0

        self.last_positions: list[dict] = []
        self.last_sync = 0.0
        self.last_sync_error: str | None = None
        self.last_protection_verify = 0.0
        self.last_worker_error: str | None = None
        self.last_worker_tick = 0.0
        self.request_count = 0
        self.retry_count = 0
        self.rate_limit_count = 0

        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._entry_queue: asyncio.Queue[int] = asyncio.Queue(
            maxsize=max(8, settings.live_entry_queue_max)
        )
        self._sync_queue: asyncio.Queue[str] = asyncio.Queue(
            maxsize=max(32, settings.live_entry_queue_max)
        )
        self._sync_pending: set[str] = set()
        self._entry_lock = asyncio.Lock()
        self._reconcile_lock = asyncio.Lock()
        self._ensure_schema()

    def _ensure_schema(self):
        db.execute(
            """CREATE TABLE IF NOT EXISTS live_execution_jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            paper_position_id INTEGER,
            strategy TEXT,
            symbol TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL)"""
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_live_jobs_status ON live_execution_jobs(kind,status,next_retry_at)"
        )

    # lifecycle / health -------------------------------------------------

    def credentials_ready(self):
        return bool(
            settings.bitget_api_key
            and settings.bitget_api_secret
            and settings.bitget_passphrase
        )

    def web_master(self):
        return db.runtime_get("live_master_enabled", "false").lower() == "true"

    def set_web_master(self, enabled):
        db.runtime_set("live_master_enabled", "true" if enabled else "false")

    def gate_status(self):
        eligible = int(
            (
                db.one(
                    "SELECT COUNT(*)n FROM strategy_state WHERE live_eligible=1 "
                    "AND live_manual_enabled=1 AND stage='FINAL' AND enabled=1"
                )
                or {"n": 0}
            )["n"]
        )
        ready = bool(
            settings.live_trading_allowed
            and self.web_master()
            and self.credentials_ready()
            and eligible > 0
        )
        return {
            "deployment_allowed": settings.live_trading_allowed,
            "web_master_enabled": self.web_master(),
            "credentials_ready": self.credentials_ready(),
            "eligible_strategies": eligible,
            "ready": ready,
            "worker_running": self._running and any(not t.done() for t in self._tasks),
            "entry_queue": self._entry_queue.qsize(),
            "sync_queue": self._sync_queue.qsize(),
            "last_worker_error": self.last_worker_error,
            "last_worker_tick": int(self.last_worker_tick * 1000)
            if self.last_worker_tick
            else None,
            "bitget_circuit": self.breaker.status(),
            "bitget_requests": self.request_count,
            "bitget_retries": self.retry_count,
            "bitget_429": self.rate_limit_count,
        }

    async def start(self):
        if self._running:
            return
        self._running = True
        self._recover_jobs()
        self._tasks = [
            asyncio.create_task(self._entry_worker(), name="bitget-live-entry-worker"),
            asyncio.create_task(self._guardian_worker(), name="bitget-live-guardian"),
            asyncio.create_task(self._stop_sync_worker(), name="bitget-live-stop-sync"),
        ]

    async def stop(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        await self.client.aclose()

    def _recover_jobs(self):
        now = int(time.time() * 1000)
        for job in db.query(
            "SELECT * FROM live_execution_jobs WHERE kind='ENTRY' "
            "AND status IN('PENDING','RETRY','RUNNING') ORDER BY created_at"
        ):
            age = (now - int(job["created_at"])) / 1000
            if age > settings.live_entry_job_max_age_sec:
                db.execute(
                    "UPDATE live_execution_jobs SET status='EXPIRED',last_error=?,updated_at=? WHERE id=?",
                    ("stale entry job after restart", now, job["id"]),
                )
                continue
            db.execute(
                "UPDATE live_execution_jobs SET status='PENDING',updated_at=? WHERE id=?",
                (now, job["id"]),
            )
            try:
                self._entry_queue.put_nowait(int(job["id"]))
            except asyncio.QueueFull:
                break

    # robust official REST -----------------------------------------------

    def _headers(self, method: str, path_with_query: str, body: str = ""):
        ts = str(int(time.time() * 1000))
        msg = ts + method.upper() + path_with_query + body
        sig = base64.b64encode(
            hmac.new(
                settings.bitget_api_secret.encode(),
                msg.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        return {
            "ACCESS-KEY": settings.bitget_api_key,
            "ACCESS-SIGN": sig,
            "ACCESS-PASSPHRASE": settings.bitget_passphrase,
            "ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

    async def _request(
        self, method: str, path: str, params=None, payload=None, private=False
    ):
        if private and not self.credentials_ready():
            raise PermissionError("Bitget private API credentials incomplete")
        if not self.breaker.allow():
            raise RuntimeError("Bitget circuit breaker is open")

        items = [(k, v) for k, v in (params or {}).items() if v is not None]
        query = urlencode(items)
        pathq = path + (f"?{query}" if query else "")
        body = (
            ""
            if payload is None
            else json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        )
        attempts = max(1, settings.bitget_retry_attempts)

        async with self.semaphore:
            for attempt in range(attempts):
                await self.pacer.acquire()
                headers = self._headers(method, pathq, body) if private else {}
                try:
                    self.request_count += 1
                    r = await self.client.request(
                        method.upper(),
                        pathq,
                        headers=headers,
                        content=body if payload is not None else None,
                    )
                    if r.status_code == 429 or r.status_code >= 500:
                        if r.status_code == 429:
                            self.rate_limit_count += 1
                        self.breaker.failure()
                        if attempt + 1 >= attempts:
                            r.raise_for_status()
                        self.retry_count += 1
                        try:
                            retry_after = float(r.headers.get("Retry-After") or 0)
                        except Exception:
                            retry_after = 0
                        await asyncio.sleep(
                            retry_after
                            or min(8.0, 0.35 * (2**attempt))
                            + random.uniform(0.0, 0.15)
                        )
                        continue
                    r.raise_for_status()
                    try:
                        j = r.json()
                    except Exception as e:
                        raise RuntimeError(f"Bitget invalid JSON on {path}: {e}") from e
                    if str(j.get("code")) != "00000":
                        # deterministic business/auth/parameter errors do not
                        # open a global circuit for unrelated symbols
                        raise RuntimeError(
                            f"Bitget {path}: {j.get('code')} {j.get('msg')}"
                        )
                    self.breaker.success()
                    return j.get("data")
                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    self.breaker.failure()
                    if attempt + 1 >= attempts:
                        raise RuntimeError(
                            f"Bitget network failure on {path}: {type(e).__name__}"
                        ) from e
                    self.retry_count += 1
                    await asyncio.sleep(
                        min(8.0, 0.35 * (2**attempt)) + random.uniform(0.0, 0.15)
                    )
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429 or e.response.status_code >= 500:
                        self.breaker.failure()
                    raise RuntimeError(
                        f"Bitget HTTP {e.response.status_code} on {path}: "
                        f"{e.response.text[:400]}"
                    ) from e
        raise RuntimeError(f"Bitget request exhausted retries: {path}")

    async def _private(self, method, path, params=None, payload=None):
        return await self._request(
            method, path, params=params, payload=payload, private=True
        )

    async def _public(self, path, params=None):
        return await self._request("GET", path, params=params, private=False)

    # exchange state / contract precision -------------------------------

    async def contracts(self, force=False):
        if (
            not force
            and self.contract_cache
            and time.time() - self.contract_cache_at < 600
        ):
            return self.contract_cache
        rows = await self._public(
            "/api/v2/mix/market/contracts",
            {"productType": settings.bitget_product_type},
        )
        self.contract_cache = {
            str(r.get("symbol", "")).upper(): r
            for r in (rows or [])
            if r.get("symbol")
        }
        self.contract_cache_at = time.time()
        return self.contract_cache

    async def ticker(self, symbol):
        rows = await self._public(
            "/api/v2/mix/market/ticker",
            {"symbol": symbol, "productType": settings.bitget_product_type},
        )
        return (rows or [{}])[0] if isinstance(rows, list) else (rows or {})

    async def accounts(self, force=False):
        if (
            not force
            and self.account_cache
            and time.time() - self.account_cache_at < 2.0
        ):
            return self.account_cache
        rows = await self._private(
            "GET",
            "/api/v2/mix/account/accounts",
            {"productType": settings.bitget_product_type},
        )
        target = next(
            (
                x
                for x in (rows or [])
                if str(x.get("marginCoin", "")).upper()
                == settings.bitget_margin_coin.upper()
            ),
            (rows or [{}])[0] if rows else {},
        )
        self.account_cache = target or {}
        self.account_cache_at = time.time()
        return self.account_cache

    async def single_account(self, symbol):
        return await self._private(
            "GET",
            "/api/v2/mix/account/account",
            {
                "symbol": symbol,
                "productType": settings.bitget_product_type,
                "marginCoin": settings.bitget_margin_coin,
            },
        )

    async def positions(self, force=False):
        if not force and time.time() - self.position_cache_at < 0.75:
            return self.position_cache
        rows = await self._private(
            "GET",
            "/api/v2/mix/position/all-position",
            {
                "productType": settings.bitget_product_type,
                "marginCoin": settings.bitget_margin_coin,
            },
        )
        self.position_cache = rows or []
        self.position_cache_at = time.time()
        return self.position_cache

    async def pending_orders(self, symbol=None):
        p = {"productType": settings.bitget_product_type, "limit": "100"}
        if symbol:
            p["symbol"] = symbol
        d = await self._private("GET", "/api/v2/mix/order/orders-pending", p)
        return (d or {}).get("entrustedList", []) if isinstance(d, dict) else (d or [])

    async def order_detail(self, symbol=None, order_id=None, client_oid=None):
        if not order_id and not client_oid:
            return None
        p = {"productType": settings.bitget_product_type}
        if symbol:
            p["symbol"] = symbol
        if order_id:
            p["orderId"] = str(order_id)
        else:
            p["clientOid"] = str(client_oid)
        d = await self._private("GET", "/api/v2/mix/order/detail", p)
        return d or None

    async def pending_plans(self, symbol):
        d = await self._private(
            "GET",
            "/api/v2/mix/order/orders-plan-pending",
            {
                "symbol": symbol,
                "planType": "profit_loss",
                "productType": settings.bitget_product_type,
                "limit": "100",
            },
        )
        return (d or {}).get("entrustedList", []) if isinstance(d, dict) else []

    async def plan_history(self, symbol, start_ms=None):
        p = {
            "symbol": symbol,
            "planType": "profit_loss",
            "productType": settings.bitget_product_type,
            "limit": "100",
        }
        if start_ms:
            p["startTime"] = str(int(start_ms))
            p["endTime"] = str(int(time.time() * 1000))
        d = await self._private(
            "GET", "/api/v2/mix/order/orders-plan-history", p
        )
        return (d or {}).get("entrustedList", []) if isinstance(d, dict) else []

    async def set_position_mode(self, mode):
        d = await self._private(
            "POST",
            "/api/v2/mix/account/set-position-mode",
            payload={"productType": settings.bitget_product_type, "posMode": mode},
        )
        actual = str((d or {}).get("posMode") or "").lower()
        if actual and actual != mode.lower():
            raise RuntimeError(
                f"Bitget position mode verify failed: {actual} != {mode}"
            )
        self.account_cache_at = 0
        return d

    async def set_margin_mode(self, symbol, mode):
        d = await self._private(
            "POST",
            "/api/v2/mix/account/set-margin-mode",
            payload={
                "symbol": symbol,
                "productType": settings.bitget_product_type,
                "marginCoin": settings.bitget_margin_coin,
                "marginMode": mode,
            },
        )
        actual = str((d or {}).get("marginMode") or "").lower()
        if actual and actual != mode.lower():
            raise RuntimeError(
                f"Bitget margin mode verify failed: {actual} != {mode}"
            )
        self.account_cache_at = 0
        return d

    async def set_leverage(self, symbol, leverage, side):
        lev = max(
            1,
            int(Decimal(str(leverage)).to_integral_value(rounding=ROUND_DOWN)),
        )
        p = {
            "symbol": symbol,
            "productType": settings.bitget_product_type,
            "marginCoin": settings.bitget_margin_coin,
            "leverage": str(lev),
        }
        if (
            settings.bitget_position_mode == "hedge_mode"
            and settings.bitget_margin_mode == "isolated"
        ):
            p["holdSide"] = "long" if side == "long" else "short"
        return await self._private(
            "POST", "/api/v2/mix/account/set-leverage", payload=p
        )

    async def max_openable_qty(self, symbol, side):
        d = await self._private(
            "GET",
            "/api/v2/mix/account/max-open",
            {
                "symbol": symbol,
                "productType": settings.bitget_product_type,
                "marginCoin": settings.bitget_margin_coin,
                "posSide": "long" if side == "long" else "short",
                "orderType": "market",
            },
        )
        return _num((d or {}).get("maxOpen"), 0.0) if isinstance(d, dict) else 0.0

    async def ensure_exchange_mode(self, symbol, side):
        desired_pos = settings.bitget_position_mode.lower()
        desired_margin = settings.bitget_margin_mode.lower()
        acc = await self.single_account(symbol)
        actual_pos = str((acc or {}).get("posMode") or "").lower()
        actual_margin = str((acc or {}).get("marginMode") or "").lower()

        if actual_pos and actual_pos != desired_pos:
            positions = await self.positions(force=True)
            orders = await self.pending_orders()
            if any(self._position_size(p) > 0 for p in positions) or orders:
                raise PermissionError(
                    f"Bitget position mode is {actual_pos}, expected {desired_pos}; "
                    "cannot switch while positions/orders exist"
                )
            await self.set_position_mode(desired_pos)
            acc = await self.single_account(symbol)
            actual_pos = str((acc or {}).get("posMode") or desired_pos).lower()
            if actual_pos != desired_pos:
                raise PermissionError(
                    f"Bitget position mode remains {actual_pos}"
                )

        if actual_margin and actual_margin != desired_margin:
            positions = await self.positions(force=True)
            orders = await self.pending_orders(symbol)
            has_symbol_position = any(
                str(p.get("symbol", "")).upper() == symbol.upper()
                and self._position_size(p) > 0
                for p in positions
            )
            if has_symbol_position or orders:
                raise PermissionError(
                    f"{symbol} margin mode is {actual_margin}, expected {desired_margin}; "
                    "cannot switch while symbol position/order exists"
                )
            await self.set_margin_mode(symbol, desired_margin)
            acc = await self.single_account(symbol)
            actual_margin = str(
                (acc or {}).get("marginMode") or desired_margin
            ).lower()
            if actual_margin != desired_margin:
                raise PermissionError(
                    f"{symbol} margin mode remains {actual_margin}"
                )
        return {
            "posMode": actual_pos or desired_pos,
            "marginMode": actual_margin or desired_margin,
        }

    @staticmethod
    def _floor_step(value, step, places):
        a = Decimal(str(value))
        inc = Decimal(str(step or "1"))
        q = (a / inc).to_integral_value(rounding=ROUND_DOWN) * inc
        return q.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)

    async def _contract(self, symbol):
        c = (await self.contracts()).get(symbol.upper())
        if not c:
            raise RuntimeError(f"Bitget contract config missing: {symbol}")
        status = str(c.get("symbolStatus") or "").lower()
        symbol_type = str(c.get("symbolType") or "").lower()
        margins = {str(x).upper() for x in (c.get("supportMarginCoins") or [])}
        if status != "normal":
            raise PermissionError(
                f"{symbol} is not API-tradable now: symbolStatus={status}"
            )
        if symbol_type and symbol_type != "perpetual":
            raise PermissionError(f"{symbol} is not perpetual")
        if margins and settings.bitget_margin_coin.upper() not in margins:
            raise PermissionError(
                f"{symbol} does not support {settings.bitget_margin_coin} margin"
            )
        return c

    async def normalize_qty(self, symbol, qty, price, leverage):
        c = await self._contract(symbol)
        places = int(c.get("volumePlace") or 6)
        step = c.get("sizeMultiplier") or str(10 ** (-places))
        q = self._floor_step(qty, step, places)
        mn = Decimal(str(c.get("minTradeNum") or 0))
        min_notional = Decimal(str(c.get("minTradeUSDT") or 0))
        maxq = Decimal(str(c.get("maxMarketOrderQty") or 0))
        min_lev = max(1.0, _num(c.get("minLever"), 1.0))
        max_lev = (
            _num(c.get("maxLever"), settings.hard_max_leverage)
            or settings.hard_max_leverage
        )
        lev = min(float(leverage), max_lev, settings.hard_max_leverage)
        lev = max(min_lev, lev)
        lev = float(
            max(
                1,
                int(Decimal(str(lev)).to_integral_value(rounding=ROUND_DOWN)),
            )
        )

        # Never enlarge a risk-sized order merely to satisfy exchange minima.
        if q <= 0 or (mn > 0 and q < mn):
            raise PermissionError(
                f"{symbol} allocated quantity {q} is below Bitget minTradeNum {mn}; "
                "skip instead of oversizing"
            )
        if maxq > 0 and q > maxq:
            q = self._floor_step(maxq, step, places)
        notional = q * Decimal(str(price))
        if min_notional > 0 and notional < min_notional:
            raise PermissionError(
                f"{symbol} allocated notional {notional} is below Bitget "
                f"minTradeUSDT {min_notional}; skip instead of oversizing"
            )
        return str(q), lev, c

    async def normalize_price(self, symbol, price):
        c = await self._contract(symbol)
        places = int(c.get("pricePlace") or 8)
        end_step = Decimal(str(c.get("priceEndStep") or "1"))
        tick = end_step * (Decimal(10) ** (-places))
        if tick <= 0:
            tick = Decimal(10) ** (-places)
        p = Decimal(str(price))
        q = (p / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
        q = q.quantize(Decimal(1).scaleb(-places))
        if q <= 0:
            raise RuntimeError(f"{symbol} normalized trigger price <= 0")
        return _text(q)

    # shared live allocation --------------------------------------------

    def _position_size(self, row):
        return abs(
            _num(
                row.get("total")
                or row.get("available")
                or row.get("holdVolume")
                or 0,
                0.0,
            )
        )

    def _position_notional(self, row):
        q = self._position_size(row)
        for k in (
            "markPrice",
            "marketPrice",
            "averageOpenPrice",
            "openPriceAvg",
        ):
            p = _num(row.get(k), 0)
            if p > 0:
                return q * p
        margin = _num(row.get("marginSize") or row.get("margin"), 0)
        lev = _num(row.get("leverage"), 1)
        return margin * max(lev, 1)

    def _acct_num(self, a, *keys):
        for k in keys:
            v = _num((a or {}).get(k), 0)
            if v > 0:
                return v
        return 0.0

    def _quality(self, strategy):
        rows = db.query(
            """SELECT SUM(net_pnl)pnl,
            SUM(CASE WHEN net_pnl>0 THEN net_pnl ELSE 0 END)gw,
            -SUM(CASE WHEN net_pnl<0 THEN net_pnl ELSE 0 END)gl,COUNT(*)n
            FROM (
              SELECT position_id,SUM(net_pnl)net_pnl
              FROM trades
              WHERE strategy=? AND variant='champion'
              AND NOT EXISTS(SELECT 1 FROM positions p WHERE p.id=trades.position_id)
              GROUP BY position_id ORDER BY MAX(closed_at) DESC LIMIT 120
            )""",
            (strategy,),
        )
        r = rows[0] if rows else {}
        pf = _num(r.get("gw")) / max(_num(r.get("gl")), 1e-9)
        n = int(r.get("n") or 0)
        q = 0.78 + min(0.42, max(-0.18, (pf - 1) * 0.22)) + min(
            0.12, n / 500
        )
        return max(0.65, min(1.35, q)), pf, n

    def _opening_order_notional(self, order):
        if str(order.get("reduceOnly") or "").upper() == "YES":
            return 0.0
        qty = abs(_num(order.get("size"), 0))
        price = _num(order.get("price"), 0)
        return qty * price if qty > 0 and price > 0 else 0.0

    async def allocate(
        self, position, params, price, account, live_positions, open_orders
    ):
        equity = self._acct_num(
            account,
            "accountEquity",
            "usdtEquity",
            "equity",
            "marginBalance",
            "available",
        )
        available = self._acct_num(
            account, "available", "crossedMaxAvailable", "maxTransferOut"
        )
        if equity <= 0:
            equity = max(available, 1.0)
        if available <= 0:
            raise PermissionError("Bitget account has no positive available margin")

        pos_count = sum(
            1 for p in live_positions if self._position_size(p) > 0
        )
        pending_count = sum(
            1
            for o in open_orders
            if str(o.get("reduceOnly") or "").upper() != "YES"
        )
        if (
            pos_count + pending_count
            >= settings.live_max_concurrent_positions
        ):
            raise PermissionError(
                "shared live portfolio max concurrent positions/orders reached"
            )

        total = sum(self._position_notional(p) for p in live_positions)
        total += sum(self._opening_order_notional(o) for o in open_orders)
        total += sum(
            _num(x.get("notional"), 0)
            for x in db.query(
                "SELECT notional FROM live_orders WHERE status='SUBMITTING'"
            )
        )

        quality, pf, n = self._quality(position["strategy"])
        stop_pct = abs(
            float(position["entry"]) - float(position["initial_stop"])
        ) / max(float(position["entry"]), 1e-12)
        if stop_pct <= 0:
            raise RuntimeError("invalid stop distance")

        learned_lev = float(params.get("leverage", 2))
        lev = min(
            settings.hard_max_leverage,
            max(1.0, learned_lev * min(1.15, quality)),
        )
        risk_pct = min(
            settings.live_max_risk_pct,
            settings.hard_max_risk_per_trade,
            settings.live_base_risk_pct * quality,
        )
        risk_cash = equity * risk_pct
        by_risk = risk_cash / stop_pct
        order_cap = equity * settings.live_max_order_equity_pct
        total_cap = max(
            0.0, equity * settings.live_max_total_notional_multiple - total
        )
        reserve = equity * settings.live_min_free_margin_pct
        margin_cap = (
            max(0.0, available - reserve)
            * lev
            * settings.live_available_balance_buffer
        )
        notional = max(
            0.0, min(by_risk, order_cap, total_cap, margin_cap)
        )
        if notional <= 0:
            raise PermissionError("shared portfolio allocator returned zero")

        return {
            "equity": equity,
            "available": available,
            "quality": quality,
            "pf": pf,
            "sample_n": n,
            "risk_pct": risk_pct,
            "risk_cash": risk_cash,
            "stop_pct": stop_pct,
            "leverage": lev,
            "notional": notional,
            "portfolio_notional_before": total,
            "portfolio_cap": equity
            * settings.live_max_total_notional_multiple,
            "position_count": pos_count,
            "pending_open_order_count": pending_count,
        }

    # durable execution queue -------------------------------------------

    async def place_from_paper(self, position, params):
        """Compatibility entrypoint: queue instead of blocking scanner."""
        return await self.queue_entry(position, params)

    async def queue_entry(self, position, params):
        state = db.state(position["strategy"])
        if (
            not state
            or state["stage"] != "FINAL"
            or not state["live_eligible"]
            or not state.get("live_manual_enabled")
        ):
            raise PermissionError(
                "strategy not FINAL/auto-eligible/manual-approved"
            )
        if not self.gate_status()["ready"]:
            raise PermissionError("live gate not ready")

        existing = db.one(
            "SELECT * FROM live_orders WHERE paper_position_id=? "
            "AND status NOT IN('FAILED','CANCELLED')",
            (position["id"],),
        )
        if existing:
            return {
                "skipped": "already submitted or in progress",
                "live_order_id": existing["id"],
            }

        now = int(time.time() * 1000)
        key = f"ENTRY:{int(position['id'])}"
        payload = json.dumps({"params": params}, separators=(",", ":"))
        db.execute(
            """INSERT INTO live_execution_jobs(
                 job_key,kind,paper_position_id,strategy,symbol,payload_json,status,
                 attempts,next_retry_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'PENDING',0,0,?,?)
               ON CONFLICT(job_key) DO UPDATE SET
                 payload_json=excluded.payload_json,
                 status=CASE
                   WHEN live_execution_jobs.status IN('DONE','RUNNING')
                   THEN live_execution_jobs.status ELSE 'PENDING' END,
                 updated_at=excluded.updated_at""",
            (
                key,
                "ENTRY",
                int(position["id"]),
                position["strategy"],
                position["symbol"],
                payload,
                now,
                now,
            ),
        )
        job = db.one(
            "SELECT * FROM live_execution_jobs WHERE job_key=?", (key,)
        )
        if job and job["status"] not in self.TERMINAL_JOB:
            try:
                self._entry_queue.put_nowait(int(job["id"]))
            except asyncio.QueueFull:
                db.execute(
                    "UPDATE live_execution_jobs SET status='RETRY',last_error=?,"
                    "next_retry_at=?,updated_at=? WHERE id=?",
                    ("entry queue full", now + 1000, now, job["id"]),
                )
                raise RuntimeError("live entry queue is full")
        return {"queued": True, "job_id": int(job["id"]) if job else None}

    def queue_symbol_sync(self, symbol):
        if not settings.live_trail_sync_enabled or not self.credentials_ready():
            return
        symbol = str(symbol).upper()
        if symbol in self._sync_pending:
            return
        try:
            self._sync_pending.add(symbol)
            self._sync_queue.put_nowait(symbol)
        except asyncio.QueueFull:
            self._sync_pending.discard(symbol)

    async def sync_from_paper_for_symbol(self, symbol):
        """Compatibility method; deliberately performs no exchange I/O."""
        self.queue_symbol_sync(symbol)

    async def _entry_worker(self):
        while self._running:
            try:
                job_id = await asyncio.wait_for(
                    self._entry_queue.get(), timeout=0.5
                )
            except asyncio.TimeoutError:
                self._requeue_due_jobs()
                continue
            try:
                await self._run_entry_job(job_id)
            except Exception as e:
                self.last_worker_error = (
                    f"entry worker: {type(e).__name__}: {e}"
                )
                db.event(
                    "LIVE_ENTRY_WORKER_ERROR", self.last_worker_error, "ERROR"
                )
            finally:
                self.last_worker_tick = time.time()
                self._entry_queue.task_done()

    def _requeue_due_jobs(self):
        now = int(time.time() * 1000)
        for job in db.query(
            "SELECT id FROM live_execution_jobs WHERE kind='ENTRY' "
            "AND status='RETRY' AND next_retry_at<=? ORDER BY updated_at LIMIT 10",
            (now,),
        ):
            try:
                self._entry_queue.put_nowait(int(job["id"]))
                db.execute(
                    "UPDATE live_execution_jobs SET status='PENDING',updated_at=? WHERE id=?",
                    (now, job["id"]),
                )
            except asyncio.QueueFull:
                break

    async def _run_entry_job(self, job_id):
        async with self._entry_lock:
            job = db.one(
                "SELECT * FROM live_execution_jobs WHERE id=?", (job_id,)
            )
            if not job or job["status"] in self.TERMINAL_JOB:
                return
            now = int(time.time() * 1000)
            age = (now - int(job["created_at"])) / 1000
            if age > settings.live_entry_job_max_age_sec:
                db.execute(
                    "UPDATE live_execution_jobs SET status='EXPIRED',last_error=?,updated_at=? WHERE id=?",
                    ("entry signal too old", now, job_id),
                )
                return

            db.execute(
                "UPDATE live_execution_jobs SET status='RUNNING',attempts=attempts+1,updated_at=? WHERE id=?",
                (now, job_id),
            )
            pos = db.one(
                "SELECT * FROM positions WHERE id=?",
                (job["paper_position_id"],),
            )
            if not pos:
                db.execute(
                    "UPDATE live_execution_jobs SET status='EXPIRED',last_error=?,updated_at=? WHERE id=?",
                    ("paper position no longer open", now, job_id),
                )
                return

            try:
                params = (
                    json.loads(job.get("payload_json") or "{}") or {}
                ).get("params") or {}
                result = await self._execute_entry(pos, params)
                db.execute(
                    "UPDATE live_execution_jobs SET status='DONE',last_error=NULL,updated_at=? WHERE id=?",
                    (int(time.time() * 1000), job_id),
                )
                db.event(
                    "LIVE_ENTRY_JOB_DONE",
                    f"{pos['strategy']} {pos['symbol']} live order accepted",
                )
                return result
            except PermissionError as e:
                db.execute(
                    "UPDATE live_execution_jobs SET status='FAILED',last_error=?,updated_at=? WHERE id=?",
                    (str(e), int(time.time() * 1000), job_id),
                )
                db.risk_event(
                    "LIVE_ENTRY_BLOCKED",
                    str(e),
                    pos["strategy"],
                    "champion",
                    pos["symbol"],
                )
            except Exception as e:
                row = db.one(
                    "SELECT attempts FROM live_execution_jobs WHERE id=?",
                    (job_id,),
                ) or {"attempts": 1}
                attempts = int(row["attempts"])
                if attempts < max(2, settings.bitget_retry_attempts):
                    delay = min(30, 2**attempts)
                    now2 = int(time.time() * 1000)
                    db.execute(
                        "UPDATE live_execution_jobs SET status='RETRY',last_error=?,"
                        "next_retry_at=?,updated_at=? WHERE id=?",
                        (str(e), now2 + delay * 1000, now2, job_id),
                    )
                else:
                    db.execute(
                        "UPDATE live_execution_jobs SET status='FAILED',last_error=?,updated_at=? WHERE id=?",
                        (str(e), int(time.time() * 1000), job_id),
                    )
                db.event(
                    "LIVE_ENTRY_JOB_FAILED",
                    f"{pos['strategy']} {pos['symbol']}: {e}",
                    "ERROR",
                )

    # actual exchange entry ---------------------------------------------

    async def _wait_position(self, symbol, tries=10):
        for _ in range(tries):
            rows = await self.positions(force=True)
            for p in rows or []:
                if (
                    str(p.get("symbol") or "").upper() == symbol.upper()
                    and self._position_size(p) > 0
                ):
                    return p
            await asyncio.sleep(0.4)
        return None

    async def _execute_entry(self, position, params):
        state = db.state(position["strategy"])
        if (
            not state
            or state["stage"] != "FINAL"
            or not state["live_eligible"]
            or not state.get("live_manual_enabled")
        ):
            raise PermissionError(
                "strategy lost FINAL/live/manual eligibility before execution"
            )
        if not self.gate_status()["ready"]:
            raise PermissionError("live gate not ready at execution time")

        existing = db.one(
            "SELECT * FROM live_orders WHERE paper_position_id=? "
            "AND status NOT IN('FAILED','CANCELLED')",
            (position["id"],),
        )
        if existing and existing["status"] == "OPEN":
            return {"skipped": "already open", "live_order_id": existing["id"]}

        live_positions = await self.reconcile(force=True)
        for lp in live_positions:
            if (
                str(lp.get("symbol") or "").upper()
                == position["symbol"].upper()
                and self._position_size(lp) > 0
            ):
                raise PermissionError(
                    "existing Bitget position on this symbol; no stacking"
                )

        open_orders = await self.pending_orders()
        if any(
            str(x.get("symbol") or "").upper() == position["symbol"].upper()
            and str(x.get("reduceOnly") or "").upper() != "YES"
            for x in open_orders
        ):
            raise PermissionError(
                "existing Bitget opening order on this symbol; no stacking"
            )

        paper_price = float(position["entry"])
        ticker = await self.ticker(position["symbol"])
        price = _num((ticker or {}).get("lastPr"), paper_price)
        drift = abs(price - paper_price) / max(paper_price, 1e-12) * 10000
        if drift > settings.live_max_entry_drift_bps:
            raise PermissionError(
                f"live entry drift {drift:.1f}bps exceeds "
                f"{settings.live_max_entry_drift_bps:.1f}bps"
            )

        await self.ensure_exchange_mode(position["symbol"], position["side"])
        account = await self.accounts(force=True)
        alloc = await self.allocate(
            position, params, price, account, live_positions, open_orders
        )

        qty, lev, _ = await self.normalize_qty(
            position["symbol"],
            alloc["notional"] / price,
            price,
            alloc["leverage"],
        )
        await self.set_leverage(position["symbol"], lev, position["side"])

        max_open = await self.max_openable_qty(
            position["symbol"], position["side"]
        )
        if (
            max_open > 0
            and float(qty)
            > max_open * settings.live_max_open_safety_ratio
        ):
            qty, lev, _ = await self.normalize_qty(
                position["symbol"],
                max_open * settings.live_max_open_safety_ratio,
                price,
                lev,
            )
            alloc["max_open_qty"] = max_open
            alloc["max_open_capped"] = True
        else:
            alloc["max_open_qty"] = max_open
            alloc["max_open_capped"] = False

        actual = float(qty) * price
        if actual <= 0:
            raise RuntimeError("normalized live qty zero")
        alloc["notional"] = actual
        alloc["normalized_qty"] = float(qty)
        alloc["leverage"] = lev

        stop = await self.normalize_price(position["symbol"], position["stop"])
        tp1 = await self.normalize_price(position["symbol"], position["tp1"])
        tp2 = await self.normalize_price(position["symbol"], position["tp2"])
        tp3 = await self.normalize_price(position["symbol"], position["tp3"])
        initial_stop = await self.normalize_price(
            position["symbol"], position["initial_stop"]
        )

        # Deterministic clientOid makes network-timeout recovery idempotent.
        client = f"ai-p{int(position['id'])}-{position['side'][0]}"
        side = "buy" if position["side"] == "long" else "sell"
        entry_payload = {
            "symbol": position["symbol"],
            "productType": settings.bitget_product_type,
            "marginMode": settings.bitget_margin_mode,
            "marginCoin": settings.bitget_margin_coin,
            "size": qty,
            "side": side,
            "orderType": "market",
            "clientOid": client,
            # Immediate exchange-side fallback while Guardian installs the
            # dedicated SL1/STOP/TP1/TP2/TP3 plans. Execute-price fields are
            # intentionally omitted so Bitget uses market execution.
            "presetStopLossPrice": stop,
            "presetStopSurplusPrice": tp3,
        }
        if settings.bitget_position_mode == "hedge_mode":
            entry_payload["tradeSide"] = "open"

        now = int(time.time() * 1000)
        plan = {
            "entry": float(position["entry"]),
            "initial_stop": float(initial_stop),
            "stop": float(stop),
            "tp1": float(tp1),
            "tp2": float(tp2),
            "tp3": float(tp3),
            "tp1_fraction": float(position["tp1_fraction"]),
            "tp2_fraction": float(position["tp2_fraction"]),
            "sl1_r": float(params.get("sl1_r", 0.62)),
            "sl1_fraction": float(params.get("sl1_fraction", 0.18)),
            "params": params,
        }

        if existing:
            live_id = int(existing["id"])
            db.execute(
                "UPDATE live_orders SET qty=?,entry_price=?,notional=?,leverage=?,"
                "allocator_json=?,client_oid=?,status='SUBMITTING',detail_json=?,updated_at=? WHERE id=?",
                (
                    float(qty),
                    price,
                    actual,
                    lev,
                    json.dumps(alloc),
                    client,
                    json.dumps({"entry_payload": entry_payload, "plan": plan}),
                    now,
                    live_id,
                ),
            )
        else:
            cur = db.execute(
                """INSERT INTO live_orders(
                paper_position_id,strategy,symbol,side,qty,entry_price,notional,leverage,
                allocator_json,client_oid,status,detail_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'SUBMITTING',?,?,?)""",
                (
                    position["id"],
                    position["strategy"],
                    position["symbol"],
                    position["side"],
                    float(qty),
                    price,
                    actual,
                    lev,
                    json.dumps(alloc),
                    client,
                    json.dumps({"entry_payload": entry_payload, "plan": plan}),
                    now,
                    now,
                ),
            )
            live_id = int(cur.lastrowid)

        try:
            try:
                entry = await self._private(
                    "POST", "/api/v2/mix/order/place-order", payload=entry_payload
                )
            except Exception:
                # If a request timed out after Bitget accepted it, recover by
                # clientOid before ever considering another opening request.
                detail = await self.order_detail(
                    symbol=position["symbol"], client_oid=client
                )
                if not detail:
                    raise
                entry = {
                    "orderId": detail.get("orderId"),
                    "clientOid": detail.get("clientOid") or client,
                    "recoveredByClientOid": True,
                }

            eid = (
                (entry or {}).get("orderId") if isinstance(entry, dict) else None
            )
            detail_json = {
                "entry": entry,
                "entry_payload": entry_payload,
                "plan": plan,
                "allocator": alloc,
                "entry_drift_bps": drift,
            }
            db.execute(
                "UPDATE live_orders SET exchange_order_id=?,status='OPEN',detail_json=?,updated_at=? WHERE id=?",
                (eid, json.dumps(detail_json), int(time.time() * 1000), live_id),
            )
            order = db.one("SELECT * FROM live_orders WHERE id=?", (live_id,))
            live_pos = await self._wait_position(position["symbol"])
            if not live_pos:
                db.execute(
                    "UPDATE live_orders SET status='ENTRY_UNCONFIRMED',updated_at=? WHERE id=?",
                    (int(time.time() * 1000), live_id),
                )
                db.risk_event(
                    "LIVE_POSITION_CONFIRM_TIMEOUT",
                    "entry accepted but position endpoint did not confirm yet; Guardian will keep reconciling",
                    position["strategy"],
                    "champion",
                    position["symbol"],
                )
                return {
                    "entry": entry,
                    "allocator": alloc,
                    "protection_verified": False,
                }

            ok = await self.ensure_protections(order, position, live_pos)
            if not ok:
                raise RuntimeError(
                    "entry placed but critical exchange protections could not be verified"
                )
            return {
                "entry": entry,
                "allocator": alloc,
                "protection_verified": True,
            }
        except Exception as e:
            current = db.one(
                "SELECT status FROM live_orders WHERE id=?", (live_id,)
            )
            if current and current["status"] == "SUBMITTING":
                db.execute(
                    "UPDATE live_orders SET status='FAILED',detail_json=?,updated_at=? WHERE id=?",
                    (
                        json.dumps(
                            {
                                "error": str(e),
                                "entry_payload": entry_payload,
                                "plan": plan,
                                "allocator": alloc,
                            }
                        ),
                        int(time.time() * 1000),
                        live_id,
                    ),
                )
            db.event(
                "LIVE_ORDER_FAILED",
                f"{position['strategy']} {position['symbol']}: {e}",
                "ERROR",
            )
            raise

    # protection guardian ------------------------------------------------

    def _hold_side(self, side):
        if settings.bitget_position_mode == "hedge_mode":
            return "long" if side == "long" else "short"
        return "buy" if side == "long" else "sell"

    def _plan_trigger(self, p, role):
        if role in {"STOP", "SL1"}:
            keys = ("stopLossTriggerPrice", "triggerPrice")
        else:
            keys = ("stopSurplusTriggerPrice", "triggerPrice")
        for k in keys:
            v = _num(p.get(k), 0)
            if v > 0:
                return v
        return 0.0

    @staticmethod
    def _near(a, b, pct=0.002):
        return a > 0 and b > 0 and abs(a - b) / max(abs(b), 1e-12) < pct

    def _upsert_protection(
        self,
        live_order_id,
        role,
        order_id,
        client_oid,
        trigger,
        size,
        status="LIVE",
        detail=None,
    ):
        now = int(time.time() * 1000)
        db.execute(
            """INSERT INTO live_protections(
            live_order_id,role,order_id,client_oid,trigger_price,size,status,detail_json,
            last_verified_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(live_order_id,role) DO UPDATE SET
            order_id=excluded.order_id,client_oid=excluded.client_oid,
            trigger_price=excluded.trigger_price,size=excluded.size,status=excluded.status,
            detail_json=excluded.detail_json,last_verified_at=excluded.last_verified_at,
            updated_at=excluded.updated_at""",
            (
                live_order_id,
                role,
                order_id,
                client_oid,
                trigger,
                size,
                status,
                json.dumps(detail or {}),
                now,
                now,
                now,
            ),
        )

    async def _place_plan(self, order, role, trigger, size, plan_type):
        cid = f"{order['client_oid']}-{role.lower()}-{uuid.uuid4().hex[:5]}"
        p = {
            "marginCoin": settings.bitget_margin_coin,
            "productType": settings.bitget_product_type,
            "symbol": order["symbol"],
            "planType": plan_type,
            "triggerPrice": await self.normalize_price(order["symbol"], trigger),
            "triggerType": "mark_price",
            "executePrice": "0",
            "holdSide": self._hold_side(order["side"]),
            "clientOid": cid,
        }
        if plan_type in {"profit_plan", "loss_plan", "moving_plan"}:
            p["size"] = str(size)
        return cid, await self._private(
            "POST", "/api/v2/mix/order/place-tpsl-order", payload=p
        )

    def _compatible_existing_plan(self, x, order, role, trigger):
        if str(x.get("symbol") or "").upper() != order["symbol"].upper():
            return False
        if not self._near(self._plan_trigger(x, role), trigger):
            return False
        plan_type = str(x.get("planType") or "").lower()
        if role == "STOP" and plan_type not in {"pos_loss", "loss_plan"}:
            return False
        if role == "TP3" and plan_type not in {"pos_profit", "profit_plan"}:
            return False
        if role == "SL1" and plan_type != "loss_plan":
            return False
        if role in {"TP1", "TP2"} and plan_type != "profit_plan":
            return False
        if role in {"SL1", "TP1", "TP2"}:
            cid = str(x.get("clientOid") or "")
            return cid.startswith(str(order["client_oid"]))
        return True

    async def ensure_protections(self, order, paper_pos=None, live_pos=None):
        detail = json.loads(order.get("detail_json") or "{}")
        plan = detail.get("plan") or {}
        entry = float(
            (paper_pos or {}).get("entry")
            or plan.get("entry")
            or order.get("entry_price")
            or 0
        )
        initial_stop = float(
            (paper_pos or {}).get("initial_stop")
            or plan.get("initial_stop")
            or plan.get("stop")
            or 0
        )
        stop = float(
            (paper_pos or {}).get("stop") or plan.get("stop") or initial_stop
        )
        tp1 = float((paper_pos or {}).get("tp1") or plan.get("tp1") or 0)
        tp2 = float((paper_pos or {}).get("tp2") or plan.get("tp2") or 0)
        tp3 = float((paper_pos or {}).get("tp3") or plan.get("tp3") or 0)
        params = (
            json.loads((paper_pos or {}).get("params_json") or "{}")
            if paper_pos
            else plan.get("params", {})
        )
        if stop <= 0 or tp3 <= 0 or entry <= 0:
            raise RuntimeError("missing protection plan prices")

        stop = float(await self.normalize_price(order["symbol"], stop))
        tp1 = float(await self.normalize_price(order["symbol"], tp1))
        tp2 = float(await self.normalize_price(order["symbol"], tp2))
        tp3 = float(await self.normalize_price(order["symbol"], tp3))
        initial_stop = float(
            await self.normalize_price(order["symbol"], initial_stop)
        )

        r = abs(entry - initial_stop)
        sg = 1 if order["side"] == "long" else -1
        sl1 = entry - sg * r * float(
            params.get("sl1_r", plan.get("sl1_r", 0.62))
        )
        sl1 = float(await self.normalize_price(order["symbol"], sl1))
        sl1f = float(
            params.get("sl1_fraction", plan.get("sl1_fraction", 0.18))
        )
        f1 = float(
            (paper_pos or {}).get("tp1_fraction")
            or plan.get("tp1_fraction")
            or 0.25
        )
        f2 = float(
            (paper_pos or {}).get("tp2_fraction")
            or plan.get("tp2_fraction")
            or 0.30
        )

        c = await self._contract(order["symbol"])
        places = int(c.get("volumePlace") or 6)
        step = c.get("sizeMultiplier") or str(10 ** (-places))
        original_total = Decimal(str(order["qty"]))
        if not live_pos:
            live_pos = next(
                (
                    p
                    for p in await self.positions(force=True)
                    if str(p.get("symbol") or "").upper()
                    == order["symbol"].upper()
                    and self._position_size(p) > 0
                ),
                None,
            )
        current_total = Decimal(
            str(self._position_size(live_pos or {}))
        )
        if current_total <= 0:
            return False

        qsl = min(
            current_total,
            self._floor_step(float(original_total) * sl1f, step, places),
        )
        q1 = min(
            current_total,
            self._floor_step(float(original_total) * f1, step, places),
        )
        q2 = min(
            current_total,
            self._floor_step(float(original_total) * f2, step, places),
        )

        required = [
            ("STOP", stop, Decimal("0"), "pos_loss"),
            ("TP3", tp3, Decimal("0"), "pos_profit"),
        ]
        if settings.live_partial_tp_enabled:
            required = [
                ("STOP", stop, Decimal("0"), "pos_loss"),
                ("SL1", sl1, qsl, "loss_plan"),
                ("TP1", tp1, q1, "profit_plan"),
                ("TP2", tp2, q2, "profit_plan"),
                ("TP3", tp3, Decimal("0"), "pos_profit"),
            ]

        for attempt in range(max(1, settings.live_protection_retry)):
            pending = await self.pending_plans(order["symbol"])
            dbp = {
                x["role"]: x
                for x in db.query(
                    "SELECT * FROM live_protections WHERE live_order_id=?",
                    (order["id"],),
                )
            }
            missing = []
            need_history = False

            for role, trig, size, ptype in required:
                pr = dbp.get(role)
                if pr and pr.get("status") == "FILLED":
                    continue

                found = None
                if pr:
                    for x in pending:
                        if (
                            pr.get("order_id")
                            and str(x.get("orderId")) == str(pr["order_id"])
                        ) or (
                            pr.get("client_oid")
                            and str(x.get("clientOid"))
                            == str(pr["client_oid"])
                        ):
                            found = x
                            break
                    if not found:
                        need_history = True
                else:
                    for x in pending:
                        if self._compatible_existing_plan(
                            x, order, role, trig
                        ):
                            found = x
                            break

                if found:
                    self._upsert_protection(
                        order["id"],
                        role,
                        str(found.get("orderId") or ""),
                        str(found.get("clientOid") or ""),
                        trig,
                        float(size),
                        "LIVE",
                        found,
                    )
                else:
                    missing.append((role, trig, size, ptype))

            history = []
            if need_history:
                try:
                    history = await self.plan_history(
                        order["symbol"],
                        max(0, int(order["created_at"]) - 3_600_000),
                    )
                except Exception:
                    history = []

            if history:
                still_missing = []
                for role, trig, size, ptype in missing:
                    pr = dbp.get(role)
                    hx_found = None
                    if pr:
                        for hx in history:
                            same = (
                                pr.get("order_id")
                                and str(hx.get("orderId"))
                                == str(pr["order_id"])
                            ) or (
                                pr.get("client_oid")
                                and str(hx.get("clientOid"))
                                == str(pr["client_oid"])
                            )
                            if same:
                                hx_found = hx
                                break
                    if hx_found:
                        status = str(
                            hx_found.get("planStatus")
                            or hx_found.get("status")
                            or ""
                        ).lower()
                        if status == "executed":
                            self._upsert_protection(
                                order["id"],
                                role,
                                str(hx_found.get("orderId") or ""),
                                str(hx_found.get("clientOid") or ""),
                                trig,
                                float(size),
                                "FILLED",
                                hx_found,
                            )
                            continue
                    still_missing.append((role, trig, size, ptype))
                missing = still_missing

            if not missing:
                return True

            for role, trig, size, ptype in missing:
                try:
                    if ptype in {"profit_plan", "loss_plan"} and size <= 0:
                        continue
                    cid, res = await self._place_plan(
                        order, role, trig, _text(size), ptype
                    )
                    oid = (
                        (res or {}).get("orderId")
                        if isinstance(res, dict)
                        else ""
                    )
                    self._upsert_protection(
                        order["id"],
                        role,
                        str(oid or ""),
                        cid,
                        trig,
                        float(size),
                        "PENDING_VERIFY",
                        res,
                    )
                except Exception as e:
                    db.risk_event(
                        "LIVE_PROTECTION_PLACE_FAILED",
                        f"{role}: {e}",
                        order["strategy"],
                        "champion",
                        order["symbol"],
                    )
            await asyncio.sleep(0.35 * (attempt + 1))

        verified = {
            x["role"]
            for x in db.query(
                "SELECT role FROM live_protections WHERE live_order_id=? "
                "AND status IN('LIVE','FILLED','PENDING_VERIFY')",
                (order["id"],),
            )
        }
        critical_ok = "STOP" in verified and "TP3" in verified
        if critical_ok:
            db.risk_event(
                "LIVE_PROTECTION_PARTIAL",
                "critical full STOP + final TP exist; partial plans will keep repairing",
                order["strategy"],
                "champion",
                order["symbol"],
            )
            return True

        db.risk_event(
            "LIVE_PROTECTION_INCOMPLETE",
            "could not verify critical full STOP and final TP",
            order["strategy"],
            "champion",
            order["symbol"],
        )
        if settings.live_emergency_close_on_protection_failure:
            await self.emergency_close(
                order, "critical protection verification failed"
            )
        return False

    async def emergency_close(self, order, why):
        rows = await self.positions(force=True)
        p = next(
            (
                x
                for x in rows or []
                if str(x.get("symbol") or "").upper()
                == order["symbol"].upper()
                and self._position_size(x) > 0
            ),
            None,
        )
        if not p:
            return
        size = _text(self._position_size(p))
        payload = {
            "symbol": order["symbol"],
            "productType": settings.bitget_product_type,
            "marginMode": settings.bitget_margin_mode,
            "marginCoin": settings.bitget_margin_coin,
            "size": size,
            "orderType": "market",
            "clientOid": "ai-emergency-" + uuid.uuid4().hex[:18],
        }
        if settings.bitget_position_mode == "hedge_mode":
            payload.update(
                {
                    "side": "buy" if order["side"] == "long" else "sell",
                    "tradeSide": "close",
                }
            )
        else:
            payload.update(
                {
                    "side": "sell" if order["side"] == "long" else "buy",
                    "reduceOnly": "YES",
                }
            )
        try:
            await self._private(
                "POST", "/api/v2/mix/order/place-order", payload=payload
            )
            db.execute(
                "UPDATE live_orders SET status='EMERGENCY_CLOSED',updated_at=? WHERE id=?",
                (int(time.time() * 1000), order["id"]),
            )
            db.event(
                "LIVE_EMERGENCY_CLOSE", f"{order['symbol']} {why}", "ERROR"
            )
        except Exception as e:
            db.event(
                "LIVE_EMERGENCY_CLOSE_FAILED",
                f"{order['symbol']} {why}: {e}",
                "ERROR",
            )
            raise

    # move only the STOP that paper strategy already calculated ----------

    async def _sync_symbol_now(self, symbol):
        if not settings.live_trail_sync_enabled or not self.credentials_ready():
            return
        for order in db.query(
            "SELECT * FROM live_orders WHERE symbol=? "
            "AND status IN('OPEN','ENTRY_UNCONFIRMED')",
            (symbol,),
        ):
            pos = db.one(
                "SELECT * FROM positions WHERE id=?",
                (order["paper_position_id"],),
            )
            if not pos:
                continue
            prot = db.one(
                "SELECT * FROM live_protections WHERE live_order_id=? AND role='STOP'",
                (order["id"],),
            )
            desired = float(await self.normalize_price(symbol, pos["stop"]))
            if not prot:
                live_pos = next(
                    (
                        x
                        for x in await self.positions(force=True)
                        if str(x.get("symbol") or "").upper()
                        == symbol.upper()
                        and self._position_size(x) > 0
                    ),
                    None,
                )
                if live_pos:
                    await self.ensure_protections(order, pos, live_pos)
                continue

            old = float(prot["trigger_price"])
            tighter = (
                desired > old * (1 + 1e-8)
                if order["side"] == "long"
                else desired < old * (1 - 1e-8)
            )
            if not tighter:
                continue

            payload = {
                "marginCoin": settings.bitget_margin_coin,
                "productType": settings.bitget_product_type,
                "symbol": symbol,
                "triggerPrice": _text(desired),
                "triggerType": "mark_price",
                "executePrice": "0",
                "size": "",
            }
            if prot.get("order_id"):
                payload["orderId"] = prot["order_id"]
            else:
                payload["clientOid"] = prot["client_oid"]
            try:
                res = await self._private(
                    "POST",
                    "/api/v2/mix/order/modify-tpsl-order",
                    payload=payload,
                )
                self._upsert_protection(
                    order["id"],
                    "STOP",
                    str(
                        (res or {}).get("orderId")
                        or prot.get("order_id")
                        or ""
                    ),
                    str(
                        (res or {}).get("clientOid")
                        or prot.get("client_oid")
                        or ""
                    ),
                    desired,
                    0,
                    "LIVE",
                    res,
                )
            except Exception as e:
                db.risk_event(
                    "LIVE_STOP_MODIFY_FAILED",
                    str(e),
                    order["strategy"],
                    "champion",
                    symbol,
                )
                db.execute(
                    "UPDATE live_protections SET status='STALE',updated_at=? WHERE id=?",
                    (int(time.time() * 1000), prot["id"]),
                )
                live_pos = next(
                    (
                        x
                        for x in await self.positions(force=True)
                        if str(x.get("symbol") or "").upper()
                        == symbol.upper()
                        and self._position_size(x) > 0
                    ),
                    None,
                )
                if live_pos:
                    await self.ensure_protections(order, pos, live_pos)

    async def _stop_sync_worker(self):
        while self._running:
            try:
                symbol = await asyncio.wait_for(
                    self._sync_queue.get(), timeout=0.5
                )
            except asyncio.TimeoutError:
                continue
            try:
                self._sync_pending.discard(symbol)
                await self._sync_symbol_now(symbol)
            except Exception as e:
                self.last_worker_error = (
                    f"stop sync: {type(e).__name__}: {e}"
                )
                db.risk_event(
                    "LIVE_TRAIL_SYNC_FAILED", str(e), symbol=symbol
                )
            finally:
                self.last_worker_tick = time.time()
                self._sync_queue.task_done()

    async def _guardian_worker(self):
        while self._running:
            started = time.monotonic()
            try:
                if self.credentials_ready():
                    await self.reconcile(force=True)
            except Exception as e:
                self.last_worker_error = (
                    f"guardian: {type(e).__name__}: {e}"
                )
                db.event(
                    "LIVE_GUARDIAN_ERROR", self.last_worker_error, "ERROR"
                )
            finally:
                self.last_worker_tick = time.time()
            elapsed = time.monotonic() - started
            await asyncio.sleep(
                max(0.5, settings.live_sync_interval_sec - elapsed)
            )

    async def reconcile(self, force=False):
        if not self.credentials_ready():
            return []
        if (
            not force
            and time.time() - self.last_sync < settings.live_sync_interval_sec
        ):
            return self.last_positions

        async with self._reconcile_lock:
            try:
                rows = await self.positions(force=True) or []
                self.last_positions = rows
                self.last_sync = time.time()
                self.last_sync_error = None
                active = {
                    str(p.get("symbol") or "").upper(): p
                    for p in rows
                    if self._position_size(p) > 0
                }

                verify_now = (
                    time.time() - self.last_protection_verify
                    >= settings.live_protection_verify_sec
                )
                for order in db.query(
                    "SELECT * FROM live_orders WHERE status IN('OPEN','ENTRY_UNCONFIRMED')"
                ):
                    live_pos = active.get(order["symbol"].upper())
                    if not live_pos:
                        detail = None
                        try:
                            detail = await self.order_detail(
                                symbol=order["symbol"],
                                order_id=order.get("exchange_order_id"),
                                client_oid=order.get("client_oid"),
                            )
                        except Exception:
                            pass
                        age = (
                            int(time.time() * 1000) - int(order["created_at"])
                        ) / 1000
                        state = str(
                            (detail or {}).get("state")
                            or (detail or {}).get("status")
                            or ""
                        ).lower()
                        if age < 15 or state in {"new", "live", "pending"}:
                            continue
                        db.execute(
                            "UPDATE live_orders SET status='CLOSED',updated_at=? WHERE id=?",
                            (int(time.time() * 1000), order["id"]),
                        )
                        db.execute(
                            "UPDATE live_protections SET status='CLOSED',updated_at=? "
                            "WHERE live_order_id=? AND status NOT IN('FILLED')",
                            (int(time.time() * 1000), order["id"]),
                        )
                        continue

                    if order["status"] == "ENTRY_UNCONFIRMED":
                        db.execute(
                            "UPDATE live_orders SET status='OPEN',updated_at=? WHERE id=?",
                            (int(time.time() * 1000), order["id"]),
                        )
                        order["status"] = "OPEN"

                    if verify_now:
                        pos = db.one(
                            "SELECT * FROM positions WHERE id=?",
                            (order["paper_position_id"],),
                        )
                        await self.ensure_protections(order, pos, live_pos)

                if verify_now:
                    self.last_protection_verify = time.time()
                return rows
            except Exception as e:
                self.last_sync_error = str(e)
                db.event("LIVE_RECONCILE_FAILED", str(e), "ERROR")
                return self.last_positions


live_adapter = BitgetLiveAdapter()
