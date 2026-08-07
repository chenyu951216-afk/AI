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
    port: int = _i("PORT",8080)
    timezone: str = os.getenv("TZ","Asia/Taipei")
    db_path: str = os.getenv("DB_PATH","/data/crypto_lab.db")
    log_level: str = os.getenv("LOG_LEVEL","INFO")
    admin_token: str = os.getenv("ADMIN_TOKEN","")

    scan_interval_sec: int = _i("SCAN_INTERVAL_SEC",30)
    scan_symbols_per_cycle: int = _i("SCAN_SYMBOLS_PER_CYCLE",36)
    universe_min_usdt_volume: float = _f("UNIVERSE_MIN_USDT_VOLUME",1_500_000)
    universe_max_symbols: int = _i("UNIVERSE_MAX_SYMBOLS",280)
    min_symbol_age_days: int = _i("MIN_SYMBOL_AGE_DAYS",3)
    market_timeout_sec: int = _i("MARKET_TIMEOUT_SEC",12)
    closed_candle_safety_ms: int = _i("CLOSED_CANDLE_SAFETY_MS",2500)
    btc_macro_refresh_sec: int = _i("BTC_MACRO_REFRESH_SEC",300)

    initial_paper_equity: float = _f("INITIAL_PAPER_EQUITY",10_000.0)
    paper_taker_fee: float = _f("PAPER_TAKER_FEE",0.0006)
    paper_maker_fee: float = _f("PAPER_MAKER_FEE",0.0002)
    paper_slippage_bps: float = _f("PAPER_SLIPPAGE_BPS",4.0)
    paper_stress_slippage_bps: float = _f("PAPER_STRESS_SLIPPAGE_BPS",12.0)

    hard_max_risk_per_trade: float = _f("HARD_MAX_RISK_PER_TRADE",0.02)
    hard_max_leverage: float = _f("HARD_MAX_LEVERAGE",5.0)
    hard_max_open_positions: int = _i("HARD_MAX_OPEN_POSITIONS",8)
    hard_max_symbol_exposure_pct: float = _f("HARD_MAX_SYMBOL_EXPOSURE_PCT",0.40)
    hard_max_total_exposure_pct: float = _f("HARD_MAX_TOTAL_EXPOSURE_PCT",2.25)
    hard_strategy_daily_loss_pct: float = _f("HARD_STRATEGY_DAILY_LOSS_PCT",0.06)
    hard_system_daily_loss_pct: float = _f("HARD_SYSTEM_DAILY_LOSS_PCT",0.10)
    hard_strategy_drawdown_pct: float = _f("HARD_STRATEGY_DRAWDOWN_PCT",0.28)
    hard_min_stop_pct: float = _f("HARD_MIN_STOP_PCT",0.0020)
    hard_max_stop_pct: float = _f("HARD_MAX_STOP_PCT",0.12)

    learning_enabled: bool = _b("LEARNING_ENABLED",True)
    learning_interval_hours: int = _i("LEARNING_INTERVAL_HOURS",24)
    learning_min_trades: int = _i("LEARNING_MIN_TRADES",40)
    learning_recent_window: int = _i("LEARNING_RECENT_WINDOW",180)
    challenger_min_trades: int = _i("CHALLENGER_MIN_TRADES",30)
    challenger_min_days: int = _i("CHALLENGER_MIN_DAYS",7)
    max_learning_changes: int = _i("MAX_LEARNING_CHANGES",2)
    final_min_trades: int = _i("FINAL_MIN_TRADES",250)
    final_min_days: int = _i("FINAL_MIN_DAYS",45)
    final_min_pf: float = _f("FINAL_MIN_PF",1.25)
    final_min_stress_pf: float = _f("FINAL_MIN_STRESS_PF",1.10)
    final_max_dd_pct: float = _f("FINAL_MAX_DD_PCT",0.10)
    final_min_robust_windows: float = _f("FINAL_MIN_ROBUST_WINDOWS",0.75)
    final_max_symbol_profit_share: float = _f("FINAL_MAX_SYMBOL_PROFIT_SHARE",0.35)
    final_min_symbols: int = _i("FINAL_MIN_SYMBOLS",5)

    post_trade_enabled: bool = _b("POST_TRADE_ENABLED",True)
    post_trade_follow_bars: int = _i("POST_TRADE_FOLLOW_BARS",16)
    post_trade_min_studies: int = _i("POST_TRADE_MIN_STUDIES",20)
    post_trade_min_signal_ratio: float = _f("POST_TRADE_MIN_SIGNAL_RATIO",0.30)

    bitget_base_url: str = os.getenv("BITGET_BASE_URL","https://api.bitget.com")
    bitget_product_type: str = os.getenv("BITGET_PRODUCT_TYPE","USDT-FUTURES")
    bitget_margin_coin: str = os.getenv("BITGET_MARGIN_COIN","USDT")
    bitget_margin_mode: str = os.getenv("BITGET_MARGIN_MODE","isolated")
    bitget_position_mode: str = os.getenv("BITGET_POSITION_MODE","one_way_mode")
    bitget_api_key: str = os.getenv("BITGET_API_KEY","")
    bitget_api_secret: str = os.getenv("BITGET_API_SECRET","")
    bitget_passphrase: str = os.getenv("BITGET_PASSPHRASE","")

    live_trading_allowed: bool = _b("LIVE_TRADING_ALLOWED",False)
    live_partial_tp_enabled: bool = _b("LIVE_PARTIAL_TP_ENABLED",True)
    live_sync_interval_sec: int = _i("LIVE_SYNC_INTERVAL_SEC",20)
    live_available_balance_buffer: float = _f("LIVE_AVAILABLE_BALANCE_BUFFER",0.90)
    live_max_entry_drift_bps: float = _f("LIVE_MAX_ENTRY_DRIFT_BPS",35.0)
    live_base_risk_pct: float = _f("LIVE_BASE_RISK_PCT",0.015)
    live_max_risk_pct: float = _f("LIVE_MAX_RISK_PCT",0.020)
    live_max_order_equity_pct: float = _f("LIVE_MAX_ORDER_EQUITY_PCT",0.85)
    live_max_total_notional_multiple: float = _f("LIVE_MAX_TOTAL_NOTIONAL_MULTIPLE",4.0)
    live_max_concurrent_positions: int = _i("LIVE_MAX_CONCURRENT_POSITIONS",8)
    live_min_free_margin_pct: float = _f("LIVE_MIN_FREE_MARGIN_PCT",0.12)
    live_protection_retry: int = _i("LIVE_PROTECTION_RETRY",4)
    live_protection_verify_sec: int = _i("LIVE_PROTECTION_VERIFY_SEC",20)
    live_emergency_close_on_protection_failure: bool = _b("LIVE_EMERGENCY_CLOSE_ON_PROTECTION_FAILURE",True)
    live_trail_sync_enabled: bool = _b("LIVE_TRAIL_SYNC_ENABLED",True)

    coinglass_enabled: bool = _b("COINGLASS_ENABLED",True)
    coinglass_api_key: str = os.getenv("COINGLASS_API_KEY","")
    coinglass_base_url: str = os.getenv("COINGLASS_BASE_URL","https://open-api-v4.coinglass.com")
    coinglass_exchange: str = os.getenv("COINGLASS_EXCHANGE","Binance")
    coinglass_cache_sec: int = _i("COINGLASS_CACHE_SEC",300)
    coinglass_top_symbols: int = _i("COINGLASS_TOP_SYMBOLS",100)

settings=Settings()
