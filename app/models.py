from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Literal
import time

Side=Literal["long","short"]
Stage=Literal["EARLY","TUNING","FINAL"]
Variant=Literal["champion","challenger"]

@dataclass
class Signal:
    strategy:str
    symbol:str
    side:Side
    score:float
    entry:float
    stop:float
    reason:str
    features:dict[str,Any]=field(default_factory=dict)
    signal_tf:str="15m"
    bar_ts:int=0
    created_at:int=field(default_factory=lambda:int(time.time()*1000))
    def risk_distance(self)->float:return abs(self.entry-self.stop)
    def to_dict(self)->dict[str,Any]:return asdict(self)

@dataclass
class StrategySpec:
    name:str
    display_name:str
    description:str
    params:dict[str,float]
    signal_tf:str
    context_tfs:tuple[str,...]
    min_score_early:float=0.62
    min_score_tuning:float=0.68
    min_score_final:float=0.72

@dataclass
class MarketRegime:
    name:str
    score:float
    btc_price:float=0.0
    btc_adx:float=0.0
    btc_atr_pct:float=0.0
    created_at:int=field(default_factory=lambda:int(time.time()*1000))
