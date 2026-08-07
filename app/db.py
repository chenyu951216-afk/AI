from __future__ import annotations
import json, os, sqlite3, threading, time
from typing import Any
from .config import settings

class DB:
    def __init__(self,path:str):
        self.path=path; parent=os.path.dirname(path)
        if parent: os.makedirs(parent,exist_ok=True)
        self.conn=sqlite3.connect(path,check_same_thread=False); self.conn.row_factory=sqlite3.Row; self.lock=threading.RLock(); self._archive_legacy_if_needed(); self._init()

    def _archive_legacy_if_needed(self):
        try:
            tables={r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "strategy_state" not in tables: return
            scols={r[1] for r in self.conn.execute("PRAGMA table_info(strategy_state)").fetchall()}
            pcols={r[1] for r in self.conn.execute("PRAGMA table_info(positions)").fetchall()} if "positions" in tables else set()
            if "description" in scols and "variant" in pcols: return
            stamp=int(time.time())
            for name in ["strategy_state","signals","positions","trades","adjustments","oi_snapshots"]:
                if name in tables:
                    self.conn.execute(f"ALTER TABLE {name} RENAME TO {name}_legacy_{stamp}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()

    def _init(self):
        with self.lock:
            self.conn.executescript("""
            PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS strategy_state(
              strategy TEXT PRIMARY KEY, display_name TEXT NOT NULL, description TEXT NOT NULL, stage TEXT NOT NULL,
              params_json TEXT NOT NULL, live_eligible INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
              final_since INTEGER, evidence_since INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS strategy_accounts(
              strategy TEXT NOT NULL, variant TEXT NOT NULL, balance REAL NOT NULL, initial_balance REAL NOT NULL,
              reset_at INTEGER NOT NULL, PRIMARY KEY(strategy,variant));
            CREATE TABLE IF NOT EXISTS challengers(
              strategy TEXT PRIMARY KEY, status TEXT NOT NULL, params_json TEXT NOT NULL, baseline_json TEXT NOT NULL,
              reason TEXT NOT NULL, started_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS signals(
              id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT NOT NULL, variant TEXT NOT NULL, symbol TEXT NOT NULL,
              side TEXT NOT NULL, score REAL NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL, reason TEXT NOT NULL,
              features_json TEXT NOT NULL, regime TEXT NOT NULL, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS positions(
              id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT NOT NULL, variant TEXT NOT NULL, symbol TEXT NOT NULL,
              side TEXT NOT NULL, qty REAL NOT NULL, entry REAL NOT NULL, initial_stop REAL NOT NULL, stop REAL NOT NULL,
              tp1 REAL NOT NULL, tp2 REAL NOT NULL, tp3 REAL NOT NULL, tp1_fraction REAL NOT NULL, tp2_fraction REAL NOT NULL,
              remaining_qty REAL NOT NULL, realized_pnl REAL NOT NULL DEFAULT 0, initial_risk_cash REAL NOT NULL,
              leverage REAL NOT NULL, opened_at INTEGER NOT NULL, stage TEXT NOT NULL, regime TEXT NOT NULL,
              tp1_hit INTEGER NOT NULL DEFAULT 0, tp2_hit INTEGER NOT NULL DEFAULT 0, max_favorable REAL NOT NULL DEFAULT 0,
              min_favorable REAL NOT NULL DEFAULT 0, UNIQUE(strategy,variant,symbol));
            CREATE TABLE IF NOT EXISTS trades(
              id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER NOT NULL, strategy TEXT NOT NULL, variant TEXT NOT NULL,
              symbol TEXT NOT NULL, side TEXT NOT NULL, entry REAL NOT NULL, exit REAL NOT NULL, qty REAL NOT NULL,
              gross_pnl REAL NOT NULL, fees REAL NOT NULL, net_pnl REAL NOT NULL, reason TEXT NOT NULL,
              initial_risk_cash REAL NOT NULL, opened_at INTEGER NOT NULL, closed_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS adjustments(
              id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT NOT NULL, kind TEXT NOT NULL, before_json TEXT NOT NULL,
              after_json TEXT NOT NULL, reason TEXT NOT NULL, accepted INTEGER NOT NULL, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS risk_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT, variant TEXT, symbol TEXT, code TEXT NOT NULL,
              detail TEXT NOT NULL, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS live_orders(
              id INTEGER PRIMARY KEY AUTOINCREMENT, paper_position_id INTEGER, strategy TEXT NOT NULL, symbol TEXT NOT NULL,
              side TEXT NOT NULL, qty REAL NOT NULL, entry_price REAL NOT NULL DEFAULT 0, notional REAL NOT NULL DEFAULT 0,
              leverage REAL NOT NULL DEFAULT 1, client_oid TEXT NOT NULL UNIQUE, exchange_order_id TEXT,
              status TEXT NOT NULL, detail_json TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS runtime_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS oi_snapshots(symbol TEXT NOT NULL, ts INTEGER NOT NULL, oi REAL NOT NULL, PRIMARY KEY(symbol,ts));
            CREATE TABLE IF NOT EXISTS regime_history(ts INTEGER PRIMARY KEY, regime TEXT NOT NULL, score REAL NOT NULL, btc_price REAL, btc_adx REAL, btc_atr_pct REAL);
            CREATE TABLE IF NOT EXISTS system_events(id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, created_at INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_trades_sv ON trades(strategy,variant,closed_at);
            CREATE INDEX IF NOT EXISTS idx_positions_sv ON positions(strategy,variant);
            CREATE INDEX IF NOT EXISTS idx2_signals_created ON signals(created_at);
            CREATE INDEX IF NOT EXISTS idx_risk_created ON risk_events(created_at);
            """)
            cols={r[1] for r in self.conn.execute("PRAGMA table_info(live_orders)").fetchall()}
            for name,ddl in {"entry_price":"REAL NOT NULL DEFAULT 0","notional":"REAL NOT NULL DEFAULT 0","leverage":"REAL NOT NULL DEFAULT 1"}.items():
                if name not in cols: self.conn.execute(f"ALTER TABLE live_orders ADD COLUMN {name} {ddl}")
            self.conn.commit()
    def execute(self,sql:str,args:tuple=())->sqlite3.Cursor:
        with self.lock:
            c=self.conn.execute(sql,args); self.conn.commit(); return c
    def query(self,sql:str,args:tuple=())->list[dict[str,Any]]:
        with self.lock: return [dict(r) for r in self.conn.execute(sql,args).fetchall()]
    def one(self,sql:str,args:tuple=())->dict[str,Any]|None:
        x=self.query(sql,args); return x[0] if x else None
    def ensure_strategy(self,name:str,display:str,description:str,params:dict):
        now=int(time.time()*1000)
        self.execute("INSERT INTO strategy_state(strategy,display_name,description,stage,params_json,evidence_since,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(strategy) DO NOTHING",(name,display,description,"EARLY",json.dumps(params),now,now))
        self.ensure_account(name,"champion")
    def ensure_account(self,strategy:str,variant:str,reset:bool=False):
        now=int(time.time()*1000)
        if reset:
            self.execute("INSERT INTO strategy_accounts(strategy,variant,balance,initial_balance,reset_at) VALUES(?,?,?,?,?) ON CONFLICT(strategy,variant) DO UPDATE SET balance=excluded.balance,initial_balance=excluded.initial_balance,reset_at=excluded.reset_at",(strategy,variant,settings.initial_paper_equity,settings.initial_paper_equity,now))
        else:
            self.execute("INSERT INTO strategy_accounts(strategy,variant,balance,initial_balance,reset_at) VALUES(?,?,?,?,?) ON CONFLICT(strategy,variant) DO NOTHING",(strategy,variant,settings.initial_paper_equity,settings.initial_paper_equity,now))
    def account(self,strategy:str,variant:str="champion"):
        return self.one("SELECT * FROM strategy_accounts WHERE strategy=? AND variant=?",(strategy,variant))
    def adjust_balance(self,strategy:str,variant:str,delta:float):
        self.execute("UPDATE strategy_accounts SET balance=balance+? WHERE strategy=? AND variant=?",(delta,strategy,variant))
    def state(self,strategy:str): return self.one("SELECT * FROM strategy_state WHERE strategy=?",(strategy,))
    def update_strategy(self,strategy:str,**kwargs):
        if not kwargs:return
        allowed={"stage","params_json","live_eligible","enabled","final_since","evidence_since"}; parts=[]; vals=[]
        for k,v in kwargs.items():
            if k not in allowed: continue
            if k=="params_json" and isinstance(v,dict): v=json.dumps(v)
            if k in {"live_eligible","enabled"}: v=int(bool(v))
            parts.append(f"{k}=?"); vals.append(v)
        parts.append("updated_at=?"); vals.append(int(time.time()*1000)); vals.append(strategy)
        self.execute(f"UPDATE strategy_state SET {','.join(parts)} WHERE strategy=?",tuple(vals))
    def runtime_get(self,key:str,default:str="") -> str:
        r=self.one("SELECT value FROM runtime_settings WHERE key=?",(key,)); return str(r["value"]) if r else default
    def runtime_set(self,key:str,value:str):
        self.execute("INSERT INTO runtime_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,str(value),int(time.time()*1000)))
    def log_adjustment(self,strategy:str,kind:str,before:dict,after:dict,reason:str,accepted:bool):
        self.execute("INSERT INTO adjustments(strategy,kind,before_json,after_json,reason,accepted,created_at) VALUES(?,?,?,?,?,?,?)",(strategy,kind,json.dumps(before),json.dumps(after),reason,int(accepted),int(time.time()*1000)))
    def risk_event(self,code:str,detail:str,strategy:str|None=None,variant:str|None=None,symbol:str|None=None):
        self.execute("INSERT INTO risk_events(strategy,variant,symbol,code,detail,created_at) VALUES(?,?,?,?,?,?)",(strategy,variant,symbol,code,detail,int(time.time()*1000)))
    def event(self,kind:str,detail:str,level:str="INFO"):
        self.execute("INSERT INTO system_events(level,kind,detail,created_at) VALUES(?,?,?,?)",(level,kind,detail,int(time.time()*1000)))

db=DB(settings.db_path)
