from __future__ import annotations
import asyncio,time
from dataclasses import dataclass
from typing import Any
import httpx,pandas as pd
from .config import settings
from .db import db
from .indicators import enrich,safe

TF_MS={"5m":5*60_000,"15m":15*60_000,"30m":30*60_000,"1H":60*60_000,"4H":4*60*60_000}
REQUIRED_TFS=("5m","15m","1H","4H")

@dataclass
class MarketSnapshot:
    symbol:str;price:float;bid:float;ask:float;quote_volume:float;frames:dict[str,pd.DataFrame]
    funding:float;oi:float;oi_change_30m:float;regime:str;macro_regime:str
    orderbook_imbalance:float|None=None;liquidation_above:float|None=None;liquidation_below:float|None=None
    liquidation_above_strength:float=0.;liquidation_below_strength:float=0.;cg_oi_change_30m:float|None=None
    change24h:float=0.;gainer_rank:int=0;loser_rank:int=0;high24h:float=0.;low24h:float=0.
    def df(self,tf:str)->pd.DataFrame:return self.frames[tf]
    def last(self,tf:str):return self.frames[tf].iloc[-1]
    def closed_ts(self,tf:str)->int:return int(self.frames[tf].iloc[-1].ts)

class BitgetPublic:
    def __init__(self):self.client=httpx.AsyncClient(base_url=settings.bitget_base_url,timeout=settings.market_timeout_sec)
    async def _get(self,path:str,params:dict[str,Any]):
        r=await self.client.get(path,params=params);r.raise_for_status();j=r.json()
        if j.get("code")!="00000":raise RuntimeError(f"Bitget {path}: {j.get('code')} {j.get('msg')}")
        return j.get("data")
    async def tickers(self):return await self._get("/api/v2/mix/market/tickers",{"productType":settings.bitget_product_type})
    async def contracts(self):return await self._get("/api/v2/mix/market/contracts",{"productType":settings.bitget_product_type})
    async def candles(self,symbol:str,tf:str,limit:int=280):
        data=await self._get("/api/v2/mix/market/candles",{"symbol":symbol,"productType":settings.bitget_product_type,"granularity":tf,"limit":min(limit,1000)})
        rows=[[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5]),float(x[6]) if len(x)>6 else 0.] for x in (data or [])]
        if not rows:return pd.DataFrame()
        df=pd.DataFrame(rows,columns=["ts","open","high","low","close","volume","quote_volume"]).sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
        now=int(time.time()*1000)-settings.closed_candle_safety_ms;dur=TF_MS[tf];df=df[(df.ts+dur)<=now].copy().reset_index(drop=True)
        return enrich(df) if not df.empty else df
    async def oi(self,symbol:str)->float:
        d=await self._get("/api/v2/mix/market/open-interest",{"symbol":symbol,"productType":settings.bitget_product_type});a=(d or {}).get("openInterestList") or [];return float(a[0]["size"]) if a else 0.
    async def funding(self,symbol:str)->float:
        d=await self._get("/api/v2/mix/market/current-fund-rate",{"symbol":symbol,"productType":settings.bitget_product_type});return float(d[0]["fundingRate"]) if d else 0.
    async def depth_imbalance(self,symbol:str,levels:int=20)->float:
        d=await self._get("/api/v2/mix/market/merge-depth",{"symbol":symbol,"productType":settings.bitget_product_type,"precision":"scale0","limit":levels});bids=(d or {}).get("bids",[]);asks=(d or {}).get("asks",[]);b=sum(float(x[1]) for x in bids[:levels]);a=sum(float(x[1]) for x in asks[:levels]);return (b-a)/(b+a) if b+a else 0.

