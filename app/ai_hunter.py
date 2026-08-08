from __future__ import annotations
import json, math, time
from typing import Any
from .db import db
from .indicators import safe

MODEL_VERSION="extreme-tail-v1"
HORIZON_BARS=96
MIN_MODEL_SAMPLES=250
BOOTSTRAP_PER_SYMBOL=12
MAX_TARGET=1.25
FEATURES=(
    "ret1","ret4","ret16","rvol","adx","rsi","atr_pct","ema20_gap","ema50_gap",
    "ema_spread","z20","range20","range_pos","body","breakout_up","breakout_down",
)

def _clip(x:float,lo:float=-4.0,hi:float=4.0)->float:
    return max(lo,min(hi,float(x)))

def _log_ratio(a:float,b:float)->float:
    return math.log(max(a,1e-12)/max(b,1e-12))

class AIExtremeHunter:
    def __init__(self):
        self._threshold_cache=(0.0,0.04,0.04)
        self._ensure_schema()

    def _ensure_schema(self):
        db.execute("""CREATE TABLE IF NOT EXISTS ai_hunter_meta(
            key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS ai_hunter_samples(
            id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT NOT NULL,bar_ts INTEGER NOT NULL,
            entry REAL NOT NULL,features_json TEXT NOT NULL,status TEXT NOT NULL,
            long_target REAL,short_target REAL,max_up_pct REAL,max_down_pct REAL,
            created_at INTEGER NOT NULL,labeled_at INTEGER,UNIQUE(symbol,bar_ts))""")
        db.execute("""CREATE TABLE IF NOT EXISTS ai_hunter_models(
            side TEXT PRIMARY KEY,weights_json TEXT NOT NULL,bias REAL NOT NULL DEFAULT 0.02,
            n INTEGER NOT NULL DEFAULT 0,mae REAL NOT NULL DEFAULT 0,updated_at INTEGER NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_ai_hunter_pending ON ai_hunter_samples(symbol,status,bar_ts)")
        now=int(time.time()*1000)
        v=db.one("SELECT value FROM ai_hunter_meta WHERE key='model_version'")
        if not v or v["value"]!=MODEL_VERSION:
            db.execute("DELETE FROM ai_hunter_samples")
            db.execute("DELETE FROM ai_hunter_models")
            db.execute("INSERT OR REPLACE INTO ai_hunter_meta(key,value,updated_at)VALUES('model_version',?,?)",(MODEL_VERSION,now))
        for side in ("long","short"):
            db.execute("INSERT OR IGNORE INTO ai_hunter_models(side,weights_json,bias,n,mae,updated_at)VALUES(?,?,?,?,?,?)",
                       (side,json.dumps([0.0]*len(FEATURES)),0.02,0,0.0,now))

    def _features(self,d,i:int)->list[float] | None:
        if i<60 or i>=len(d): return None
        x=d.iloc[i]
        close=safe(x.close);atr=safe(x.atr14)
        if close<=0 or atr<=0:return None
        lo=safe(d.low.iloc[max(0,i-31):i+1].min(),close)
        hi=safe(d.high.iloc[max(0,i-31):i+1].max(),close)
        pos=(close-lo)/max(hi-lo,1e-12)
        body=(safe(x.close)-safe(x.open))/max(safe(x.high)-safe(x.low),1e-12)
        hh=safe(x.hh20,close);ll=safe(x.ll20,close)
        vals=[
            safe(x.ret1)/.025,
            safe(x.ret4)/.07,
            safe(x.ret16)/.18,
            math.log(max(safe(x.rvol20,1.0),.05)),
            (safe(x.adx14,20)-20)/15,
            (safe(x.rsi14,50)-50)/25,
            safe(x.atr_pct)/.05,
            (close/max(safe(x.ema20,close),1e-12)-1)/.06,
            (close/max(safe(x.ema50,close),1e-12)-1)/.12,
            (safe(x.ema20,close)/max(safe(x.ema50,close),1e-12)-1)/.10,
            safe(x.z20)/3,
            safe(x.range20)/.20,
            (pos-.5)*2,
            body,
            (close-hh)/max(atr,1e-12),
            (ll-close)/max(atr,1e-12),
        ]
        return [_clip(v) for v in vals]

    def _targets(self,d,i:int)->tuple[float,float,float,float] | None:
        if i+HORIZON_BARS>=len(d):return None
        entry=safe(d.iloc[i].close)
        if entry<=0:return None
        fut=d.iloc[i+1:i+1+HORIZON_BARS]
        if fut.empty:return None
        max_px=safe(fut.high.max(),entry);min_px=max(safe(fut.low.min(),entry),1e-12)
        up=max(0.0,max_px/entry-1)
        down=max(0.0,1-min_px/entry)
        lt=min(MAX_TARGET,max(0.0,_log_ratio(max_px,entry)/math.log(10)))
        st=min(MAX_TARGET,max(0.0,_log_ratio(entry,min_px)/math.log(10)))
        return lt,st,up,down

    def _load_model(self,side:str)->dict[str,Any]:
        r=db.one("SELECT * FROM ai_hunter_models WHERE side=?",(side,))
        if not r:return {"weights":[0.0]*len(FEATURES),"bias":.02,"n":0,"mae":0.0}
        try:w=json.loads(r["weights_json"])
        except Exception:w=[0.0]*len(FEATURES)
        if len(w)!=len(FEATURES):w=[0.0]*len(FEATURES)
        return {"weights":[float(x) for x in w],"bias":float(r["bias"]),"n":int(r["n"]),"mae":float(r["mae"])}

    def _predict_raw(self,model:dict[str,Any],x:list[float])->float:
        z=model["bias"]+sum(a*b for a,b in zip(model["weights"],x))
        return max(0.0,min(MAX_TARGET,z))

    def _update(self,side:str,x:list[float],target:float):
        m=self._load_model(side);pred=self._predict_raw(m,x);err=target-pred
        n=m["n"]+1;lr=.030/math.sqrt(1+n/1800);tail_weight=min(16.0,1.0+20.0*target*target)
        g=max(-.25,min(.25,err))*lr*tail_weight;wd=0.00015
        w=[max(-2.5,min(2.5,wi*(1-wd)+g*xi)) for wi,xi in zip(m["weights"],x)]
        b=max(0.0,min(.35,m["bias"]+g*.30));mae=((m["mae"]*m["n"])+abs(err))/n
        db.execute("UPDATE ai_hunter_models SET weights_json=?,bias=?,n=?,mae=?,updated_at=? WHERE side=?",
                   (json.dumps(w),b,n,mae,int(time.time()*1000),side))

    def _label_sample(self,row,d)->bool:
        ts=int(row["bar_ts"]);idxs=d.index[d.ts==ts].tolist()
        if not idxs:return False
        i=int(idxs[0]);t=self._targets(d,i)
        if not t:return False
        lt,st,up,down=t
        try:x=[float(v) for v in json.loads(row["features_json"])]
        except Exception:return False
        self._update("long",x,lt);self._update("short",x,st)
        db.execute("""UPDATE ai_hunter_samples SET status='LABELED',long_target=?,short_target=?,
                   max_up_pct=?,max_down_pct=?,labeled_at=? WHERE id=?""",
                   (lt,st,up,down,int(time.time()*1000),row["id"]))
        return True

    def _label_pending(self,symbol:str,d):
        cutoff=int(d.iloc[-1].ts)-HORIZON_BARS*15*60_000
        rows=db.query("""SELECT * FROM ai_hunter_samples
                         WHERE symbol=? AND status='PENDING' AND bar_ts<=?
                         ORDER BY bar_ts LIMIT 24""",(symbol,cutoff))
        for r in rows:self._label_sample(r,d)

    def _bootstrap(self,symbol:str,d):
        if len(d)<180:return
        end=len(d)-HORIZON_BARS-1;start=max(70,end-BOOTSTRAP_PER_SYMBOL*5)
        if end<=start:return
        picks=list(range(start,end+1,5))[-BOOTSTRAP_PER_SYMBOL:];now=int(time.time()*1000)
        for i in picks:
            x=self._features(d,i);t=self._targets(d,i)
            if not x or not t:continue
            ts=int(d.iloc[i].ts);entry=safe(d.iloc[i].close);lt,st,up,down=t
            cur=db.execute("""INSERT OR IGNORE INTO ai_hunter_samples
                (symbol,bar_ts,entry,features_json,status,long_target,short_target,max_up_pct,max_down_pct,created_at,labeled_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (symbol,ts,entry,json.dumps(x),"LABELED",lt,st,up,down,now,now))
            if cur.rowcount:
                self._update("long",x,lt);self._update("short",x,st)

    def observe(self,snap):
        d=snap.df("15m");self._label_pending(snap.symbol,d);self._bootstrap(snap.symbol,d)
        i=len(d)-1;x=self._features(d,i)
        if not x:return None
        ts=int(d.iloc[i].ts);now=int(time.time()*1000)
        db.execute("""INSERT OR IGNORE INTO ai_hunter_samples
            (symbol,bar_ts,entry,features_json,status,created_at)
            VALUES(?,?,?,?,?,?)""",(snap.symbol,ts,float(d.iloc[i].close),json.dumps(x),"PENDING",now))
        return x

    def _thresholds(self)->tuple[float,float]:
        now=time.time()
        if now-self._threshold_cache[0]<60:return self._threshold_cache[1],self._threshold_cache[2]
        rows=db.query("""SELECT long_target,short_target FROM ai_hunter_samples
                         WHERE status='LABELED' ORDER BY labeled_at DESC LIMIT 3000""")
        def q(vals,p=.90):
            a=sorted(float(v) for v in vals if v is not None)
            if not a:return .04
            return a[min(len(a)-1,int((len(a)-1)*p))]
        ql=max(.035,q([r["long_target"] for r in rows]));qs=max(.035,q([r["short_target"] for r in rows]))
        self._threshold_cache=(now,ql,qs);return ql,qs

    def predict(self,snap)->dict[str,Any] | None:
        x=self.observe(snap)
        if not x:return None
        lm=self._load_model("long");sm=self._load_model("short")
        if min(lm["n"],sm["n"])<MIN_MODEL_SAMPLES:
            return {"ready":False,"long_pred":0.0,"short_pred":0.0,"long_n":lm["n"],"short_n":sm["n"]}
        lp=self._predict_raw(lm,x);sp=self._predict_raw(sm,x);ql,qs=self._thresholds()
        side=None;pred=thr=0.0
        if lp>=ql and lp>=sp*1.12:side="long";pred=lp;thr=ql
        elif sp>=qs and sp>=lp*1.12:side="short";pred=sp;thr=qs
        edge=abs(lp-sp)
        confidence=max(0.0,min(1.0,(pred/max(thr,1e-9)-1)*.75+edge*3)) if side else 0.0
        return {"ready":True,"side":side,"confidence":confidence,"long_pred":lp,"short_pred":sp,
                "long_threshold":ql,"short_threshold":qs,"long_n":lm["n"],"short_n":sm["n"]}

    def status(self)->dict[str,Any]:
        total=int((db.one("SELECT COUNT(*)n FROM ai_hunter_samples") or {"n":0})["n"])
        labeled=int((db.one("SELECT COUNT(*)n FROM ai_hunter_samples WHERE status='LABELED'") or {"n":0})["n"])
        pending=max(0,total-labeled);lm=self._load_model("long");sm=self._load_model("short");ql,qs=self._thresholds()
        rows=db.query("""SELECT max_up_pct,max_down_pct FROM ai_hunter_samples
                         WHERE status='LABELED' ORDER BY labeled_at DESC LIMIT 2000""")
        max_up=max([float(r.get("max_up_pct") or 0) for r in rows],default=0)
        max_down=max([float(r.get("max_down_pct") or 0) for r in rows],default=0)
        imp=[]
        for side,m in (("L",lm),("S",sm)):
            for name,w in zip(FEATURES,m["weights"]):imp.append((abs(w),f"{side}:{name}",w))
        imp=sorted(imp,reverse=True)[:5]
        return {
            "model_version":MODEL_VERSION,"labeled_samples":labeled,"pending_samples":pending,
            "long_n":lm["n"],"short_n":sm["n"],"long_mae":round(lm["mae"],4),"short_mae":round(sm["mae"],4),
            "long_tail_threshold":round(ql,4),"short_tail_threshold":round(qs,4),
            "max_observed_up_pct":round(max_up*100,2),"max_observed_down_pct":round(max_down*100,2),
            "ready":min(lm["n"],sm["n"])>=MIN_MODEL_SAMPLES,
            "model_learning_pct":round(min(100.0,labeled/2000*100),1),
            "top_features":[{"feature":n,"weight":round(w,4)} for _,n,w in imp],
            "objective":"long 10x / short 90% collapse log-tail proxy; 24h fully-closed future labels"
        }

ai_hunter=AIExtremeHunter()
