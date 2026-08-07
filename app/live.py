from __future__ import annotations
import asyncio,base64,hashlib,hmac,json,time,uuid
from decimal import Decimal,ROUND_DOWN
from urllib.parse import urlencode
import httpx
from .config import settings
from .db import db

class BitgetLiveAdapter:
    def __init__(self):
        self.client=httpx.AsyncClient(base_url=settings.bitget_base_url,timeout=15);self.contract_cache={};self.contract_cache_at=0.;self.last_positions=[];self.last_sync=0.;self.last_sync_error=None;self.last_protection_verify=0.
    def credentials_ready(self):return bool(settings.bitget_api_key and settings.bitget_api_secret and settings.bitget_passphrase)
    def web_master(self):return db.runtime_get("live_master_enabled","false").lower()=="true"
    def set_web_master(self,enabled):db.runtime_set("live_master_enabled","true" if enabled else "false")
    def gate_status(self):
        eligible=int((db.one("SELECT COUNT(*)n FROM strategy_state WHERE live_eligible=1 AND live_manual_enabled=1 AND stage='FINAL' AND enabled=1") or {"n":0})["n"]);ready=bool(settings.live_trading_allowed and self.web_master() and self.credentials_ready() and eligible>0)
        return {"deployment_allowed":settings.live_trading_allowed,"web_master_enabled":self.web_master(),"credentials_ready":self.credentials_ready(),"eligible_strategies":eligible,"ready":ready}
    def _headers(self,method,path,body="",query=""):
        ts=str(int(time.time()*1000));msg=ts+method.upper()+path+("?"+query if query else "")+body;sig=base64.b64encode(hmac.new(settings.bitget_api_secret.encode(),msg.encode(),hashlib.sha256).digest()).decode();return {"ACCESS-KEY":settings.bitget_api_key,"ACCESS-SIGN":sig,"ACCESS-PASSPHRASE":settings.bitget_passphrase,"ACCESS-TIMESTAMP":ts,"Content-Type":"application/json","locale":"en-US"}
    async def _private(self,method,path,params=None,payload=None):
        params=params or {};payload=payload or {};query=urlencode(params);body=json.dumps(payload,separators=(",",":")) if payload else "";h=self._headers(method,path,body,query);r=await (self.client.get(path,params=params,headers=h) if method=="GET" else self.client.post(path,content=body,headers=h));r.raise_for_status();j=r.json()
        if j.get("code")!="00000":raise RuntimeError(f"Bitget {path}: {j.get('code')} {j.get('msg')}")
        return j.get("data")
    async def _public(self,path,params):
        r=await self.client.get(path,params=params);r.raise_for_status();j=r.json()
        if j.get("code")!="00000":raise RuntimeError(j)
        return j.get("data")
    async def contracts(self):
        if time.time()-self.contract_cache_at<600 and self.contract_cache:return self.contract_cache
        rows=await self._public("/api/v2/mix/market/contracts",{"productType":settings.bitget_product_type});self.contract_cache={r["symbol"]:r for r in rows or []};self.contract_cache_at=time.time();return self.contract_cache
    async def ticker(self,symbol):
        rows=await self._public("/api/v2/mix/market/ticker",{"symbol":symbol,"productType":settings.bitget_product_type});return (rows or [{}])[0] if isinstance(rows,list) else (rows or {})
    async def account(self,symbol):return await self._private("GET","/api/v2/mix/account/account",{"symbol":symbol,"productType":settings.bitget_product_type,"marginCoin":settings.bitget_margin_coin})
    async def positions(self):return await self._private("GET","/api/v2/mix/position/all-position",{"productType":settings.bitget_product_type,"marginCoin":settings.bitget_margin_coin})
    async def pending_plans(self,symbol):
        d=await self._private("GET","/api/v2/mix/order/orders-plan-pending",{"symbol":symbol,"planType":"profit_loss","productType":settings.bitget_product_type,"limit":"100"});return (d or {}).get("entrustedList",[]) if isinstance(d,dict) else []
    async def set_leverage(self,symbol,leverage,side):
        p={"symbol":symbol,"productType":settings.bitget_product_type,"marginCoin":settings.bitget_margin_coin,"leverage":str(leverage)}
        if settings.bitget_position_mode=="hedge_mode" and settings.bitget_margin_mode=="isolated":p["holdSide"]="long" if side=="long" else "short"
        return await self._private("POST","/api/v2/mix/account/set-leverage",payload=p)
    @staticmethod
    def _floor_step(value,step,places):
        a=Decimal(str(value));inc=Decimal(str(step or "1"));q=(a/inc).to_integral_value(rounding=ROUND_DOWN)*inc;return q.quantize(Decimal(1).scaleb(-places),rounding=ROUND_DOWN)
    async def normalize(self,symbol,qty,price,leverage):
        c=(await self.contracts()).get(symbol)
        if not c:raise RuntimeError("contract config missing")
        if c.get("symbolStatus") not in {"normal","listed"}:raise RuntimeError(f"symbol not tradable: {c.get('symbolStatus')}")
        lev=min(float(leverage),float(c.get("maxLever") or settings.hard_max_leverage),settings.hard_max_leverage);places=int(c.get("volumePlace") or 6);step=c.get("sizeMultiplier") or str(10**-places);q=self._floor_step(qty,step,places);mn=Decimal(str(c.get("minTradeNum") or 0));mu=Decimal(str(c.get("minTradeUSDT") or 0))
        if q<mn:q=self._floor_step(float(mn),step,places)
        if q*Decimal(str(price))<mu:q=self._floor_step(float(mu/Decimal(str(price))*Decimal("1.01")),step,places)
        q=min(q,Decimal(str(c.get("maxMarketOrderQty") or 1e30)));return str(q),lev,c
    def _position_size(self,row):
        try:return abs(float(row.get("total") or row.get("available") or row.get("holdVolume") or 0))
        except Exception:return 0.
    def _position_notional(self,row):
        q=self._position_size(row)
        for k in ["markPrice","marketPrice","averageOpenPrice","openPriceAvg"]:
            try:
                p=float(row.get(k) or 0)
                if p>0:return q*p
            except Exception:pass
        return 0.
    def _acct_num(self,a,*keys):
        for k in keys:
            try:
                v=float((a or {}).get(k) or 0)
                if v>0:return v
            except Exception:pass
        return 0.
    def _quality(self,strategy):
        rows=db.query("""SELECT SUM(net_pnl)pnl,SUM(CASE WHEN net_pnl>0 THEN net_pnl ELSE 0 END)gw,-SUM(CASE WHEN net_pnl<0 THEN net_pnl ELSE 0 END)gl,COUNT(*)n FROM (SELECT position_id,SUM(net_pnl)net_pnl FROM trades WHERE strategy=? AND variant='champion' AND NOT EXISTS(SELECT 1 FROM positions p WHERE p.id=trades.position_id) GROUP BY position_id ORDER BY MAX(closed_at) DESC LIMIT 120)""",(strategy,));r=rows[0] if rows else {};pf=float(r.get("gw") or 0)/max(float(r.get("gl") or 0),1e-9);n=int(r.get("n") or 0);q=.78+min(.42,max(-.18,(pf-1)*.22))+min(.12,n/500);return max(.65,min(1.35,q)),pf,n
    async def allocate(self,position,params,price,account,live_positions):
        equity=self._acct_num(account,"accountEquity","usdtEquity","equity","marginBalance","available");available=self._acct_num(account,"available","crossedMaxAvailable","maxTransferOut")
        if equity<=0:equity=max(available,1)
        if available<=0:raise PermissionError("Bitget account has no positive available margin")
        if sum(1 for p in live_positions if self._position_size(p)>0)>=settings.live_max_concurrent_positions:raise PermissionError("shared live portfolio max concurrent positions reached")
        total=sum(self._position_notional(p) for p in live_positions);quality,pf,n=self._quality(position["strategy"]);stop_pct=abs(float(position["entry"])-float(position["initial_stop"]))/max(float(position["entry"]),1e-12)
        if stop_pct<=0:raise RuntimeError("invalid stop distance")
        learned_lev=float(params.get("leverage",2));lev=min(settings.hard_max_leverage,max(1.,learned_lev*min(1.15,quality)));risk_pct=min(settings.live_max_risk_pct,settings.hard_max_risk_per_trade,settings.live_base_risk_pct*quality);risk_cash=equity*risk_pct;by_risk=risk_cash/stop_pct;order_cap=equity*settings.live_max_order_equity_pct;total_cap=max(0,equity*settings.live_max_total_notional_multiple-total);reserve=equity*settings.live_min_free_margin_pct;margin_cap=max(0,available-reserve)*lev*settings.live_available_balance_buffer;notional=max(0,min(by_risk,order_cap,total_cap,margin_cap))
        if notional<=0:raise PermissionError("shared portfolio allocator returned zero")
        return {"equity":equity,"available":available,"quality":quality,"pf":pf,"sample_n":n,"risk_pct":risk_pct,"risk_cash":risk_cash,"stop_pct":stop_pct,"leverage":lev,"notional":notional,"portfolio_notional_before":total,"portfolio_cap":equity*settings.live_max_total_notional_multiple}
    def _hold_side(self,side):return ("long" if side=="long" else "short") if settings.bitget_position_mode=="hedge_mode" else ("buy" if side=="long" else "sell")
    async def _wait_position(self,symbol,tries=8):
        for _ in range(tries):
            rows=await self.positions()
            for p in rows or []:
                if str(p.get("symbol") or "").upper()==symbol.upper() and self._position_size(p)>0:return p
            await asyncio.sleep(.45)
        return None
    def _plan_trigger(self,p,role):
        keys=["stopLossTriggerPrice","triggerPrice"] if role in {"STOP","SL1"} else ["stopSurplusTriggerPrice","triggerPrice"]
        for k in keys:
            try:
                v=float(p.get(k) or 0)
                if v>0:return v
            except Exception:pass
        return 0.
    def _near(self,a,b):return a>0 and b>0 and abs(a-b)/max(abs(b),1e-12)<.002
    def _upsert_protection(self,live_order_id,role,order_id,client_oid,trigger,size,status="LIVE",detail=None):
        now=int(time.time()*1000);db.execute("""INSERT INTO live_protections(live_order_id,role,order_id,client_oid,trigger_price,size,status,detail_json,last_verified_at,created_at,updated_at)VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(live_order_id,role) DO UPDATE SET order_id=excluded.order_id,client_oid=excluded.client_oid,trigger_price=excluded.trigger_price,size=excluded.size,status=excluded.status,detail_json=excluded.detail_json,last_verified_at=excluded.last_verified_at,updated_at=excluded.updated_at""",(live_order_id,role,order_id,client_oid,trigger,size,status,json.dumps(detail or {}),now,now,now))
    async def _place_pos_protection(self,order,role,trigger):
        p={"marginCoin":settings.bitget_margin_coin,"productType":settings.bitget_product_type,"symbol":order["symbol"],"holdSide":self._hold_side(order["side"])};oid=f"{order['client_oid']}-{role.lower()}-guard-{uuid.uuid4().hex[:6]}"
        if role=="STOP":p.update({"stopLossTriggerPrice":str(trigger),"stopLossTriggerType":"mark_price","stopLossExecutePrice":"0","stopLossClientOid":oid})
        else:p.update({"stopSurplusTriggerPrice":str(trigger),"stopSurplusTriggerType":"mark_price","stopSurplusExecutePrice":"0","stopSurplusClientOid":oid})
        return oid,await self._private("POST","/api/v2/mix/order/place-pos-tpsl",payload=p)
    async def _place_partial_plan(self,order,role,trigger,size,plan_type):
        oid=f"{order['client_oid']}-{role.lower()}";p={"marginCoin":settings.bitget_margin_coin,"productType":settings.bitget_product_type,"symbol":order["symbol"],"planType":plan_type,"triggerPrice":str(trigger),"triggerType":"mark_price","executePrice":"0","holdSide":self._hold_side(order["side"]),"size":str(size),"clientOid":oid};return oid,await self._private("POST","/api/v2/mix/order/place-tpsl-order",payload=p)
    async def ensure_protections(self,order,paper_pos=None):
        detail=json.loads(order.get("detail_json") or "{}");plan=detail.get("plan") or {};entry=float((paper_pos or {}).get("entry") or plan.get("entry") or order.get("entry_price") or 0);initial_stop=float((paper_pos or {}).get("initial_stop") or plan.get("initial_stop") or plan.get("stop") or 0);stop=float((paper_pos or {}).get("stop") or plan.get("stop") or initial_stop);tp1=float((paper_pos or {}).get("tp1") or plan.get("tp1") or 0);tp2=float((paper_pos or {}).get("tp2") or plan.get("tp2") or 0);tp3=float((paper_pos or {}).get("tp3") or plan.get("tp3") or 0);params=json.loads((paper_pos or {}).get("params_json") or "{}") if paper_pos else plan.get("params",{});r=abs(entry-initial_stop);sg=1 if order["side"]=="long" else -1;sl1=entry-sg*r*float(params.get("sl1_r",plan.get("sl1_r",.62)));sl1f=float(params.get("sl1_fraction",plan.get("sl1_fraction",.18)));f1=float((paper_pos or {}).get("tp1_fraction") or plan.get("tp1_fraction") or .25);f2=float((paper_pos or {}).get("tp2_fraction") or plan.get("tp2_fraction") or .30)
        if stop<=0 or tp3<=0 or entry<=0:raise RuntimeError("missing protection plan prices")
        contract=(await self.contracts()).get(order["symbol"],{});places=int(contract.get("volumePlace") or 6);step=contract.get("sizeMultiplier") or str(10**-places);total=Decimal(str(order["qty"]));qsl=self._floor_step(float(total)*sl1f,step,places);q1=self._floor_step(float(total)*f1,step,places);q2=self._floor_step(float(total)*f2,step,places)
        required=[("STOP",stop,Decimal("0"),"pos_loss"),("TP3",tp3,Decimal("0"),"pos_profit")]
        if settings.live_partial_tp_enabled:required=[("STOP",stop,Decimal("0"),"pos_loss"),("SL1",sl1,qsl,"loss_plan"),("TP1",tp1,q1,"profit_plan"),("TP2",tp2,q2,"profit_plan"),("TP3",tp3,Decimal("0"),"pos_profit")]
        for attempt in range(max(1,settings.live_protection_retry)):
            pending=await self.pending_plans(order["symbol"]);dbp={x["role"]:x for x in db.query("SELECT * FROM live_protections WHERE live_order_id=?",(order["id"],))};missing=[]
            for role,trig,size,ptype in required:
                pr=dbp.get(role);found=None
                if pr:
                    for x in pending:
                        if (pr.get("order_id") and str(x.get("orderId"))==str(pr["order_id"])) or (pr.get("client_oid") and str(x.get("clientOid"))==str(pr["client_oid"])):found=x;break
                if not found:
                    for x in pending:
                        cid=str(x.get("clientOid") or "")
                        if cid.startswith(str(order["client_oid"])) and role.lower() in cid.lower() and self._near(self._plan_trigger(x,role),trig):found=x;break
                if found:self._upsert_protection(order["id"],role,str(found.get("orderId") or ""),str(found.get("clientOid") or ""),trig,float(size),"LIVE",found)
                else:missing.append((role,trig,size,ptype))
            if not missing:return True
            for role,trig,size,ptype in missing:
                try:
                    if role in {"STOP","TP3"}:
                        cid,res=await self._place_pos_protection(order,role,trig);self._upsert_protection(order["id"],role,"",cid,trig,0,"PENDING_VERIFY",res)
                    elif size>0:
                        cid,res=await self._place_partial_plan(order,role,trig,size,ptype);oid=(res or {}).get("orderId") if isinstance(res,dict) else "";self._upsert_protection(order["id"],role,str(oid or ""),cid,trig,float(size),"LIVE",res)
                except Exception as e:db.risk_event("LIVE_PROTECTION_PLACE_FAILED",f"{role}: {e}",order["strategy"],"champion",order["symbol"])
            await asyncio.sleep(.45*(attempt+1))
        verified={x["role"] for x in db.query("SELECT role FROM live_protections WHERE live_order_id=? AND status='LIVE'",(order["id"],))};critical_ok="STOP" in verified and "TP3" in verified
        if critical_ok:db.risk_event("LIVE_PROTECTION_PARTIAL","Critical full STOP + final TP verified; partial SL/TP will keep repairing",order["strategy"],"champion",order["symbol"]);return True
        db.risk_event("LIVE_PROTECTION_INCOMPLETE","Could not verify critical full STOP and final TP",order["strategy"],"champion",order["symbol"])
        if settings.live_emergency_close_on_protection_failure:await self.emergency_close(order,"critical protection verification failed")
        return False
    async def emergency_close(self,order,why):
        rows=await self.positions();p=next((x for x in rows or [] if str(x.get("symbol") or "").upper()==order["symbol"].upper() and self._position_size(x)>0),None)
        if not p:return
        size=str(self._position_size(p));payload={"symbol":order["symbol"],"productType":settings.bitget_product_type,"marginMode":settings.bitget_margin_mode,"marginCoin":settings.bitget_margin_coin,"size":size,"orderType":"market","clientOid":"ai-emergency-"+uuid.uuid4().hex[:18]}
        if settings.bitget_position_mode=="hedge_mode":payload.update({"side":"buy" if order["side"]=="long" else "sell","tradeSide":"close"})
        else:payload.update({"side":"sell" if order["side"]=="long" else "buy","reduceOnly":"YES"})
        try:await self._private("POST","/api/v2/mix/order/place-order",payload=payload);db.execute("UPDATE live_orders SET status='EMERGENCY_CLOSED',updated_at=? WHERE id=?",(int(time.time()*1000),order["id"]));db.event("LIVE_EMERGENCY_CLOSE",f"{order['symbol']} {why}","ERROR")
        except Exception as e:db.event("LIVE_EMERGENCY_CLOSE_FAILED",f"{order['symbol']} {why}: {e}","ERROR");raise
    async def sync_from_paper_for_symbol(self,symbol):
        if not settings.live_trail_sync_enabled or not self.credentials_ready():return
        for order in db.query("SELECT * FROM live_orders WHERE symbol=? AND status='OPEN'",(symbol,)):
            pos=db.one("SELECT * FROM positions WHERE id=?",(order["paper_position_id"],))
            if not pos:continue
            prot=db.one("SELECT * FROM live_protections WHERE live_order_id=? AND role='STOP'",(order["id"],));desired=float(pos["stop"])
            if not prot:await self.ensure_protections(order,pos);continue
            old=float(prot["trigger_price"]);tighter=desired>old*(1+1e-8) if order["side"]=="long" else desired<old*(1-1e-8)
            if not tighter:continue
            payload={"marginCoin":settings.bitget_margin_coin,"productType":settings.bitget_product_type,"symbol":symbol,"triggerPrice":str(desired),"triggerType":"mark_price","executePrice":"0","size":""}
            if prot.get("order_id"):payload["orderId"]=prot["order_id"]
            else:payload["clientOid"]=prot["client_oid"]
            try:res=await self._private("POST","/api/v2/mix/order/modify-tpsl-order",payload=payload);self._upsert_protection(order["id"],"STOP",str((res or {}).get("orderId") or prot.get("order_id") or ""),str((res or {}).get("clientOid") or prot.get("client_oid") or ""),desired,0,"LIVE",res)
            except Exception as e:db.risk_event("LIVE_STOP_MODIFY_FAILED",str(e),order["strategy"],"champion",symbol);db.execute("UPDATE live_protections SET status='STALE' WHERE id=?",(prot["id"],));await self.ensure_protections(order,pos)
    async def reconcile(self,force=False):
        if not self.credentials_ready():return []
        if not force and time.time()-self.last_sync<settings.live_sync_interval_sec:return self.last_positions
        try:
            rows=await self.positions() or [];self.last_positions=rows;self.last_sync=time.time();self.last_sync_error=None;active={str(p.get("symbol") or "").upper() for p in rows if self._position_size(p)>0}
            for order in db.query("SELECT * FROM live_orders WHERE status='OPEN'"):
                if order["symbol"].upper() not in active:
                    db.execute("UPDATE live_orders SET status='CLOSED',updated_at=? WHERE id=?",(int(time.time()*1000),order["id"]));db.execute("UPDATE live_protections SET status='CLOSED',updated_at=? WHERE live_order_id=?",(int(time.time()*1000),order["id"]));continue
                if time.time()-self.last_protection_verify>=settings.live_protection_verify_sec:
                    pos=db.one("SELECT * FROM positions WHERE id=?",(order["paper_position_id"],));await self.ensure_protections(order,pos)
            self.last_protection_verify=time.time();return rows
        except Exception as e:self.last_sync_error=str(e);db.event("LIVE_RECONCILE_FAILED",str(e),"ERROR");return self.last_positions
    async def place_from_paper(self,position,params):
        state=db.state(position["strategy"]);g=self.gate_status()
        if not state or state["stage"]!="FINAL" or not state["live_eligible"] or not state.get("live_manual_enabled"):raise PermissionError("strategy not FINAL/auto-eligible/manual-approved")
        if not g["ready"]:raise PermissionError("live gate not ready")
        if db.one("SELECT id FROM live_orders WHERE paper_position_id=? AND status NOT IN('FAILED','CANCELLED')",(position["id"],)):return {"skipped":"already submitted"}
        live_positions=await self.reconcile(True)
        for lp in live_positions:
            if str(lp.get("symbol") or "").upper()==position["symbol"].upper() and self._position_size(lp)>0:raise PermissionError("existing Bitget position on this symbol; no stacking")
        paper_price=float(position["entry"]);ticker=await self.ticker(position["symbol"]);price=float((ticker or {}).get("lastPr") or paper_price);drift=abs(price-paper_price)/max(paper_price,1e-12)*10000
        if drift>settings.live_max_entry_drift_bps:raise PermissionError(f"live entry drift {drift:.1f}bps exceeds limit")
        acct=await self.account(position["symbol"]);alloc=await self.allocate(position,params,price,acct,live_positions);qty,lev,_=await self.normalize(position["symbol"],alloc["notional"]/price,price,alloc["leverage"]);actual=float(qty)*price
        if actual<=0:raise RuntimeError("normalized live qty zero")
        await self.set_leverage(position["symbol"],lev,position["side"]);client="ai-"+uuid.uuid4().hex[:24];side="buy" if position["side"]=="long" else "sell";payload={"symbol":position["symbol"],"productType":settings.bitget_product_type,"marginMode":settings.bitget_margin_mode,"marginCoin":settings.bitget_margin_coin,"size":qty,"side":side,"orderType":"market","clientOid":client,"presetStopLossPrice":str(position["stop"]),"presetStopLossExecutePrice":"0","presetStopSurplusPrice":str(position["tp3"]),"presetStopSurplusExecutePrice":"0"}
        if settings.bitget_position_mode=="hedge_mode":payload["tradeSide"]="open"
        now=int(time.time()*1000);plan={"entry":float(position["entry"]),"initial_stop":float(position["initial_stop"]),"stop":float(position["stop"]),"tp1":float(position["tp1"]),"tp2":float(position["tp2"]),"tp3":float(position["tp3"]),"tp1_fraction":float(position["tp1_fraction"]),"tp2_fraction":float(position["tp2_fraction"]),"sl1_r":float(params.get("sl1_r",.62)),"sl1_fraction":float(params.get("sl1_fraction",.18)),"params":params};cur=db.execute("INSERT INTO live_orders(paper_position_id,strategy,symbol,side,qty,entry_price,notional,leverage,allocator_json,client_oid,status,detail_json,created_at,updated_at)VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(position["id"],position["strategy"],position["symbol"],position["side"],float(qty),price,actual,lev,json.dumps(alloc),client,"SUBMITTING",json.dumps({"entry_payload":payload,"plan":plan}),now,now));live_id=int(cur.lastrowid)
        try:
            entry=await self._private("POST","/api/v2/mix/order/place-order",payload=payload);eid=(entry or {}).get("orderId") if isinstance(entry,dict) else None;detail={"entry":entry,"entry_payload":payload,"plan":plan,"allocator":alloc,"entry_drift_bps":drift};db.execute("UPDATE live_orders SET exchange_order_id=?,status='OPEN',detail_json=?,updated_at=? WHERE id=?",(eid,json.dumps(detail),int(time.time()*1000),live_id));order=db.one("SELECT * FROM live_orders WHERE id=?",(live_id,));live_pos=await self._wait_position(position["symbol"])
            if not live_pos:db.risk_event("LIVE_POSITION_CONFIRM_TIMEOUT","entry returned but position confirmation timed out; Guardian continues",position["strategy"],"champion",position["symbol"])
            ok=await self.ensure_protections(order,position)
            if not ok:raise RuntimeError("entry placed but critical exchange protections could not be verified")
            return {"entry":entry,"allocator":alloc,"protection_verified":True}
        except Exception as e:
            db.execute("UPDATE live_orders SET status=CASE WHEN status='SUBMITTING' THEN 'FAILED' ELSE status END,detail_json=?,updated_at=? WHERE id=?",(json.dumps({"error":str(e),"entry_payload":payload,"plan":plan,"allocator":alloc}),int(time.time()*1000),live_id));db.event("LIVE_ORDER_FAILED",f"{position['strategy']} {position['symbol']}: {e}","ERROR");raise

live_adapter=BitgetLiveAdapter()