class CoinGlass:
    def __init__(self):
        self.client=httpx.AsyncClient(base_url=settings.coinglass_base_url,timeout=15,headers={"CG-API-KEY":settings.coinglass_api_key} if settings.coinglass_api_key else {});self.cache={};self.last_ok=0.;self.last_error=None;self.last_endpoint=None;self.success_count=0;self.error_count=0;self.liq_last_ok=0.;self.liq_last_error=None;self.liq_source=None
    def ready(self):return bool(settings.coinglass_enabled and settings.coinglass_api_key)
    def health(self):
        if not settings.coinglass_enabled:return {"status":"DISABLED","ok":False,"last_error":None,"last_ok":None,"liquidation_status":"DISABLED"}
        if not settings.coinglass_api_key:return {"status":"NO_KEY","ok":False,"last_error":None,"last_ok":None,"liquidation_status":"NO_KEY"}
        if self.last_error and (not self.last_ok or time.time()-self.last_ok>settings.coinglass_cache_sec*2):status="API_ERROR"
        elif self.last_ok:status="OK"
        else:status="WAITING_FIRST_SUCCESS"
        liq_status="OK" if self.liq_last_ok and not self.liq_last_error else "ERROR" if self.liq_last_error else "WAITING"
        return {"status":status,"ok":status=="OK","last_error":self.last_error,"last_ok":int(self.last_ok*1000) if self.last_ok else None,"endpoint":self.last_endpoint,"success_count":self.success_count,"error_count":self.error_count,"liquidation_status":liq_status,"liquidation_error":self.liq_last_error,"liquidation_last_ok":int(self.liq_last_ok*1000) if self.liq_last_ok else None,"liquidation_source":self.liq_source}
    async def _get_cached(self,key,path,params):
        c=self.cache.get(key)
        if c and time.time()-c[0]<settings.coinglass_cache_sec:return c[1]
        try:
            r=await self.client.get(path,params=params);r.raise_for_status();j=r.json()
            if str(j.get("code")) not in {"0","00000"}:raise RuntimeError(f"{j.get('code')} {j.get('msg','CoinGlass error')}")
            d=j.get("data");self.cache[key]=(time.time(),d);self.last_ok=time.time();self.last_error=None;self.last_endpoint=path;self.success_count+=1;return d
        except Exception as e:self.last_error=str(e);self.last_endpoint=path;self.error_count+=1;raise
    @staticmethod
    def _clusters_from_heatmap(d,price):
        ys=[float(x) for x in (d or {}).get("y_axis",[])];weights={}
        for row in (d or {}).get("liquidation_leverage_data",[]):
            if len(row)>=3:
                try:weights[int(row[1])]=weights.get(int(row[1]),0)+float(row[2])
                except Exception:pass
        above=[(ys[i],v) for i,v in weights.items() if 0<=i<len(ys) and ys[i]>price];below=[(ys[i],v) for i,v in weights.items() if 0<=i<len(ys) and ys[i]<price];a=max(above,key=lambda z:z[1],default=(0.,0.));b=max(below,key=lambda z:z[1],default=(0.,0.));return {"above":a[0],"above_strength":a[1],"below":b[0],"below_strength":b[1],"source":"heatmap_model1"}
    @staticmethod
    def _clusters_from_map(d,price):
        raw=(d or {}).get("data",d or {});levels=[]
        if isinstance(raw,dict):
            for k,v in raw.items():
                try:
                    px=float(k);strength=0.
                    for row in v if isinstance(v,list) else []:
                        if isinstance(row,list) and len(row)>=2:strength+=abs(float(row[1]))
                    if strength>0:levels.append((px,strength))
                except Exception:pass
        above=[x for x in levels if x[0]>price];below=[x for x in levels if x[0]<price];a=max(above,key=lambda z:z[1],default=(0.,0.));b=max(below,key=lambda z:z[1],default=(0.,0.));return {"above":a[0],"above_strength":a[1],"below":b[0],"below_strength":b[1],"source":"liquidation_map"}
    async def liquidation_clusters(self,symbol,price):
        if not self.ready():return {}
        try:
            d=await self._get_cached("liq1:"+symbol,"/api/futures/liquidation/heatmap/model1",{"exchange":settings.coinglass_exchange,"symbol":symbol,"range":"24h"}) or {};z=self._clusters_from_heatmap(d,price)
            if z.get("above") or z.get("below"):self.liq_last_ok=time.time();self.liq_last_error=None;self.liq_source=z.get("source");return z
            raise RuntimeError("CoinGlass heatmap returned no usable liquidation levels")
        except Exception as first:
            try:
                d=await self._get_cached("liqmap:"+symbol,"/api/futures/liquidation/map",{"exchange":settings.coinglass_exchange,"symbol":symbol,"range":"1d"}) or {};z=self._clusters_from_map(d,price)
                if z.get("above") or z.get("below"):self.liq_last_ok=time.time();self.liq_last_error=None;self.liq_source=z.get("source");return z
                self.liq_last_error=f"heatmap: {first}; map: no usable levels";return {}
            except Exception as second:self.liq_last_error=f"heatmap: {first}; map: {second}";return {}
    async def oi_change_30m(self,symbol):
        if not self.ready():return None
        coin=symbol.removesuffix("USDT")
        try:
            d=await self._get_cached("oi:"+coin,"/api/futures/open-interest/exchange-list",{"symbol":coin}) or [];row=next((x for x in d if str(x.get("exchange")).lower()=="all"),None) or (d[0] if d else None);return float(row.get("open_interest_change_percent_30m")) if row else None
        except Exception:return None

