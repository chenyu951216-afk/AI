from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Literal
import time

Side = Literal["long","short"]
Stage = Literal["EARLY","TUNING","FINAL"]
Variant = Literal["champion","challenger"]

@dataclass
class Signal:
    strategy: str
    symbol: str
    side: Side
    score: float
    entry: float
    stop: float
    reason: str
    features: dict[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time()*1000))
    def risk_distance(self) -> float: return abs(self.entry-self.stop)
    def to_dict(self) -> dict[str,Any]: return asdict(self)

@dataclass
class StrategySpec:
    name: str
    display_name: str
    description: str
    params: dict[str, float]
    regimes: tuple[str, ...] = ("ANY",)
    min_score: float = 0.65

@dataclass
class MarketRegime:
    name: str
    score: float
    btc_price: float
    btc_adx: float
    btc_atr_pct: float
    created_at: int = field(default_factory=lambda: int(time.time()*1000))
