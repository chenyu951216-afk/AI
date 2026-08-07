from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

import httpx

from .config import settings
from .db import db


class BitgetLiveAdapter:
    def __init__(self):
        self.client = httpx.AsyncClient(base_url=settings.bitget_base_url, timeout=15)
        self.contract_cache: dict[str, dict] = {}
        self.contract_cache_at = 0.0
        self.last_positions: list[dict] = []
        self.last_sync = 0.0
        self.last_sync_error: str | None = None

    def credentials_ready(self) -> bool:
        return bool(settings.bitget_api_key and settings.bitget_api_secret and settings.bitget_passphrase)

    def web_master(self) -> bool:
        return db.runtime_get("live_master_enabled", "false").lower() == "true"

    def gate_status(self) -> dict:
        eligible = (db.one(
            "SELECT COUNT(*) n FROM strategy_state WHERE live_eligible=1 AND stage='FINAL' AND enabled=1"
        ) or {"n": 0})["n"]
        ready = bool(
            settings.live_trading_allowed
            and self.web_master()
            and self.credentials_ready()
            and int(eligible) > 0
        )
        return {
            "deployment_allowed": settings.live_trading_allowed,
            "web_master_enabled": self.web_master(),
            "credentials_ready": self.credentials_ready(),
            "eligible_strategies": int(eligible),
            "ready": ready,
        }

    def set_web_master(self, enabled: bool) -> None:
        db.runtime_set("live_master_enabled", "true" if enabled else "false")

    def _headers(self, method: str, path: str, body: str = "", query: str = "") -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        msg = ts + method.upper() + path + ("?" + query if query else "") + body
        sig = base64.b64encode(
            hmac.new(settings.bitget_api_secret.encode(), msg.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "ACCESS-KEY": settings.bitget_api_key,
            "ACCESS-SIGN": sig,
            "ACCESS-PASSPHRASE": settings.bitget_passphrase,
            "ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

    async def _private(self, method: str, path: str, params=None, payload=None):
        params = params or {}
        payload = payload or {}
        query = urlencode(params)
        body = json.dumps(payload, separators=(",", ":")) if payload else ""
        headers = self._headers(method, path, body, query)
        if method == "GET":
            response = await self.client.get(path, params=params, headers=headers)
        else:
            response = await self.client.post(path, content=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != "00000":
            raise RuntimeError(f"Bitget {path}: {data.get('code')} {data.get('msg')}")
        return data.get("data")

    async def _public(self, path: str, params: dict):
        response = await self.client.get(path, params=params)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != "00000":
            raise RuntimeError(data)
        return data.get("data")

    async def contracts(self) -> dict[str, dict]:
        if time.time() - self.contract_cache_at < 600 and self.contract_cache:
            return self.contract_cache
        rows = await self._public(
            "/api/v2/mix/market/contracts", {"productType": settings.bitget_product_type}
        )
        self.contract_cache = {row["symbol"]: row for row in rows or []}
        self.contract_cache_at = time.time()
        return self.contract_cache

    async def ticker(self, symbol: str) -> dict:
        rows = await self._public(
            "/api/v2/mix/market/ticker",
            {"symbol": symbol, "productType": settings.bitget_product_type},
        )
        return (rows or [{}])[0] if isinstance(rows, list) else (rows or {})

    async def account(self, symbol: str):
        return await self._private(
            "GET",
            "/api/v2/mix/account/account",
            {
                "symbol": symbol,
                "productType": settings.bitget_product_type,
                "marginCoin": settings.bitget_margin_coin,
            },
        )

    async def positions(self):
        return await self._private(
            "GET",
            "/api/v2/mix/position/all-position",
            {"productType": settings.bitget_product_type, "marginCoin": settings.bitget_margin_coin},
        )

    async def reconcile(self, force: bool = False):
        if not self.credentials_ready():
            return []
        if not force and time.time() - self.last_sync < settings.live_sync_interval_sec:
            return self.last_positions
        try:
            rows = await self.positions() or []
            self.last_positions = rows
            self.last_sync = time.time()
            self.last_sync_error = None
            active = set()
            for row in rows:
                try:
                    size = abs(float(row.get("total") or row.get("available") or row.get("holdVolume") or 0))
                    if size > 0:
                        active.add(str(row.get("symbol") or "").upper())
                except Exception:
                    pass
            for order in db.query("SELECT id,symbol FROM live_orders WHERE status='OPEN'"):
                if str(order["symbol"]).upper() not in active:
                    db.execute(
                        "UPDATE live_orders SET status='CLOSED',updated_at=? WHERE id=?",
                        (int(time.time() * 1000), order["id"]),
                    )
            return rows
        except Exception as exc:
            self.last_sync_error = str(exc)
            db.event("LIVE_RECONCILE_FAILED", str(exc), "ERROR")
            return self.last_positions

    async def set_leverage(self, symbol: str, leverage: float, position_side: str):
        payload = {
            "symbol": symbol,
            "productType": settings.bitget_product_type,
            "marginCoin": settings.bitget_margin_coin,
            "leverage": str(leverage),
        }
        if settings.bitget_position_mode == "hedge_mode" and settings.bitget_margin_mode == "isolated":
            payload["holdSide"] = "long" if position_side == "long" else "short"
        return await self._private("POST", "/api/v2/mix/account/set-leverage", payload=payload)

    @staticmethod
    def _floor_step(value: float, step: str, places: int) -> Decimal:
        amount = Decimal(str(value))
        increment = Decimal(str(step or "1"))
        quantized = (amount / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        return quantized.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)

    async def normalize(self, symbol: str, qty: float, price: float, leverage: float):
        contract = (await self.contracts()).get(symbol)
        if not contract:
            raise RuntimeError("contract config missing")
        if contract.get("symbolStatus") not in {"normal", "listed"}:
            raise RuntimeError(f"symbol not tradable: {contract.get('symbolStatus')}")

        lev = min(
            float(leverage),
            float(contract.get("maxLever") or settings.hard_max_leverage),
            settings.hard_max_leverage,
        )
        places = int(contract.get("volumePlace") or 6)
        step = contract.get("sizeMultiplier") or str(10 ** -places)
        normalized = self._floor_step(qty, step, places)
        min_qty = Decimal(str(contract.get("minTradeNum") or 0))
        min_usdt = Decimal(str(contract.get("minTradeUSDT") or 0))

        if normalized < min_qty:
            normalized = self._floor_step(float(min_qty), step, places)
        if normalized * Decimal(str(price)) < min_usdt:
            normalized = self._floor_step(
                float(min_usdt / Decimal(str(price)) * Decimal("1.01")), step, places
            )
        max_qty = Decimal(str(contract.get("maxMarketOrderQty") or 1e30))
        normalized = min(normalized, max_qty)
        return str(normalized), lev, contract

    def _strategy_open_notional(self, strategy: str) -> float:
        row = db.one(
            "SELECT COALESCE(SUM(notional),0) n FROM live_orders WHERE strategy=? AND status='OPEN'",
            (strategy,),
        )
        return float(row["n"] if row else 0)

    async def place_from_paper(self, position: dict, params: dict):
        state = db.state(position["strategy"])
        gates = self.gate_status()
        if not state or state["stage"] != "FINAL" or not state["live_eligible"]:
            raise PermissionError("strategy not FINAL/live eligible")
        if not gates["ready"]:
            raise PermissionError("live gate not ready")
        if db.one(
            "SELECT id FROM live_orders WHERE paper_position_id=? AND status NOT IN ('FAILED','CANCELLED')",
            (position["id"],),
        ):
            return {"skipped": "already submitted"}

        live_positions = await self.reconcile(force=True)
        for live_position in live_positions:
            try:
                same_symbol = str(live_position.get("symbol") or "").upper() == str(position["symbol"]).upper()
                live_size = abs(
                    float(
                        live_position.get("total")
                        or live_position.get("available")
                        or live_position.get("holdVolume")
                        or 0
                    )
                )
                if same_symbol and live_size > 0:
                    raise PermissionError(
                        "existing Bitget position on this symbol; automation will not stack on manual/external position"
                    )
            except ValueError:
                pass

        paper_price = float(position["entry"])
        ticker = await self.ticker(position["symbol"])
        market_price = float((ticker or {}).get("lastPr") or paper_price)
        drift_bps = abs(market_price - paper_price) / max(paper_price, 1e-12) * 10000
        if drift_bps > settings.live_max_entry_drift_bps:
            raise PermissionError(
                f"live entry drift {drift_bps:.1f}bps exceeds {settings.live_max_entry_drift_bps:.1f}bps"
            )
        price = market_price
        desired_notional = float(position["qty"]) * price
        already_open = self._strategy_open_notional(position["strategy"])
        strategy_remaining = max(0.0, settings.live_max_strategy_notional_usdt - already_open)
        desired_notional = min(
            desired_notional,
            settings.live_max_order_notional_usdt,
            strategy_remaining,
        )
        if desired_notional <= 0:
            raise PermissionError("strategy live notional cap reached")

        qty, leverage, contract = await self.normalize(
            position["symbol"], desired_notional / max(price, 1e-12), price, float(position["leverage"])
        )

        # Cap real-money use by currently available margin as a final exchange-account safety layer.
        account = await self.account(position["symbol"])
        available = float((account or {}).get("available") or 0)
        if available <= 0:
            raise PermissionError("Bitget account has no positive available margin")
        account_notional_cap = available * leverage * max(0.05, min(settings.live_available_balance_buffer, 1.0))
        desired_notional = min(desired_notional, account_notional_cap)
        qty, leverage, contract = await self.normalize(
            position["symbol"], desired_notional / max(price, 1e-12), price, leverage
        )
        if float(qty) <= 0:
            raise RuntimeError("normalized live qty is zero")

        actual_notional = float(qty) * price
        if actual_notional > strategy_remaining + 1e-9:
            raise PermissionError("normalized quantity would breach strategy live notional cap")
        if actual_notional > settings.live_max_order_notional_usdt + 1e-9:
            raise PermissionError("normalized quantity would breach live per-order cap")
        if actual_notional > account_notional_cap + 1e-9:
            raise PermissionError("normalized quantity would breach available-margin cap")

        await self.set_leverage(position["symbol"], leverage, position["side"])

        client_oid = "ai-" + uuid.uuid4().hex[:24]
        side = "buy" if position["side"] == "long" else "sell"
        payload = {
            "symbol": position["symbol"],
            "productType": settings.bitget_product_type,
            "marginMode": settings.bitget_margin_mode,
            "marginCoin": settings.bitget_margin_coin,
            "size": qty,
            "side": side,
            "orderType": "market",
            "clientOid": client_oid,
            "presetStopLossPrice": str(position["stop"]),
            "presetStopLossExecutePrice": "0",
        }
        if settings.bitget_position_mode == "hedge_mode":
            payload["tradeSide"] = "open"

        now = int(time.time() * 1000)
        db.execute(
            """INSERT INTO live_orders(
                paper_position_id,strategy,symbol,side,qty,entry_price,notional,leverage,client_oid,status,
                detail_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                position["id"],
                position["strategy"],
                position["symbol"],
                position["side"],
                float(qty),
                price,
                actual_notional,
                leverage,
                client_oid,
                "SUBMITTING",
                json.dumps(payload),
                now,
                now,
            ),
        )

        try:
            entry_result = await self._private("POST", "/api/v2/mix/order/place-order", payload=payload)
            exchange_order_id = entry_result.get("orderId") if isinstance(entry_result, dict) else None
            protections = []
            if settings.live_partial_tp_enabled:
                total = Decimal(qty)
                places = int(contract.get("volumePlace") or 6)
                step = contract.get("sizeMultiplier") or str(10 ** -places)
                q1 = self._floor_step(float(total) * float(position["tp1_fraction"]), step, places)
                q2 = self._floor_step(float(total) * float(position["tp2_fraction"]), step, places)
                q3 = total - q1 - q2
                if settings.bitget_position_mode == "hedge_mode":
                    hold_side = "long" if position["side"] == "long" else "short"
                else:
                    hold_side = "buy" if position["side"] == "long" else "sell"

                for label, trigger, part_qty in (
                    ("tp1", position["tp1"], q1),
                    ("tp2", position["tp2"], q2),
                    ("tp3", position["tp3"], q3),
                ):
                    if part_qty <= 0:
                        continue
                    tp_payload = {
                        "marginCoin": settings.bitget_margin_coin,
                        "productType": settings.bitget_product_type,
                        "symbol": position["symbol"],
                        "planType": "profit_plan",
                        "triggerPrice": str(trigger),
                        "triggerType": "mark_price",
                        "executePrice": "0",
                        "holdSide": hold_side,
                        "size": str(part_qty),
                        "clientOid": f"{client_oid}-{label}",
                    }
                    protections.append(
                        await self._private(
                            "POST", "/api/v2/mix/order/place-tpsl-order", payload=tp_payload
                        )
                    )

            detail = {
                "entry": entry_result,
                "protections": protections,
                "normalized_qty": qty,
                "notional": actual_notional,
                "leverage": leverage,
                "available_margin_before": available,
                "paper_entry": paper_price,
                "market_entry_reference": market_price,
                "entry_drift_bps": drift_bps,
            }
            db.execute(
                "UPDATE live_orders SET exchange_order_id=?,status='OPEN',detail_json=?,updated_at=? WHERE client_oid=?",
                (exchange_order_id, json.dumps(detail), int(time.time() * 1000), client_oid),
            )
            return detail
        except Exception as exc:
            db.execute(
                "UPDATE live_orders SET status='FAILED',detail_json=?,updated_at=? WHERE client_oid=?",
                (
                    json.dumps({"error": str(exc), "payload": payload}),
                    int(time.time() * 1000),
                    client_oid,
                ),
            )
            db.event(
                "LIVE_ORDER_FAILED",
                f"{position['strategy']} {position['symbol']}: {exc}",
                "ERROR",
            )
            raise


live_adapter = BitgetLiveAdapter()
