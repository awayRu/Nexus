"""NEXUS API — FastAPI + SQLAlchemy + PostgreSQL/SQLite development mode."""
from __future__ import annotations

import base64
import hashlib
import io
import hmac
import json
import mimetypes
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

APP_VERSION = "2.0.0"
ENV = os.getenv("NEXUS_ENV", "development").lower()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./nexus_dev.db")
secret_from_env = os.getenv("NEXUS_SECRET_KEY", "")
if ENV == "production" and len(secret_from_env) < 32:
    raise RuntimeError("Production requires NEXUS_SECRET_KEY with at least 32 characters.")
if ENV == "production" and not DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg://")):
    raise RuntimeError("Production requires PostgreSQL via DATABASE_URL.")
SECRET_KEY = secret_from_env or ("DEV_ONLY_" + hashlib.sha256(b"NEXUS LOCAL DEVELOPMENT KEY ONLY").hexdigest())
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./uploads")).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 32_000_000
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
IMAGE_NAME_RE = re.compile(r"^[a-f0-9]{40}\.(jpg|png|webp)$")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow())
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow())


class FriendRequest(Base):
    __tablename__ = "friend_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    receiver_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow())
    __table_args__ = (UniqueConstraint("sender_id", "receiver_id", name="uq_friend_pair"),)


class Friendship(Base):
    __tablename__ = "friendships"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    friend_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    __table_args__ = (UniqueConstraint("user_id", "friend_id", name="uq_friendship"),)


class Chat(Base):
    __tablename__ = "chats"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    is_group: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow())


class ChatMember(Base):
    __tablename__ = "chat_members"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_chat_member"),)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id"), index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_path: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow(), index=True)


Base.metadata.create_all(engine)
app = FastAPI(title="NEXUS API", version=APP_VERSION, docs_url="/docs" if ENV != "production" else None, redoc_url=None)
origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type"])

# A modest per-process guardrail. For multi-worker production, move this to Redis/API gateway.
_rate_lock = threading.Lock()
_rate_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_rate_requests_seen = 0
RATE_RULES = {
    "auth": (10, 60),
    "images": (10, 60),
    "messages": (45, 60),
    "write": (60, 60),
    "read": (180, 60),
}


def _rate_bucket(method: str, path: str) -> str:
    if path in ("/auth/login", "/auth/register"):
        return "auth"
    if path.endswith("/images") and method == "POST":
        return "images"
    if re.fullmatch(r"/chats/\d+/messages", path) and method == "POST":
        return "messages"
    return "write" if method in {"POST", "DELETE", "PUT", "PATCH"} else "read"


@app.middleware("http")
async def security_and_rate_limit(request: Request, call_next):
    global _rate_requests_seen
    path = request.url.path
    if path not in {"/health", "/docs", "/openapi.json", "/redoc"}:
        bucket = _rate_bucket(request.method, path)
        limit, window = RATE_RULES[bucket]
        ip = request.client.host if request.client else "unknown"
        key = (ip, bucket)
        ts = time.monotonic()
        with _rate_lock:
            _rate_requests_seen += 1
            if _rate_requests_seen % 500 == 0 and len(_rate_hits) > 1000:
                for old_key, old_hits in list(_rate_hits.items()):
                    if not old_hits or ts - old_hits[-1] > 3600:
                        _rate_hits.pop(old_key, None)
            hits = _rate_hits[key]
            while hits and ts - hits[0] >= window:
                hits.popleft()
            if len(hits) >= limit:
                response = JSONResponse({"detail": "Слишком много запросов. Повторите позже."}, status_code=429)
                response.headers["Retry-After"] = str(max(1, int(window - (ts - hits[0]))))
                response.headers["X-Content-Type-Options"] = "nosniff"
                response.headers["X-Frame-Options"] = "DENY"
                response.headers["Referrer-Policy"] = "no-referrer"
                return response
            hits.append(ts)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if ENV == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def is_online(user: User) -> bool:
    return (utcnow() - as_utc(user.last_seen)).total_seconds() <= 45


