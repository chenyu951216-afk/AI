from __future__ import annotations
import asyncio, time
from dataclasses import dataclass
from typing import Any
import httpx, pandas as pd
from .config import settings
from .db import db
from .indicators import enrich, safe
from .models import MarketRegime

@dataclass
class MarketSnapshot:
    symbol:str; price:float; bid:float; ask:float; quote_volume:float; candles_15m:pd.DataFrame; candles_1h:pd.DataFrame
    funding:float; oi:float; oi_change_30m:float; regime:str
    orderbook_imbalance:float|None=None; liquidation_above:float|None=None; liquidation_below:float|None=None
    liquidation_above_strength:float=0.0; liquidation_below_strength:float=0.0; cg_oi_change_30m:float|None=None

class BitgetPublic:
    def __init__(self): self.client=httpx.AsyncClient(base_url=settings.bitget_base_url,timeout=settings.market_timeout_sec)
    async def _get(self,path:str,params:dict[str,Any]):
        r=await self.client.get(path,params=params); r.raise_for_status(); j=r.json()
        if j.get("code")!="00000": raise RuntimeError(f"Bitget {path}: {j.get('code')} {j.get('msg')}")
        return j.get("data")
    async def tickers(self): return await self._get("/api/v2/mix/market/tickers",{"productType":settings.bitget_product_type})
    async def contracts(self): return await self._get("/api/v2/mix/market/contracts",{"productType":settings.bitget_product_type})
    async def candles(self,symbol:str,granularity:str,limit:int=240):
        data=await self._get("/api/v2/mix/market/candles",{"symbol":symbol,"productType":settings.bitget_product_type,"granularity":granularity,"limit":min(limit,1000)})
        rows=[[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5]),float(x[6]) if len(x)>6 else 0.] for x in (data or [])]
        df=pd.DataFrame(rows,columns=["ts","open","high","low","close","volume","quote_volume"])
        return enrich(df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)) if not df.empty else df
    async def oi(self,symbol:str)->float:
        d=await self._get("/api/v2/mix/market/open-interest",{"symbol":symbol,"productType":settings.bitget_product_type}); a=(d or {}).get("openInterestList") or []
        return float(a[0]["size"]) if a else 0.0
    async def funding(self,symbol:str)->float:
        d=await self._get("/api/v2/mix/market/current-fund-rate",{"symbol":symbol,"productType":settings.bitget_product_type}); return float(d[0]["fundingRate"]) if d else 0.0
    async def depth_imbalance(self,symbol:str,levels:int=20)->float:
        d=await self._get("/api/v2/mix/market/merge-depth",{"symbol":symbol,"productType":settings.bitget_product_type,"precision":"scale0","limit":levels}); bids=(d or {}).get("bids",[]); asks=(d or {}).get("asks",[])
        b=sum(float(x[1]) for x in bids[:levels]); a=sum(float(x[1]) for x in asks[:levels]); return (b-a)/(b+a) if b+a else 0.0

class CoinGlass:
    def __init__(self):
        self.client=httpx.AsyncClient(base_url=settings.coinglass_base_url,timeout=15,headers={"CG-API-KEY":settings.coinglass_api_key} if settings.coinglass_api_key else {})
        self.cache={}
    def ready(self): return bool(settings.coinglass_enabled and settings.coinglass_api_key)
    async def _get_cached(self,key,path,params):
        c=self.cache.get(key)
        if c and time.time()-c[0]<settings.coinglass_cache_sec:return c[1]
        r=await self.client.get(path,params=params); r.raise_for_status(); j=r.json()
        if str(j.get("code")) not in {"0","00000"}: raise RuntimeError(j.get("msg","CoinGlass error"))
        d=j.get("data"); self.cache[key]=(time.time(),d); return d
    async def liquidation_clusters(self,symbol:str,price:float)->dict[str,float]:
        if not self.ready():return {}
        try:
            d=await self._get_cached("liq:"+symbol,"/api/futures/liquidation/heatmap/model1",{"exchange":settings.coinglass_exchange,"symbol":symbol,"range":"24h"}) or {}
            ys=[float(x) for x in d.get("y_axis",[])]; weights={}
            for row in d.get("liquidation_leverage_data",[]):
                if len(row)>=3: weights[int(row[1])]=weights.get(int(row[1]),0)+float(row[2])
            above=[(ys[i],w) for i,w in weights.items() if 0<=i<len(ys) and ys[i]>price]; below=[(ys[i],w) for i,w in weights.items() if 0<=i<len(ys) and ys[i]<price]
            a=max(above,key=lambda z:z[1],default=(0.,0.)); b=max(below,key=lambda z:z[1],default=(0.,0.)); return {"above":a[0],"above_strength":a[1],"below":b[0],"below_strength":b[1]}
        except Exception:return {}
    async def oi_change_30m(self,symbol:str)->float|None:
        if not self.ready():return None
        coin=symbol.removesuffix("USDT")
        try:
            d=await self._get_cached("oi:"+coin,"/api/futures/open-interest/exchange-list",{"symbol":coin}) or []
            row=next((x for x in d if str(x.get("exchange")).lower()=="all"),None) or (d[0] if d else None)
            return float(row.get("open_interest_change_percent_30m")) if row else None
        except Exception:return None

