"""
chart.py — Быстрая генерация графика на Pillow (без matplotlib/pandas).
"""
import io
import requests
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

from config import BINANCE_FAPI

# ── Цвета ──
BG       = (17, 17, 17)
UP       = (38, 166, 154)
DOWN     = (239, 83, 80)
GRID     = (40, 40, 40)
TEXT     = (180, 180, 180)
WHITE    = (255, 255, 255)
GREEN    = (0, 255, 136)
GOLD     = (255, 215, 0)
GREEN_BG = (0, 255, 136, 40)

# ── Размеры ──
W, H         = 1200, 600
PAD_LEFT     = 80
PAD_RIGHT    = 20
PAD_TOP      = 50
PAD_BOTTOM   = 20
VOL_H        = 80  # высота зоны объёма


def fetch_klines(symbol: str, signal_time_sec: float,
                 before: int = 60, after: int = 30) -> list[dict]:
    """Забираем 1m свечи с Binance Futures."""
    total = before + after
    end_ms = int((signal_time_sec + after * 60) * 1000)

    resp = requests.get(
        f"{BINANCE_FAPI}/fapi/v1/klines",
        params={"symbol": symbol, "interval": "1m", "limit": total, "endTime": end_ms},
        timeout=10,
    )
    resp.raise_for_status()
    raw = resp.json()

    klines = []
    for k in raw:
        klines.append({
            "time":  k[0],
            "open":  float(k[1]),
            "high":  float(k[2]),
            "low":   float(k[3]),
            "close": float(k[4]),
            "vol":   float(k[5]),
        })
    return klines


def _format_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:.4f}"
    elif p >= 0.001:
        return f"{p:.6f}"
    else:
        return f"{p:.8f}"


def generate_signal_chart(
    symbol: str,
    signal_time_sec: float,
    first_price: float,
    strength: str,
    avg_usd: float,
    dpi: int = 1,  # unused, kept for API compat
) -> io.BytesIO:
    """Генерирует PNG-график с отметкой включения. Возвращает BytesIO."""
    klines = fetch_klines(symbol, signal_time_sec)
    n = len(klines)
    if n == 0:
        raise ValueError(f"No kline data for {symbol}")

    # ── Находим сигнальную свечу ──
    signal_ms = int(signal_time_sec * 1000)
    signal_idx = 0
    best_diff = abs(klines[0]["time"] - signal_ms)
    for i, k in enumerate(klines):
        diff = abs(k["time"] - signal_ms)
        if diff < best_diff:
            best_diff = diff
            signal_idx = i

    # ── Диапазон цен ──
    all_high = max(k["high"] for k in klines)
    all_low  = min(k["low"]  for k in klines)
    price_range = all_high - all_low
    if price_range == 0:
        price_range = all_high * 0.01
    padding = price_range * 0.05
    pmin = all_low - padding
    pmax = all_high + padding

    # ── Диапазон объёма ──
    max_vol = max(k["vol"] for k in klines) or 1

    # ── Зоны графика ──
    chart_top = PAD_TOP
    chart_bot = H - PAD_BOTTOM - VOL_H
    chart_h = chart_bot - chart_top
    vol_top = chart_bot + 4
    vol_bot = H - PAD_BOTTOM
    vol_h = vol_bot - vol_top
    candle_area_w = W - PAD_LEFT - PAD_RIGHT
    candle_w = max(2, candle_area_w // n - 1)

    def price_y(p: float) -> int:
        return int(chart_top + (pmax - p) / (pmax - pmin) * chart_h)

    def candle_x(i: int) -> int:
        return PAD_LEFT + int((i + 0.5) * candle_area_w / n)

    # ── Рисуем ──
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Шрифт (fallback на default)
    try:
        font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        font_md = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        font_lg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    except OSError:
        font_sm = ImageFont.load_default()
        font_md = font_sm
        font_lg = font_sm

    # ── Сетка по цене ──
    num_grid = 5
    for i in range(num_grid + 1):
        p = pmin + (pmax - pmin) * i / num_grid
        y = price_y(p)
        draw.line([(PAD_LEFT, y), (W - PAD_RIGHT, y)], fill=GRID, width=1)
        label = _format_price(p)
        draw.text((4, y - 6), label, fill=TEXT, font=font_sm)

    # ── Свечи + объём ──
    for i, k in enumerate(klines):
        x = candle_x(i)
        o, h, l, c, v = k["open"], k["high"], k["low"], k["close"], k["vol"]
        color = UP if c >= o else DOWN

        # Wick
        draw.line([(x, price_y(h)), (x, price_y(l))], fill=color, width=1)

        # Body
        y_open = price_y(o)
        y_close = price_y(c)
        y_top = min(y_open, y_close)
        y_bot = max(y_open, y_close)
        if y_bot - y_top < 1:
            y_bot = y_top + 1
        draw.rectangle(
            [(x - candle_w // 2, y_top), (x + candle_w // 2, y_bot)],
            fill=color,
        )

        # Volume bar
        vh = int(v / max_vol * vol_h)
        draw.rectangle(
            [(x - candle_w // 2, vol_bot - vh), (x + candle_w // 2, vol_bot)],
            fill=(*color, 120) if len(color) == 3 else color,
        )

    # ── Подсветка момента сигнала (вертикальная полоса) ──
    sx = candle_x(signal_idx)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    band_w = candle_w * 3
    odraw.rectangle(
        [(sx - band_w, chart_top), (sx + band_w, vol_bot)],
        fill=(0, 255, 136, 35),
    )
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # ── Линия entry price ──
    ey = price_y(first_price)
    # Пунктир
    for dx in range(PAD_LEFT, W - PAD_RIGHT, 8):
        draw.line([(dx, ey), (dx + 4, ey)], fill=GOLD, width=1)
    draw.text((W - PAD_RIGHT - 80, ey - 14), _format_price(first_price), fill=GOLD, font=font_sm)

    # ── Стрелка ▲ под сигнальной свечой ──
    arrow_y = price_y(klines[signal_idx]["low"]) + 10
    arrow_size = 10
    draw.polygon(
        [
            (sx, arrow_y + arrow_size),
            (sx - arrow_size, arrow_y - arrow_size // 2),
            (sx + arrow_size, arrow_y - arrow_size // 2),
        ],
        fill=GREEN,
    )

    # ── Заголовок ──
    signal_dt = datetime.fromtimestamp(signal_time_sec, tz=timezone.utc)
    ts_str = signal_dt.strftime("%Y-%m-%d %H:%M UTC")
    usd_label = f"${avg_usd:,.0f}" if avg_usd < 1000 else f"${avg_usd / 1000:.1f}K"
    title = f"{symbol}  •  {strength}  •  {ts_str}"
    subtitle = f"Entry: {_format_price(first_price)}  |  Print: {usd_label}"

    draw.text((PAD_LEFT, 8), title, fill=WHITE, font=font_lg)
    draw.text((PAD_LEFT, 28), subtitle, fill=TEXT, font=font_md)

    # ── Сохраняем в BytesIO ──
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
