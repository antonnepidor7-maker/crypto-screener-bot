"""
Crypto Screener for Binance Futures - Configuration
"""
import json
import os

# ========================
# Symbol Filters
# ========================

# Exclude stablecoins and their pairs
STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD", "EUR"}

# Exclude heavy coins (high price, dominant market - too noisy for this pattern)
HEAVY_COINS = {"BTC", "ETH", "BNB"}

# 24h quote volume threshold (USDT) — per market
MIN_24H_VOLUME_FUTURES = 10_000_000  # $10M
MIN_24H_VOLUME_SPOT = 5_000_000      # $5M
# Legacy fallback
MIN_24H_VOLUME = MIN_24H_VOLUME_FUTURES

# Minimum dollar value per print to consider ($100)
MIN_PRINT_USD = 100

# ========================
# Detection Parameters
# ========================

# Minimum number of matching trades to flag a pattern
MIN_REPEATS = 5

# Maximum time window to look for repeating pattern (seconds)
MAX_WINDOW = 15

# Quantity tolerance: relative (percentage) + absolute minimum
TOLERANCE_REL = 0.10      # 10%
TOLERANCE_ABS = 0.0       # no absolute floor by default

# Interval consistency: max drift from median interval (percentage)
INTERVAL_DRIFT = 0.25  # 25%

# ========================
# WebSocket
# ========================

BINANCE_WS_BASE = "wss://fstream.binance.com/ws"
BINANCE_FAPI = "https://fapi.binance.com"

# Binance Spot endpoints
BINANCE_SPOT_WS_BASE = "wss://stream.binance.com:9443/ws"
BINANCE_API = "https://api.binance.com"

# ========================
# Market Type
# ========================

# Which markets to monitor: "futures", "spot", or "both"
MARKET_TYPE = os.environ.get("MARKET_TYPE", "both").lower()

# ========================
# Alert Strength (interval-based)
# ========================

# Interval thresholds for alert strength classification (seconds)
INTERVAL_STRONG_MAX = 0.5    # < 0.5s = STRONG
INTERVAL_MEDIUM_MAX = 1.0    # 0.5s - 1.0s = MEDIUM
# > 1.0s = WEAK

# Local Xray SOCKS5 proxy (bypasses Binance geo-block)
# Set to "" to disable proxy (if Binance is accessible directly)
BINANCE_PROXY = os.environ.get("BINANCE_PROXY", "socks5://127.0.0.1:10808")

# Max symbols per WebSocket connection
MAX_STREAMS_PER_CONN = 1024

# ========================
# Cleanup & Memory
# ========================

TRADE_TTL = 30
CLEANUP_INTERVAL = 30

# ========================
# Telegram (from env)
# ========================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is not set. "
        "Set it via environment variable or .env file."
    )

# ========================
# Output
# ========================

ALERT_COOLDOWN = 60

# ========================
# Auth
# ========================

AUTH_USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")


def load_auth_users() -> dict[str, str]:
    """Load authorized users from users.json. Returns {login: password}."""
    try:
        with open(AUTH_USERS_FILE, "r") as f:
            users = json.load(f)
        if not isinstance(users, dict):
            raise ValueError("users.json must be a JSON object {login: password}")
        return users
    except FileNotFoundError:
        print(f"[!] Warning: {AUTH_USERS_FILE} not found. No users configured.", flush=True)
        return {}
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[!] Warning: Failed to parse {AUTH_USERS_FILE}: {e}", flush=True)
        return {}


# Loaded on import — restart app after editing users.json
AUTH_USERS: dict[str, str] = load_auth_users()


def reload_auth_users() -> dict[str, str]:
    """Reload users from disk. Returns updated dict."""
    global AUTH_USERS
    AUTH_USERS = load_auth_users()
    return AUTH_USERS


def save_auth_users(users: dict[str, str]) -> None:
    """Save users dict to users.json and reload."""
    global AUTH_USERS
    with open(AUTH_USERS_FILE, "w") as f:
        json.dump(users, f, indent=4, ensure_ascii=False)
    AUTH_USERS = users.copy()


# Admin logins (can manage users via /admin panel)
ADMIN_LOGINS = {"admin"}
