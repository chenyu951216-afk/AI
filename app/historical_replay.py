from __future__ import annotations
import asyncio,json,math,time,statistics
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
import httpx,pandas as pd
from .config import settings
from .db import db
from .indicators import enrich,safe
from .market import MarketSnapshot,TF_MS,market
from .risk import COMMON_BOUNDS,risk_manager
from .strategies import SPEC_MAP,build_strategy,min_score
from .learning import SPECIFIC_BOUNDS,STOP_PARAM,SIZING,EXITS,INT_KEYS

REPLAY_MODEL_VERSION="walk-forward-v2-strict-asof-forward-settlement"
ARCHIVE_REQUIRED={"oi_trend","funding_squeeze","orderbook","liquidation_magnet"}
RANK_REPLAY={"loser_rebound_cycle","gainer_pullback_cycle"}
AI_OWN_HISTORY={"ai_extreme_hunter"}

@dataclass
class ReplayResult:
    params:dict[str,Any]
    trades:list[dict[str,Any]]
    ambiguous_bars:int=0
    unclosed:int=0

class ReplayHistoryClient:
    def __init__(self):
        self.client=httpx.AsyncClient(base_url=settings.bitget_base_url,timeout=max(20,settings.market_timeout_sec))
        self.lock=asyncio.Lock();self.next_at=0.;self.cache={}
    async def _pace(self):
        async with self.lock:
            wait=self.next_at-time.time()
            if wait>0:await asyncio.sleep(wait)
            self.next_at=time.time()+max(.05,settings.historical_replay_request_spacing_ms/1000)
    async def _get(self,path,params):
        await self._pace();r=await self.client.get(path,params=params);r.raise_for_status();j=r.json()
        if j.get("code")!="00000":raise RuntimeError(f"Bitget replay {path}: {j.get('code')} {j.get('msg')}")
        return j.get("data") or []
    async def candles(self,symbol,tf,start_ms,end_ms):
        warmup=max(TF_MS[tf]*260,3*86400000);fetch_start=max(0,start_ms-warmup);key=(symbol,tf,fetch_start,end_ms//3600000);c=self.cache.get(key)
        if c and time.time()-c[0]<settings.historical_replay_cache_hours*3600:return c[1]
        cursor=end_ms-1;rows={};pages=0
        while cursor>fetch_start and pages<120:
            data=await self._get("/api/v2/mix/market/history-candles",{"symbol":symbol,"productType":settings.bitget_product_type,"granularity":tf,"endTime":cursor,"limit":200});pages+=1
            if not data:break
            ts=[]
            for x in data:
                try:
                    t=int(x[0]);ts.append(t)
                    if fetch_start<=t<end_ms:rows[t]=[t,float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5]),float(x[6]) if len(x)>6 else 0.]
                except Exception:pass
            if not ts:break
            oldest=min(ts);cursor=oldest-1
            if oldest<=fetch_start:break
        if not rows:return pd.DataFrame()
        df=pd.DataFrame(list(rows.values()),columns=["ts","open","high","low","close","volume","quote_volume"]).sort_values("ts").reset_index(drop=True)
        df=df[(df.ts+TF_MS[tf])<=end_ms].copy().reset_index(drop=True);df=enrich(df) if not df.empty else df;self.cache[key]=(time.time(),df);return df

