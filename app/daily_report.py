from __future__ import annotations
import asyncio,json,time
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from typing import Any
import httpx
from .config import settings
from .db import db
from .learning import completed_roundtrips,metrics,robust_windows,sample_days,profit_concentration,symbol_count,exit_diagnostics,learning_progress,STOP_PARAM
from .risk import risk_manager
from .market import market
from .strategies import SPEC_MAP
from .ai_hunter import ai_hunter

REPORT_COLOR=0xFF8535
GOOD_COLOR=0x4FE09A
BAD_COLOR=0xFF6475
MAX_DESCRIPTION=3500
SEND_SPACING_SEC=1.0
RETRY_DELAY_SEC=600
STALE_RUNNING_SEC=1800


def _f(v,d=2):
    try:return f"{float(v):,.{d}f}"
    except Exception:return "0"

def _pct(v,d=1):
    try:return f"{float(v)*100:.{d}f}%"
    except Exception:return "0%"

def _local_dt(ms:int,tz:ZoneInfo)->datetime:return datetime.fromtimestamp(ms/1000,tz)
def _hm(ms:int,tz:ZoneInfo)->str:return _local_dt(ms,tz).strftime("%m/%d %H:%M")
def _short(s:Any,n:int=260)->str:
    x=str(s or "").replace("\n"," ").strip()
    return x if len(x)<=n else x[:n-1]+"…"

def _json(x):
    try:return json.loads(x or "{}")
    except Exception:return {}

def _diff(before,after):
    b=_json(before) if isinstance(before,str) else (before or {});a=_json(after) if isinstance(after,str) else (after or {});out=[]
    for k in sorted(set(b)|set(a)):
        if b.get(k)!=a.get(k):out.append(f"{k}: {b.get(k)} → {a.get(k)}")
    return out

