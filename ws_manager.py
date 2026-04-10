"""
WebSocket manager for Binance Futures and Spot trade streams.
Handles multiple connections (max 1024 streams each).
"""
import asyncio
import json
import aiohttp
from config import (
    BINANCE_WS_BASE, BINANCE_SPOT_WS_BASE,
    MAX_STREAMS_PER_CONN, BINANCE_PROXY,
)


def _make_connector():
    """Create aiohttp connector, optionally via SOCKS proxy."""
    if BINANCE_PROXY:
        from aiohttp_socks import ProxyConnector
        return ProxyConnector.from_url(BINANCE_PROXY)
    return None


class WSManager:
    def __init__(self, symbols: list[str], on_trade: callable, market: str = "futures"):
        self.symbols = symbols
        self.on_trade = on_trade
        self.market = market  # "futures" or "spot"
        self._tasks: list[asyncio.Task] = []
        self._running = False

    async def start(self):
        """Launch WebSocket connections for all symbols."""
        self._running = True

        chunks = [
            self.symbols[i:i + MAX_STREAMS_PER_CONN]
            for i in range(0, len(self.symbols), MAX_STREAMS_PER_CONN)
        ]

        label = self.market.upper()
        print(f"[WS-{label}] Connecting {len(self.symbols)} symbols in {len(chunks)} connection(s)")

        for idx, chunk in enumerate(chunks):
            task = asyncio.create_task(
                self._connect_stream(chunk, idx + 1),
                name=f"ws-{self.market}-{idx + 1}"
            )
            self._tasks.append(task)

    async def stop(self):
        """Gracefully close all connections."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def _get_ws_url(self, symbols: list[str]) -> str:
        """Build combined stream URL for the market type."""
        streams = "/".join(f"{s.lower()}@trade" for s in symbols)
        if self.market == "spot":
            return f"{BINANCE_SPOT_WS_BASE}/{streams}"
        return f"{BINANCE_WS_BASE}/{streams}"

    async def _connect_stream(self, symbols: list[str], conn_id: int):
        """
        Connect to combined trade stream for a chunk of symbols.
        Auto-reconnects on failure.
        """
        url = self._get_ws_url(symbols)
        label = self.market.upper()

        while self._running:
            try:
                connector = _make_connector()
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.ws_connect(url, heartbeat=30) as ws:
                        print(f"[WS-{label}-{conn_id}] Connected ({len(symbols)} streams)")

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(msg.data)
                                    self._process_trade(data)
                                except (json.JSONDecodeError, KeyError):
                                    continue
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR
                            ):
                                print(f"[WS-{label}-{conn_id}] Connection closed, reconnecting...")
                                break

            except Exception as e:
                if self._running:
                    print(f"[WS-{label}-{conn_id}] Error: {e}, reconnecting in 3s...")
                    await asyncio.sleep(3)

    def _process_trade(self, data: dict):
        """
        Parse trade event and pass to callback.
        Futures: m=true → SELL (buyer is maker), m=false → BUY (buyer is taker)
        Spot: same logic with 'm' field
        """
        side = "BUY" if data.get("m") is False else "SELL"

        trade = {
            "symbol": data["s"],
            "price": float(data["p"]),
            "qty": float(data["q"]),
            "time": data["T"],
            "event_time": data["E"],
            "side": side,
            "market": self.market,
        }

        self.on_trade(trade)
