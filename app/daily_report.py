from __future__ import annotations
import asyncio,json,time
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from typing import Any
import httpx
from .config import settings
from .db import db
from .learning import completed_roundtrips,metrics,exit_diagnostics,learning_progress
from .risk import risk_manager
from .market import market
from .ai_hunter import ai_hunter

GOOD_COLOR=0x4FE09A
BAD_COLOR=0xFF6475
MAX_DESCRIPTION=3600
SEND_SPACING_SEC=1.0
RETRY_DELAY_SEC=600
STALE_RUNNING_SEC=1800


def _f(v,d=2):
    try:return f"{float(v):,.{d}f}"
    except Exception:return "0"

def _pct(v,d=1):
    try:return f"{float(v)*100:.{d}f}%"
    except Exception:return "0%"

def _short(s:Any,n:int=260)->str:
    x=str(s or "").replace("\n"," ").strip()
    return x if len(x)<=n else x[:n-1]+"…"

def _json(x):
    try:return json.loads(x or "{}")
    except Exception:return {}

def _diff(before,after):
    b=_json(before) if isinstance(before,str) else (before or {})
    a=_json(after) if isinstance(after,str) else (after or {})
    out=[]
    for k in sorted(set(b)|set(a)):
        if b.get(k)!=a.get(k):out.append(f"{k}: {b.get(k)} → {a.get(k)}")
    return out

def _chunks(lines,max_chars=MAX_DESCRIPTION):
    out=[];cur=[];size=0
    for raw in lines:
        line=str(raw)
        add=len(line)+(1 if cur else 0)
        if cur and size+add>max_chars:
            out.append("\n".join(cur));cur=[line];size=len(line)
        else:
            cur.append(line);size+=add
    if cur:out.append("\n".join(cur))
    return out or ["沒有資料"]