def _chunks(lines,max_chars=MAX_DESCRIPTION):
    out=[];cur=[];size=0
    for raw in lines:
        line=str(raw)
        if len(line)>max_chars:
            if cur:out.append("\n".join(cur));cur=[];size=0
            for i in range(0,len(line),max_chars):out.append(line[i:i+max_chars])
            continue
        add=len(line)+(1 if cur else 0)
        if cur and size+add>max_chars:
            out.append("\n".join(cur));cur=[line];size=len(line)
        else:cur.append(line);size+=add
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
        now=now or datetime.now(self.tz);target=now.replace(hour=settings.daily_report_hour,minute=settings.daily_report_minute,second=0,microsecond=0)
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
        now=datetime.now(self.tz);today_target=now.replace(hour=settings.daily_report_hour,minute=settings.daily_report_minute,second=0,microsecond=0)
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
        self.busy=True;self.last_error=None;started=int(time.time()*1000);start_ms=int(start.timestamp()*1000);end_ms=int(end.timestamp()*1000)
        row=db.one("SELECT attempts FROM daily_discord_reports WHERE report_key=?",(key,));attempts=int((row or {}).get("attempts") or 0)+1
        db.execute("INSERT INTO daily_discord_reports(report_key,window_start,window_end,status,attempts,started_at,error)VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(report_key) DO UPDATE SET status='RUNNING',attempts=excluded.attempts,started_at=excluded.started_at,error=NULL",(key,start_ms,end_ms,"RUNNING",attempts,started))
        try:
            payload=self._collect(start_ms,end_ms,key)
            message_ids=[]
            summary=payload["summary"]
            header=[
                f"**統計區間** {start.strftime('%Y/%m/%d %H:%M')} → {end.strftime('%Y/%m/%d %H:%M')}（{settings.timezone}）",
                f"**開始統整** {datetime.now(self.tz).strftime('%Y/%m/%d %H:%M:%S')}；截止時間固定為 17:00，17:00 後的新資料歸下一份日報。",
                f"**12 策略合計** 實現損益 `{_f(summary['realized_pnl'])} U` · 成交 fills `{summary['fills']}` · 完整平倉 `{summary['roundtrips']}` · 新進場 `{summary['entries']}`",
                f"**研究** Post-Trade 完成 `{summary['post_studies']}` · 學習/調整事件 `{summary['adjustments']}` · 策略風控事件 `{summary['risk_events']}`",
                f"**系統** WARN/ERROR `{summary['system_issues']}` · 報告會逐策略獨立發送，不混用學習資料。",
            ]
            if summary["system_issue_lines"]:
                header.append("\n**今日系統問題**")
                header.extend(summary["system_issue_lines"])
            mid=await self._send_embed(f"📊 Crypto Strategy Lab 每日研究報告 · {key}","\n".join(header),GOOD_COLOR if summary["realized_pnl"]>=0 else BAD_COLOR)
            if mid:message_ids.append(mid)
            for i,s in enumerate(payload["strategies"],1):
                lines=self._strategy_lines(s)
                chunks=_chunks(lines)
                for part,desc in enumerate(chunks,1):
                    suffix=f" · {part}/{len(chunks)}" if len(chunks)>1 else ""
                    mid=await self._send_embed(f"{i:02d}/12 · {s['display_name']}{suffix}",desc,GOOD_COLOR if s["daily_realized_pnl"]>=0 else BAD_COLOR)
                    if mid:message_ids.append(mid)
            completed=int(time.time()*1000);db.execute("UPDATE daily_discord_reports SET status='SENT',completed_at=?,error=NULL,payload_json=?,message_ids_json=? WHERE report_key=?",(completed,json.dumps(payload,ensure_ascii=False),json.dumps(message_ids),key));db.event("DAILY_REPORT_SENT",f"{key} sent {len(message_ids)} Discord messages")
            return {"status":"SENT","report_key":key,"messages":len(message_ids)}
        except Exception as e:
            self.last_error=f"{type(e).__name__}: {e}";db.execute("UPDATE daily_discord_reports SET status='FAILED',error=? WHERE report_key=?",(self.last_error,key));db.event("DAILY_REPORT_FAILED",f"{key}: {self.last_error}","ERROR");raise
        finally:self.busy=False
    def _previous_progress(self,end_ms):
        r=db.one("SELECT payload_json FROM daily_discord_reports WHERE status='SENT' AND window_end<? ORDER BY window_end DESC LIMIT 1",(end_ms,));out={}
        if not r:return out
        try:
            p=json.loads(r["payload_json"] or "{}");out={x["strategy"]:float(x.get("learning_progress_pct") or 0) for x in p.get("strategies",[])}
        except Exception:pass
        return out
    def _completed_window(self,strategy,start_ms,end_ms):
        return db.query("""SELECT t.position_id,t.strategy,t.variant,MIN(t.symbol)symbol,MIN(t.side)side,SUM(t.gross_pnl)gross_pnl,SUM(t.fees)fees,SUM(t.net_pnl)net_pnl,MAX(t.closed_at)closed_at,MIN(t.opened_at)opened_at,MAX(t.initial_risk_cash)initial_risk_cash FROM trades t WHERE t.strategy=? AND t.variant='champion' AND NOT EXISTS(SELECT 1 FROM positions p WHERE p.id=t.position_id) GROUP BY t.position_id,t.strategy,t.variant HAVING MAX(t.closed_at)>=? AND MAX(t.closed_at)<? ORDER BY closed_at""",(strategy,start_ms,end_ms))
    def _collect(self,start_ms,end_ms,key):
        prev=self._previous_progress(end_ms);strategies=[];sum_pnl=0.;sum_fills=sum_round=sum_entries=sum_post=sum_adj=sum_risk=0
        states=db.query("SELECT * FROM strategy_state ORDER BY strategy")
        for state in states:
            name=state["strategy"];params=_json(state["params_json"]);since=int(state.get("evidence_since") or 0);all_tr=completed_roundtrips(name,"champion",100000,since);m=metrics(all_tr);diag=exit_diagnostics(name,"champion",since);progress=learning_progress(all_tr,m,diag);acc=db.account(name,"champion");equity=risk_manager.equity(name,"champion",market.prices());fills=db.query("SELECT * FROM trades WHERE strategy=? AND variant='champion' AND closed_at>=? AND closed_at<? ORDER BY closed_at,id",(name,start_ms,end_ms));completed=self._completed_window(name,start_ms,end_ms);cm=metrics(completed);entries=db.query("""SELECT position_id,MIN(symbol)symbol,MIN(side)side,MIN(entry)entry,MIN(opened_at)opened_at FROM trades WHERE strategy=? AND variant='champion' AND opened_at>=? AND opened_at<? GROUP BY position_id UNION ALL SELECT id position_id,symbol,side,entry,opened_at FROM positions WHERE strategy=? AND variant='champion' AND opened_at>=? AND opened_at<? ORDER BY opened_at""",(name,start_ms,end_ms,name,start_ms,end_ms));open_pos=db.query("SELECT * FROM positions WHERE strategy=? AND variant='champion' ORDER BY opened_at",(name,));posts=db.query("SELECT * FROM post_trade_studies WHERE strategy=? AND variant='champion' AND completed_at>=? AND completed_at<? ORDER BY completed_at",(name,start_ms,end_ms));adjustments=db.query("SELECT * FROM adjustments WHERE strategy=? AND created_at>=? AND created_at<? ORDER BY created_at,id",(name,start_ms,end_ms));risks=db.query("SELECT * FROM risk_events WHERE strategy=? AND created_at>=? AND created_at<? ORDER BY created_at,id",(name,start_ms,end_ms));signals=int((db.one("SELECT COUNT(*)n FROM signals WHERE strategy=? AND variant='champion' AND created_at>=? AND created_at<?",(name,start_ms,end_ms)) or {"n":0})["n"]);challenger=db.one("SELECT * FROM challengers WHERE strategy=? AND status='ACTIVE'",(name,));cp=None
            if challenger:
                ctr=completed_roundtrips(name,"challenger",10000,int(challenger["started_at"]));cmet=metrics(ctr);cp={"domain":challenger.get("domain"),"started_at":challenger["started_at"],"trades":cmet["n"],"pf":cmet["pf"],"expectancy":cmet["expectancy"],"dd":cmet["max_dd_pct"],"reason":challenger["reason"],"params":_json(challenger["params_json"])}
            day_pnl=sum(float(x["net_pnl"]) for x in fills);sum_pnl+=day_pnl;sum_fills+=len(fills);sum_round+=len(completed);sum_entries+=len(entries);sum_post+=len(posts);sum_adj+=len(adjustments);sum_risk+=len(risks)
            strategies.append({"strategy":name,"display_name":state["display_name"],"description":state["description"],"stage":state["stage"],"signal_tf":state.get("signal_tf"),"live_eligible":bool(state["live_eligible"]),"enabled":bool(state["enabled"]),"params":params,"daily_realized_pnl":day_pnl,"fills":fills,"completed":completed,"completed_metrics":cm,"entries":entries,"open_positions":open_pos,"posts":posts,"adjustments":adjustments,"risks":risks,"signals":signals,"balance":float((acc or {}).get("balance") or 0),"equity":equity,"return_pct":((equity/float((acc or {}).get("initial_balance") or settings.initial_paper_equity))-1)*100,"lifetime_metrics":m,"learning_progress_pct":float(progress["pct"]),"progress_delta":float(progress["pct"])-float(prev.get(name,progress["pct"])),"progress":progress,"exit_diag":diag,"challenger":cp,"ai_model":ai_hunter.status() if name=="ai_extreme_hunter" else None,"data_health":market.cg.health() if name=="liquidation_magnet" else None})
        sysissues=db.query("SELECT * FROM system_events WHERE created_at>=? AND created_at<? AND level IN ('WARN','ERROR') ORDER BY created_at DESC LIMIT 30",(start_ms,end_ms));summary={"realized_pnl":sum_pnl,"fills":sum_fills,"roundtrips":sum_round,"entries":sum_entries,"post_studies":sum_post,"adjustments":sum_adj,"risk_events":sum_risk,"system_issues":len(sysissues),"system_issue_lines":[f"- `{x['level']}` {_short(x['kind'],50)}：{_short(x['detail'],220)}" for x in sysissues[:8]]}
        return {"report_key":key,"window_start":start_ms,"window_end":end_ms,"generated_at":int(time.time()*1000),"summary":summary,"strategies":strategies}
    def _strategy_lines(self,s):
        tz=self.tz;m=s["completed_metrics"];lm=s["lifetime_metrics"];p=s["params"];prog=s["progress"];delta=s["progress_delta"];stop_key=STOP_PARAM.get(s["strategy"]);lines=[
            f"**策略** `{s['strategy']}` · {s['description']}",
            f"**階段 / 進度** `{s['stage']}` · 真實學習成熟度 **{s['learning_progress_pct']:.1f}%**（較上一份 {delta:+.1f}pp） · LIVE eligible={'YES' if s['live_eligible'] else 'NO'}",
            f"**目前弱項** "+(" · ".join(f"{x['name']} {x['pct']:.0f}%" for x in prog.get('weakest',[])) or "尚無足夠樣本"),
            "",
            "**① 今日績效**",
            f"- 24h 實現損益：**{_f(s['daily_realized_pnl'])} U**；成交 fills {len(s['fills'])}；完整平倉 {len(s['completed'])}；新進場 {len(s['entries'])}；策略訊號 {s['signals']}",
            f"- 今日完整平倉：Win {_pct(m['win_rate'])} · PF {_f(m['pf'])} · Avg R {_f(m['avg_r'],3)} · Expectancy {_f(m['expectancy'])} U",
            f"- 帳戶快照：Balance {_f(s['balance'])}U · Equity {_f(s['equity'])}U · 累計 Return {_f(s['return_pct'])}% · 累計 PF {_f(lm['pf'])} · DD {_pct(lm['max_dd_pct'])}",
            "",
            "**② 今日進場 / 成交明細**",
        ]
        if s["entries"]:
            seen=set()
            for x in s["entries"]:
                k=x["position_id"]
                if k in seen:continue
                seen.add(k);lines.append(f"- `{_hm(int(x['opened_at']),tz)}` {x['symbol']} **{str(x['side']).upper()}** ENTRY {_f(x['entry'],8)} · position #{k}")
        else:lines.append("- 今日沒有新進場。")
        if s["fills"]:
            lines.append("**今日分批出場 / 平倉**")
            for x in s["fills"]:lines.append(f"- `{_hm(int(x['closed_at']),tz)}` #{x['position_id']} {x['symbol']} {str(x['side']).upper()} **{x['reason']}** · {_f(x['entry'],8)} → {_f(x['exit'],8)} · qty {_f(x['qty'],6)} · net **{_f(x['net_pnl'])}U**")
        if s["open_positions"]:
            lines.append("**17:00 統整時仍持有**")
            for x in s["open_positions"]:lines.append(f"- #{x['id']} {x['symbol']} {str(x['side']).upper()} · entry {_f(x['entry'],8)} · current stop {_f(x['stop'],8)} · TP {_f(x['tp1'],8)}/{_f(x['tp2'],8)}/{_f(x['tp3'],8)} · remaining {_f(x['remaining_qty'],6)}")
        else:lines.append("- 統整時沒有未平倉 Champion 倉位。")
        lines += ["","**③ 今日 Post-Trade：這套策略學到了什麼**"]
        if s["posts"]:
            for x in s["posts"]:
                a=_json(x["analysis_json"]);lines.append(f"- `{_hm(int(x['completed_at']),tz)}` {x['symbol']}：{_short(a.get('reason') or x['analysis_json'],650)}")
        else:lines.append("- 今日沒有新的完整出場後研究完成；仍可能有 FOLLOWING 樣本正在等後續已收 K。")
        lines += ["","**④ 今日策略 / 止損止盈 / 倉位學習調整**"]
        if s["adjustments"]:
            for x in s["adjustments"]:
                changes=_diff(x["before_json"],x["after_json"]);change_text="；".join(changes) if changes else "無參數差異（Stage/狀態事件）";result="採用" if x["accepted"] else "測試/拒絕";lines.append(f"- `{_hm(int(x['created_at']),tz)}` **{x['kind']} / {result}**：{_short(change_text,850)} · 原因：{_short(x['reason'],500)}")
        else:lines.append("- 今日沒有建立、採用或拒絕新的 Challenger / Stage 變更。")
        lines += ["","**⑤ 現行 Champion 出場邏輯**"]
        if stop_key and stop_key in p:lines.append(f"- Full Stop 核心參數 `{stop_key}={p.get(stop_key)}`；實際價格仍由這套策略自己的結構/ATR 計算。")
        lines.append(f"- SL1 {p.get('sl1_r')}R / {_pct(p.get('sl1_fraction'))} · TP1/2/3 {p.get('tp1_r')}R / {p.get('tp2_r')}R / {p.get('tp3_r')}R · TP 分批 {_pct(p.get('tp1_fraction'))} / {_pct(p.get('tp2_fraction'))}")
        lines.append(f"- BE：{p.get('breakeven_trigger_r')}R 啟動 → +{p.get('breakeven_offset_r')}R；Trailing：{p.get('trail_start_r')}R 啟動 / 距離 {p.get('trail_r')}R")
        lines += ["","**⑥ 目前學習狀況**"]
        c=s["challenger"]
        if c:lines.append(f"- Active Challenger：**{str(c['domain']).upper()}** · trades {c['trades']} · PF {_f(c['pf'])} · expectancy {_f(c['expectancy'])}U · DD {_pct(c['dd'])} · {_short(c['reason'],550)}")
        else:lines.append("- 目前沒有 Active Challenger；Champion 繼續累積自己策略的 closed-K evidence。")
        d=s["exit_diag"]
        if d.get("n"):lines.append(f"- 出場研究累計 {d['n']} 筆 · Avg capture {_f(d['avg_capture'],3)} · Avg MFE {_f(d['avg_mfe_r'],2)}R · 主要 flags：{_short(d.get('ratios'),450)}")
        if s.get("ai_model"):
            a=s["ai_model"];lines.append(f"- AI Tail Model：{a.get('status')} · labeled {a.get('labeled')} / pending {a.get('pending')} · Long n={a.get('long_n')} Short n={a.get('short_n')} · threshold L={a.get('long_threshold')} S={a.get('short_threshold')} · max up={a.get('max_up_pct')} max down={a.get('max_down_pct')}")
        if s.get("data_health"):
            h=s["data_health"];lines.append(f"- CoinGlass：{h.get('status')} / liquidation {h.get('liquidation_status')} · source={h.get('liquidation_source')} · error={_short(h.get('liquidation_error') or h.get('last_error'),300) or 'none'}")
        lines += ["","**⑦ 今日策略問題 / 風控事件**"]
        if s["risks"]:
            for x in s["risks"]:lines.append(f"- `{_hm(int(x['created_at']),tz)}` **{x['code']}** {x.get('symbol') or ''}：{_short(x['detail'],500)}")
        else:lines.append("- 今日沒有記錄到這套策略的 risk/error event。")
        return lines
    async def _send_embed(self,title,description,color):
        url=settings.discord_webhook_url.strip();sep="&" if "?" in url else "?";url=f"{url}{sep}wait=true";payload={"username":"Crypto Strategy Lab","allowed_mentions":{"parse":[]},"embeds":[{"title":title[:256],"description":description[:4096],"color":int(color),"footer":{"text":f"Taiwan daily research · cutoff {settings.daily_report_hour:02d}:{settings.daily_report_minute:02d} · closed-K evidence"}}]}
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
