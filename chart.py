"""
chart.py — Генерация PNG-графика с отметкой включения алгоритма.
"""
import matplotlib
matplotlib.use("Agg")  # headless сервер — без GUI

import io
import time
from datetime import datetime, timezone

import mplfinance as mpf
import pandas as pd
import requests

from config import BINANCE_FAPI


def fetch_klines(symbol: str, signal_time_sec: float,
                 before: int = 60, after: int = 30) -> pd.DataFrame:
    """Забираем 1m свечи: before до сигнала + after после."""
    total = before + after
    end_ms = int((signal_time_sec + after * 60) * 1000)

    resp = requests.get(
        f"{BINANCE_FAPI}/fapi/v1/klines",
        params={"symbol": symbol, "interval": "1m", "limit": total, "endTime": end_ms},
        timeout=10,
    )
    resp.raise_for_status()
    klines = resp.json()

    df = pd.DataFrame(klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df.set_index("open_time", inplace=True)
    return df


def generate_signal_chart(
    symbol: str,
    signal_time_sec: float,
    first_price: float,
    strength: str,
    avg_usd: float,
    dpi: int = 150,
) -> io.BytesIO:
    """
    Генерирует тёмный график со стрелкой на сигнальной свече.
    Возвращает BytesIO с PNG.
    """
    df = fetch_klines(symbol, signal_time_sec)

    if df.empty:
        raise ValueError(f"No kline data for {symbol}")

    # Ищем ближайшую свечу к моменту сигнала
    signal_dt = datetime.fromtimestamp(signal_time_sec, tz=timezone.utc)
    signal_idx = df.index.get_indexer([pd.Timestamp(signal_dt)], method="nearest")[0]

    # ── Стрелка вниз (под свечой) ──
    marker = [float("nan")] * len(df)
    marker[signal_idx] = df["low"].iloc[signal_idx] * 0.9995

    arrow = mpf.make_addplot(
        marker,
        type="scatter",
        marker="^",
        markersize=180,
        color="#00ff88",
        edgecolors="white",
        linewidths=0.8,
    )

    # ── Вертикальная подсветка момента ──
    vlines = dict(
        vlines=[df.index[signal_idx]],
        linewidths=3,
        colors="#00ff8840",
    )

    # ── Линия entry price ──
    hlines = dict(
        hlines=[first_price],
        colors="#FFD700",
        linestyle="--",
        linewidths=1.2,
    )

    # ── Тёмный стиль ──
    mc = mpf.make_marketcolors(
        up="#26a69a", down="#ef5350",
        edge="inherit", wick="inherit",
        volume="#555555",
    )
    style = mpf.make_mpf_style(
        marketcolors=mc,
        gridstyle=":",
        gridcolor="#333333",
        facecolor="#111111",
        figcolor="#111111",
        rc={
            "text.color": "white",
            "axes.labelcolor": "white",
            "xtick.color": "#888888",
            "ytick.color": "#888888",
        },
    )

    # ── Заголовок ──
    ts_str = signal_dt.strftime("%Y-%m-%d %H:%M UTC")
    usd_label = f"${avg_usd:,.0f}" if avg_usd < 1000 else f"${avg_usd / 1000:.1f}K"
    title = f"{symbol}  •  {strength}  •  {ts_str}\nEntry: {first_price}  |  Print: {usd_label}"

    # ── Рендер ──
    buf = io.BytesIO()
    mpf.plot(
        df,
        type="candle",
        style=style,
        volume=True,
        addplot=arrow,
        vlines=vlines,
        hlines=hlines,
        title=title,
        figsize=(12, 6),
        tight_layout=True,
        savefig=dict(fname=buf, dpi=dpi, bbox_inches="tight", facecolor="#111111"),
    )
    buf.seek(0)
    return buf
