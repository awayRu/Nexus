__version__ = "2.0.0"
"""NEXUS messenger Android client. Works in Pydroid 3 with Kivy, requests and websocket-client."""
import io
import json
import os
import queue
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from kivy.app import App
from kivy.clock import Clock
from kivy.core.image import Image as CoreImage
from kivy.metrics import dp
from kivy.properties import ListProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.image import AsyncImage
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.textinput import TextInput
from kivy.utils import get_color_from_hex, platform
from kivy.graphics import Color, RoundedRectangle

try:
    import websocket
except ImportError:
    websocket = None  # HTTP polling remains available as a fallback.

GREEN = get_color_from_hex("#22F477")
GREEN_DARK = get_color_from_hex("#0B6D34")
BG = get_color_from_hex("#080D0A")
PANEL = get_color_from_hex("#111A14")
PANEL_ALT = get_color_from_hex("#17231A")
TEXT = get_color_from_hex("#E0F5E5")
MUTED = get_color_from_hex("#87A18D")
ERROR = get_color_from_hex("#FF7979")

KV = r"""
<NexusButton@Button>:
    background_normal: ''
    background_down: ''
    background_color: (0.13, 0.96, 0.47, 1) if self.state == 'normal' else (0.08, 0.58, 0.29, 1)
    color: (0.02, 0.07, 0.035, 1)
    bold: True
    font_size: '13sp'
    size_hint_y: None
    height: dp(44)
<NexusSecondaryButton@Button>:
    background_normal: ''
    background_down: ''
    background_color: (0.10, 0.16, 0.12, 1) if self.state == 'normal' else (0.14, 0.28, 0.18, 1)
    color: (0.83, 0.96, 0.86, 1)
    bold: True
    font_size: '12sp'
    size_hint_y: None
    height: dp(42)
<NexusLabel@Label>:
    color: (0.85, 0.96, 0.88, 1)
    halign: 'left'
    valign: 'middle'
    text_size: self.size
<NexusInput@TextInput>:
    background_normal: ''
    background_active: ''
    background_color: (0.055, 0.085, 0.065, 1)
    foreground_color: (0.90, 1, 0.92, 1)
    cursor_color: (0.13, 0.96, 0.47, 1)
    hint_text_color: (0.43, 0.57, 0.47, 1)
    selection_color: (0.10, 0.50, 0.24, 1)
    padding: dp(12), dp(12)
    size_hint_y: None
    height: dp(46)
    multiline: False
<NexusRoot>:
    orientation: 'vertical'
    padding: dp(13)
    spacing: dp(10)
    canvas.before:
        Color:
            rgba: (0.025, 0.045, 0.03, 1)
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        size_hint_y: None
        height: dp(48)
        spacing: dp(8)
        NexusLabel:
            text: '[b]N E X U S[/b]'
            markup: True
            font_size: '24sp'
            color: (0.13, 0.96, 0.47, 1)
        NexusLabel:
            text: root.status
            font_size: '11sp'
            halign: 'right'
            color: (0.53, 0.78, 0.60, 1)
    ScreenManager:
        id: screens
        Screen:
            name: 'auth'
            BoxLayout:
                orientation: 'vertical'
                spacing: dp(11)
                padding: dp(4), dp(15)
                Widget:
                    size_hint_y: .12
                NexusLabel:
                    text: 'ОБЩАЙСЯ БЕЗ ГРАНИЦ'
                    bold: True
                    font_size: '19sp'
                    color: (0.86, 1, 0.90, 1)
                    size_hint_y: None
                    height: dp(32)
                NexusLabel:
                    text: 'Личные сообщения, друзья и группы — в одном месте.'
                    font_size: '13sp'
                    color: (0.52, 0.70, 0.56, 1)
                    size_hint_y: None
                    height: dp(38)
                NexusInput:
                    id: server_url
                    text: root.server_url
                    hint_text: 'Адрес API: https://your-domain.example'
                NexusInput:
                    id: username
                    hint_text: 'Логин: 3–32 символа'
                NexusInput:
                    id: password
                    hint_text: 'Пароль (минимум 10 символов при регистрации)'
                    password: True
                BoxLayout:
                    size_hint_y: None
                    height: dp(46)
                    spacing: dp(8)
                    NexusButton:
                        text: 'ВОЙТИ'
                        on_release: root.authenticate(False)
                    NexusButton:
                        text: 'СОЗДАТЬ АККАУНТ'
                        on_release: root.authenticate(True)
                NexusLabel:
                    text: 'Используй адрес своего развёрнутого API. Для публичного сервера обязательно HTTPS.'
                    font_size: '11sp'
                    color: (0.48, 0.62, 0.51, 1)
                    size_hint_y: None
                    height: dp(48)
                Widget:
                    size_hint_y: .18
        Screen:
            name: 'chats'
            BoxLayout:
                orientation: 'vertical'
                spacing: dp(9)
                BoxLayout:
                    size_hint_y: None
                    height: dp(40)
                    NexusLabel:
                        text: '[b]ВАШИ ЧАТЫ[/b]'
                        markup: True
                        font_size: '18sp'
                    NexusSecondaryButton:
                        text: 'ДРУЗЬЯ'
                        size_hint_x: .37
                        on_release: root.open_friends()
                    NexusSecondaryButton:
                        text: 'ГРУППА +'
                        size_hint_x: .37
                        on_release: root.create_group()
                NexusInput:
                    id: chat_search
                    hint_text: 'Поиск по чатам…'
                    on_text: root.filter_chats(self.text)
                ScrollView:
                    do_scroll_x: False
                    bar_width: dp(3)
                    GridLayout:
                        id: chat_list
                        cols: 1
                        spacing: dp(7)
                        padding: 0, dp(2), 0, dp(6)
                        size_hint_y: None
                        height: self.minimum_height
                BoxLayout:
                    size_hint_y: None
                    height: dp(44)
                    spacing: dp(8)
                    NexusSecondaryButton:
                        text: 'ОБНОВИТЬ'
                        on_release: root.load_chats()
                    NexusSecondaryButton:
                        text: 'ВЫЙТИ'
                        on_release: root.logout()
        Screen:
            name: 'chat'
            BoxLayout:
                orientation: 'vertical'
                spacing: dp(8)
                BoxLayout:
                    size_hint_y: None
                    height: dp(44)
                    NexusSecondaryButton:
                        text: '← ЧАТЫ'
                        size_hint_x: .34
                        on_release: root.back_to_chats()
                    NexusLabel:
                        id: chat_title
                        text: 'Переписка'
                        font_size: '16sp'
                        bold: True
                ScrollView:
                    id: messages_scroll
                    do_scroll_x: False
                    bar_width: dp(3)
                    GridLayout:
                        id: messages
                        cols: 1
                        spacing: dp(7)
                        padding: 0, dp(4), 0, dp(10)
                        size_hint_y: None
                        height: self.minimum_height
                BoxLayout:
                    size_hint_y: None
                    height: dp(48)
                    spacing: dp(7)
                    NexusInput:
                        id: message_text
                        hint_text: 'Сообщение…'
                        on_text_validate: root.send_message()
                    NexusButton:
                        text: '➤'
                        size_hint_x: .20
                        on_release: root.send_message()
                BoxLayout:
                    size_hint_y: None
                    height: dp(42)
                    spacing: dp(7)
                    NexusSecondaryButton:
                        text: '＋ ФОТО'
                        on_release: root.pick_photo()
                    NexusSecondaryButton:
                        text: 'ОБНОВИТЬ'
                        on_release: root.load_messages()
"""
from kivy.lang import Builder
Builder.load_string(KV)


