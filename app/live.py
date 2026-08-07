from __future__ import annotations

import base64, hashlib, hmac, json, time, uuid
import httpx

from .config import settings


class BitgetLiveAdapter:
    """Private trading connector. It is intentionally locked unless FINAL + global unlock are both true."""
    def __init__(self):
        self.client=httpx.AsyncClient(base_url=settings.bitget_base_url,timeout=12)

    def _ready(self):
        return bool(settings.live_trading_enabled and settings.final_live_unlock_token and settings.bitget_api_key and settings.bitget_api_secret and settings.bitget_passphrase)

    def _headers(self, method:str, path:str, body:str="", query:str=""):
        ts=str(int(time.time()*1000)); msg=ts+method.upper()+path+("?"+query if query else "")+body
        sig=base64.b64encode(hmac.new(settings.bitget_api_secret.encode(),msg.encode(),hashlib.sha256).digest()).decode()
        return {"ACCESS-KEY":settings.bitget_api_key,"ACCESS-SIGN":sig,"ACCESS-PASSPHRASE":settings.bitget_passphrase,"ACCESS-TIMESTAMP":ts,"Content-Type":"application/json","locale":"en-US"}

    async def place_final_order(self, *, stage:str, symbol:str, side:str, size:str, stop:str, take_profit:str):
        if stage!="FINAL": raise PermissionError("Strategy is not FINAL; live order blocked")
        if not self._ready(): raise PermissionError("Live trading is globally locked")
        path="/api/v2/mix/order/place-order"
        payload={"symbol":symbol,"productType":settings.bitget_product_type,"marginMode":"isolated","marginCoin":"USDT","size":size,"side":"buy" if side=="long" else "sell","orderType":"market","clientOid":"lab-"+uuid.uuid4().hex[:24]}
        body=json.dumps(payload,separators=(",",":")); r=await self.client.post(path,content=body,headers=self._headers("POST",path,body)); r.raise_for_status(); out=r.json()
        if out.get("code")!="00000": raise RuntimeError(out)
        # Attach exchange-native TP/SL immediately after entry request.
        tpath="/api/v2/mix/order/place-pos-tpsl"
        tp={"marginCoin":"USDT","productType":settings.bitget_product_type,"symbol":symbol,"stopSurplusTriggerPrice":take_profit,"stopSurplusTriggerType":"mark_price","stopSurplusExecutePrice":"0","stopLossTriggerPrice":stop,"stopLossTriggerType":"mark_price","stopLossExecutePrice":"0","holdSide":side}
        tbody=json.dumps(tp,separators=(",",":")); tr=await self.client.post(tpath,content=tbody,headers=self._headers("POST",tpath,tbody)); tr.raise_for_status()
        return {"order":out,"protection":tr.json()}

live_adapter=BitgetLiveAdapter()
