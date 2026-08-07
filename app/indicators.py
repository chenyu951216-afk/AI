from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df.close.shift(1)
    tr = pd.concat([(df.high-df.low).abs(), (df.high-prev).abs(), (df.low-prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df.high.diff()
    down = -df.low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    a = atr(df, n)
    plus_di = 100 * plus_dm.ewm(alpha=1/n, adjust=False).mean() / a.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1/n, adjust=False).mean() / a.replace(0, np.nan)
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    return dx.ewm(alpha=1/n, adjust=False).mean()


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["ema20"] = ema(d.close, 20)
    d["ema50"] = ema(d.close, 50)
    d["ema200"] = ema(d.close, 200)
    d["rsi14"] = rsi(d.close, 14)
    d["atr14"] = atr(d, 14)
    d["adx14"] = adx(d, 14)
    d["vol_ma20"] = d.volume.rolling(20).mean()
    d["rvol20"] = d.volume / d.vol_ma20.replace(0, np.nan)
    d["bb_mid"] = d.close.rolling(20).mean()
    d["bb_std"] = d.close.rolling(20).std(ddof=0)
    d["z20"] = (d.close-d.bb_mid) / d.bb_std.replace(0, np.nan)
    typical = (d.high+d.low+d.close)/3
    d["vwap"] = (typical*d.volume).rolling(48).sum() / d.volume.rolling(48).sum().replace(0, np.nan)
    d["hh20"] = d.high.shift(1).rolling(20).max()
    d["ll20"] = d.low.shift(1).rolling(20).min()
    d["hh50"] = d.high.shift(1).rolling(50).max()
    d["ll50"] = d.low.shift(1).rolling(50).min()
    d["range20"] = (d.high.rolling(20).max()-d.low.rolling(20).min()) / d.close.replace(0, np.nan)
    d["ret1"] = d.close.pct_change()
    d["ret4"] = d.close.pct_change(4)
    return d


def fib_context(df: pd.DataFrame, lookback: int = 60) -> dict[str, float]:
    w = df.iloc[-lookback:]
    hi_idx = w.high.idxmax(); lo_idx = w.low.idxmin()
    hi = float(w.high.max()); lo = float(w.low.min())
    if hi <= lo:
        return {"dir": 0, "hi": hi, "lo": lo, "fib50": lo, "fib618": lo, "fib786": lo}
    up = hi_idx > lo_idx
    rng = hi-lo
    if up:
        return {"dir": 1, "hi": hi, "lo": lo, "fib50": hi-.5*rng, "fib618": hi-.618*rng, "fib786": hi-.786*rng, "ext1272": hi+.272*rng}
    return {"dir": -1, "hi": hi, "lo": lo, "fib50": lo+.5*rng, "fib618": lo+.618*rng, "fib786": lo+.786*rng, "ext1272": lo-.272*rng}


def safe(v, default=0.0) -> float:
    try:
        x = float(v)
        return x if np.isfinite(x) else default
    except Exception:
        return default
