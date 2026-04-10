"""
Telegram notifier with login/password authorization and alert preferences.

Commands:
  /start      — begin auth
  /stop       — deauthorize
  /status     — show auth status
  /test       — send test alerts (all levels)
  /settings   — manage alert level preferences
  /markets    — manage market filters (futures/spot)
"""
import aiohttp
import asyncio
import time
import secrets
import json
import os
from config import TELEGRAM_BOT_TOKEN, AUTH_USERS
from chart import generate_signal_chart

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Auth flow states
_STATE_NONE = 0
_STATE_ASK_LOGIN = 1
_STATE_ASK_PASSWORD = 2
_STATE_AUTHORIZED = 3

# Available alert levels
ALL_LEVELS = {"STRONG", "MEDIUM", "WEAK"}
ALL_MARKETS = {"futures", "spot"}

# User preferences file
_PREFS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_prefs.json")


def _load_prefs() -> dict:
    """Load user preferences from file. {chat_id_str: {levels: [...], markets: [...]}}"""
    if os.path.exists(_PREFS_FILE):
        try:
            with open(_PREFS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_prefs(prefs: dict):
    """Save user preferences to file."""
    try:
        with open(_PREFS_FILE, "w") as f:
            json.dump(prefs, f, indent=2, ensure_ascii=False)
    except IOError as e:
        print(f"[TG] Failed to save prefs: {e}", flush=True)


class TelegramNotifier:
    def __init__(self):
        # chat_id -> auth state
        self._auth_states: dict[int, int] = {}
        # chat_id -> entered login (temporary during auth flow)
        self._pending_logins: dict[int, str] = {}
        # set of authorized chat_ids
        self._authorized_chats: set[int] = set()
        # chat_id -> display name
        self._chat_titles: dict[int, str] = {}
        # user preferences: {chat_id: {levels: set, markets: set}}
        self._user_prefs: dict[int, dict] = {}
        self._session: aiohttp.ClientSession | None = None
        self._offset = 0

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=60, sock_connect=10)
        self._session = aiohttp.ClientSession(timeout=timeout)
        # Load persisted preferences
        raw_prefs = _load_prefs()
        for cid_str, pref in raw_prefs.items():
            cid = int(cid_str)
            self._user_prefs[cid] = {
                "levels": set(pref.get("levels", list(ALL_LEVELS))),
                "markets": set(pref.get("markets", list(ALL_MARKETS))),
            }

    async def stop(self):
        if self._session:
            await self._session.close()

    def _get_user_prefs(self, chat_id: int) -> dict:
        """Get user preferences with defaults."""
        if chat_id not in self._user_prefs:
            self._user_prefs[chat_id] = {
                "levels": set(ALL_LEVELS),
                "markets": set(ALL_MARKETS),
            }
        return self._user_prefs[chat_id]

    def _persist_prefs(self):
        """Write current preferences to disk."""
        to_save = {}
        for cid, pref in self._user_prefs.items():
            to_save[str(cid)] = {
                "levels": sorted(pref["levels"]),
                "markets": sorted(pref["markets"]),
            }
        _save_prefs(to_save)

    def _should_send_alert(self, chat_id: int, level: str, market: str) -> bool:
        """Check if this alert should be sent to this user."""
        prefs = self._get_user_prefs(chat_id)
        return level in prefs["levels"] and market in prefs["markets"]

    async def _send_message(self, chat_id: int, text: str, reply_markup: dict | None = None):
        """Send a message to a specific chat."""
        try:
            url = f"{API_BASE}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            async with self._session.post(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"[TG] Send error to {chat_id}: {body[:100]}", flush=True)
        except Exception as e:
            print(f"[TG] Send error to {chat_id}: {type(e).__name__}: {e}", flush=True)

    async def _send_photo(self, chat_id: int, photo_buf, caption: str):
        """Send a photo to a specific chat."""
        try:
            photo_buf.seek(0)
            form = aiohttp.FormData()
            form.add_field("chat_id", str(chat_id))
            form.add_field("caption", caption)
            form.add_field("parse_mode", "HTML")
            form.add_field(
                "photo",
                photo_buf,
                filename="signal.png",
                content_type="image/png",
            )
            async with self._session.post(
                f"{API_BASE}/sendPhoto",
                data=form,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 403:
                    self._authorized_chats.discard(chat_id)
                    self._auth_states[chat_id] = _STATE_NONE
                    return False
                elif resp.status != 200:
                    body = await resp.text()
                    print(f"[TG] Photo error to {chat_id}: {body[:200]}", flush=True)
                return resp.status == 200
        except Exception as e:
            print(f"[TG] Photo error to {chat_id}: {type(e).__name__}: {e}", flush=True)
            return False

    def _format_prefs_text(self, chat_id: int) -> str:
        """Format current preferences for display."""
        prefs = self._get_user_prefs(chat_id)
        level_names = {"STRONG": "🔴 Сильные", "MEDIUM": "🟡 Средние", "WEAK": "🟢 Слабые"}
        market_names = {"futures": "📈 Фьючерсы", "spot": "💱 Спот"}

        lines = ["⚙️ <b>Настройки алертов</b>\n"]
        lines.append("<b>Уровни:</b>")
        for lvl in ("STRONG", "MEDIUM", "WEAK"):
            check = "✅" if lvl in prefs["levels"] else "❌"
            lines.append(f"  {check} {level_names[lvl]}")
        lines.append("\n<b>Рынки:</b>")
        for mkt in ("futures", "spot"):
            check = "✅" if mkt in prefs["markets"] else "❌"
            lines.append(f"  {check} {market_names[mkt]}")

        return "\n".join(lines)

    async def _handle_message(self, msg: dict):
        """Process an incoming message."""
        chat = msg.get("chat", {})
        chat_id = chat["id"]
        text = msg.get("text", "").strip()
        title = chat.get("title") or chat.get("first_name", str(chat_id))
        self._chat_titles[chat_id] = title

        state = self._auth_states.get(chat_id, _STATE_NONE)

        # ── Callback query (inline button press) ──
        # Handled separately in _handle_callback_query

        # /start — begin auth or show help if already authorized
        if text == "/start":
            if state == _STATE_AUTHORIZED:
                await self._send_message(chat_id,
                    "✅ Вы уже авторизованы.\n"
                    "Алерты будут приходить автоматически.\n\n"
                    "/settings — настройки алертов\n"
                    "/stop — отключиться от алертов"
                )
            else:
                self._auth_states[chat_id] = _STATE_ASK_LOGIN
                await self._send_message(chat_id,
                    "🔐 <b>Авторизация</b>\n\n"
                    "Введите ваш <b>логин</b>:"
                )
            return

        # /stop — deauthorize
        if text == "/stop":
            self._authorized_chats.discard(chat_id)
            self._auth_states[chat_id] = _STATE_NONE
            self._pending_logins.pop(chat_id, None)
            await self._send_message(chat_id, "🚫 Вы отключены от алертов.\n/start — авторизоваться заново.")
            return

        # /status
        if text == "/status":
            if state == _STATE_AUTHORIZED:
                prefs = self._get_user_prefs(chat_id)
                level_names = {"STRONG": "Сильные", "MEDIUM": "Средние", "WEAK": "Слабые"}
                market_names = {"futures": "Фьючерсы", "spot": "Спот"}
                lvls = ", ".join(level_names.get(l, l) for l in sorted(prefs["levels"]))
                mkts = ", ".join(market_names.get(m, m) for m in sorted(prefs["markets"]))
                await self._send_message(chat_id,
                    f"✅ Авторизован: <b>{title}</b>\n"
                    f"📊 Активных чатов: {len(self._authorized_chats)}\n"
                    f"🔔 Уровни: {lvls}\n"
                    f"📈 Рынки: {mkts}"
                )
            else:
                await self._send_message(chat_id, "❌ Не авторизован.\n/start — войти.")
            return

        # /settings — alert level & market preferences
        if text == "/settings":
            if state != _STATE_AUTHORIZED:
                await self._send_message(chat_id, "❌ Не авторизован.\n/start — войти.")
                return

            prefs_text = self._format_prefs_text(chat_id)
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🔴 Сильные", "callback_data": "toggle:level:STRONG"},
                        {"text": "🟡 Средние", "callback_data": "toggle:level:MEDIUM"},
                        {"text": "🟢 Слабые", "callback_data": "toggle:level:WEAK"},
                    ],
                    [
                        {"text": "📈 Фьючерсы", "callback_data": "toggle:market:futures"},
                        {"text": "💱 Спот", "callback_data": "toggle:market:spot"},
                    ],
                    [
                        {"text": "✅ Все", "callback_data": "toggle:all:on"},
                        {"text": "❌ Сброс", "callback_data": "toggle:all:off"},
                    ],
                ]
            }
            await self._send_message(chat_id, prefs_text, reply_markup=keyboard)
            return

        # /test — send a test alert at each level
        if text == "/test":
            if state != _STATE_AUTHORIZED:
                await self._send_message(chat_id, "❌ Не авторизован.\n/start — войти.")
                return
            await self._send_message(chat_id, "🧪 Отправляю тестовые алерты...")
            try:
                await self.send_alert(
                    symbol="BTCUSDT", side="BUY", count=5, avg_qty=0.025,
                    avg_interval=1.5, first_price=83500.0,
                    strength="🟢 СЛАБЫЙ BUY", avg_usd=2075.0,
                    signal_time_sec=time.time(), market="futures", level="WEAK",
                )
                await asyncio.sleep(0.5)
                await self.send_alert(
                    symbol="ETHUSDT", side="SELL", count=5, avg_qty=2.5,
                    avg_interval=0.7, first_price=1850.0,
                    strength="🟡 СРЕДНИЙ SELL", avg_usd=4625.0,
                    signal_time_sec=time.time(), market="spot", level="MEDIUM",
                )
                await asyncio.sleep(0.5)
                await self.send_alert(
                    symbol="SOLUSDT", side="BUY", count=8, avg_qty=50.0,
                    avg_interval=0.3, first_price=150.0,
                    strength="🔴 СИЛЬНЫЙ BUY", avg_usd=7500.0,
                    signal_time_sec=time.time(), market="futures", level="STRONG",
                )
                await self._send_message(chat_id, "✅ Тестовые алерты отправлены!")
            except Exception as e:
                await self._send_message(chat_id, f"❌ Ошибка: {type(e).__name__}: {e}")
            return

        # Auth flow: waiting for login
        if state == _STATE_ASK_LOGIN:
            self._pending_logins[chat_id] = text
            self._auth_states[chat_id] = _STATE_ASK_PASSWORD
            await self._send_message(chat_id, "🔑 Введите <b>пароль</b>:")
            return

        # Auth flow: waiting for password
        if state == _STATE_ASK_PASSWORD:
            login = self._pending_logins.pop(chat_id, "")

            if not AUTH_USERS:
                self._auth_states[chat_id] = _STATE_NONE
                await self._send_message(chat_id, "❌ Сервер: пользователи не настроены.")
                return

            if login not in AUTH_USERS:
                self._auth_states[chat_id] = _STATE_NONE
                await self._send_message(chat_id,
                    "❌ Неверный логин или пароль.\n/start — попробовать снова."
                )
                print(f"[TG] Auth failed for chat {chat_id} ({title}): unknown login '{login}'", flush=True)
                return

            correct_password = AUTH_USERS[login]
            is_correct = secrets.compare_digest(
                text.encode("utf-8"),
                correct_password.encode("utf-8"),
            )

            if not is_correct:
                self._auth_states[chat_id] = _STATE_NONE
                await self._send_message(chat_id,
                    "❌ Неверный логин или пароль.\n/start — попробовать снова."
                )
                print(f"[TG] Auth failed for chat {chat_id} ({title}): wrong password for '{login}'", flush=True)
                return

            # Success
            self._authorized_chats.add(chat_id)
            self._auth_states[chat_id] = _STATE_AUTHORIZED
            print(f"[TG] Auth OK: {login} → chat {chat_id} ({title})", flush=True)
            await self._send_message(chat_id,
                f"✅ <b>Добро пожаловать, {login}!</b>\n\n"
                "Алерты будут приходить автоматически.\n"
                "/settings — настройки алертов\n"
                "/stop — отключиться\n"
                "/status — проверить статус"
            )
            return

        # Not in auth flow, not authorized — prompt to start
        if state != _STATE_AUTHORIZED:
            await self._send_message(chat_id, "🔐 Введите /start для авторизации.")

    async def _handle_callback_query(self, cb: dict):
        """Handle inline button presses from /settings."""
        chat_id = cb["message"]["chat"]["id"]
        data = cb.get("data", "")
        cb_id = cb.get("id", "")

        state = self._auth_states.get(chat_id, _STATE_NONE)
        if state != _STATE_AUTHORIZED:
            await self._answer_callback(cb_id, "❌ Не авторизован")
            return

        prefs = self._get_user_prefs(chat_id)

        # Parse toggle action
        parts = data.split(":")
        if len(parts) == 3 and parts[0] == "toggle":
            category = parts[1]  # "level", "market", or "all"
            value = parts[2]

            if category == "level" and value in ALL_LEVELS:
                if value in prefs["levels"]:
                    prefs["levels"].discard(value)
                else:
                    prefs["levels"].add(value)
                # Ensure at least one level is active
                if not prefs["levels"]:
                    prefs["levels"].add(value)

            elif category == "market" and value in ALL_MARKETS:
                if value in prefs["markets"]:
                    prefs["markets"].discard(value)
                else:
                    prefs["markets"].add(value)
                if not prefs["markets"]:
                    prefs["markets"].add(value)

            elif category == "all":
                if value == "on":
                    prefs["levels"] = set(ALL_LEVELS)
                    prefs["markets"] = set(ALL_MARKETS)
                elif value == "off":
                    # Keep at least one active
                    prefs["levels"] = {"STRONG"}
                    prefs["markets"] = set(ALL_MARKETS)

            self._persist_prefs()

            # Update message with new prefs
            prefs_text = self._format_prefs_text(chat_id)
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🔴 Сильные", "callback_data": "toggle:level:STRONG"},
                        {"text": "🟡 Средние", "callback_data": "toggle:level:MEDIUM"},
                        {"text": "🟢 Слабые", "callback_data": "toggle:level:WEAK"},
                    ],
                    [
                        {"text": "📈 Фьючерсы", "callback_data": "toggle:market:futures"},
                        {"text": "💱 Спот", "callback_data": "toggle:market:spot"},
                    ],
                    [
                        {"text": "✅ Все", "callback_data": "toggle:all:on"},
                        {"text": "❌ Сброс", "callback_data": "toggle:all:off"},
                    ],
                ]
            }

            await self._edit_message(chat_id, cb["message"]["message_id"], prefs_text, keyboard)
            await self._answer_callback(cb_id, "✅ Настройки обновлены")

    async def _answer_callback(self, callback_query_id: str, text: str = ""):
        """Answer a callback query to remove loading spinner."""
        try:
            url = f"{API_BASE}/answerCallbackQuery"
            payload = {"callback_query_id": callback_query_id}
            if text:
                payload["text"] = text
            async with self._session.post(url, json=payload,
                timeout=aiohttp.ClientTimeout(total=5)) as resp:
                pass
        except Exception:
            pass

    async def _edit_message(self, chat_id: int, message_id: int, text: str, reply_markup: dict):
        """Edit an existing message (for inline keyboard updates)."""
        try:
            url = f"{API_BASE}/editMessageText"
            payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
            }
            async with self._session.post(url, json=payload,
                timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"[TG] Edit error: {body[:100]}", flush=True)
        except Exception as e:
            print(f"[TG] Edit error: {type(e).__name__}: {e}", flush=True)

    async def _poll_updates(self):
        """Long-poll Telegram for new messages and callback queries."""
        while True:
            try:
                url = f"{API_BASE}/getUpdates"
                params = {"offset": self._offset, "timeout": 30}
                async with self._session.get(url, params=params) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        for update in data.get("result", []):
                            self._offset = update["update_id"] + 1
                            msg = update.get("message")
                            if msg:
                                await self._handle_message(msg)
                            cb = update.get("callback_query")
                            if cb:
                                await self._handle_callback_query(cb)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[TG] Poll error: {type(e).__name__}: {e}", flush=True)
                await asyncio.sleep(5)

    def start_polling(self):
        """Start the message polling loop. Returns an asyncio.Task."""
        return asyncio.create_task(self._poll_updates())

    async def send_alert(self, symbol: str, side: str, count: int, avg_qty: float,
                         avg_interval: float, first_price: float,
                         strength: str, avg_usd: float,
                         signal_time_sec: float,
                         market: str = "futures", level: str = "WEAK"):
        """Send chart + text alert to authorized chats (filtered by user preferences)."""
        if not self._authorized_chats:
            print("[TG] No authorized chats. Alerts not sent.", flush=True)
            return

        # Generate chart once (shared across all recipients)
        loop = asyncio.get_event_loop()
        try:
            chart_buf = await loop.run_in_executor(
                None,
                lambda: generate_signal_chart(
                    symbol=symbol,
                    signal_time_sec=signal_time_sec,
                    first_price=first_price,
                    strength=strength,
                    avg_usd=avg_usd,
                    side=side,
                    market=market,
                )
            )
        except Exception as e:
            print(f"[TG] Chart generation failed: {e}", flush=True)
            chart_buf = None

        # Build alert text
        qty_str = _format_qty(avg_qty)
        usd_str = _format_usd(avg_usd)
        side_emoji = "🔴" if side == "SELL" else "🟢"
        market_label = "📈 Фьючерсы" if market == "futures" else "💱 Спот"
        level_names = {"STRONG": "Сильный", "MEDIUM": "Средний", "WEAK": "Слабый"}

        text_msg = (
            f"{side_emoji} {strength}\n"
            f"\n"
            f"📊 <b>Пара:</b> {symbol}\n"
            f"🏪 <b>Рынок:</b> {market_label}\n"
            f"📈 <b>Направление:</b> {side}\n"
            f"💰 <b>Цена:</b> {first_price}\n"
            f"🔄 <b>Принтов:</b> {count}\n"
            f"📦 <b>Размер:</b> {qty_str}\n"
            f"💵 <b>$/принт:</b> {usd_str}\n"
            f"⏱ <b>Интервал:</b> {avg_interval:.2f}с\n"
            f"🏷 <b>Уровень:</b> {level_names.get(level, level)}"
        )

        caption_emoji = "🔴" if side == "SELL" else "🟢"
        caption = f"{caption_emoji} <b>{symbol}</b> • {side} • {market_label}"

        # Send to each authorized user (filtered by their preferences)
        for cid in list(self._authorized_chats):
            if not self._should_send_alert(cid, level, market):
                continue

            try:
                # 1) Photo with chart
                if chart_buf:
                    await self._send_photo(cid, chart_buf, caption)

                # 2) Text with details
                await self._send_message(cid, text_msg)

            except Exception as e:
                print(f"[TG] Send error to {cid}: {type(e).__name__}: {e}", flush=True)


def _format_qty(qty: float) -> str:
    if qty >= 1_000_000:
        return f"{qty / 1_000_000:.2f}M"
    elif qty >= 1_000:
        return f"{qty / 1_000:.2f}K"
    elif qty >= 1:
        return f"{qty:.2f}"
    elif qty >= 0.01:
        return f"{qty:.4f}"
    else:
        return f"{qty:.8f}"


def _format_usd(usd: float) -> str:
    if usd >= 1_000_000:
        return f"${usd / 1_000_000:.2f}M"
    elif usd >= 1_000:
        return f"${usd / 1_000:.1f}K"
    else:
        return f"${usd:.0f}"