def db_dep():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest_hex = encoded.split(":", 1)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def token_for(user_id: int) -> str:
    # Compact HMAC-signed bearer token (not a standards-compliant JWT).
    payload = {"sub": user_id, "exp": int(time.time()) + 7 * 24 * 3600, "iat": int(time.time())}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(SECRET_KEY.encode(), raw, hashlib.sha256).digest()
    return raw.decode() + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def decode_token(token: str) -> int:
    try:
        raw, signature = token.split(".", 1)
        expected = base64.urlsafe_b64encode(hmac.new(SECRET_KEY.encode(), raw.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        padded = raw + "=" * ((4 - len(raw) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if int(payload["exp"]) < int(time.time()):
            raise ValueError("expired")
        return int(payload["sub"])
    except Exception:
        raise HTTPException(401, "Недействительный или просроченный токен")


def current_user(request: Request, db: Session = Depends(db_dep)) -> User:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "Требуется авторизация")
    uid = decode_token(header.split(None, 1)[1].strip())
    user = db.get(User, uid)
    if not user:
        raise HTTPException(401, "Пользователь не найден")
    return user


def user_by_name(db: Session, name: str) -> Optional[User]:
    return db.scalar(select(User).where(User.username == name))


def member(db: Session, chat_id: int, user_id: int) -> bool:
    return db.scalar(select(ChatMember.id).where(ChatMember.chat_id == chat_id, ChatMember.user_id == user_id)) is not None


def require_member(db: Session, chat_id: int, user_id: int) -> None:
    if not member(db, chat_id, user_id):
        # A 404 avoids disclosing existence of another user's private chat.
        raise HTTPException(404, "Чат не найден")


def is_friend(db: Session, a: int, b: int) -> bool:
    return db.scalar(select(Friendship.id).where(Friendship.user_id == a, Friendship.friend_id == b)) is not None


def make_dm(db: Session, a: int, b: int) -> Chat:
    candidates = db.scalars(select(Chat).join(ChatMember, ChatMember.chat_id == Chat.id).where(Chat.is_group.is_(False), ChatMember.user_id == a)).all()
    for chat in candidates:
        ids = set(db.scalars(select(ChatMember.user_id).where(ChatMember.chat_id == chat.id)).all())
        if ids == {a, b}:
            return chat
    chat = Chat(title=None, is_group=False, created_by=a)
    db.add(chat)
    db.flush()
    db.add_all([ChatMember(chat_id=chat.id, user_id=a), ChatMember(chat_id=chat.id, user_id=b)])
    db.commit()
    db.refresh(chat)
    return chat


def public_message(db: Session, msg: Message) -> dict:
    sender = db.get(User, msg.sender_id)
    return {
        "id": msg.id, "chat_id": msg.chat_id, "sender_id": msg.sender_id,
        "sender_username": sender.username if sender else "?", "text": msg.text,
        "image_url": f"/chats/{msg.chat_id}/images/{msg.image_path}" if msg.image_path else None,
        "created_at": as_utc(msg.created_at).isoformat(),
    }


class WebSocketHub:
    """In-process delivery hub; use Redis pub/sub when horizontally scaling the API."""
    def __init__(self):
        self.connections: dict[WebSocket, dict] = {}

    async def connect(self, ws: WebSocket, user_id: int):
        self.connections[ws] = {"user_id": user_id, "chats": set()}

    def disconnect(self, ws: WebSocket):
        self.connections.pop(ws, None)

    async def send(self, ws: WebSocket, payload: dict):
        try:
            await ws.send_json(payload)
            return True
        except Exception:
            self.disconnect(ws)
            return False

    async def message_event(self, member_ids: set[int], chat_id: int, public: dict):
        for ws, info in list(self.connections.items()):
            if info["user_id"] not in member_ids:
                continue
            payload = {"type": "new_message", "chat_id": chat_id, "message_id": public["id"]}
            if chat_id in info["chats"]:
                payload["message"] = public
            await self.send(ws, payload)

    async def user_event(self, user_id: int, payload: dict):
        for ws, info in list(self.connections.items()):
            if info["user_id"] == user_id:
                await self.send(ws, payload)


hub = WebSocketHub()


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=1, max_length=128)


class FriendBody(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_]+$")


class GroupBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    members: list[str] = Field(default_factory=list, max_length=50)


class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.get("/health")
def health(db: Session = Depends(db_dep)):
    try:
        db.execute(select(1))
    except Exception:
        raise HTTPException(503, "Database unavailable")
    return {"status": "ok", "service": "NEXUS API", "version": APP_VERSION, "database": "connected"}


@app.post("/auth/register")
def register(body: Credentials, db: Session = Depends(db_dep)):
    if len(body.password) < 10:
        raise HTTPException(400, "Пароль должен содержать минимум 10 символов")
    if user_by_name(db, body.username):
        raise HTTPException(409, "Этот логин уже занят")
    user = User(username=body.username, password_hash=password_hash(body.password), last_seen=utcnow())
    db.add(user)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(409, "Не удалось создать пользователя: возможно, логин занят")
    db.refresh(user)
    return {"access_token": token_for(user.id), "token_type": "bearer", "user": {"id": user.id, "username": user.username}}


@app.post("/auth/login")
def login(body: Credentials, db: Session = Depends(db_dep)):
    user = user_by_name(db, body.username)
    if not user or not verify_password(body.password, user.password_hash):
        # Same response for missing account and incorrect password.
        raise HTTPException(401, "Неверный логин или пароль")
    user.last_seen = utcnow()
    db.commit()
    return {"access_token": token_for(user.id), "token_type": "bearer", "user": {"id": user.id, "username": user.username}}


@app.get("/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username, "online": is_online(user)}


@app.post("/presence/heartbeat")
def heartbeat(user: User = Depends(current_user), db: Session = Depends(db_dep)):
    user.last_seen = utcnow()
    db.commit()
    return {"ok": True, "server_time": utcnow().isoformat()}


@app.get("/users/search")
def search_users(q: str = Query(min_length=1, max_length=32), user: User = Depends(current_user), db: Session = Depends(db_dep)):
    q = q.strip()
    if not q:
        return []
    rows = db.scalars(select(User).where(User.username.ilike("%" + q + "%"), User.id != user.id).limit(20)).all()
    return [{"id": x.id, "username": x.username, "online": is_online(x), "is_friend": is_friend(db, user.id, x.id)} for x in rows]


@app.post("/friends/requests")
def friend_request(body: FriendBody, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    other = user_by_name(db, body.username)
    if not other:
        raise HTTPException(404, "Пользователь не найден")
    if other.id == user.id:
        raise HTTPException(400, "Нельзя добавить самого себя")
    if is_friend(db, user.id, other.id):
        raise HTTPException(409, "Вы уже друзья")
    reverse = db.scalar(select(FriendRequest).where(FriendRequest.sender_id == other.id, FriendRequest.receiver_id == user.id, FriendRequest.status == "pending"))
    if reverse:
        raise HTTPException(409, "У этого пользователя уже есть встречная заявка — откройте входящие заявки")
    existing = db.scalar(select(FriendRequest).where(FriendRequest.sender_id == user.id, FriendRequest.receiver_id == other.id))
    if existing and existing.status == "pending":
        raise HTTPException(409, "Заявка уже отправлена")
    if existing:
        existing.status = "pending"
        existing.created_at = utcnow()
        fr = existing
    else:
        fr = FriendRequest(sender_id=user.id, receiver_id=other.id, status="pending")
        db.add(fr)
    db.commit()
    db.refresh(fr)
    return {"id": fr.id, "status": fr.status}


@app.get("/friends/requests")
def incoming_requests(user: User = Depends(current_user), db: Session = Depends(db_dep)):
    rows = db.scalars(select(FriendRequest).where(FriendRequest.receiver_id == user.id, FriendRequest.status == "pending").order_by(FriendRequest.created_at.desc())).all()
    return [{"id": x.id, "username": db.get(User, x.sender_id).username, "created_at": as_utc(x.created_at).isoformat()} for x in rows]


@app.post("/friends/requests/{request_id}/accept")
def accept_request(request_id: int, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    fr = db.get(FriendRequest, request_id)
    if not fr or fr.receiver_id != user.id or fr.status != "pending":
        raise HTTPException(404, "Заявка не найдена")
    fr.status = "accepted"
    for a, b in ((user.id, fr.sender_id), (fr.sender_id, user.id)):
        if not db.scalar(select(Friendship.id).where(Friendship.user_id == a, Friendship.friend_id == b)):
            db.add(Friendship(user_id=a, friend_id=b))
    db.commit()
    chat = make_dm(db, user.id, fr.sender_id)
    return {"ok": True, "chat_id": chat.id}


@app.post("/friends/requests/{request_id}/decline")
def decline_request(request_id: int, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    fr = db.get(FriendRequest, request_id)
    if not fr or fr.receiver_id != user.id or fr.status != "pending":
        raise HTTPException(404, "Заявка не найдена")
    fr.status = "declined"
    db.commit()
    return {"ok": True}


@app.get("/friends")
def friends(user: User = Depends(current_user), db: Session = Depends(db_dep)):
    rows = db.scalars(select(Friendship).where(Friendship.user_id == user.id)).all()
    output = []
    for rel in rows:
        person = db.get(User, rel.friend_id)
        if person:
            output.append({"id": person.id, "username": person.username, "online": is_online(person)})
    return sorted(output, key=lambda x: (not x["online"], x["username"].lower()))


@app.post("/chats/direct/{username}")
def open_direct(username: str, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    other = user_by_name(db, username)
    if not other or other.id == user.id:
        raise HTTPException(404, "Пользователь не найден")
    if not is_friend(db, user.id, other.id):
        raise HTTPException(403, "Сначала добавьте пользователя в друзья")
    chat = make_dm(db, user.id, other.id)
    return {"id": chat.id, "title": None, "is_group": False, "peer_username": other.username, "peer_online": is_online(other), "last_message": "Нет сообщений"}


@app.post("/groups")
def create_group(body: GroupBody, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "Укажите название группы")
    ids = {user.id}
    for raw_name in body.members:
        name = raw_name.strip()
        if not USERNAME_RE.fullmatch(name):
            raise HTTPException(400, f"Некорректный логин: {name}")
        other = user_by_name(db, name)
        if not other:
            raise HTTPException(404, f"Пользователь @{name} не найден")
        if other.id != user.id and not is_friend(db, user.id, other.id):
            raise HTTPException(403, f"Сначала добавьте @{name} в друзья")
        ids.add(other.id)
    chat = Chat(title=title, is_group=True, created_by=user.id)
    db.add(chat)
    db.flush()
    db.add_all([ChatMember(chat_id=chat.id, user_id=uid) for uid in ids])
    db.commit()
    db.refresh(chat)
    return {"id": chat.id, "title": chat.title, "members": len(ids), "is_group": True}


@app.get("/chats")
def list_chats(user: User = Depends(current_user), db: Session = Depends(db_dep)):
    memberships = db.scalars(select(ChatMember).where(ChatMember.user_id == user.id)).all()
    output = []
    for cm in memberships:
        chat = db.get(Chat, cm.chat_id)
        if not chat:
            continue
        members = db.scalars(select(ChatMember).where(ChatMember.chat_id == chat.id)).all()
        peer = None
        if not chat.is_group:
            peer_id = next((x.user_id for x in members if x.user_id != user.id), None)
            peer = db.get(User, peer_id) if peer_id else None
        last = db.scalar(select(Message).where(Message.chat_id == chat.id).order_by(Message.id.desc()).limit(1))
        output.append({
            "id": chat.id, "title": chat.title if chat.is_group else None, "is_group": chat.is_group,
            "peer_username": peer.username if peer else None, "peer_online": is_online(peer) if peer else False,
            "last_message": ((last.text or "[Фото]")[:100]) if last else "Нет сообщений",
            "last_message_at": as_utc(last.created_at if last else chat.created_at).isoformat(), "unread_count": 0,
        })
    return sorted(output, key=lambda x: x["last_message_at"], reverse=True)


def _chat_member_ids(db: Session, chat_id: int) -> set[int]:
    return set(db.scalars(select(ChatMember.user_id).where(ChatMember.chat_id == chat_id)).all())


@app.get("/chats/{chat_id}/messages")
def get_messages(chat_id: int, limit: int = Query(default=100, ge=1, le=100), before_id: Optional[int] = None,
                 user: User = Depends(current_user), db: Session = Depends(db_dep)):
    require_member(db, chat_id, user.id)
    stmt = select(Message).where(Message.chat_id == chat_id)
    if before_id:
        stmt = stmt.where(Message.id < before_id)
    rows = db.scalars(stmt.order_by(Message.id.desc()).limit(limit)).all()
    return [public_message(db, msg) for msg in reversed(rows)]


@app.post("/chats/{chat_id}/messages")
async def send_message(chat_id: int, body: MessageBody, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    require_member(db, chat_id, user.id)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    msg = Message(chat_id=chat_id, sender_id=user.id, text=text)
    user.last_seen = utcnow()
    db.add(msg)
    db.commit()
    db.refresh(msg)
    public = public_message(db, msg)
    await hub.message_event(_chat_member_ids(db, chat_id), chat_id, public)
    return public


@app.post("/chats/{chat_id}/images")
async def send_image(chat_id: int, file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(db_dep)):
    require_member(db, chat_id, user.id)
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(415, "Разрешены JPG, PNG и WebP")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if not data:
        raise HTTPException(400, "Файл пустой")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Максимальный размер изображения — 8 МБ")
    signatures = {
        "image/jpeg": data.startswith(bytes.fromhex("FFD8FF")),
        "image/png": data.startswith(bytes.fromhex("89504E470D0A1A0A")),
        "image/webp": len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    }
    if not signatures.get(file.content_type, False):
        raise HTTPException(415, "Файл не похож на поддерживаемое изображение")
    try:
        with Image.open(io.BytesIO(data)) as image:
            actual_type = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(image.format)
            if actual_type != file.content_type:
                raise HTTPException(415, "Тип изображения не совпадает с содержимым")
            if image.width < 1 or image.height < 1 or image.width > 10000 or image.height > 10000 or image.width * image.height > 32_000_000:
                raise HTTPException(413, "Недопустимые размеры изображения")
            image.verify()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(415, "Повреждённое или небезопасное изображение")
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[file.content_type]
    name = secrets.token_hex(20) + ext
    target = (UPLOAD_DIR / name).resolve()
    if target.parent != UPLOAD_DIR:
        raise HTTPException(400, "Некорректное имя файла")
    target.write_bytes(data)
    msg = Message(chat_id=chat_id, sender_id=user.id, image_path=name)
    user.last_seen = utcnow()
    db.add(msg)
    db.commit()
    db.refresh(msg)
    public = public_message(db, msg)
    await hub.message_event(_chat_member_ids(db, chat_id), chat_id, public)
    return public


@app.get("/chats/{chat_id}/images/{filename}")
def get_image(chat_id: int, filename: str, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    require_member(db, chat_id, user.id)
    if not IMAGE_NAME_RE.fullmatch(filename):
        raise HTTPException(404, "Изображение не найдено")
    msg = db.scalar(select(Message).where(Message.chat_id == chat_id, Message.image_path == filename))
    if not msg:
        raise HTTPException(404, "Изображение не найдено")
    path = (UPLOAD_DIR / filename).resolve()
    if path.parent != UPLOAD_DIR or not path.is_file():
        raise HTTPException(404, "Изображение не найдено")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=None, headers={"Cache-Control": "private, max-age=3600"})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    header = ws.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        await ws.close(code=4401, reason="Authorization required")
        return
    try:
        uid = decode_token(header.split(None, 1)[1].strip())
    except HTTPException:
        await ws.close(code=4401, reason="Invalid token")
        return
    with SessionLocal() as db:
        if not db.get(User, uid):
            await ws.close(code=4401, reason="User not found")
            return
    await ws.accept()
    await hub.connect(ws, uid)
    await hub.send(ws, {"type": "ready", "user_id": uid})
    try:
        while True:
            data = await ws.receive_json()
            action = data.get("action") if isinstance(data, dict) else None
            if action == "subscribe":
                try:
                    chat_id = int(data.get("chat_id"))
                except (TypeError, ValueError):
                    await hub.send(ws, {"type": "error", "detail": "Invalid chat id"})
                    continue
                with SessionLocal() as db:
                    if member(db, chat_id, uid):
                        hub.connections[ws]["chats"].add(chat_id)
                        await hub.send(ws, {"type": "subscribed", "chat_id": chat_id})
                    else:
                        await hub.send(ws, {"type": "error", "detail": "Chat not found"})
            elif action == "unsubscribe":
                try:
                    chat_id = int(data.get("chat_id"))
                    hub.connections[ws]["chats"].discard(chat_id)
                    await hub.send(ws, {"type": "unsubscribed", "chat_id": chat_id})
                except (TypeError, ValueError, KeyError):
                    await hub.send(ws, {"type": "error", "detail": "Invalid chat id"})
            elif action == "ping":
                await hub.send(ws, {"type": "pong", "time": utcnow().isoformat()})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.disconnect(ws)
