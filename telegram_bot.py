"""
Telegram notifier with login/password authorization, alert preferences,
and admin panel for user management.

Commands:
  /start        — begin auth
  /stop         — deauthorize
  /status       — show auth status
  /test         — send test alerts (all levels)
  /settings     — manage alert level preferences
  /markets      — manage market filters (futures/spot)

Admin commands (admin only):
  /admin        — open admin panel
  /adduser <login> <password>   — add new user
  /removeuser <login>           — remove user
  /listusers                    — list all users
  /changepass <login> <password> — change user password
"""
import aiohttp
import asyncio
import time
import secrets
import json
import os
from config import (
    TELEGRAM_BOT_TOKEN, AUTH_USERS,
    reload_auth_users, save_auth_users, ADMIN_LOGINS,
)
from chart import generate_signal_chart

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Auth flow states
_STATE_NONE = 0
_STATE_ASK_LOGIN = 1
_STATE_ASK_PASSWORD = 2
_STATE_AUTHORIZED = 3

# Admin flow states
_ADMIN_NONE = 0
_ADMIN_ADD_LOGIN = 1
_ADMIN_ADD_PASSWORD = 2
_ADMIN_CHANGE_LOGIN = 3
_ADMIN_CHANGE_PASSWORD = 4
_ADMIN_REMOVE_CONFIRM = 5

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
        # chat_id -> logged-in login name
        self._chat_logins: dict[int, str] = {}
        # chat_id -> display name
        self._chat_titles: dict[int, str] = {}
        # user preferences: {chat_id: {levels: set, markets: set}}
        self._user_prefs: dict[int, dict] = {}
        # admin flow states: {chat_id: (state, data)}
        self._admin_states: dict[int, tuple] = {}
        self._session: aiohttp.ClientSession | None = None
        self._offset = 0

    # ── Helpers ──

    def _is_admin(self, chat_id: int) -> bool:
        """Check if the logged-in user is an admin."""
        login = self._chat_logins.get(chat_id, "")
        return login in ADMIN_LOGINS

    def _is_authorized(self, chat_id: int) -> bool:
        """Check if chat is authorized."""
        return self._auth_states.get(chat_id) == _STATE_AUTHORIZED

    # ── Lifecycle ──

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

    # ── Preferences ──

    def _get_user_prefs(self, chat_id: int) -> dict:
        if chat_id not in self._user_prefs:
            self._user_prefs[chat_id] = {
                "levels": set(ALL_LEVELS),
                "markets": set(ALL_MARKETS),
            }
        return self._user_prefs[chat_id]

    def _persist_prefs(self):
        to_save = {}
        for cid, pref in self._user_prefs.items():
            to_save[str(cid)] = {
                "levels": sorted(pref["levels"]),
                "markets": sorted(pref["markets"]),
            }
        _save_prefs(to_save)

    def _should_send_alert(self, chat_id: int, level: str, market: str) -> bool:
        prefs = self._get_user_prefs(chat_id)
        return level in prefs["levels"] and market in prefs["markets"]

    # ── Telegram API helpers ──

    async def _send_message(self, chat_id: int, text: str, reply_markup: dict | None = None):
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

    async def _answer_callback(self, callback_query_id: str, text: str = ""):
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

    # ── Formatting ──

    def _format_prefs_text(self, chat_id: int) -> str:
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

    def _format_admin_panel(self) -> str:
        """Format admin panel text."""
        users = reload_auth_users()
        lines = [
            "👑 <b>Админ-панель</b>\n",
            f"👥 Пользователей: <b>{len(users)}</b>\n",
            "<b>Команды:</b>",
            "  /adduser &lt;логин&gt; &lt;пароль&gt; — добавить",
            "  /removeuser &lt;логин&gt; — удалить",
            "  /changepass &lt;логин&gt; &lt;пароль&gt; — сменить пароль",
            "  /listusers — список всех",
            "",
            "Или нажмите кнопку ниже 👇",
        ]
        return "\n".join(lines)

    def _format_user_list(self) -> str:
        """Format the user list for admin."""
        users = reload_auth_users()
        if not users:
            return "📋 <b>Пользователи</b>\n\nСписок пуст."
        lines = [f"📋 <b>Пользователи</b> ({len(users)}):\n"]
        for i, login in enumerate(sorted(users.keys()), 1):
            admin_badge = " 👑" if login in ADMIN_LOGINS else ""
            lines.append(f"  {i}. <code>{login}</code>{admin_badge}")
        return "\n".join(lines)

    # ── Admin handlers ──

    async def _handle_admin_command(self, chat_id: int, text: str) -> bool:
        """
        Handle admin commands. Returns True if command was handled.
        Also handles multi-step admin flows.
        """
        # Check admin flow states first
        admin_state = self._admin_states.get(chat_id)

        if admin_state:
            state, data = admin_state

            if state == _ADMIN_ADD_LOGIN:
                # User typed the new login
                new_login = text.strip()
                if not new_login:
                    await self._send_message(chat_id, "❌ Логин не может быть пустым. Введите логин:")
                    return True
                users = reload_auth_users()
                if new_login in users:
                    await self._send_message(chat_id,
                        f"❌ Пользователь <code>{new_login}</code> уже существует.\n"
                        "Введите другой логин или /admin для отмены:"
                    )
                    return True
                self._admin_states[chat_id] = (_ADMIN_ADD_PASSWORD, new_login)
                await self._send_message(chat_id,
                    f"✅ Логин: <code>{new_login}</code>\n\n"
                    "🔑 Введите <b>пароль</b> для нового пользователя:"
                )
                return True

            if state == _ADMIN_ADD_PASSWORD:
                # User typed the new password
                new_login = data
                new_password = text.strip()
                if not new_password:
                    await self._send_message(chat_id, "❌ Пароль не может быть пустым. Введите пароль:")
                    return True
                users = reload_auth_users()
                users[new_login] = new_password
                save_auth_users(users)
                self._admin_states.pop(chat_id, None)
                print(f"[TG] Admin: user '{new_login}' added by {self._chat_logins.get(chat_id)}", flush=True)
                await self._send_message(chat_id,
                    f"✅ <b>Пользователь добавлен!</b>\n\n"
                    f"👤 Логин: <code>{new_login}</code>\n"
                    f"🔑 Пароль: <code>{new_password}</code>\n\n"
                    f"⚠️ Пользователь может авторизоваться сразу (без перезапуска)."
                )
                return True

            if state == _ADMIN_CHANGE_LOGIN:
                # User typed the login to change password for
                target_login = text.strip()
                users = reload_auth_users()
                if target_login not in users:
                    await self._send_message(chat_id,
                        f"❌ Пользователь <code>{target_login}</code> не найден.\n"
                        "Введите другой логин или /admin для отмены:"
                    )
                    return True
                self._admin_states[chat_id] = (_ADMIN_CHANGE_PASSWORD, target_login)
                await self._send_message(chat_id,
                    f"✅ Найден: <code>{target_login}</code>\n\n"
                    "🔑 Введите <b>новый пароль</b>:"
                )
                return True

            if state == _ADMIN_CHANGE_PASSWORD:
                # User typed the new password
                target_login = data
                new_password = text.strip()
                if not new_password:
                    await self._send_message(chat_id, "❌ Пароль не может быть пустым. Введите пароль:")
                    return True
                users = reload_auth_users()
                users[target_login] = new_password
                save_auth_users(users)
                self._admin_states.pop(chat_id, None)
                print(f"[TG] Admin: password changed for '{target_login}' by {self._chat_logins.get(chat_id)}", flush=True)
                await self._send_message(chat_id,
                    f"✅ <b>Пароль изменён!</b>\n\n"
                    f"👤 Логин: <code>{target_login}</code>\n"
                    f"🔑 Новый пароль: <code>{new_password}</code>"
                )
                return True

            if state == _ADMIN_REMOVE_CONFIRM:
                target_login = data
                if text.strip().lower() in ("да", "yes", "y", "д"):
                    users = reload_auth_users()
                    if target_login in users:
                        # Don't allow removing admin
                        if target_login in ADMIN_LOGINS:
                            self._admin_states.pop(chat_id, None)
                            await self._send_message(chat_id, "❌ Нельзя удалить администратора!")
                            return True
                        del users[target_login]
                        save_auth_users(users)
                        # Deauthorize any sessions with this login
                        for cid, login in list(self._chat_logins.items()):
                            if login == target_login:
                                self._authorized_chats.discard(cid)
                                self._auth_states[cid] = _STATE_NONE
                                self._chat_logins.pop(cid, None)
                        print(f"[TG] Admin: user '{target_login}' removed by {self._chat_logins.get(chat_id)}", flush=True)
                        self._admin_states.pop(chat_id, None)
                        await self._send_message(chat_id,
                            f"✅ Пользователь <code>{target_login}</code> удалён.\n"
                            f"Активные сессии деавторизованы."
                        )
                    else:
                        self._admin_states.pop(chat_id, None)
                        await self._send_message(chat_id, f"❌ Пользователь <code>{target_login}</code> не найден.")
                else:
                    self._admin_states.pop(chat_id, None)
                    await self._send_message(chat_id, "❌ Удаление отменено.")
                return True

        # Admin commands (not in flow)
        if not self._is_admin(chat_id):
            return False

        if text == "/admin":
            panel_text = self._format_admin_panel()
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "📋 Список пользователей", "callback_data": "admin:list"},
                        {"text": "➕ Добавить", "callback_data": "admin:add"},
                    ],
                    [
                        {"text": "🔑 Сменить пароль", "callback_data": "admin:change"},
                        {"text": "🗑 Удалить", "callback_data": "admin:remove"},
                    ],
                ]
            }
            await self._send_message(chat_id, panel_text, reply_markup=keyboard)
            return True

        if text.startswith("/adduser"):
            parts = text.split(maxsplit=2)
            if len(parts) == 3:
                # Inline: /adduser login password
                _, new_login, new_password = parts
                users = reload_auth_users()
                if new_login in users:
                    await self._send_message(chat_id,
                        f"❌ Пользователь <code>{new_login}</code> уже существует.")
                    return True
                users[new_login] = new_password
                save_auth_users(users)
                print(f"[TG] Admin: user '{new_login}' added by {self._chat_logins.get(chat_id)}", flush=True)
                await self._send_message(chat_id,
                    f"✅ <b>Пользователь добавлен!</b>\n\n"
                    f"👤 Логин: <code>{new_login}</code>\n"
                    f"🔑 Пароль: <code>{new_password}</code>"
                )
            else:
                # Interactive flow
                self._admin_states[chat_id] = (_ADMIN_ADD_LOGIN, None)
                await self._send_message(chat_id,
                    "➕ <b>Добавление пользователя</b>\n\n"
                    "Введите <b>логин</b> нового пользователя:"
                )
            return True

        if text.startswith("/removeuser"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                target_login = parts[1].strip()
                if target_login in ADMIN_LOGINS:
                    await self._send_message(chat_id, "❌ Нельзя удалить администратора!")
                    return True
                users = reload_auth_users()
                if target_login not in users:
                    await self._send_message(chat_id,
                        f"❌ Пользователь <code>{target_login}</code> не найден.")
                    return True
                self._admin_states[chat_id] = (_ADMIN_REMOVE_CONFIRM, target_login)
                await self._send_message(chat_id,
                    f"⚠️ <b>Подтверждение удаления</b>\n\n"
                    f"Удалить пользователя <code>{target_login}</code>?\n"
                    f"Ответьте <b>да</b> или <b>нет</b>."
                )
            else:
                # Show user list with remove buttons
                users = reload_auth_users()
                removable = [u for u in sorted(users.keys()) if u not in ADMIN_LOGINS]
                if not removable:
                    await self._send_message(chat_id, "📋 Нет пользователей для удаления.")
                    return True
                keyboard_rows = []
                row = []
                for i, login in enumerate(removable):
                    row.append({"text": f"🗑 {login}", "callback_data": f"admin:rmconfirm:{login}"})
                    if len(row) == 3:
                        keyboard_rows.append(row)
                        row = []
                if row:
                    keyboard_rows.append(row)
                await self._send_message(chat_id,
                    "🗑 <b>Удаление пользователя</b>\n\nВыберите пользователя:",
                    reply_markup={"inline_keyboard": keyboard_rows}
                )
            return True

        if text.startswith("/changepass"):
            parts = text.split(maxsplit=2)
            if len(parts) == 3:
                _, target_login, new_password = parts
                users = reload_auth_users()
                if target_login not in users:
                    await self._send_message(chat_id,
                        f"❌ Пользователь <code>{target_login}</code> не найден.")
                    return True
                users[target_login] = new_password
                save_auth_users(users)
                print(f"[TG] Admin: password changed for '{target_login}' by {self._chat_logins.get(chat_id)}", flush=True)
                await self._send_message(chat_id,
                    f"✅ <b>Пароль изменён!</b>\n\n"
                    f"👤 Логин: <code>{target_login}</code>\n"
                    f"🔑 Новый пароль: <code>{new_password}</code>"
                )
            else:
                self._admin_states[chat_id] = (_ADMIN_CHANGE_LOGIN, None)
                await self._send_message(chat_id,
                    "🔑 <b>Смена пароля</b>\n\n"
                    "Введите <b>логин</b> пользователя:"
                )
            return True

        if text == "/listusers":
            list_text = self._format_user_list()
            await self._send_message(chat_id, list_text)
            return True

        return False

    # ── Admin callback handler ──

    async def _handle_admin_callback(self, chat_id: int, cb_id: str, data: str, msg_id: int) -> bool:
        """Handle admin inline button callbacks. Returns True if handled."""
        if not self._is_admin(chat_id):
            await self._answer_callback(cb_id, "❌ Нет доступа")
            return True

        if data == "admin:list":
            list_text = self._format_user_list()
            users = reload_auth_users()
            # Add inline remove buttons for non-admin users
            removable = [u for u in sorted(users.keys()) if u not in ADMIN_LOGINS]
            keyboard_rows = []
            row = []
            for i, login in enumerate(removable):
                row.append({"text": f"🗑 {login}", "callback_data": f"admin:rmconfirm:{login}"})
                if len(row) == 3:
                    keyboard_rows.append(row)
                    row = []
            if row:
                keyboard_rows.append(row)
            if not keyboard_rows:
                keyboard_rows = [[{"text": "🔙 Назад", "callback_data": "admin:back"}]]
            else:
                keyboard_rows.append([{"text": "🔙 Назад", "callback_data": "admin:back"}])

            await self._edit_message(chat_id, msg_id, list_text,
                reply_markup={"inline_keyboard": keyboard_rows})
            await self._answer_callback(cb_id)
            return True

        if data == "admin:add":
            self._admin_states[chat_id] = (_ADMIN_ADD_LOGIN, None)
            await self._edit_message(chat_id, msg_id,
                "➕ <b>Добавление пользователя</b>\n\n"
                "Отправьте: <code>/adduser логин пароль</code>\n"
                "Или введите логин следующим сообщением:",
                reply_markup={"inline_keyboard": [
                    [{"text": "🔙 Назад", "callback_data": "admin:back"}]
                ]}
            )
            await self._answer_callback(cb_id)
            return True

        if data == "admin:change":
            users = reload_auth_users()
            if not users:
                await self._answer_callback(cb_id, "Нет пользователей")
                return True
            keyboard_rows = []
            row = []
            for login in sorted(users.keys()):
                row.append({"text": f"🔑 {login}", "callback_data": f"admin:chpass:{login}"})
                if len(row) == 3:
                    keyboard_rows.append(row)
                    row = []
            if row:
                keyboard_rows.append(row)
            keyboard_rows.append([{"text": "🔙 Назад", "callback_data": "admin:back"}])
            await self._edit_message(chat_id, msg_id,
                "🔑 <b>Смена пароля</b>\n\nВыберите пользователя:",
                reply_markup={"inline_keyboard": keyboard_rows})
            await self._answer_callback(cb_id)
            return True

        if data == "admin:remove":
            users = reload_auth_users()
            removable = [u for u in sorted(users.keys()) if u not in ADMIN_LOGINS]
            if not removable:
                await self._answer_callback(cb_id, "Нет пользователей для удаления")
                return True
            keyboard_rows = []
            row = []
            for login in removable:
                row.append({"text": f"🗑 {login}", "callback_data": f"admin:rmconfirm:{login}"})
                if len(row) == 3:
                    keyboard_rows.append(row)
                    row = []
            if row:
                keyboard_rows.append(row)
            keyboard_rows.append([{"text": "🔙 Назад", "callback_data": "admin:back"}])
            await self._edit_message(chat_id, msg_id,
                "🗑 <b>Удаление пользователя</b>\n\nВыберите пользователя:",
                reply_markup={"inline_keyboard": keyboard_rows})
            await self._answer_callback(cb_id)
            return True

        if data == "admin:back":
            panel_text = self._format_admin_panel()
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "📋 Список пользователей", "callback_data": "admin:list"},
                        {"text": "➕ Добавить", "callback_data": "admin:add"},
                    ],
                    [
                        {"text": "🔑 Сменить пароль", "callback_data": "admin:change"},
                        {"text": "🗑 Удалить", "callback_data": "admin:remove"},
                    ],
                ]
            }
            await self._edit_message(chat_id, msg_id, panel_text, reply_markup=keyboard)
            await self._answer_callback(cb_id)
            return True

        if data.startswith("admin:rmconfirm:"):
            target_login = data.split(":", 2)[2]
            await self._edit_message(chat_id, msg_id,
                f"⚠️ <b>Подтверждение</b>\n\n"
                f"Удалить <code>{target_login}</code>?",
                reply_markup={"inline_keyboard": [
                    [
                        {"text": "✅ Да, удалить", "callback_data": f"admin:rmexec:{target_login}"},
                        {"text": "❌ Отмена", "callback_data": "admin:remove"},
                    ]
                ]}
            )
            await self._answer_callback(cb_id)
            return True

        if data.startswith("admin:rmexec:"):
            target_login = data.split(":", 2)[2]
            users = reload_auth_users()
            if target_login in users and target_login not in ADMIN_LOGINS:
                del users[target_login]
                save_auth_users(users)
                # Deauthorize sessions
                for cid, login in list(self._chat_logins.items()):
                    if login == target_login:
                        self._authorized_chats.discard(cid)
                        self._auth_states[cid] = _STATE_NONE
                        self._chat_logins.pop(cid, None)
                print(f"[TG] Admin: user '{target_login}' removed via callback by {self._chat_logins.get(chat_id)}", flush=True)
                await self._edit_message(chat_id, msg_id,
                    f"✅ Пользователь <code>{target_login}</code> удалён.\n\n"
                    "Активные сессии деавторизованы.",
                    reply_markup={"inline_keyboard": [
                        [{"text": "🔙 В админку", "callback_data": "admin:back"}]
                    ]}
                )
            else:
                await self._edit_message(chat_id, msg_id,
                    f"❌ Не удалось удалить <code>{target_login}</code>",
                    reply_markup={"inline_keyboard": [
                        [{"text": "🔙 В админку", "callback_data": "admin:back"}]
                    ]}
                )
            await self._answer_callback(cb_id)
            return True

        if data.startswith("admin:chpass:"):
            target_login = data.split(":", 2)[2]
            self._admin_states[chat_id] = (_ADMIN_CHANGE_PASSWORD, target_login)
            await self._edit_message(chat_id, msg_id,
                f"🔑 <b>Смена пароля</b>\n\n"
                f"Пользователь: <code>{target_login}</code>\n\n"
                f"Отправьте новый пароль следующим сообщением:",
                reply_markup={"inline_keyboard": [
                    [{"text": "🔙 Отмена", "callback_data": "admin:change"}]
                ]}
            )
            await self._answer_callback(cb_id)
            return True

        return False

    # ── Main message handler ──

    async def _handle_message(self, msg: dict):
        chat = msg.get("chat", {})
        chat_id = chat["id"]
        text = msg.get("text", "").strip()
        title = chat.get("title") or chat.get("first_name", str(chat_id))
        self._chat_titles[chat_id] = title

        state = self._auth_states.get(chat_id, _STATE_NONE)

        # ── Check admin flow states first (even before auth check) ──
        if chat_id in self._admin_states and self._is_authorized(chat_id) and self._is_admin(chat_id):
            handled = await self._handle_admin_command(chat_id, text)
            if handled:
                return

        # ── Admin commands ──
        if text.startswith("/") and self._is_authorized(chat_id):
            handled = await self._handle_admin_command(chat_id, text)
            if handled:
                return

        # /start — begin auth or show help if already authorized
        if text == "/start":
            if state == _STATE_AUTHORIZED:
                login = self._chat_logins.get(chat_id, "?")
                admin_badge = " 👑" if self._is_admin(chat_id) else ""
                help_text = (
                    f"✅ Авторизован: <b>{login}</b>{admin_badge}\n"
                    "Алерты будут приходить автоматически.\n\n"
                    "/settings — настройки алертов\n"
                    "/status — статус\n"
                    "/stop — отключиться"
                )
                if self._is_admin(chat_id):
                    help_text += "\n\n👑 <b>Админ-команды:</b>\n/admin — панель управления\n/adduser — добавить\n/removeuser — удалить\n/listusers — список\n/changepass — сменить пароль"
                await self._send_message(chat_id, help_text)
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
            self._chat_logins.pop(chat_id, None)
            self._admin_states.pop(chat_id, None)
            await self._send_message(chat_id, "🚫 Вы отключены от алертов.\n/start — авторизоваться заново.")
            return

        # /status
        if text == "/status":
            if state == _STATE_AUTHORIZED:
                login = self._chat_logins.get(chat_id, "?")
                admin_badge = " 👑" if self._is_admin(chat_id) else ""
                prefs = self._get_user_prefs(chat_id)
                level_names = {"STRONG": "Сильные", "MEDIUM": "Средние", "WEAK": "Слабые"}
                market_names = {"futures": "Фьючерсы", "spot": "Спот"}
                lvls = ", ".join(level_names.get(l, l) for l in sorted(prefs["levels"]))
                mkts = ", ".join(market_names.get(m, m) for m in sorted(prefs["markets"]))
                await self._send_message(chat_id,
                    f"✅ <b>{login}</b>{admin_badge}\n"
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

            # Reload users from disk (in case admin added someone)
            current_users = reload_auth_users()

            if login not in current_users:
                self._auth_states[chat_id] = _STATE_NONE
                await self._send_message(chat_id,
                    "❌ Неверный логин или пароль.\n/start — попробовать снова."
                )
                print(f"[TG] Auth failed for chat {chat_id} ({title}): unknown login '{login}'", flush=True)
                return

            correct_password = current_users[login]
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
            self._chat_logins[chat_id] = login
            print(f"[TG] Auth OK: {login} → chat {chat_id} ({title})", flush=True)

            admin_badge = " 👑" if login in ADMIN_LOGINS else ""
            welcome = (
                f"✅ <b>Добро пожаловать, {login}!</b>{admin_badge}\n\n"
                "Алерты будут приходить автоматически.\n"
                "/settings — настройки алертов\n"
                "/stop — отключиться\n"
                "/status — проверить статус"
            )
            if login in ADMIN_LOGINS:
                welcome += "\n\n👑 Вы администратор!\n/admin — панель управления"
            await self._send_message(chat_id, welcome)
            return

        # Not in auth flow, not authorized — prompt to start
        if state != _STATE_AUTHORIZED:
            await self._send_message(chat_id, "🔐 Введите /start для авторизации.")

    # ── Callback query handler ──

    async def _handle_callback_query(self, cb: dict):
        chat_id = cb["message"]["chat"]["id"]
        data = cb.get("data", "")
        cb_id = cb.get("id", "")
        msg_id = cb["message"]["message_id"]

        state = self._auth_states.get(chat_id, _STATE_NONE)
        if state != _STATE_AUTHORIZED:
            await self._answer_callback(cb_id, "❌ Не авторизован")
            return

        # ── Admin callbacks ──
        if data.startswith("admin:"):
            handled = await self._handle_admin_callback(chat_id, cb_id, data, msg_id)
            if handled:
                return

        # ── Settings toggle callbacks ──
        prefs = self._get_user_prefs(chat_id)

        parts = data.split(":")
        if len(parts) == 3 and parts[0] == "toggle":
            category = parts[1]
            value = parts[2]

            if category == "level" and value in ALL_LEVELS:
                if value in prefs["levels"]:
                    prefs["levels"].discard(value)
                else:
                    prefs["levels"].add(value)
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
                    prefs["levels"] = {"STRONG"}
                    prefs["markets"] = set(ALL_MARKETS)

            self._persist_prefs()

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

            await self._edit_message(chat_id, msg_id, prefs_text, keyboard)
            await self._answer_callback(cb_id, "✅ Настройки обновлены")

    # ── Polling ──

    async def _poll_updates(self):
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
        return asyncio.create_task(self._poll_updates())

    # ── Alert sending ──

    async def send_alert(self, symbol: str, side: str, count: int, avg_qty: float,
                         avg_interval: float, first_price: float,
                         strength: str, avg_usd: float,
                         signal_time_sec: float,
                         market: str = "futures", level: str = "WEAK"):
        if not self._authorized_chats:
            print("[TG] No authorized chats. Alerts not sent.", flush=True)
            return

        # Generate chart once
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

        for cid in list(self._authorized_chats):
            if not self._should_send_alert(cid, level, market):
                continue
            try:
                if chart_buf:
                    await self._send_photo(cid, chart_buf, caption)
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