class MarketData:
    def __init__(self):
        self.bitget=BitgetPublic(); self.cg=CoinGlass(); self.ticker_map={}; self.contract_map={}; self.rotation=0; self._regime=MarketRegime("NEUTRAL",0,0,0,0); self._regime_at=0
    async def refresh_universe(self)->list[str]:
        tickers,contracts=await asyncio.gather(self.bitget.tickers(),self.bitget.contracts()); self.ticker_map={x["symbol"]:x for x in tickers if x.get("symbol")}; self.contract_map={x["symbol"]:x for x in contracts if x.get("symbol")}
        now=int(time.time()*1000); out=[]
        for t in tickers:
            try:
                sym=t["symbol"]; c=self.contract_map.get(sym,{})
                vol=float(t.get("usdtVolume") or t.get("quoteVolume") or 0); last=float(t.get("lastPr") or 0); launch=int(c.get("launchTime") or 0); age=(now-launch)/86400000 if launch>0 else 999
                if sym.endswith("USDT") and last>0 and vol>=settings.universe_min_usdt_volume and c.get("symbolStatus") in {"normal","listed",None} and age>=settings.min_symbol_age_days: out.append((sym,vol))
            except Exception: pass
        out.sort(key=lambda x:x[1],reverse=True); return [s for s,_ in out[:settings.universe_max_symbols]]
    def batch_for_cycle(self,u:list[str])->list[str]:
        if not u:return []
        n=min(settings.scan_symbols_per_cycle,len(u)); start=self.rotation%len(u); self.rotation=(start+n)%len(u); return [u[(start+i)%len(u)] for i in range(n)]
    def ticker_price(self,symbol:str)->float:
        try:return float(self.ticker_map.get(symbol,{}).get("lastPr") or 0)
        except Exception:return 0.0
    def prices(self): return {s:self.ticker_price(s) for s in self.ticker_map}
    async def regime(self)->MarketRegime:
        if time.time()-self._regime_at<settings.regime_refresh_sec:return self._regime
        try:
            h=await self.bitget.candles("BTCUSDT","1H",240); x=h.iloc[-1]; atrp=safe(x.atr_pct); med=safe(h.atr_pct.iloc[-80:-1].median()); ad=safe(x.adx14); price=safe(x.close)
            if atrp>max(.01,med*1.7): name="HIGH_VOL"; score=min(1,atrp/max(med,1e-6)/2)
            elif ad>=22 and x.ema20>x.ema50>x.ema200: name="BULL_TREND"; score=min(1,ad/40)
            elif ad>=22 and x.ema20<x.ema50<x.ema200: name="BEAR_TREND"; score=min(1,ad/40)
            elif ad<18: name="RANGE"; score=min(1,(18-ad)/18+.4)
            else: name="NEUTRAL"; score=.5
            self._regime=MarketRegime(name,score,price,ad,atrp); self._regime_at=time.time(); db.execute("INSERT OR REPLACE INTO regime_history(ts,regime,score,btc_price,btc_adx,btc_atr_pct) VALUES(?,?,?,?,?,?)",(self._regime.created_at,name,score,price,ad,atrp))
        except Exception: pass
        return self._regime
    async def snapshot(self,symbol:str,need_depth=False,need_liq=False,need_cg_oi=False)->MarketSnapshot|None:
        t=self.ticker_map.get(symbol,{})
        try: price=float(t.get("lastPr") or 0); bid=float(t.get("bidPr") or price); ask=float(t.get("askPr") or price); vol=float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
        except Exception:return None
        if price<=0:return None
        try:
            c15,c1h,oi,fund,reg=await asyncio.gather(self.bitget.candles(symbol,"15m",240),self.bitget.candles(symbol,"1H",240),self.bitget.oi(symbol),self.bitget.funding(symbol),self.regime())
            if len(c15)<80 or len(c1h)<80:return None
        except Exception:return None
        now=int(time.time()*1000); bucket=now-now%(5*60*1000); db.execute("INSERT OR REPLACE INTO oi_snapshots(symbol,ts,oi) VALUES(?,?,?)",(symbol,bucket,oi)); old=db.one("SELECT oi FROM oi_snapshots WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",(symbol,now-25*60*1000)); chg=(oi-float(old["oi"]))/float(old["oi"]) if old and float(old["oi"]) else 0.0
        depth=await self.bitget.depth_imbalance(symbol) if need_depth else None; liq=await self.cg.liquidation_clusters(symbol,price) if need_liq else {}; cg_oi=await self.cg.oi_change_30m(symbol) if need_cg_oi else None
        return MarketSnapshot(symbol,price,bid,ask,vol,c15,c1h,fund,oi,chg,reg.name,depth,liq.get("above"),liq.get("below"),liq.get("above_strength",0),liq.get("below_strength",0),cg_oi)
market=MarketData()
