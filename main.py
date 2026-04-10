"""
Crypto Screener - Binance Futures & Spot Algo Detector
Finds repeating market buy patterns in real-time.
"""
import asyncio
import signal
import sys
import time

from symbols import get_filtered_symbols
from ws_manager import WSManager
from detector import Detector
from telegram_bot import TelegramNotifier
from config import CLEANUP_INTERVAL, MARKET_TYPE


async def cleanup_loop(detector: Detector):
    """Periodically clean old trades from memory."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        detector.cleanup()


async def stats_loop(all_symbols: dict[str, list[str]], detector: Detector):
    """Print periodic status."""
    while True:
        await asyncio.sleep(60)
        ts = time.strftime("%H:%M:%S")
        active = len(detector._trades)
        total = sum(len(s) for s in all_symbols.values())
        markets = ", ".join(f"{m}:{len(s)}" for m, s in all_symbols.items())
        print(f"[{ts}] Monitoring {total} symbols ({markets}) | Active: {active} | {detector.get_stats()}", flush=True)


async def main():
    # Step 1: Fetch and filter symbols for each market
    print(f"[*] Fetching Binance symbols (market={MARKET_TYPE})...", flush=True)
    try:
        all_symbols = await get_filtered_symbols()
    except Exception as e:
        print(f"[!] Failed to fetch symbols: {e}", flush=True)
        sys.exit(1)

    total = sum(len(s) for s in all_symbols.values())
    if total == 0:
        print("[!] No symbols matched filters. Check config.", flush=True)
        sys.exit(1)

    for market, syms in all_symbols.items():
        print(f"[✓] {market.upper()}: {len(syms)} symbols", flush=True)
        print(f"    Examples: {', '.join(syms[:10])}{'...' if len(syms) > 10 else ''}")

    # Step 2: Initialize Telegram
    tg = TelegramNotifier()
    await tg.start()
    polling_task = tg.start_polling()
    print(f"[TG] Bot active. Send /start to authenticate and receive alerts.", flush=True)

    # Step 3: Initialize detector with Telegram callback
    detector = Detector(on_alert=tg.send_alert)

    # Step 4: Start WebSocket connections for each market
    ws_managers = []
    for market, syms in all_symbols.items():
        if not syms:
            continue
        ws = WSManager(syms, detector.on_trade, market=market)
        ws_managers.append(ws)

    print(f"\n[*] Starting screener ({MARKET_TYPE})... Press Ctrl+C to stop.\n", flush=True)
    for ws in ws_managers:
        await ws.start()

    # Step 5: Run background tasks
    tasks = [
        asyncio.create_task(cleanup_loop(detector)),
        asyncio.create_task(stats_loop(all_symbols, detector)),
    ]

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        print("\n[*] Shutting down...", flush=True)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    await stop_event.wait()

    # Cleanup
    for task in tasks:
        task.cancel()
    polling_task.cancel()
    for ws in ws_managers:
        await ws.stop()
    await tg.stop()
    print(f"[✓] Stopped. {detector.get_stats()}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
