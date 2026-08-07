from __future__ import annotations
from dataclasses import dataclass
import os


def _b(name: str, default: bool=False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1","true","yes","on"}

def _i(name: str, default: int) -> int:
    try: return int(os.getenv(name, str(default)))
    except Exception: return default

def _f(name: str, default: float) -> float:
    try: return float(os.getenv(name, str(default)))
    except Exception: return default

@dataclass(frozen=True)
class Settings:
    port: int = _i("PORT", 8080)
    timezone: str = os.getenv("TZ", "Asia/Taipei")
    db_path: str = os.getenv("DB_PATH", "/data/crypto_lab.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    scan_interval_sec: int = _i("SCAN_INTERVAL_SEC", 45)
    scan_symbols_per_cycle: int = _i("SCAN_SYMBOLS_PER_CYCLE", 30)
    universe_min_usdt_volume: float = _f("UNIVERSE_MIN_USDT_VOLUME", 3_000_000)
    universe_max_symbols: int = _i("UNIVERSE_MAX_SYMBOLS", 250)
    min_symbol_age_days: int = _i("MIN_SYMBOL_AGE_DAYS", 7)
    market_timeout_sec: int = _i("MARKET_TIMEOUT_SEC", 12)
    regime_refresh_sec: int = _i("REGIME_REFRESH_SEC", 300)

    initial_paper_equity: float = _f("INITIAL_PAPER_EQUITY", 10_000.0)
    paper_taker_fee: float = _f("PAPER_TAKER_FEE", 0.0006)
    paper_maker_fee: float = _f("PAPER_MAKER_FEE", 0.0002)
    paper_slippage_bps: float = _f("PAPER_SLIPPAGE_BPS", 4.0)
    paper_stress_slippage_bps: float = _f("PAPER_STRESS_SLIPPAGE_BPS", 12.0)

    hard_max_risk_per_trade: float = _f("HARD_MAX_RISK_PER_TRADE", 0.02)
    hard_max_leverage: float = _f("HARD_MAX_LEVERAGE", 5.0)
    hard_max_open_positions: int = _i("HARD_MAX_OPEN_POSITIONS", 8)
    hard_max_symbol_exposure_pct: float = _f("HARD_MAX_SYMBOL_EXPOSURE_PCT", 0.35)
    hard_max_total_exposure_pct: float = _f("HARD_MAX_TOTAL_EXPOSURE_PCT", 2.0)
    hard_strategy_daily_loss_pct: float = _f("HARD_STRATEGY_DAILY_LOSS_PCT", 0.05)
    hard_system_daily_loss_pct: float = _f("HARD_SYSTEM_DAILY_LOSS_PCT", 0.08)
    hard_strategy_drawdown_pct: float = _f("HARD_STRATEGY_DRAWDOWN_PCT", 0.25)
    hard_min_stop_pct: float = _f("HARD_MIN_STOP_PCT", 0.0025)
    hard_max_stop_pct: float = _f("HARD_MAX_STOP_PCT", 0.10)

    learning_enabled: bool = _b("LEARNING_ENABLED", True)
    learning_interval_hours: int = _i("LEARNING_INTERVAL_HOURS", 24)
    learning_min_trades: int = _i("LEARNING_MIN_TRADES", 50)
    learning_recent_window: int = _i("LEARNING_RECENT_WINDOW", 160)
    challenger_min_trades: int = _i("CHALLENGER_MIN_TRADES", 30)
    challenger_min_days: int = _i("CHALLENGER_MIN_DAYS", 7)
    max_learning_changes: int = _i("MAX_LEARNING_CHANGES", 2)
    final_min_trades: int = _i("FINAL_MIN_TRADES", 250)
    final_min_days: int = _i("FINAL_MIN_DAYS", 45)
    final_min_pf: float = _f("FINAL_MIN_PF", 1.25)
    final_min_stress_pf: float = _f("FINAL_MIN_STRESS_PF", 1.10)
    final_max_dd_pct: float = _f("FINAL_MAX_DD_PCT", 0.10)
    final_min_robust_windows: float = _f("FINAL_MIN_ROBUST_WINDOWS", 0.75)
    final_max_symbol_profit_share: float = _f("FINAL_MAX_SYMBOL_PROFIT_SHARE", 0.35)
    final_min_symbols: int = _i("FINAL_MIN_SYMBOLS", 5)

    bitget_base_url: str = os.getenv("BITGET_BASE_URL", "https://api.bitget.com")
    bitget_product_type: str = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")
    bitget_margin_coin: str = os.getenv("BITGET_MARGIN_COIN", "USDT")
    bitget_margin_mode: str = os.getenv("BITGET_MARGIN_MODE", "isolated")
    bitget_position_mode: str = os.getenv("BITGET_POSITION_MODE", "one_way_mode")
    bitget_api_key: str = os.getenv("BITGET_API_KEY", "")
    bitget_api_secret: str = os.getenv("BITGET_API_SECRET", "")
    bitget_passphrase: str = os.getenv("BITGET_PASSPHRASE", "")

    live_trading_allowed: bool = _b("LIVE_TRADING_ALLOWED", False)
    live_partial_tp_enabled: bool = _b("LIVE_PARTIAL_TP_ENABLED", True)
    live_max_strategy_notional_usdt: float = _f("LIVE_MAX_STRATEGY_NOTIONAL_USDT", 2500.0)
    live_max_order_notional_usdt: float = _f("LIVE_MAX_ORDER_NOTIONAL_USDT", 1000.0)
    live_sync_interval_sec: int = _i("LIVE_SYNC_INTERVAL_SEC", 30)
    live_available_balance_buffer: float = _f("LIVE_AVAILABLE_BALANCE_BUFFER", 0.90)
    live_max_entry_drift_bps: float = _f("LIVE_MAX_ENTRY_DRIFT_BPS", 35.0)

    coinglass_enabled: bool = _b("COINGLASS_ENABLED", True)
    coinglass_api_key: str = os.getenv("COINGLASS_API_KEY", "")
    coinglass_base_url: str = os.getenv("COINGLASS_BASE_URL", "https://open-api-v4.coinglass.com")
    coinglass_exchange: str = os.getenv("COINGLASS_EXCHANGE", "Binance")
    coinglass_cache_sec: int = _i("COINGLASS_CACHE_SEC", 300)
    coinglass_top_symbols: int = _i("COINGLASS_TOP_SYMBOLS", 80)

    admin_token: str = os.getenv("ADMIN_TOKEN", "")

settings = Settings()
