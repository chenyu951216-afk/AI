from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pandas as pd

from .config import settings
from .db import db
from .indicators import enrich


@dataclass
class MarketSnapshot:
    symbol: str
    price: float
    bid: float
    ask: float
    quote_volume: float
    candles_15m: pd.DataFrame
    candles_1h: pd.DataFrame
    funding: float
    oi: float
    oi_change_30m: float
    orderbook_imbalance: float | None = None
    liquidation_above: float | None = None
    liquidation_below: float | None = None
    liquidation_above_strength: float = 0.0
    liquidation_below_strength: float = 0.0


class BitgetPublic:
    def __init__(self):
        self.client = httpx.AsyncClient(base_url=settings.bitget_base_url, timeout=12)

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        r = await self.client.get(path, params=params)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != "00000":
            raise RuntimeError(f"Bitget {path}: {j.get('code')} {j.get('msg')}")
        return j.get("data")

    async def tickers(self) -> list[dict[str, Any]]:
        return await self._get("/api/v2/mix/market/tickers", {"productType": settings.bitget_product_type})

    async def candles(self, symbol: str, granularity: str, limit: int = 220) -> pd.DataFrame:
        data = await self._get("/api/v2/mix/market/candles", {
            "symbol": symbol, "productType": settings.bitget_product_type,
            "granularity": granularity, "limit": min(limit, 1000)
        })
        rows = []
        for x in data or []:
            # [ts, open, high, low, close, baseVol, quoteVol]
            rows.append([int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), float(x[6]) if len(x) > 6 else 0.0])
        df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume","quote_volume"])
        if df.empty:
            return df
        df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
        return enrich(df)

    async def oi(self, symbol: str) -> float:
        data = await self._get("/api/v2/mix/market/open-interest", {"symbol": symbol, "productType": settings.bitget_product_type})
        arr = (data or {}).get("openInterestList") or []
        return float(arr[0]["size"]) if arr else 0.0

    async def funding(self, symbol: str) -> float:
        data = await self._get("/api/v2/mix/market/current-fund-rate", {"symbol": symbol, "productType": settings.bitget_product_type})
        return float(data[0]["fundingRate"]) if data else 0.0

    async def depth_imbalance(self, symbol: str, levels: int = 20) -> float:
        data = await self._get("/api/v2/mix/market/merge-depth", {"symbol": symbol, "productType": settings.bitget_product_type, "precision":"scale0", "limit": levels})
        bids = data.get("bids", []) if isinstance(data, dict) else []
        asks = data.get("asks", []) if isinstance(data, dict) else []
        b = sum(float(x[1]) for x in bids[:levels]); a = sum(float(x[1]) for x in asks[:levels])
        return (b-a)/(b+a) if (b+a) else 0.0


class CoinGlass:
    def __init__(self):
        self.client = httpx.AsyncClient(base_url=settings.coinglass_base_url, timeout=15, headers={"CG-API-KEY": settings.coinglass_api_key} if settings.coinglass_api_key else {})
        self.cache: dict[str, tuple[float, dict[str,float]]] = {}

    async def liquidation_clusters(self, symbol: str, price: float) -> dict[str, float]:
        if not (settings.coinglass_enabled and settings.coinglass_api_key):
            return {}
        cached = self.cache.get(symbol)
        if cached and time.time()-cached[0] < 300:
            return cached[1]
        try:
            r = await self.client.get("/api/futures/liquidation/heatmap/model1", params={"exchange":settings.coinglass_exchange,"symbol":symbol,"range":"24h"})
            r.raise_for_status(); j = r.json(); data = j.get("data") or {}
            ys = [float(x) for x in data.get("y_axis", [])]
            weights: dict[int,float] = {}
            for item in data.get("liquidation_leverage_data", []):
                if len(item) >= 3:
                    weights[int(item[1])] = weights.get(int(item[1]), 0.0) + float(item[2])
            above = [(ys[i], w) for i,w in weights.items() if 0 <= i < len(ys) and ys[i] > price]
            below = [(ys[i], w) for i,w in weights.items() if 0 <= i < len(ys) and ys[i] < price]
            a = max(above, key=lambda x:x[1], default=(0.0,0.0)); b = max(below, key=lambda x:x[1], default=(0.0,0.0))
            out = {"above":a[0],"above_strength":a[1],"below":b[0],"below_strength":b[1]}
            self.cache[symbol] = (time.time(), out)
            return out
        except Exception:
            return {}


class MarketData:
    def __init__(self):
        self.bitget = BitgetPublic(); self.cg = CoinGlass()
        self.ticker_map: dict[str,dict[str,Any]] = {}
        self.rotation = 0

    async def refresh_tickers(self) -> list[str]:
        tickers = await self.bitget.tickers()
        self.ticker_map = {x["symbol"]:x for x in tickers if x.get("symbol")}
        eligible = []
        for t in tickers:
            try:
                vol = float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
                last = float(t.get("lastPr") or 0)
                if last > 0 and vol >= settings.universe_min_usdt_volume and str(t.get("symbol","")).endswith("USDT"):
                    eligible.append((t["symbol"], vol))
            except Exception:
                pass
        eligible.sort(key=lambda x:x[1], reverse=True)
        return [x[0] for x in eligible]

    def batch_for_cycle(self, universe: list[str]) -> list[str]:
        n = min(settings.scan_symbols_per_cycle, len(universe))
        if not n: return []
        start = self.rotation % len(universe); self.rotation = (start+n) % len(universe)
        return [universe[(start+i)%len(universe)] for i in range(n)]

    def ticker_price(self, symbol: str) -> float:
        t = self.ticker_map.get(symbol, {})
        try: return float(t.get("lastPr") or 0)
        except Exception: return 0.0

    async def snapshot(self, symbol: str, with_depth: bool = False, with_liq: bool = False) -> MarketSnapshot | None:
        t = self.ticker_map.get(symbol, {})
        try:
            price=float(t.get("lastPr") or 0); bid=float(t.get("bidPr") or price); ask=float(t.get("askPr") or price); vol=float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
        except Exception:
            return None
        if price <= 0: return None
        try:
            c15,c1h,oi,funding = await asyncio.gather(self.bitget.candles(symbol,"15m",240), self.bitget.candles(symbol,"1H",240), self.bitget.oi(symbol), self.bitget.funding(symbol))
            if len(c15)<80 or len(c1h)<80: return None
        except Exception:
            return None
        now=int(time.time()*1000); bucket=now-(now%(5*60*1000))
        db.execute("INSERT OR REPLACE INTO oi_snapshots(symbol,ts,oi) VALUES(?,?,?)",(symbol,bucket,oi))
        old=db.one("SELECT oi FROM oi_snapshots WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",(symbol,now-25*60*1000))
        oi_chg=((oi-float(old["oi"]))/float(old["oi"])) if old and float(old["oi"]) else 0.0
        imb = await self.bitget.depth_imbalance(symbol) if with_depth else None
        liq = await self.cg.liquidation_clusters(symbol,price) if with_liq else {}
        return MarketSnapshot(symbol,price,bid,ask,vol,c15,c1h,funding,oi,oi_chg,imb,liq.get("above"),liq.get("below"),liq.get("above_strength",0),liq.get("below_strength",0))

market = MarketData()
