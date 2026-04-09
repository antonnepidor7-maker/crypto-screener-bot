"""
Fetch and filter Binance Futures USDT-M symbols.
"""
import json
import os
import aiohttp
import asyncio
from config import (
    BINANCE_FAPI, STABLECOINS, HEAVY_COINS, MIN_24H_VOLUME,
    BINANCE_PROXY,
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


async def get_filtered_symbols() -> list[str]:
    """
    Returns list of symbols matching criteria:
    - USDT quote asset
    - Not a stablecoin base
    - Not a heavy coin
    - Not in top-150 market cap blacklist
    - 24h quote volume > MIN_24H_VOLUME
    """
    blacklist = _load_blacklist()

    url = f"{BINANCE_FAPI}/fapi/v1/ticker/24hr"

    connector = _make_connector()
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Binance API error: {resp.status}")
            tickers = await resp.json()

    symbols = []
    for t in tickers:
        sym = t["symbol"]

        # Must end with USDT
        if not sym.endswith("USDT"):
            continue

        base = sym[:-4]  # strip "USDT"

        # Skip stablecoins and heavy coins
        if base in STABLECOINS or base in HEAVY_COINS:
            continue

        # Skip if any stablecoin substring in base name
        if any(st in base for st in STABLECOINS if st != base):
            continue

        # Skip top-150 market cap
        if base in blacklist:
            continue

        # Volume filter
        try:
            vol = float(t.get("quoteVolume", 0))
        except (ValueError, TypeError):
            continue

        if vol < MIN_24H_VOLUME:
            continue

        symbols.append(sym)

    symbols.sort()
    return symbols


if __name__ == "__main__":
    async def main():
        syms = await get_filtered_symbols()
        print(f"Found {len(syms)} symbols")
        print(", ".join(syms[:20]) + ("..." if len(syms) > 20 else ""))

    asyncio.run(main())
