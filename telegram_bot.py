"""
Telegram notifier with login/password authorization.

Flow:
  1. User sends /start
  2. Bot asks for login
  3. User sends login
  4. Bot asks for password
  5. User sends password → bot validates against users.json
  6. Authorized chat receives alerts
"""
import aiohttp
import asyncio
import time
import secrets
from config import TELEGRAM_BOT_TOKEN, AUTH_USERS
from chart import generate_signal_chart

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Auth flow states
_STATE_NONE = 0
_STATE_ASK_LOGIN = 1
_STATE_ASK_PASSWORD = 2
_STATE_AUTHORIZED = 3


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
        self._session: aiohttp.ClientSession | None = None
        self._offset = 0
        self._last_discovery = 0

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=60, sock_connect=10)
        self._session = aiohttp.ClientSession(timeout=timeout)

    async def stop(self):
        if self._session:
            await self._session.close()

    async def _send_message(self, chat_id: int, text: str):
        """Send a message to a specific chat."""
        try:
            url = f"{API_BASE}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            }
            async with self._session.post(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"[TG] Send error to {chat_id}: {body[:100]}", flush=True)
        except Exception as e:
            print(f"[TG] Send error to {chat_id}: {type(e).__name__}: {e}", flush=True)

    async def _handle_message(self, msg: dict):
        """Process an incoming message."""
        chat = msg.get("chat", {})
        chat_id = chat["id"]
        text = msg.get("text", "").strip()
        title = chat.get("title") or chat.get("first_name", str(chat_id))
        self._chat_titles[chat_id] = title

        state = self._auth_states.get(chat_id, _STATE_NONE)

        # /start — begin auth or show help if already authorized
        if text == "/start":
            if state == _STATE_AUTHORIZED:
                await self._send_message(chat_id,
                    "✅ Вы уже авторизованы.\n"
                    "Алерты будут приходить автоматически.\n\n"
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
                await self._send_message(chat_id,
                    f"✅ Авторизован: <b>{title}</b>\n"
                    f"📊 Активных чатов: {len(self._authorized_chats)}"
                )
            else:
                await self._send_message(chat_id, "❌ Не авторизован.\n/start — войти.")
            return

        # /test — send a test alert
        if text == "/test":
            if state != _STATE_AUTHORIZED:
                await self._send_message(chat_id, "❌ Не авторизован.\n/start — войти.")
                return
            await self._send_message(chat_id, "🧪 Отправляю тестовый алерт...")
            try:
                await self.send_alert(
                    symbol="BTCUSDT",
                    count=5,
                    avg_qty=0.025,
                    avg_interval=1.05,
                    first_price=83500.0,
                    strength="🟢 ТЕСТОВЫЙ АЛЕРТ",
                    avg_usd=115.0,
                    signal_time_sec=time.time(),
                )
                await self._send_message(chat_id, "✅ Тестовый алерт отправлен!")
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
                "/stop — отключиться\n"
                "/status — проверить статус"
            )
            return

        # Not in auth flow, not authorized — prompt to start
        if state != _STATE_AUTHORIZED:
            await self._send_message(chat_id, "🔐 Введите /start для авторизации.")

    async def _poll_updates(self):
        """Long-poll Telegram for new messages."""
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
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[TG] Poll error: {type(e).__name__}: {e}", flush=True)
                await asyncio.sleep(5)

    def start_polling(self):
        """Start the message polling loop. Returns an asyncio.Task."""
        return asyncio.create_task(self._poll_updates())

    async def send_alert(self, symbol: str, count: int, avg_qty: float,
                         avg_interval: float, first_price: float,
                         strength: str, avg_usd: float,
                         signal_time_sec: float):
        """Send chart + text alert to authorized chats."""
        if not self._authorized_chats:
            print("[TG] No authorized chats. Alerts not sent.", flush=True)
            return

        # ── Текст алерта (отдельным сообщением) ──
        qty_str = _format_qty(avg_qty)
        usd_str = _format_usd(avg_usd)
        text_msg = (
            f"{strength}\n"
            f"\n"
            f"📊 <b>Пара:</b> {symbol}\n"
            f"💰 <b>Цена:</b> {first_price}\n"
            f"🔄 <b>Принтов:</b> {count}\n"
            f"📦 <b>Размер:</b> {qty_str}\n"
            f"💵 <b>$/принт:</b> {usd_str}\n"
            f"⏱ <b>Интервал:</b> {avg_interval:.2f}с"
        )

        # ── Генерируем график (в executor, чтобы не блочить event loop) ──
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
                )
            )
        except Exception as e:
            print(f"[TG] Chart generation failed: {e}", flush=True)
            chart_buf = None

        for cid in list(self._authorized_chats):
            try:
                # 1) Отправляем фото с графиком
                if chart_buf:
                    chart_buf.seek(0)
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(cid))
                    form.add_field("caption", f"📈 <b>{symbol}</b>")
                    form.add_field("parse_mode", "HTML")
                    form.add_field(
                        "photo",
                        chart_buf,
                        filename=f"{symbol}_signal.png",
                        content_type="image/png",
                    )
                    async with self._session.post(
                        f"{API_BASE}/sendPhoto",
                        data=form,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status == 403:
                            self._authorized_chats.discard(cid)
                            self._auth_states[cid] = _STATE_NONE
                            continue
                        elif resp.status != 200:
                            body = await resp.text()
                            print(f"[TG] Photo error to {cid}: {body[:200]}", flush=True)

                # 2) Отправляем текст с деталями
                payload = {
                    "chat_id": cid,
                    "text": text_msg,
                    "parse_mode": "HTML",
                }
                async with self._session.post(
                    f"{API_BASE}/sendMessage",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 403:
                        self._authorized_chats.discard(cid)
                        self._auth_states[cid] = _STATE_NONE
                    elif resp.status != 200:
                        body = await resp.text()
                        print(f"[TG] Text error to {cid}: {body[:100]}", flush=True)

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
