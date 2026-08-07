from __future__ import annotations

from dataclasses import dataclass
import os


def _b(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return default


@dataclass(frozen=True)
class Settings:
    port: int = _i("PORT", 8080)
    timezone: str = os.getenv("TZ", "Asia/Taipei")
    db_path: str = os.getenv("DB_PATH", "/data/crypto_lab.db")
    scan_interval_sec: int = _i("SCAN_INTERVAL_SEC", 60)
    scan_symbols_per_cycle: int = _i("SCAN_SYMBOLS_PER_CYCLE", 24)
    universe_min_usdt_volume: float = _f("UNIVERSE_MIN_USDT_VOLUME", 2_000_000)
    max_open_positions_per_strategy: int = _i("MAX_OPEN_POSITIONS_PER_STRATEGY", 4)
    initial_paper_equity: float = _f("INITIAL_PAPER_EQUITY", 10_000.0)
    paper_taker_fee: float = _f("PAPER_TAKER_FEE", 0.0006)
    paper_slippage_bps: float = _f("PAPER_SLIPPAGE_BPS", 4.0)
    paper_max_leverage: float = _f("PAPER_MAX_LEVERAGE", 3.0)

    bitget_base_url: str = os.getenv("BITGET_BASE_URL", "https://api.bitget.com")
    bitget_product_type: str = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")
    bitget_api_key: str = os.getenv("BITGET_API_KEY", "")
    bitget_api_secret: str = os.getenv("BITGET_API_SECRET", "")
    bitget_passphrase: str = os.getenv("BITGET_PASSPHRASE", "")

    coinglass_api_key: str = os.getenv("COINGLASS_API_KEY", "")
    coinglass_base_url: str = os.getenv("COINGLASS_BASE_URL", "https://open-api-v4.coinglass.com")
    coinglass_exchange: str = os.getenv("COINGLASS_EXCHANGE", "Binance")
    coinglass_enabled: bool = _b("COINGLASS_ENABLED", True)

    learning_enabled: bool = _b("LEARNING_ENABLED", True)
    learning_min_trades: int = _i("LEARNING_MIN_TRADES", 60)
    learning_interval_hours: int = _i("LEARNING_INTERVAL_HOURS", 24)
    learning_recent_window: int = _i("LEARNING_RECENT_WINDOW", 120)

    live_trading_enabled: bool = _b("LIVE_TRADING_ENABLED", False)
    final_live_unlock_token: str = os.getenv("FINAL_LIVE_UNLOCK_TOKEN", "")
    admin_token: str = os.getenv("ADMIN_TOKEN", "")


settings = Settings()