class NexusBubble(BoxLayout):
    bg_color = ListProperty(PANEL_ALT)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        with self.canvas.before:
            self._bg_instruction = Color(*self.bg_color)
            self._bg_shape = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(12)])
        self.bind(pos=self._sync_bg, size=self._sync_bg, bg_color=self._sync_color)

    def _sync_bg(self, *_):
        self._bg_shape.pos = self.pos
        self._bg_shape.size = self.size

    def _sync_color(self, *_):
        self._bg_instruction.rgba = self.bg_color


class NexusRoot(BoxLayout):
    status = StringProperty("Готов к подключению")
    server_url = StringProperty("")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.token = None
        self.username = ""
        self.chats = []
        self.active_chat = None
        self.poll_event = None
        self.ws_stop = threading.Event()
        self.ws_outbound = queue.Queue()
        self.ws_socket = None
        self.ws_thread = None
        self.ws_connected = False
        self._refresh_ticks = 0
        self._chat_filter = ""
        self._friends_popup = None
        self._friends_list = None
        self._friends_info = None
        self._friends_input = None
        self._image_cache = None

    def load_saved_settings(self):
        try:
            p = Path(App.get_running_app().user_data_dir) / "settings.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                self.server_url = str(data.get("server_url", "")).strip()
                self.ids.server_url.text = self.server_url
        except Exception:
            pass

    def save_settings(self):
        try:
            p = Path(App.get_running_app().user_data_dir) / "settings.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"server_url": self.server_url}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _validate_server(self):
        raw = self.ids.server_url.text.strip().rstrip("/")
        parts = urlparse(raw)
        if parts.scheme not in ("https", "http") or not parts.netloc:
            raise ValueError("Введи полный адрес сервера, например https://nexus.example.com")
        if parts.scheme != "https" and (not parts.hostname or (parts.hostname not in ("localhost", "127.0.0.1", "10.0.2.2") and not parts.hostname.startswith("192.168.") and not parts.hostname.startswith("10."))):
            raise ValueError("Для удалённого сервера используй HTTPS. HTTP допустим только для локальной разработки.")
        self.server_url = raw
        self.save_settings()

    def _request(self, method, path, **kwargs):
        url = path if path.startswith("http://") or path.startswith("https://") else self.server_url.rstrip("/") + (path if path.startswith("/") else "/" + path)
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        response = requests.request(method, url, headers=headers, timeout=15, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")
        return response

    def _api_json(self, method, path, **kwargs):
        response = self._request(method, path, **kwargs)
        return response.json() if response.content else {}

    def show_error(self, message):
        text = str(message)
        self.status = "Ошибка соединения" if len(text) > 35 else text
        body = Label(text=text[:700], color=TEXT, halign="left", valign="middle")
        body.bind(size=lambda w, s: setattr(w, "text_size", (s[0] - dp(12), None)))
        Popup(title="NEXUS", content=body, size_hint=(.90, .32), separator_color=GREEN).open()

    def show_info(self, message):
        self.status = str(message)[:70]

    def _run_bg(self, fn, callback=None):
        def worker():
            try:
                result, error = fn(), None
            except Exception as exc:
                result, error = None, exc
            if callback:
                Clock.schedule_once(lambda _dt: callback(result, error), 0)
        threading.Thread(target=worker, daemon=True).start()

    def authenticate(self, register):
        try:
            self._validate_server()
        except Exception as exc:
            self.show_error(exc)
            return
        username = self.ids.username.text.strip()
        password = self.ids.password.text
        if not (3 <= len(username) <= 32) or not username.replace("_", "").isalnum():
            self.show_error("Логин: 3–32 символа, только латинские буквы, цифры и подчёркивание.")
            return
        if register and len(password) < 10:
            self.show_error("Для регистрации нужен пароль длиной не менее 10 символов.")
            return
        if not password:
            self.show_error("Введи пароль.")
            return
        self.status = "Подключение…"
        path = "/auth/register" if register else "/auth/login"
        def call():
            health = self._api_json("GET", "/health")
            if health.get("status") != "ok":
                raise RuntimeError("Сервер ответил, но база данных не готова.")
            return self._api_json("POST", path, json={"username": username, "password": password})
        def done(data, err):
            if err:
                self.show_error("Не удалось подключиться. Проверь адрес сервера и интернет.\n\n" + str(err))
                return
            self.token = data["access_token"]
            self.username = data.get("user", {}).get("username", username)
            self.status = "@" + self.username + " • онлайн"
            self.ids.screens.current = "chats"
            self._start_ws()
            self.load_chats()
            if self.poll_event:
                self.poll_event.cancel()
            self.poll_event = Clock.schedule_interval(self._background_refresh, 5)
        self._run_bg(call, done)

    def logout(self):
        if self.poll_event:
            self.poll_event.cancel()
            self.poll_event = None
        self._stop_ws()
        self.token = None
        self.username = ""
        self.active_chat = None
        self.chats = []
        self.ids.screens.current = "auth"
        self.status = "Вы вышли из аккаунта"

    def _start_ws(self):
        self._stop_ws()
        if websocket is None or not self.token:
            self.status = "Онлайн • резервный режим"
            return
        self.ws_stop = threading.Event()
        self.ws_outbound = queue.Queue()
        stop = self.ws_stop
        def worker():
            delay = 1
            while not stop.is_set():
                sock = None
                try:
                    ws_scheme = "wss" if self.server_url.startswith("https://") else "ws"
                    ws_url = ws_scheme + "://" + self.server_url.split("://", 1)[1].rstrip("/") + "/ws"
                    sock = websocket.create_connection(ws_url, timeout=5, header={"Authorization": "Bearer " + self.token}, enable_multithread=True)
                    sock.settimeout(1.0)
                    self.ws_socket = sock
                    Clock.schedule_once(lambda _dt: self._set_ws_state(True), 0)
                    delay = 1
                    if self.active_chat:
                        self.ws_outbound.put({"action": "subscribe", "chat_id": self.active_chat["id"]})
                    while not stop.is_set():
                        try:
                            while True:
                                outgoing = self.ws_outbound.get_nowait()
                                sock.send(json.dumps(outgoing))
                        except queue.Empty:
                            pass
                        try:
                            raw = sock.recv()
                            if not raw:
                                break
                            event = json.loads(raw)
                            Clock.schedule_once(lambda _dt, e=event: self._handle_ws_event(e), 0)
                        except websocket.WebSocketTimeoutException:
                            continue
                except Exception:
                    if not stop.is_set():
                        Clock.schedule_once(lambda _dt: self._set_ws_state(False), 0)
                finally:
                    if sock:
                        try: sock.close()
                        except Exception: pass
                    if self.ws_socket is sock:
                        self.ws_socket = None
                    Clock.schedule_once(lambda _dt: self._set_ws_state(False), 0)
                if not stop.is_set():
                    stop.wait(delay)
                    delay = min(delay * 2, 15)
        self.ws_thread = threading.Thread(target=worker, daemon=True, name="NEXUS-WebSocket")
        self.ws_thread.start()

    def _stop_ws(self):
        if getattr(self, "ws_stop", None):
            self.ws_stop.set()
        sock = getattr(self, "ws_socket", None)
        if sock:
            try: sock.close()
            except Exception: pass
        self.ws_connected = False
        self.ws_socket = None

    def _set_ws_state(self, connected):
        self.ws_connected = connected
        if self.token:
            self.status = ("● Онлайн • WebSocket" if connected else "● Подключение… / резервный режим") + (" • @" + self.username)

    def _ws_send(self, data):
        if self.ws_thread and self.ws_thread.is_alive() and not self.ws_stop.is_set():
            self.ws_outbound.put(data)

    def _handle_ws_event(self, event):
        kind = event.get("type")
        chat_id = event.get("chat_id")
        if kind == "new_message":
            self.load_chats(silent=True)
            if self.active_chat and chat_id == self.active_chat.get("id") and self.ids.screens.current == "chat":
                self.load_messages(silent=True)
        elif kind == "ready":
            self.show_info("WebSocket подключён")

    def _background_refresh(self, _dt):
        if not self.token:
            return
        self._refresh_ticks += 1
        def call():
            result = {}
            if self._refresh_ticks % 4 == 0:
                result = self._api_json("POST", "/presence/heartbeat")
            if not self.ws_connected and self.active_chat and self.ids.screens.current == "chat" and self._refresh_ticks % 2 == 0:
                self._api_json("GET", f"/chats/{self.active_chat['id']}/messages?limit=100")
            return result
        def done(_data, err):
            if err and "401" in str(err):
                self.logout()
                self.show_error("Сессия истекла. Войди снова.")
                return
            # Polling is the fallback if WebSocket is unavailable.
            if not self.ws_connected and self._refresh_ticks % 2 == 0:
                if self.ids.screens.current == "chat": self.load_messages(silent=True)
                elif self.ids.screens.current == "chats": self.load_chats(silent=True)
            elif self.ws_connected and self._refresh_ticks % 4 == 0 and self.ids.screens.current == "chats":
                self.load_chats(silent=True)
        self._run_bg(call, done)

    def load_chats(self, silent=False):
        if not self.token:
            return
        def call(): return self._api_json("GET", "/chats")
        def done(data, err):
            if err:
                if not silent: self.show_error("Не удалось загрузить чаты: " + str(err))
                return
            self.chats = data
            self.filter_chats(self._chat_filter)
            if not silent:
                self.status = f"@{self.username} • {len(data)} чатов"
        self._run_bg(call, done)

    def filter_chats(self, value):
        self._chat_filter = (value or "").strip().lower()
        if "chat_list" not in self.ids:
            return
        box = self.ids.chat_list
        box.clear_widgets()
        visible = [c for c in self.chats if self._chat_filter in ((c.get("title") or "") + " " + (c.get("peer_username") or "") + " " + (c.get("last_message") or "")).lower()]
        if not visible:
            box.add_widget(Label(text="Здесь пока пусто\nОткрой «Друзья», чтобы начать общение", color=MUTED, halign="center", size_hint_y=None, height=dp(100)))
            return
        for chat in visible:
            title = chat.get("title") or ("@" + chat.get("peer_username", "личный чат"))
            online = "● " if chat.get("peer_online") else ""
            kind = "ГРУППА" if chat.get("is_group") else ("В СЕТИ" if chat.get("peer_online") else "ЛИЧНЫЙ ЧАТ")
            btn = Button(text=f"{online}{title}\n{chat.get('last_message') or 'Нет сообщений'}\n{kind}",
                         size_hint_y=None, height=dp(82), halign="left", valign="middle",
                         background_normal="", background_down="", background_color=PANEL,
                         color=TEXT, font_size="13sp")
            btn.bind(size=lambda w, s: setattr(w, "text_size", (s[0]-dp(24), None)))
            btn.bind(on_release=lambda _btn, c=chat: self.open_chat(c))
            box.add_widget(btn)

    def open_chat(self, chat):
        if self.active_chat and self.active_chat.get("id") != chat.get("id"):
            self._ws_send({"action": "unsubscribe", "chat_id": self.active_chat["id"]})
        self.active_chat = chat
        title = chat.get("title") or ("@" + chat.get("peer_username", "личный чат"))
        if chat.get("peer_online"):
            title += "  ●"
        self.ids.chat_title.text = title
        self.ids.screens.current = "chat"
        self.ids.messages.clear_widgets()
        self._ws_send({"action": "subscribe", "chat_id": chat["id"]})
        self.load_messages()

    def back_to_chats(self):
        if self.active_chat:
            self._ws_send({"action": "unsubscribe", "chat_id": self.active_chat["id"]})
        self.ids.screens.current = "chats"
        self.load_chats()

    def load_messages(self, silent=False):
        if not self.active_chat or not self.token:
            return
        chat_id = self.active_chat["id"]
        def call(): return self._api_json("GET", f"/chats/{chat_id}/messages?limit=100")
        def done(items, err):
            if err:
                if not silent: self.show_error("Не удалось загрузить сообщения: " + str(err))
                return
            if not self.active_chat or self.active_chat.get("id") != chat_id:
                return
            box = self.ids.messages
            old_ids = [getattr(w, "message_id", None) for w in box.children]
            new_ids = [m.get("id") for m in items]
            if old_ids and sorted(x for x in old_ids if x is not None) == sorted(new_ids):
                return
            box.clear_widgets()
            for message in items:
                who = "ВЫ" if message.get("sender_username") == self.username else "@" + message.get("sender_username", "?")
                timestamp = (message.get("created_at") or "").replace("T", " ")[:16]
                bubble = NexusBubble(orientation="vertical", spacing=dp(4), padding=(dp(11), dp(7)),
                                     size_hint_y=None, bg_color=(PANEL_ALT if message.get("sender_username") != self.username else get_color_from_hex("#10351E")))
                bubble.message_id = message.get("id")
                head = Label(text=f"[b]{who}[/b]   [size=10sp]{timestamp}[/size]", markup=True, color=GREEN if who == "ВЫ" else MUTED,
                             size_hint_y=None, height=dp(21), halign="left", valign="middle")
                head.bind(size=lambda w, s: setattr(w, "text_size", (s[0], None)))
                bubble.add_widget(head)
                text = message.get("text")
                image_url = message.get("image_url")
                if text:
                    lines = max(1, min(12, (len(text) // 42) + text.count("\n") + 1))
                    lbl = Label(text=text, color=TEXT, halign="left", valign="top", size_hint_y=None,
                                height=dp(max(25, lines * 21)), font_size="14sp")
                    lbl.bind(size=lambda w, s: setattr(w, "text_size", (s[0], None)))
                    bubble.add_widget(lbl)
                if image_url:
                    thumb = AsyncImage(source="", size_hint=(None, None), size=(dp(190), dp(165)), allow_stretch=True, keep_ratio=True)
                    bubble.add_widget(thumb)
                    self._load_image_async(image_url, thumb)
                bubble.height = sum(w.height for w in bubble.children) + dp(22)
                box.add_widget(bubble)
            Clock.schedule_once(lambda _dt: setattr(self.ids.messages_scroll, "scroll_y", 0), .15)
        self._run_bg(call, done)

    def _load_image_async(self, path, widget):
        cache_dir = Path(App.get_running_app().user_data_dir) / "image_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        file_name = path.rsplit("/", 1)[-1]
        cached = cache_dir / file_name
        def call():
            if cached.exists() and cached.stat().st_size:
                return str(cached)
            response = self._request("GET", path)
            if len(response.content) > 8 * 1024 * 1024:
                raise RuntimeError("Изображение больше 8 МБ")
            cached.write_bytes(response.content)
            return str(cached)
        def done(filename, err):
            if not err and filename:
                widget.source = filename
                widget.reload()
        self._run_bg(call, done)

    def send_message(self):
        if not self.active_chat:
            return
        message = self.ids.message_text.text.strip()
        if not message:
            return
        if len(message) > 4000:
            self.show_error("Сообщение слишком длинное. Максимум — 4000 символов.")
            return
        chat_id = self.active_chat["id"]
        self.ids.message_text.text = ""
        self.ids.message_text.focus = True
        def call(): return self._api_json("POST", f"/chats/{chat_id}/messages", json={"text": message})
        def done(_result, err):
            if err:
                self.ids.message_text.text = message
                self.show_error("Сообщение не отправлено: " + str(err))
            else:
                self.load_messages(silent=True)
                self.load_chats(silent=True)
        self._run_bg(call, done)

    def _action_button(self, text, callback, secondary=False, width=None):
        btn = Button(text=text, size_hint_y=None, height=dp(40),
                     size_hint_x=width if width else 1,
                     background_normal="", background_down="",
                     background_color=(PANEL_ALT if secondary else GREEN),
                     color=(TEXT if secondary else BG), bold=True, font_size="11sp")
        btn.bind(on_release=callback)
        return btn

    def open_friends(self):
        if not self.token:
            return
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        self._friends_input = TextInput(hint_text="Логин пользователя", multiline=False, size_hint_y=None, height=dp(44),
                                        background_normal="", background_active="", background_color=PANEL,
                                        foreground_color=TEXT, cursor_color=GREEN, hint_text_color=MUTED, padding=dp(10))
        content.add_widget(self._friends_input)
        actions = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(5))
        actions.add_widget(self._action_button("НАЙТИ", lambda *_: self._search_users(), width=1))
        actions.add_widget(self._action_button("ЗАЯВКА", lambda *_: self._send_friend_request(), width=1))
        content.add_widget(actions)
        tabs = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(5))
        tabs.add_widget(self._action_button("ДРУЗЬЯ", lambda *_: self._load_friends(), True))
        tabs.add_widget(self._action_button("ВХОДЯЩИЕ", lambda *_: self._load_incoming(), True))
        content.add_widget(tabs)
        self._friends_info = Label(text="Найди человека по логину или открой список друзей.", color=MUTED, size_hint_y=None, height=dp(38), halign="left", valign="middle")
        self._friends_info.bind(size=lambda w,s: setattr(w,"text_size",(s[0],None)))
        content.add_widget(self._friends_info)
        scroll = __import__("kivy.uix.scrollview", fromlist=["ScrollView"]).ScrollView(do_scroll_x=False)
        self._friends_list = __import__("kivy.uix.gridlayout", fromlist=["GridLayout"]).GridLayout(cols=1, spacing=dp(6), size_hint_y=None)
        self._friends_list.bind(minimum_height=self._friends_list.setter("height"))
        scroll.add_widget(self._friends_list)
        content.add_widget(scroll)
        self._friends_popup = Popup(title="Друзья и контакты", content=content, size_hint=(.94, .82), separator_color=GREEN)
        self._friends_popup.open()

    def _set_friend_results(self, rows, mode):
        grid = self._friends_list
        grid.clear_widgets()
        if not rows:
            self._friends_info.text = "Ничего не найдено" if mode == "search" else "Список пуст"
            return
        self._friends_info.text = {"search": "Результаты поиска", "friends": "Ваши друзья", "incoming": "Входящие заявки"}.get(mode, "")
        for row in rows:
            line = BoxLayout(size_hint_y=None, height=dp(54), spacing=dp(5), padding=(dp(4), 0))
            label_text = "@" + row.get("username", "?")
            if row.get("online"):
                label_text += "  ●"
            label = Label(text=label_text, color=GREEN if row.get("online") else TEXT, halign="left", valign="middle")
            label.bind(size=lambda w,s: setattr(w,"text_size",(s[0],None)))
            line.add_widget(label)
            uid = row.get("id")
            if mode == "search":
                if row.get("is_friend"):
                    btn = self._action_button("ЧАТ", lambda *_args, r=row: self._open_dm(r["username"]), width=.35)
                else:
                    btn = self._action_button("ДОБАВИТЬ", lambda *_args, n=row["username"]: self._send_friend_request(n), width=.42)
                line.add_widget(btn)
            elif mode == "friends":
                line.add_widget(self._action_button("НАПИСАТЬ", lambda *_args, n=row["username"]: self._open_dm(n), width=.48))
            elif mode == "incoming":
                btns = BoxLayout(size_hint_x=.56, spacing=dp(4))
                btns.add_widget(self._action_button("✓", lambda *_args, i=uid: self._accept_friend(i), width=1))
                btns.add_widget(self._action_button("×", lambda *_args, i=uid: self._decline_friend(i), secondary=True, width=1))
                line.add_widget(btns)
            grid.add_widget(line)

    def _search_users(self):
        query = self._friends_input.text.strip()
        if len(query) < 1:
            self._friends_info.text = "Введи логин для поиска"
            return
        def call(): return self._api_json("GET", "/users/search", params={"q": query})
        def done(data, err):
            if err: self._friends_info.text = str(err)
            else: self._set_friend_results(data, "search")
        self._run_bg(call, done)

    def _send_friend_request(self, username=None):
        name = (username or self._friends_input.text).strip()
        if not name:
            self._friends_info.text = "Введи логин человека"
            return
        def call(): return self._api_json("POST", "/friends/requests", json={"username": name})
        def done(data, err):
            self._friends_info.text = "Заявка отправлена" if not err else str(err)
            if not err: self._search_users()
        self._run_bg(call, done)

    def _load_friends(self):
        def call(): return self._api_json("GET", "/friends")
        def done(data, err):
            if err: self._friends_info.text = str(err)
            else: self._set_friend_results(data, "friends")
        self._run_bg(call, done)

    def _load_incoming(self):
        def call(): return self._api_json("GET", "/friends/requests")
        def done(data, err):
            if err: self._friends_info.text = str(err)
            else: self._set_friend_results(data, "incoming")
        self._run_bg(call, done)

    def _accept_friend(self, request_id):
        def call(): return self._api_json("POST", f"/friends/requests/{request_id}/accept")
        def done(data, err):
            if err: self._friends_info.text = str(err)
            else:
                self._friends_info.text = "Теперь вы друзья — личный чат создан"
                self._load_incoming()
                self.load_chats(silent=True)
        self._run_bg(call, done)

    def _decline_friend(self, request_id):
        def call(): return self._api_json("POST", f"/friends/requests/{request_id}/decline")
        def done(data, err):
            if err: self._friends_info.text = str(err)
            else: self._load_incoming()
        self._run_bg(call, done)

    def _open_dm(self, username):
        def call(): return self._api_json("POST", f"/chats/direct/{username}")
        def done(data, err):
            if err:
                self._friends_info.text = str(err)
                return
            if self._friends_popup: self._friends_popup.dismiss()
            self.load_chats(silent=True)
            self.open_chat(data)
        self._run_bg(call, done)

    def create_group(self):
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        title = TextInput(hint_text="Название группы", multiline=False, size_hint_y=None, height=dp(44), background_normal="", background_active="", background_color=PANEL, foreground_color=TEXT, hint_text_color=MUTED, cursor_color=GREEN)
        names = TextInput(hint_text="Логины друзей через запятую", multiline=False, size_hint_y=None, height=dp(44), background_normal="", background_active="", background_color=PANEL, foreground_color=TEXT, hint_text_color=MUTED, cursor_color=GREEN)
        info = Label(text="Все участники, кроме вас, должны быть в друзьях.", color=MUTED, halign="left", valign="middle", size_hint_y=None, height=dp(52))
        info.bind(size=lambda w,s: setattr(w,"text_size",(s[0],None)))
        content.add_widget(title); content.add_widget(names); content.add_widget(info)
        pop = Popup(title="Новая группа", content=content, size_hint=(.92,.40), separator_color=GREEN)
        btn = self._action_button("СОЗДАТЬ ГРУППУ", lambda *_: create(), width=1)
        content.add_widget(btn)
        def create():
            label = title.text.strip()
            members = [x.strip() for x in names.text.split(",") if x.strip()]
            if not label:
                info.text = "Укажи название группы"; return
            def call(): return self._api_json("POST", "/groups", json={"title": label, "members": members})
            def done(data, err):
                if err: info.text = str(err)
                else:
                    pop.dismiss(); self.load_chats(); self.show_info("Группа создана")
                    self.open_chat({"id": data["id"], "title": data["title"], "is_group": True})
            self._run_bg(call, done)
        pop.open()

    def pick_photo(self):
        if not self.active_chat:
            return
        # Android's document/photo picker works with scoped storage, unlike browsing the whole
        # /storage/emulated/0 directory on recent Android versions.
        if platform == "android" and self._launch_android_photo_picker():
            return
        chooser = FileChooserListView(path="/storage/emulated/0", filters=["*.png", "*.jpg", "*.jpeg", "*.webp"])
        layout = BoxLayout(orientation="vertical", spacing=dp(7), padding=dp(6))
        layout.add_widget(chooser)
        pop = Popup(title="Выбрать изображение (до 8 МБ)", content=layout, size_hint=(.96,.86), separator_color=GREEN)
        send = self._action_button("ОТПРАВИТЬ ФОТО", lambda *_: upload(), width=1)
        layout.add_widget(send)
        chat_id = self.active_chat["id"]
        def upload():
            if not chooser.selection:
                self.show_error("Сначала выбери файл")
                return
            path = chooser.selection[0]
            def call():
                size = os.path.getsize(path)
                if size > 8 * 1024 * 1024:
                    raise RuntimeError("Максимальный размер файла — 8 МБ")
                extension = os.path.splitext(path)[1].lower()
                mime = {".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".png":"image/png", ".webp":"image/webp"}.get(extension)
                if not mime: raise RuntimeError("Разрешены JPG, PNG и WebP")
                with open(path, "rb") as stream:
                    return self._api_json("POST", f"/chats/{chat_id}/images", files={"file": (os.path.basename(path), stream, mime)})
            def done(_data, err):
                pop.dismiss()
                if err: self.show_error("Фото не отправлено: " + str(err))
                else: self.load_messages(silent=True); self.load_chats(silent=True)
            self._run_bg(call, done)
        pop.open()

    def _launch_android_photo_picker(self):
        try:
            from android import activity
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            self._android_picker_request = 28471
            self._android_picker_activity = activity
            activity.bind(on_activity_result=self._on_android_photo_result)
            intent = Intent(Intent.ACTION_GET_CONTENT)
            intent.setType("image/*")
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            PythonActivity.mActivity.startActivityForResult(intent, self._android_picker_request)
            self.show_info("Выбери изображение в системном окне")
            return True
        except Exception:
            try:
                if getattr(self, "_android_picker_activity", None):
                    self._android_picker_activity.unbind(on_activity_result=self._on_android_photo_result)
            except Exception:
                pass
            return False

    def _on_android_photo_result(self, request_code, result_code, intent):
        if request_code != getattr(self, "_android_picker_request", None):
            return
        try:
            self._android_picker_activity.unbind(on_activity_result=self._on_android_photo_result)
        except Exception:
            pass
        if result_code != -1 or intent is None:
            self.show_info("Выбор изображения отменён")
            return
        uri = intent.getData()
        if uri is None:
            self.show_error("Не удалось получить выбранный файл")
            return
        chat_id = self.active_chat.get("id") if self.active_chat else None
        if not chat_id:
            return
        def read_uri():
            from jnius import autoclass, jarray
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            resolver = PythonActivity.mActivity.getContentResolver()
            mime = str(resolver.getType(uri) or "image/jpeg").lower()
            ext_by_mime = {"image/jpeg": ("image.jpg", "image/jpeg"), "image/png": ("image.png", "image/png"), "image/webp": ("image.webp", "image/webp")}
            if mime not in ext_by_mime:
                raise RuntimeError("Разрешены изображения JPG, PNG и WebP")
            stream = resolver.openInputStream(uri)
            output_class = autoclass("java.io.ByteArrayOutputStream")
            output = output_class()
            buffer = jarray("b")([0] * 16384)
            total = 0
            try:
                while True:
                    count = stream.read(buffer)
                    if count == -1:
                        break
                    total += count
                    if total > 8 * 1024 * 1024:
                        raise RuntimeError("Максимальный размер изображения — 8 МБ")
                    output.write(buffer, 0, count)
            finally:
                stream.close()
            content = bytes((int(value) & 0xFF) for value in output.toByteArray())
            output.close()
            if not content:
                raise RuntimeError("Выбранный файл пуст")
            filename, actual_mime = ext_by_mime[mime]
            return filename, actual_mime, content
        def read_done(payload, err):
            if err:
                self.show_error("Не удалось прочитать изображение: " + str(err))
                return
            filename, mime, content = payload
            def upload():
                return self._api_json("POST", f"/chats/{chat_id}/images", files={"file": (filename, io.BytesIO(content), mime)})
            def uploaded(_data, upload_err):
                if upload_err:
                    self.show_error("Фото не отправлено: " + str(upload_err))
                else:
                    self.load_messages(silent=True)
                    self.load_chats(silent=True)
            self._run_bg(upload, uploaded)
        self._run_bg(read_uri, read_done)


class NexusApp(App):
    title = "NEXUS"

    def build(self):
        self.root_widget = NexusRoot()
        Clock.schedule_once(lambda _dt: self.root_widget.load_saved_settings(), 0)
        return self.root_widget

    def on_stop(self):
        root = getattr(self, "root_widget", None)
        if root:
            if root.poll_event:
                root.poll_event.cancel()
            root._stop_ws()


if __name__ == "__main__":
    NexusApp().run()
