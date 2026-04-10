"""
Fetch and filter Binance Futures and Spot USDT-M symbols.
"""
import json
import os
import aiohttp
import asyncio
from config import (
    BINANCE_FAPI, BINANCE_API, STABLECOINS, HEAVY_COINS,
    MIN_24H_VOLUME_FUTURES, MIN_24H_VOLUME_SPOT, BINANCE_PROXY, MARKET_TYPE,
)


def _make_connector():
    """Create aiohttp connector, optionally via SOCKS proxy."""
    if BINANCE_PROXY:
        from aiohttp_socks import ProxyConnector
        return ProxyConnector.from_url(BINANCE_PROXY)
    return None


_DIR = os.path.dirname(os.path.abspath(__file__))
_BLACKLIST_FILE = os.path.join(_DIR, "top150_blacklist.json")


def _load_blacklist() -> set[str]:
    """Load top-150 market cap blacklist from file."""
    if os.path.exists(_BLACKLIST_FILE):
        with open(_BLACKLIST_FILE) as f:
            return set(json.load(f))
    return set()


def _filter_symbol(sym: str, blacklist: set[str], tickers_map: dict, min_volume: float) -> bool:
    """Check if symbol passes all filters. Returns True if should include."""
    if not sym.endswith("USDT"):
        return False

    base = sym[:-4]

    if base in STABLECOINS or base in HEAVY_COINS:
        return False

    if any(st in base for st in STABLECOINS if st != base):
        return False

    if base in blacklist:
        return False

    try:
        vol = float(tickers_map.get(sym, {}).get("quoteVolume", 0))
    except (ValueError, TypeError):
        return False

    return vol >= min_volume


async def _fetch_futures_symbols(blacklist: set[str]) -> list[str]:
    """Fetch and filter Binance Futures USDT-M symbols."""
    url = f"{BINANCE_FAPI}/fapi/v1/ticker/24hr"
    connector = _make_connector()
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Binance Futures API error: {resp.status}")
            tickers = await resp.json()

    tickers_map = {t["symbol"]: t for t in tickers}
    symbols = [s for s in tickers_map if _filter_symbol(s, blacklist, tickers_map, MIN_24H_VOLUME_FUTURES)]
    symbols.sort()
    return symbols


async def _fetch_spot_symbols(blacklist: set[str]) -> list[str]:
    """Fetch and filter Binance Spot USDT symbols."""
    url = f"{BINANCE_API}/api/v3/ticker/24hr"
    connector = _make_connector()
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Binance Spot API error: {resp.status}")
            tickers = await resp.json()

    tickers_map = {t["symbol"]: t for t in tickers}
    symbols = [s for s in tickers_map if _filter_symbol(s, blacklist, tickers_map, MIN_24H_VOLUME_SPOT)]
    symbols.sort()
    return symbols


async def get_filtered_symbols() -> dict[str, list[str]]:
    """
    Returns dict with filtered symbols per market type.
    {"futures": [...], "spot": [...]}
    Respects MARKET_TYPE config.
    """
    blacklist = _load_blacklist()
    result = {}

    if MARKET_TYPE in ("futures", "both"):
        result["futures"] = await _fetch_futures_symbols(blacklist)

    if MARKET_TYPE in ("spot", "both"):
        result["spot"] = await _fetch_spot_symbols(blacklist)

    return result


if __name__ == "__main__":
    async def main():
        markets = await get_filtered_symbols()
        for market, syms in markets.items():
            print(f"[{market}] Found {len(syms)} symbols")
            print(f"  {', '.join(syms[:20])}{'...' if len(syms) > 20 else ''}")

    asyncio.run(main())