class MarketData:
    def __init__(self):self.bitget=BitgetPublic();self.cg=CoinGlass();self.ticker_map={};self.contract_map={};self.rotation=0;self._macro="NEUTRAL";self._macro_at=0.;self.rank_map={}
    async def refresh_universe(self):
        tickers,contracts=await asyncio.gather(self.bitget.tickers(),self.bitget.contracts());self.ticker_map={x["symbol"]:x for x in tickers if x.get("symbol")};self.contract_map={x["symbol"]:x for x in contracts if x.get("symbol")};now=int(time.time()*1000);out=[]
        for t in tickers:
            try:
                s=t["symbol"];c=self.contract_map.get(s,{});vol=float(t.get("usdtVolume") or t.get("quoteVolume") or 0);last=float(t.get("lastPr") or 0);launch=int(c.get("launchTime") or 0);age=(now-launch)/86400000 if launch else 999
                if s.endswith("USDT") and last>0 and vol>=settings.universe_min_usdt_volume and c.get("symbolStatus") in {"normal","listed",None} and age>=settings.min_symbol_age_days:out.append((s,vol,float(t.get("change24h") or 0)))
            except Exception:pass
        out.sort(key=lambda x:x[1],reverse=True);selected=out[:settings.universe_max_symbols];symbols=[s for s,_,_ in selected];gain=sorted(selected,key=lambda x:x[2],reverse=True);lose=sorted(selected,key=lambda x:x[2]);self.rank_map={}
        for i,(s,_,chg) in enumerate(gain,1):self.rank_map.setdefault(s,{})["gainer_rank"]=i;self.rank_map[s]["change24h"]=chg
        for i,(s,_,chg) in enumerate(lose,1):self.rank_map.setdefault(s,{})["loser_rank"]=i;self.rank_map[s]["change24h"]=chg
        return symbols
    def batch_for_cycle(self,u):
        if not u:return []
        n=min(settings.scan_symbols_per_cycle,len(u));start=self.rotation%len(u);self.rotation=(start+n)%len(u);return [u[(start+i)%len(u)] for i in range(n)]
    def ticker_price(self,symbol):
        try:return float(self.ticker_map.get(symbol,{}).get("lastPr") or 0)
        except Exception:return 0.
    def prices(self):return {s:self.ticker_price(s) for s in self.ticker_map}
    def local_regime(self,frames):
        h=frames["1H"];h4=frames["4H"];x=h.iloc[-1];z=h4.iloc[-1];ad=safe(x.adx14);atrp=safe(x.atr_pct);med=safe(h.atr_pct.iloc[-80:-1].median(),atrp)
        if atrp>max(.012,med*1.8):return "HIGH_VOL"
        if ad>=20 and x.ema20>x.ema50 and z.ema20>=z.ema50:return "BULL_TREND"
        if ad>=20 and x.ema20<x.ema50 and z.ema20<=z.ema50:return "BEAR_TREND"
        if ad<18:return "RANGE"
        return "NEUTRAL"
    async def macro_regime(self):
        if time.time()-self._macro_at<settings.btc_macro_refresh_sec:return self._macro
        try:
            h=await self.bitget.candles("BTCUSDT","1H",240);x=h.iloc[-1];ad=safe(x.adx14)
            if safe(x.atr_pct)>max(.015,safe(h.atr_pct.iloc[-80:-1].median())*1.9):self._macro="HIGH_VOL"
            elif ad>=24 and x.ema20>x.ema50:self._macro="BULL"
            elif ad>=24 and x.ema20<x.ema50:self._macro="BEAR"
            else:self._macro="NEUTRAL"
            self._macro_at=time.time();db.execute("INSERT OR REPLACE INTO regime_history(ts,regime,score,btc_price,btc_adx,btc_atr_pct) VALUES(?,?,?,?,?,?)",(int(time.time()*1000),self._macro,.35,safe(x.close),ad,safe(x.atr_pct)))
        except Exception:pass
        return self._macro
    async def snapshot(self,symbol,need_depth=False,need_liq=False,need_cg_oi=False):
        t=self.ticker_map.get(symbol,{})
        try:price=float(t.get("lastPr") or 0);bid=float(t.get("bidPr") or price);ask=float(t.get("askPr") or price);vol=float(t.get("usdtVolume") or t.get("quoteVolume") or 0);high24=float(t.get("high24h") or price);low24=float(t.get("low24h") or price)
        except Exception:return None
        if price<=0:return None
        try:
            frame_tasks=[self.bitget.candles(symbol,tf,280) for tf in REQUIRED_TFS];res=await asyncio.gather(*frame_tasks,self.bitget.oi(symbol),self.bitget.funding(symbol),self.macro_regime());frames={tf:res[i] for i,tf in enumerate(REQUIRED_TFS)};oi=res[-3];fund=res[-2];macro=res[-1]
            if any(len(frames[tf])<80 for tf in REQUIRED_TFS):return None
        except Exception:return None
        now=int(time.time()*1000);bucket=now-now%(5*60_000);db.execute("INSERT OR REPLACE INTO oi_snapshots(symbol,ts,oi)VALUES(?,?,?)",(symbol,bucket,oi));old=db.one("SELECT oi FROM oi_snapshots WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",(symbol,now-25*60_000));chg=(oi-float(old["oi"]))/float(old["oi"]) if old and float(old["oi"]) else 0.;depth=await self.bitget.depth_imbalance(symbol) if need_depth else None;liq=await self.cg.liquidation_clusters(symbol,price) if need_liq else {};cg_oi=await self.cg.oi_change_30m(symbol) if need_cg_oi else None;reg=self.local_regime(frames);rank=self.rank_map.get(symbol,{})
        return MarketSnapshot(symbol,price,bid,ask,vol,frames,fund,oi,chg,reg,macro,depth,liq.get("above"),liq.get("below"),liq.get("above_strength",0),liq.get("below_strength",0),cg_oi,float(rank.get("change24h",0)),int(rank.get("gainer_rank",0)),int(rank.get("loser_rank",0)),high24,low24)

market=MarketData()
