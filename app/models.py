from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal
import time

Side = Literal["long", "short"]
Stage = Literal["EARLY", "TUNING", "FINAL"]


@dataclass
class Signal:
    strategy: str
    symbol: str
    side: Side
    score: float
    entry: float
    stop: float
    take_profits: list[float]
    trailing_atr: float
    reason: str
    features: dict[str, float] = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def risk_distance(self) -> float:
        return abs(self.entry - self.stop)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StrategySpec:
    name: str
    display_name: str
    stage: Stage
    risk_pct: float
    description: str
    params: dict[str, float]
    min_score: float = 0.60
    enabled: bool = True
    live_eligible: bool = False


@dataclass
class Position:
    id: int
    strategy: str
    symbol: str
    side: Side
    qty: float
    entry: float
    stop: float
    tp1: float
    tp2: float
    tp3: float
    remaining_qty: float
    realized_pnl: float
    opened_at: int
    trailing_atr: float
    stage: Stage
    tp1_hit: bool = False
    tp2_hit: bool = False