class HistoricalReplayLab:
    def __init__(self):
        self.client=ReplayHistoryClient();self.task=None;self.busy=False;self.last_error=None;self.last_run=None;self.rotation=0;self._ensure_schema()
    def _ensure_schema(self):
        db.execute("""CREATE TABLE IF NOT EXISTS replay_feature_archive(
            symbol TEXT NOT NULL,ts INTEGER NOT NULL,funding REAL,oi REAL,oi_change_30m REAL,
            orderbook_imbalance REAL,liquidation_mode TEXT,liquidation_above REAL,liquidation_below REAL,
            liquidation_above_strength REAL,liquidation_below_strength REAL,liquidation_long_usd REAL,
            liquidation_short_usd REAL,liquidation_spike_ratio REAL,change24h REAL,gainer_rank INTEGER,
            loser_rank INTEGER,PRIMARY KEY(symbol,ts))""")
        db.execute("""CREATE TABLE IF NOT EXISTS replay_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,strategy TEXT NOT NULL,domain TEXT NOT NULL,status TEXT NOT NULL,
            data_quality TEXT NOT NULL,window_start INTEGER NOT NULL,window_end INTEGER NOT NULL,symbols_json TEXT NOT NULL,
            baseline_json TEXT NOT NULL DEFAULT '{}',best_json TEXT NOT NULL DEFAULT '{}',detail_json TEXT NOT NULL DEFAULT '{}',
            started_at INTEGER NOT NULL,completed_at INTEGER,error TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS replay_proposals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,strategy TEXT NOT NULL,domain TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'READY',
            baseline_json TEXT NOT NULL,params_json TEXT NOT NULL,score_delta REAL NOT NULL,confidence REAL NOT NULL,
            folds_json TEXT NOT NULL,reason TEXT NOT NULL,created_at INTEGER NOT NULL,consumed_at INTEGER)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_replay_runs_strategy ON replay_runs(strategy,completed_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_replay_proposals_strategy ON replay_proposals(strategy,status,created_at)")
    def configured(self):return bool(settings.historical_replay_enabled)
    async def start(self):
        if self.task and not self.task.done():return
        self.task=asyncio.create_task(self.loop(),name="historical-replay-lab")
    async def loop(self):
        await asyncio.sleep(max(5,settings.historical_replay_start_delay_sec))
        while True:
            try:
                if self.configured() and not self.busy:await self.run_next()
            except Exception as e:
                self.last_error=f"{type(e).__name__}: {e}";db.event("HISTORICAL_REPLAY_ERROR",self.last_error,"ERROR")
            await asyncio.sleep(max(30,settings.historical_replay_sleep_sec))
    def archive_snapshot(self,snap:MarketSnapshot):
        try:
            ts=int(snap.closed_ts("5m"));db.execute("""INSERT OR REPLACE INTO replay_feature_archive(symbol,ts,funding,oi,oi_change_30m,orderbook_imbalance,liquidation_mode,liquidation_above,liquidation_below,liquidation_above_strength,liquidation_below_strength,liquidation_long_usd,liquidation_short_usd,liquidation_spike_ratio,change24h,gainer_rank,loser_rank)VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(snap.symbol,ts,snap.funding,snap.oi,snap.oi_change_30m,snap.orderbook_imbalance,snap.liquidation_mode,snap.liquidation_above,snap.liquidation_below,snap.liquidation_above_strength,snap.liquidation_below_strength,snap.liquidation_long_usd,snap.liquidation_short_usd,snap.liquidation_spike_ratio,snap.change24h,snap.gainer_rank,snap.loser_rank))
        except Exception:pass
    def status(self):
        latest=db.one("SELECT * FROM replay_runs ORDER BY started_at DESC LIMIT 1");ready=int((db.one("SELECT COUNT(*)n FROM replay_proposals WHERE status='READY'") or {"n":0})["n"]);archive=int((db.one("SELECT COUNT(*)n FROM replay_feature_archive") or {"n":0})["n"])
        return {"enabled":settings.historical_replay_enabled,"busy":self.busy,"last_error":self.last_error,"last_run":self.last_run,"ready_proposals":ready,"archive_rows":archive,"latest":latest,"model_version":REPLAY_MODEL_VERSION}
    def _quality(self,name):
        if name in ARCHIVE_REQUIRED:return "LOCAL_ASOF_ARCHIVE+OHLCV"
        if name in RANK_REPLAY:return "OHLCV+CROSS_SECTION_SAMPLE_NO_LOOKAHEAD"
        return "OHLCV_NO_LOOKAHEAD"
    def _strategy_order(self):return list(SPEC_MAP.keys())
    async def run_next(self):
        order=self._strategy_order()
        if not order:return None
        for _ in range(len(order)):
            name=order[self.rotation%len(order)];self.rotation+=1
            if name in AI_OWN_HISTORY:
                self._log_skip(name,"AI_OWN_HISTORICAL_TRAINER","AI hunter already uses its own closed-K bootstrap and delayed future-label pipeline");continue
            if name in ARCHIVE_REQUIRED and not self._archive_ready(name):
                self._log_skip(name,"WAITING_LOCAL_ARCHIVE","Bitget public history does not provide faithful past OI/orderbook/liquidation snapshots; replay waits for locally archived as-of features instead of fabricating them");continue
            return await self.run_strategy(name)
        return None
    def _archive_ready(self,name):
        col={"oi_trend":"oi_change_30m","funding_squeeze":"oi_change_30m","orderbook":"orderbook_imbalance","liquidation_magnet":"liquidation_mode"}[name]
        q="SELECT COUNT(*)n FROM replay_feature_archive WHERE liquidation_mode IS NOT NULL AND liquidation_mode!='NONE'" if col=="liquidation_mode" else f"SELECT COUNT(*)n FROM replay_feature_archive WHERE {col} IS NOT NULL"
        return int((db.one(q) or {"n":0})["n"])>=settings.historical_replay_min_archive_rows
    def _log_skip(self,name,status,reason):
        last=db.one("SELECT * FROM replay_runs WHERE strategy=? ORDER BY started_at DESC LIMIT 1",(name,));now=int(time.time()*1000)
        if last and last["status"]==status and now-int(last["started_at"])<6*3600000:return
        db.execute("INSERT INTO replay_runs(strategy,domain,status,data_quality,window_start,window_end,symbols_json,detail_json,started_at,completed_at)VALUES(?,?,?,?,?,?,?,?,?,?)",(name,"none",status,status,now,now,"[]",json.dumps({"reason":reason},ensure_ascii=False),now,now))
    def _pick_symbols(self):
        rows=[]
        for s,t in market.ticker_map.items():
            try:
                if s.endswith("USDT"):rows.append((s,float(t.get("usdtVolume") or t.get("quoteVolume") or 0)))
            except Exception:pass
        rows.sort(key=lambda x:x[1],reverse=True);symbols=[s for s,_ in rows[:settings.historical_replay_symbols]]
        if "BTCUSDT" not in symbols:symbols.append("BTCUSDT")
        return symbols
    def _domain(self,name):
        n=int((db.one("SELECT COUNT(*)n FROM replay_runs WHERE strategy=? AND status='COMPLETE'",(name,)) or {"n":0})["n"]);return ("entry","exit","sizing")[n%3]
    async def run_strategy(self,name):
        if self.busy:return {"status":"BUSY"}
        state=db.state(name)
        if not state or not state["enabled"]:return {"status":"DISABLED"}
        if name in AI_OWN_HISTORY:return {"status":"AI_OWN_HISTORICAL_TRAINER"}
        if name in ARCHIVE_REQUIRED and not self._archive_ready(name):return {"status":"WAITING_LOCAL_ARCHIVE"}
        self.busy=True;self.last_error=None;started=int(time.time()*1000);domain=self._domain(name);now=int(time.time()*1000);decision_end=now-settings.historical_replay_forward_reserve_hours*3600000;start=decision_end-settings.historical_replay_lookback_days*86400000;fetch_end=now;symbols=self._pick_symbols();rid=None
        try:
            cur=db.execute("INSERT INTO replay_runs(strategy,domain,status,data_quality,window_start,window_end,symbols_json,started_at)VALUES(?,?,?,?,?,?,?,?)",(name,domain,"RUNNING",self._quality(name),start,decision_end,json.dumps(symbols),started));rid=int(cur.lastrowid)
            frames={};tfs=set(SPEC_MAP[name].context_tfs)|{SPEC_MAP[name].signal_tf,"15m","1H","4H"}
            for symbol in symbols:
                frames[symbol]={}
                for tf in sorted(tfs,key=lambda x:TF_MS[x]):
                    d=await self.client.candles(symbol,tf,start,fetch_end)
                    if len(d)>=100:frames[symbol][tf]=d
            symbols=[s for s in symbols if all(tf in frames.get(s,{}) for tf in tfs)]
            if name in RANK_REPLAY and len(symbols)<8:raise RuntimeError("not enough cross-sectional symbols for rank replay")
            if not symbols:raise RuntimeError("no replay symbols with complete historical frames")
            base=risk_manager.sanitize(json.loads(state["params_json"]),state["stage"]);candidates=self._candidates(name,base,domain,state["stage"])
            base_result=await asyncio.to_thread(self._simulate,name,base,state["stage"],symbols,frames,start,decision_end,fetch_end)
            evaluations=[]
            for p in candidates:
                r=await asyncio.to_thread(self._simulate,name,p,state["stage"],symbols,frames,start,decision_end,fetch_end);c=self._compare(base_result,r,start,decision_end)
                if name in RANK_REPLAY and c["confidence"]<.68:c["eligible"]=False;c["rank_sample_guard"]=True
                evaluations.append((p,r,c))
            bm=self._metrics(base_result.trades);best=None
            for p,r,c in evaluations:
                if c["eligible"] and (best is None or c["delta"]>best[2]["delta"]):best=(p,r,c)
            detail={"baseline":bm,"candidate_count":len(evaluations),"candidates":[{"params":p,"metrics":self._metrics(r.trades),"compare":c} for p,r,c in evaluations],"ambiguous_baseline":base_result.ambiguous_bars,"unclosed_baseline":base_result.unclosed,"rule":"entries only before window_end; existing positions continue through forward reserve; no new reserve-period entries"}
            best_json={}
            if best:
                p,r,c=best;best_json={"params":p,"metrics":self._metrics(r.trades),"compare":c};self._proposal(name,domain,base,p,c,started)
            completed=int(time.time()*1000);db.execute("UPDATE replay_runs SET status='COMPLETE',symbols_json=?,baseline_json=?,best_json=?,detail_json=?,completed_at=? WHERE id=?",(json.dumps(symbols),json.dumps(bm),json.dumps(best_json),json.dumps(detail),completed,rid));self.last_run={"strategy":name,"domain":domain,"completed_at":completed,"baseline_trades":bm["n"],"proposal":bool(best),"quality":self._quality(name)};db.event("HISTORICAL_REPLAY_COMPLETE",f"{name} {domain} n={bm['n']} proposal={bool(best)}")
            return self.last_run
        except Exception as e:
            self.last_error=f"{type(e).__name__}: {e}";completed=int(time.time()*1000)
            if rid:db.execute("UPDATE replay_runs SET status='FAILED',error=?,completed_at=? WHERE id=?",(self.last_error,completed,rid))
            db.event("HISTORICAL_REPLAY_FAILED",f"{name}: {self.last_error}","ERROR");return {"status":"FAILED","strategy":name,"error":self.last_error}
        finally:self.busy=False
    def _candidates(self,name,base,domain,stage):
        bounds={**COMMON_BOUNDS,**SPECIFIC_BOUNDS.get(name,{})};stop=STOP_PARAM.get(name)
        if domain=="entry":keys=[k for k in SPECIFIC_BOUNDS.get(name,{}) if k!=stop]
        elif domain=="exit":keys=([stop] if stop else [])+[k for k in EXITS if k in base]
        else:keys=[k for k in SIZING if k in base]
        keys=[k for k in keys if k and k in base and k in bounds];run_n=int((db.one("SELECT COUNT(*)n FROM replay_runs WHERE strategy=?",(name,)) or {"n":0})["n"]);out=[]
        if not keys:return out
        for j in range(min(settings.historical_replay_max_candidates,len(keys)*2)):
            k=keys[(run_n+j//2)%len(keys)];direction=1 if j%2==0 else -1;lo,hi=bounds[k];p=deepcopy(base);v=float(p[k]);step=(hi-lo)*(.05 if domain=="entry" else .06 if domain=="exit" else .07);nv=max(lo,min(hi,v+direction*step));p[k]=int(round(nv)) if k in INT_KEYS else round(nv,8);p=risk_manager.sanitize(p,stage)
            if p!=base and all(p!=x for x in out):out.append(p)
        return out
    def _proposal(self,name,domain,base,params,cmp,now):
        state=db.state(name);current=risk_manager.sanitize(json.loads(state["params_json"]),state["stage"])
        if current!=base:
            db.log_adjustment(name,"historical_replay_stale",base,params,"Replay finished after Champion changed; candidate discarded instead of mixing evidence.",False);return
        same=db.one("SELECT id FROM replay_proposals WHERE strategy=? AND status IN ('READY','SEEDED') AND params_json=?",(name,json.dumps(params)))
        if same:return
        reason=f"Walk-forward replay pretraining only; no-lookahead folds={cmp['folds_good']}/{cmp['folds_total']} scoreΔ={cmp['delta']:.3f} confidence={cmp['confidence']:.2f}. Historical evidence NEVER counts toward FINAL."
        cur=db.execute("INSERT INTO replay_proposals(strategy,domain,status,baseline_json,params_json,score_delta,confidence,folds_json,reason,created_at)VALUES(?,?,?,?,?,?,?,?,?,?)",(name,domain,"READY",json.dumps(base),json.dumps(params),cmp["delta"],cmp["confidence"],json.dumps(cmp["folds"]),reason,now));pid=int(cur.lastrowid);db.log_adjustment(name,"historical_replay_proposal",base,params,reason,False)
        if settings.historical_replay_seed_challenger and not db.one("SELECT 1 FROM challengers WHERE strategy=? AND status='ACTIVE'",(name,)):
            candidate=risk_manager.sanitize(params,state["stage"]);ts=int(time.time()*1000);why=f"REPLAY_SEEDED {domain}: {reason} Forward paper validation starts now and must satisfy normal Challenger gates."
            db.execute("INSERT OR REPLACE INTO challengers(strategy,status,domain,params_json,baseline_json,reason,started_at,updated_at)VALUES(?,?,?,?,?,?,?,?)",(name,"ACTIVE",domain,json.dumps(candidate),json.dumps(base),why,ts,ts));db.execute("DELETE FROM positions WHERE strategy=? AND variant='challenger'",(name,));db.ensure_account(name,"challenger",reset=True);db.execute("UPDATE replay_proposals SET status='SEEDED',consumed_at=? WHERE id=?",(ts,pid));db.log_adjustment(name,"replay_challenger_started",base,candidate,why,False)
    def _archive_at(self,symbol,decision_close):return db.one("SELECT * FROM replay_feature_archive WHERE symbol=? AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",(symbol,decision_close,decision_close-20*60_000))
    def _macro(self,btc,decision_close):
        h=btc.get("1H")
        if h is None:return "NEUTRAL"
        z=h[(h.ts+TF_MS["1H"])<=decision_close]
        if len(z)<80:return "NEUTRAL"
        x=z.iloc[-1];ad=safe(x.adx14);med=safe(z.atr_pct.iloc[-80:-1].median(),safe(x.atr_pct))
        if safe(x.atr_pct)>max(.015,med*1.9):return "HIGH_VOL"
        if ad>=24 and x.ema20>x.ema50:return "BULL"
        if ad>=24 and x.ema20<x.ema50:return "BEAR"
        return "NEUTRAL"
    def _local_regime(self,frames):
        h=frames["1H"];h4=frames["4H"];x=h.iloc[-1];z=h4.iloc[-1];ad=safe(x.adx14);atrp=safe(x.atr_pct);med=safe(h.atr_pct.iloc[-80:-1].median(),atrp)
        if atrp>max(.012,med*1.8):return "HIGH_VOL"
        if ad>=20 and x.ema20>x.ema50 and z.ema20>=z.ema50:return "BULL_TREND"
        if ad>=20 and x.ema20<x.ema50 and z.ema20<=z.ema50:return "BEAR_TREND"
        if ad<18:return "RANGE"
        return "NEUTRAL"
    def _rank_series(self,symbols,frames):
        by={};times=set()
        for s in symbols:
            d=frames[s]["15m"].copy();d["chg24"]=d.close/d.close.shift(96)-1;mp={int(r.ts+TF_MS["15m"]):float(r.chg24) for _,r in d.dropna(subset=["chg24"]).iterrows()};by[s]=mp;times.update(mp)
        out={}
        for ts in sorted(times):
            vals=[(s,m.get(ts)) for s,m in by.items() if m.get(ts) is not None and math.isfinite(m.get(ts))];gain=sorted(vals,key=lambda x:x[1],reverse=True);lose=sorted(vals,key=lambda x:x[1]);g={s:i+1 for i,(s,_) in enumerate(gain)};l={s:i+1 for i,(s,_) in enumerate(lose)};out[ts]={s:(v,g.get(s,0),l.get(s,0)) for s,v in vals}
        return out
    def _snapshot(self,name,symbol,decision_close,frames,rank_cache):
        sf={}
        for tf,d in frames[symbol].items():
            z=d[(d.ts+TF_MS[tf])<=decision_close]
            if len(z)<80:return None
            sf[tf]=z.iloc[-320:].copy()
        x=sf[SPEC_MAP[name].signal_tf].iloc[-1];price=float(x.close);rank=rank_cache.get(decision_close,{}).get(symbol,(0.,0,0));arch=self._archive_at(symbol,decision_close) if name in ARCHIVE_REQUIRED else None
        if name in ARCHIVE_REQUIRED and not arch:return None
        funding=float((arch or {}).get("funding") or 0);oi=float((arch or {}).get("oi") or 0);oic=float((arch or {}).get("oi_change_30m") or 0);ob=(arch or {}).get("orderbook_imbalance");lm=str((arch or {}).get("liquidation_mode") or "NONE")
        if name=="orderbook" and ob is None:return None
        if name=="liquidation_magnet" and lm=="NONE":return None
        macro=self._macro(frames.get("BTCUSDT",{}),decision_close);reg=self._local_regime(sf);qv=float(sf["15m"].quote_volume.iloc[-96:].sum()) if "15m" in sf else 0
        return MarketSnapshot(symbol,price,price,price,qv,sf,funding,oi,oic,reg,macro,float(ob) if ob is not None else None,(arch or {}).get("liquidation_above"),(arch or {}).get("liquidation_below"),float((arch or {}).get("liquidation_above_strength") or 0),float((arch or {}).get("liquidation_below_strength") or 0),None,float((arch or {}).get("liquidation_long_usd") or 0),float((arch or {}).get("liquidation_short_usd") or 0),float((arch or {}).get("liquidation_spike_ratio") or 0),lm,float(rank[0]),int(rank[1]),int(rank[2]),float(sf["15m"].high.iloc[-96:].max()),float(sf["15m"].low.iloc[-96:].min()))
    def _simulate(self,name,params,stage,symbols,frames,start,decision_end,fetch_end):
        spec=SPEC_MAP[name];tf=spec.signal_tf;params=risk_manager.sanitize(params,stage);events={};rank_cache=self._rank_series(symbols,frames) if name in RANK_REPLAY else {}
        for s in symbols:
            for _,r in frames[s][tf].iterrows():
                dc=int(r.ts+TF_MS[tf])
                if start<=dc<fetch_end:events.setdefault(dc,[]).append(s)
        balance=settings.initial_paper_equity;initial=balance;positions={};trades=[];ambiguous=0;closed_pnls=[]
        for dc in sorted(events):
            for s in list(positions):
                row=frames[s][tf][(frames[s][tf].ts+TF_MS[tf])==dc]
                if row.empty:continue
                pos=positions[s];fills,amb=self._manage_bar(pos,row.iloc[-1],params);ambiguous+=amb
                for fill in fills:balance+=fill["net_pnl"];closed_pnls.append((dc,fill["net_pnl"]));pos["fills"].append(fill)
                if pos.get("closed"):
                    net=sum(x["net_pnl"] for x in pos["fills"]);trades.append({"symbol":s,"side":pos["side"],"opened_at":pos["opened_at"],"closed_at":dc,"net_pnl":net,"initial_risk_cash":pos["initial_risk_cash"],"r":net/max(pos["initial_risk_cash"],1e-12)});positions.pop(s,None)
            if dc>=decision_end:continue
            candidates=[]
            for s in events[dc]:
                if s in positions:continue
                snap=self._snapshot(name,s,dc,frames,rank_cache)
                if not snap:continue
                sig=build_strategy(name,params).evaluate(snap)
                if sig and sig.score>=min_score(name,stage):candidates.append((sig.score,sig))
            candidates.sort(key=lambda x:x[0],reverse=True)
            for _,sig in candidates:
                if sig.symbol in positions or len(positions)>=params["max_positions"]:continue
                equity=balance+self._unrealized(positions,frames,tf,dc);recent=sum(p for ts,p in closed_pnls if ts>=dc-86400000)
                if equity<=0 or equity<=initial*(1-settings.hard_strategy_drawdown_pct) or recent<=-settings.hard_strategy_daily_loss_pct*initial:continue
                sp=abs(sig.entry-sig.stop)/max(sig.entry,1e-12)
                if sp<settings.hard_min_stop_pct or sp>settings.hard_max_stop_pct:continue
                total=sum(abs(p["remaining"]*p["entry"]) for p in positions.values());dist=abs(sig.entry-sig.stop);risk=equity*params["risk_pct"];q=min(risk/max(dist,1e-12),equity*params["leverage"]/sig.entry,max(0,equity*params["max_total_exposure_pct"]-total)/sig.entry,equity*params["max_symbol_exposure_pct"]/sig.entry,equity*params["max_position_notional_pct"]/sig.entry)
                if q<=0:continue
                slip=settings.paper_slippage_bps/10000;entry=sig.entry*(1+slip if sig.side=="long" else 1-slip);rd=abs(entry-sig.stop);sg=1 if sig.side=="long" else -1
                positions[sig.symbol]={"side":sig.side,"entry":entry,"initial_stop":sig.stop,"stop":sig.stop,"sl1":entry-sg*rd*params["sl1_r"],"tp1":entry+sg*rd*params["tp1_r"],"tp2":entry+sg*rd*params["tp2_r"],"tp3":entry+sg*rd*params["tp3_r"],"qty":q,"remaining":q,"initial_risk_cash":risk,"opened_at":dc,"sl1_hit":False,"tp1_hit":False,"tp2_hit":False,"fills":[],"closed":False}
        return ReplayResult(params,trades,ambiguous,len(positions))
    def _unrealized(self,positions,frames,tf,dc):
        x=0.
        for s,p in positions.items():
            z=frames[s][tf][(frames[s][tf].ts+TF_MS[tf])<=dc]
            if z.empty:continue
            px=float(z.iloc[-1].close);sg=1 if p["side"]=="long" else -1;x+=(px-p["entry"])*p["remaining"]*sg
        return x
    def _fill(self,pos,price,qty,reason):
        qty=min(qty,pos["remaining"]);slip=settings.paper_slippage_bps/10000;fill=price*(1-slip if pos["side"]=="long" else 1+slip);sg=1 if pos["side"]=="long" else -1;gross=(fill-pos["entry"])*qty*sg;fees=(pos["entry"]*qty+fill*qty)*settings.paper_taker_fee;net=gross-fees;pos["remaining"]-=qty
        if pos["remaining"]<=1e-12:pos["remaining"]=0;pos["closed"]=True
        return {"reason":reason,"exit":fill,"qty":qty,"net_pnl":net}
    def _manage_bar(self,pos,bar,p):
        long=pos["side"]=="long";low=float(bar.low);high=float(bar.high);close=float(bar.close);fills=[];amb=0;stop_hit=low<=pos["stop"] if long else high>=pos["stop"];sl1_hit=(not pos["sl1_hit"]) and (low<=pos["sl1"] if long else high>=pos["sl1"]);tp1_hit=(not pos["tp1_hit"]) and (high>=pos["tp1"] if long else low<=pos["tp1"]);tp2_hit=(not pos["tp2_hit"]) and (high>=pos["tp2"] if long else low<=pos["tp2"]);tp3_hit=high>=pos["tp3"] if long else low<=pos["tp3"]
        if stop_hit:fills.append(self._fill(pos,pos["stop"],pos["remaining"],"STOP"));return fills,int(tp1_hit or tp2_hit or tp3_hit)
        if sl1_hit:
            fills.append(self._fill(pos,pos["sl1"],pos["qty"]*p.get("sl1_fraction",.18),"SL1"));pos["sl1_hit"]=True
            if tp1_hit or tp2_hit or tp3_hit:amb+=1;tp1_hit=tp2_hit=tp3_hit=False
        if not pos["closed"] and tp1_hit:fills.append(self._fill(pos,pos["tp1"],pos["qty"]*p.get("tp1_fraction",.25),"TP1"));pos["tp1_hit"]=True
        if not pos["closed"] and tp2_hit:fills.append(self._fill(pos,pos["tp2"],pos["qty"]*p.get("tp2_fraction",.30),"TP2"));pos["tp2_hit"]=True
        if not pos["closed"] and tp3_hit:fills.append(self._fill(pos,pos["tp3"],pos["remaining"],"TP3"))
        if not pos["closed"]:
            r=max(abs(pos["entry"]-pos["initial_stop"]),1e-12);sg=1 if long else -1;close_r=(close-pos["entry"])*sg/r;ns=pos["stop"]
            if close_r>=p.get("breakeven_trigger_r",.85):
                be=pos["entry"]+sg*r*p.get("breakeven_offset_r",.04);ns=max(ns,be) if long else min(ns,be)
            if close_r>=p.get("trail_start_r",1.35):
                candidate=close-sg*r*p.get("trail_r",1.0);ns=max(ns,candidate) if long else min(ns,candidate)
            pos["stop"]=ns
        return fills,amb
    def _metrics(self,trades):
        if not trades:return {"n":0,"pf":0,"expectancy_r":0,"avg_r":0,"win_rate":0,"max_dd_pct":0,"profit_concentration":1,"score":-999}
        rs=[float(x["r"]) for x in trades];wins=[x for x in rs if x>0];loss=[x for x in rs if x<0];gw=sum(wins);gl=-sum(loss);eq=1.;peak=1.;dd=0.;by={}
        for t,r in zip(trades,rs):eq+=r*.01;peak=max(peak,eq);dd=max(dd,(peak-eq)/max(peak,1e-12));by[t["symbol"]]=by.get(t["symbol"],0)+max(0,r)
        pf=gw/gl if gl else 9.99;exp=statistics.fmean(rs);conc=max(by.values(),default=0)/max(sum(by.values()),1e-12);score=math.log(max(pf,.05))*1.25+exp*1.4-dd*3.5-math.log(max(conc,.05))*.08
        return {"n":len(rs),"pf":pf,"expectancy_r":exp,"avg_r":exp,"win_rate":len(wins)/len(rs),"max_dd_pct":dd,"profit_concentration":conc,"score":score}
    def _fold_metrics(self,trades,start,end):
        folds=[];n=max(2,settings.historical_replay_folds);span=(end-start)/n
        for i in range(n):
            a=start+i*span;b=end if i==n-1 else start+(i+1)*span;folds.append(self._metrics([t for t in trades if a<=t["opened_at"]<b]))
        return folds
    def _compare(self,base,cand,start,end):
        bm=self._metrics(base.trades);cm=self._metrics(cand.trades);bf=self._fold_metrics(base.trades,start,end);cf=self._fold_metrics(cand.trades,start,end);good=0;foldrows=[]
        for b,c in zip(bf,cf):
            ok=c["n"]>=max(4,settings.historical_replay_min_trades//settings.historical_replay_folds//2) and c["expectancy_r"]>0 and c["score"]>=b["score"]-.03
            if ok:good+=1
            foldrows.append({"baseline":b,"candidate":c,"good":ok})
        delta=cm["score"]-bm["score"];need=max(2,math.ceil(settings.historical_replay_folds*.67));last=cf[-1] if cf else {"expectancy_r":0};confidence=max(0,min(1,.35*good/max(len(cf),1)+.25*min(1,cm["n"]/max(settings.historical_replay_min_trades*2,1))+.25*min(1,max(delta,0)/.25)+.15*min(1,max(cm["pf"]-1,0)/.6)));eligible=cm["n"]>=settings.historical_replay_min_trades and cm["expectancy_r"]>0 and cm["pf"]>=1.03 and cm["max_dd_pct"]<=min(.28,bm["max_dd_pct"]+.06 if bm["n"] else .28) and cm["profit_concentration"]<=.60 and good>=need and last["expectancy_r"]>0 and delta>=.06 and confidence>=.55
        return {"eligible":eligible,"delta":delta,"confidence":confidence,"folds_good":good,"folds_total":len(cf),"folds":foldrows,"baseline":bm,"candidate":cm,"ambiguous_bars":cand.ambiguous_bars,"unclosed":cand.unclosed}

historical_replay=HistoricalReplayLab()