class DailyDiscordReporter:
    def __init__(self):
        self.tz=ZoneInfo(settings.timezone or "Asia/Taipei")
        self.client=httpx.AsyncClient(timeout=30)
        self.task=None;self.busy=False;self.last_error=None
        self._ensure_schema()
    def _ensure_schema(self):
        db.execute("""CREATE TABLE IF NOT EXISTS daily_discord_reports(
            report_key TEXT PRIMARY KEY,window_start INTEGER NOT NULL,window_end INTEGER NOT NULL,
            status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,started_at INTEGER,
            completed_at INTEGER,error TEXT,payload_json TEXT NOT NULL DEFAULT '{}',
            message_ids_json TEXT NOT NULL DEFAULT '[]')""")
    def configured(self):return bool(settings.discord_daily_report_enabled and settings.discord_webhook_url)
    def _latest_target(self,now:datetime|None=None):
        now=now or datetime.now(self.tz)
        target=now.replace(hour=settings.daily_report_hour,minute=settings.daily_report_minute,second=0,microsecond=0)
        if now<target:target-=timedelta(days=1)
        return target
    def _window(self,target:datetime):return target-timedelta(days=1),target
    def status(self):
        row=db.one("SELECT * FROM daily_discord_reports ORDER BY window_end DESC LIMIT 1")
        return {"enabled":settings.discord_daily_report_enabled,"configured":self.configured(),"timezone":settings.timezone,"hour":settings.daily_report_hour,"minute":settings.daily_report_minute,"busy":self.busy,"last_error":self.last_error,"latest":row}
    async def start(self):
        if self.task and not self.task.done():return
        self.task=asyncio.create_task(self.loop(),name="daily-discord-reporter")
    async def loop(self):
        while True:
            try:await self.maybe_run()
            except Exception as e:
                self.last_error=f"{type(e).__name__}: {e}";db.event("DAILY_REPORT_LOOP_ERROR",self.last_error,"ERROR")
            await asyncio.sleep(30)
    async def maybe_run(self):
        if not self.configured() or self.busy:return None
        now=datetime.now(self.tz)
        today_target=now.replace(hour=settings.daily_report_hour,minute=settings.daily_report_minute,second=0,microsecond=0)
        if now<today_target:return None
        start,end=self._window(today_target);key=end.strftime("%Y-%m-%d")
        row=db.one("SELECT * FROM daily_discord_reports WHERE report_key=?",(key,))
        now_ms=int(time.time()*1000)
        if row and row["status"]=="SENT":return row
        if row and row["status"]=="RUNNING" and now_ms-int(row.get("started_at") or 0)<STALE_RUNNING_SEC*1000:return row
        if row and row["status"]=="FAILED" and now_ms-int(row.get("started_at") or 0)<RETRY_DELAY_SEC*1000:return row
        return await self._run(key,start,end)
    async def run_latest(self,force=False):
        target=self._latest_target();start,end=self._window(target);key=target.strftime("%Y-%m-%d")
        if force:db.execute("DELETE FROM daily_discord_reports WHERE report_key=? AND status!='RUNNING'",(key,))
        return await self._run(key,start,end)
    async def _run(self,key,start,end):
        if self.busy:return {"status":"BUSY"}
        if not self.configured():return {"status":"DISABLED_OR_MISSING_WEBHOOK"}
        self.busy=True;self.last_error=None
        started=int(time.time()*1000);start_ms=int(start.timestamp()*1000);end_ms=int(end.timestamp()*1000)
        row=db.one("SELECT attempts FROM daily_discord_reports WHERE report_key=?",(key,));attempts=int((row or {}).get("attempts") or 0)+1
        db.execute("INSERT INTO daily_discord_reports(report_key,window_start,window_end,status,attempts,started_at,error)VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(report_key) DO UPDATE SET status='RUNNING',attempts=excluded.attempts,started_at=excluded.started_at,error=NULL",(key,start_ms,end_ms,"RUNNING",attempts,started))
        try:
            payload=self._collect(start_ms,end_ms,key)
            message_ids=[]
            total=len(payload["strategies"])
            for i,s in enumerate(payload["strategies"],1):
                chunks=_chunks(self._strategy_lines(s))
                for part,desc in enumerate(chunks,1):
                    suffix=f" · {part}/{len(chunks)}" if len(chunks)>1 else ""
                    title=f"{i:02d}/{total:02d} · {s['display_name']}{suffix}"
                    mid=await self._send_embed(title,desc,GOOD_COLOR if s["daily_realized_pnl"]>=0 else BAD_COLOR)
                    if mid:message_ids.append(mid)
            completed=int(time.time()*1000)
            db.execute("UPDATE daily_discord_reports SET status='SENT',completed_at=?,error=NULL,payload_json=?,message_ids_json=? WHERE report_key=?",(completed,json.dumps(payload,ensure_ascii=False),json.dumps(message_ids),key))
            db.event("DAILY_REPORT_SENT",f"{key} sent {len(message_ids)} per-strategy Discord messages")
            return {"status":"SENT","report_key":key,"messages":len(message_ids)}
        except Exception as e:
            self.last_error=f"{type(e).__name__}: {e}"
            db.execute("UPDATE daily_discord_reports SET status='FAILED',error=? WHERE report_key=?",(self.last_error,key))
            db.event("DAILY_REPORT_FAILED",f"{key}: {self.last_error}","ERROR")
            raise
        finally:self.busy=False
    def _previous_progress(self,end_ms):
        r=db.one("SELECT payload_json FROM daily_discord_reports WHERE status='SENT' AND window_end<? ORDER BY window_end DESC LIMIT 1",(end_ms,));out={}
        if not r:return out
        try:
            p=json.loads(r["payload_json"] or "{}")
            out={x["strategy"]:float(x.get("learning_progress_pct") or 0) for x in p.get("strategies",[])}
        except Exception:pass
        return out
    def _completed_window(self,strategy,start_ms,end_ms):
        return db.query("""SELECT t.position_id,t.strategy,t.variant,MIN(t.symbol)symbol,MIN(t.side)side,
            SUM(t.gross_pnl)gross_pnl,SUM(t.fees)fees,SUM(t.net_pnl)net_pnl,
            MAX(t.closed_at)closed_at,MIN(t.opened_at)opened_at,MAX(t.initial_risk_cash)initial_risk_cash
            FROM trades t WHERE t.strategy=? AND t.variant='champion'
            AND NOT EXISTS(SELECT 1 FROM positions p WHERE p.id=t.position_id)
            GROUP BY t.position_id,t.strategy,t.variant
            HAVING MAX(t.closed_at)>=? AND MAX(t.closed_at)<? ORDER BY closed_at""",(strategy,start_ms,end_ms))
    def _coin_rows(self,fills,completed,open_pos,start_ms,end_ms):
        rows={}
        def row(sym):return rows.setdefault(sym,{"symbol":sym,"realized":0.0,"fills":0,"roundtrips":0,"entries":set(),"open":0})
        for x in fills:
            z=row(x["symbol"]);z["realized"]+=float(x["net_pnl"]);z["fills"]+=1
            if start_ms<=int(x.get("opened_at") or 0)<end_ms:z["entries"].add(int(x["position_id"]))
        for x in completed:row(x["symbol"])["roundtrips"]+=1
        for x in open_pos:
            z=row(x["symbol"]);z["open"]+=1
            if start_ms<=int(x.get("opened_at") or 0)<end_ms:z["entries"].add(int(x["id"]))
        out=[]
        for z in rows.values():
            z["entries"]=len(z["entries"]);out.append(z)
        out.sort(key=lambda z:(abs(z["realized"]),z["fills"],z["entries"]),reverse=True)
        return out
    def _today_post_summary(self,posts):
        flags={};captures=[];mfes=[]
        for x in posts:
            a=_json(x.get("analysis_json"))
            for f in a.get("flags",[]) or []:flags[f]=flags.get(f,0)+1
            try:captures.append(float(a.get("capture_ratio")))
            except Exception:pass
            try:mfes.append(float(a.get("in_trade_mfe_r")))
            except Exception:pass
        top=sorted(flags.items(),key=lambda kv:kv[1],reverse=True)[:5]
        return {"n":len(posts),"flags":top,"avg_capture":sum(captures)/len(captures) if captures else None,"avg_mfe_r":sum(mfes)/len(mfes) if mfes else None}
    def _collect(self,start_ms,end_ms,key):
        prev=self._previous_progress(end_ms);strategies=[];sum_pnl=0.0
        prices=market.prices()
        states=db.query("SELECT * FROM strategy_state ORDER BY strategy")
        for state in states:
            name=state["strategy"];since=int(state.get("evidence_since") or 0)
            all_tr=completed_roundtrips(name,"champion",100000,since);lm=metrics(all_tr);diag=exit_diagnostics(name,"champion",since);progress=learning_progress(all_tr,lm,diag)
            acc=db.account(name,"champion");equity=risk_manager.equity(name,"champion",prices)
            fills=db.query("SELECT * FROM trades WHERE strategy=? AND variant='champion' AND closed_at>=? AND closed_at<? ORDER BY closed_at,id",(name,start_ms,end_ms))
            completed=self._completed_window(name,start_ms,end_ms);cm=metrics(completed)
            open_pos=db.query("SELECT * FROM positions WHERE strategy=? AND variant='champion' ORDER BY opened_at",(name,))
            posts=db.query("SELECT * FROM post_trade_studies WHERE strategy=? AND variant='champion' AND completed_at>=? AND completed_at<? ORDER BY completed_at",(name,start_ms,end_ms))
            adjustments=db.query("SELECT * FROM adjustments WHERE strategy=? AND created_at>=? AND created_at<? ORDER BY created_at,id",(name,start_ms,end_ms))
            replay_runs=db.query("SELECT * FROM replay_runs WHERE strategy=? AND status='COMPLETE' AND completed_at>=? AND completed_at<? ORDER BY completed_at",(name,start_ms,end_ms))
            replay_proposals=db.query("SELECT * FROM replay_proposals WHERE strategy=? AND created_at>=? AND created_at<? ORDER BY created_at",(name,start_ms,end_ms))
            challenger=db.one("SELECT * FROM challengers WHERE strategy=? AND status='ACTIVE'",(name,));cp=None
            if challenger:
                ctr=completed_roundtrips(name,"challenger",10000,int(challenger["started_at"]));cmet=metrics(ctr)
                cp={"domain":challenger.get("domain"),"trades":cmet["n"],"pf":cmet["pf"],"expectancy":cmet["expectancy"],"dd":cmet["max_dd_pct"],"reason":challenger["reason"]}
            day_pnl=sum(float(x["net_pnl"]) for x in fills);sum_pnl+=day_pnl
            initial=float((acc or {}).get("initial_balance") or settings.initial_paper_equity)
            strategies.append({
                "strategy":name,"display_name":state["display_name"],"stage":state["stage"],
                "daily_realized_pnl":day_pnl,"fills":fills,"completed":completed,"completed_metrics":cm,
                "coin_rows":self._coin_rows(fills,completed,open_pos,start_ms,end_ms),"open_positions":len(open_pos),
                "balance":float((acc or {}).get("balance") or 0),"equity":equity,"return_pct":((equity/initial)-1)*100,
                "lifetime_metrics":lm,"learning_progress_pct":float(progress["pct"]),
                "progress_delta":float(progress["pct"])-float(prev.get(name,progress["pct"])),
                "posts":posts,"today_post_summary":self._today_post_summary(posts),"adjustments":adjustments,
                "exit_diag":diag,"challenger":cp,"replay_runs":replay_runs,"replay_proposals":replay_proposals,
                "ai_model":ai_hunter.status() if name=="ai_extreme_hunter" else None,
            })
        return {"report_key":key,"window_start":start_ms,"window_end":end_ms,"generated_at":int(time.time()*1000),"summary":{"realized_pnl":sum_pnl},"strategies":strategies}
    def _strategy_lines(self,s):
        m=s["completed_metrics"];lm=s["lifetime_metrics"];delta=s["progress_delta"]
        lines=[
            f"**24h PAPER 損益（17:00 → 17:00）**",
            f"- 實現損益：**{_f(s['daily_realized_pnl'])} U** · 完整平倉 {len(s['completed'])} · fills {len(s['fills'])} · 目前未平倉 {s['open_positions']}",
            f"- 今日：Win {_pct(m['win_rate'])} · PF {_f(m['pf'])} · Avg R {_f(m['avg_r'],3)}",
            f"- 帳戶：Equity **{_f(s['equity'])}U** · 累計 Return **{_f(s['return_pct'])}%** · 累計 PF {_f(lm['pf'])} · DD {_pct(lm['max_dd_pct'])}",
            "",
            "**各幣損益**",
        ]
        if s["coin_rows"]:
            for z in s["coin_rows"]:
                status=[]
                if z["entries"]:status.append(f"今日進場 {z['entries']}")
                if z["roundtrips"]:status.append(f"完整平倉 {z['roundtrips']}")
                if z["open"]:status.append(f"未平倉 {z['open']}")
                extra=" · ".join(status) if status else f"fills {z['fills']}"
                lines.append(f"- **{z['symbol']}**：實現 **{_f(z['realized'])}U** · {extra}")
        else:lines.append("- 這個 24h 區間沒有建立或成交任何 Champion 倉位。")
        lines += ["","**今天學到 / 調整了什麼**",f"- 階段 `{s['stage']}` · 真實學習進度 **{s['learning_progress_pct']:.1f}%**（{delta:+.1f}pp）"]
        learned=False;ps=s["today_post_summary"]
        if ps["n"]:
            learned=True
            flagtxt="、".join(f"{k} {v}/{ps['n']}" for k,v in ps["flags"]) or "沒有集中型異常 flag"
            cap="N/A" if ps["avg_capture"] is None else _f(ps["avg_capture"],3)
            mfe="N/A" if ps["avg_mfe_r"] is None else _f(ps["avg_mfe_r"],2)+"R"
            lines.append(f"- Post-Trade 新完成 **{ps['n']}** 筆：{flagtxt} · Avg capture {cap} · Avg MFE {mfe}")
        if s["replay_runs"]:
            learned=True
            domains={}
            for r in s["replay_runs"]:domains[r.get("domain")]=domains.get(r.get("domain"),0)+1
            text="、".join(f"{str(k).upper()} {v}輪" for k,v in domains.items())
            lines.append(f"- Historical Replay：今日完成 **{len(s['replay_runs'])}** 輪（{text}），提出候選 {len(s['replay_proposals'])} 組。")
        for p in s["replay_proposals"][:3]:
            learned=True;changes=_diff(p.get("baseline_json"),p.get("params_json"));txt="；".join(changes) if changes else "候選參數已建立"
            lines.append(f"- Replay 候選 **{str(p.get('domain')).upper()}**：{_short(txt,650)} · confidence {_f(p.get('confidence'),2)}")
        if s["adjustments"]:
            learned=True
            for x in s["adjustments"][:5]:
                changes=_diff(x["before_json"],x["after_json"]);txt="；".join(changes) if changes else "Stage/狀態變更"
                result="已採用" if x["accepted"] else "測試/候選"
                lines.append(f"- **{x['kind']} / {result}**：{_short(txt,700)}")
        if s["challenger"]:
            learned=True;c=s["challenger"]
            lines.append(f"- Active Challenger **{str(c['domain']).upper()}**：{c['trades']} trades · PF {_f(c['pf'])} · expectancy {_f(c['expectancy'])}U · DD {_pct(c['dd'])}")
        if s.get("ai_model"):
            learned=True;a=s["ai_model"]
            lines.append(f"- AI Tail Model：{a.get('status')} · labeled {a.get('labeled')} · pending {a.get('pending')} · Long n={a.get('long_n')} / Short n={a.get('short_n')}")
        if not learned:lines.append("- 今日沒有新的有效學習結論或參數調整；繼續累積這套策略自己的 closed-K evidence。")
        return lines
    async def _send_embed(self,title,description,color):
        url=settings.discord_webhook_url.strip();sep="&" if "?" in url else "?";url=f"{url}{sep}wait=true"
        payload={"username":"Crypto Strategy Lab","allowed_mentions":{"parse":[]},"embeds":[{"title":title[:256],"description":description[:4096],"color":int(color),"footer":{"text":f"Taiwan daily strategy report · cutoff {settings.daily_report_hour:02d}:{settings.daily_report_minute:02d} · PnL + learning only"}}]}
        last=None
        for attempt in range(5):
            r=await self.client.post(url,json=payload)
            if r.status_code==429:
                last=f"Discord 429: {_short(r.text,300)}"
                try:retry=float((r.json() or {}).get("retry_after") or 1)
                except Exception:retry=1
                await asyncio.sleep(max(1,min(retry,60)));continue
            if 200<=r.status_code<300:
                try:mid=(r.json() or {}).get("id")
                except Exception:mid=None
                await asyncio.sleep(SEND_SPACING_SEC);return mid
            last=f"Discord HTTP {r.status_code}: {_short(r.text,500)}";await asyncio.sleep(min(2**attempt,12))
        raise RuntimeError(last or "Discord webhook send failed")

daily_reporter=DailyDiscordReporter()
