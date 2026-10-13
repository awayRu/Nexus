"""Contract tests for key NEXUS API paths. Run: cd server && pytest -q"""
import os
import sys
import tempfile
from pathlib import Path

TEST_DIR = Path(tempfile.mkdtemp(prefix="nexus-tests-"))
os.environ["NEXUS_ENV"] = "test"
os.environ["NEXUS_SECRET_KEY"] = "test-only-key-which-is-not-used-in-production-123456789"
os.environ["DATABASE_URL"] = "sqlite:///" + str(TEST_DIR / "test.db")
os.environ["UPLOAD_DIR"] = str(TEST_DIR / "uploads")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from app import app, _rate_hits, _rate_lock

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolate_rate_limiter():
    with _rate_lock:
        _rate_hits.clear()


def register(name):
    response = client.post("/auth/register", json={"username": name, "password": "CorrectHorseBattery!2026"})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token):
    return {"Authorization": "Bearer " + token}


def test_health_and_registration_login():
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["database"] == "connected"
    token = register("nova_test")
    assert client.get("/me", headers=auth(token)).json()["username"] == "nova_test"
    login = client.post("/auth/login", json={"username": "nova_test", "password": "CorrectHorseBattery!2026"})
    assert login.status_code == 200
    bad = client.post("/auth/login", json={"username": "nova_test", "password": "wrong"})
    assert bad.status_code == 401


def test_friend_request_creates_direct_chat_and_messages():
    alice = register("alice_test")
    bob = register("bob_test")
    sent = client.post("/friends/requests", headers=auth(alice), json={"username": "bob_test"})
    assert sent.status_code == 200, sent.text
    incoming = client.get("/friends/requests", headers=auth(bob))
    assert incoming.status_code == 200 and len(incoming.json()) == 1
    accepted = client.post(f"/friends/requests/{incoming.json()[0]['id']}/accept", headers=auth(bob))
    assert accepted.status_code == 200, accepted.text
    chat_id = accepted.json()["chat_id"]
    sent_msg = client.post(f"/chats/{chat_id}/messages", headers=auth(alice), json={"text": "Привет, NEXUS!"})
    assert sent_msg.status_code == 200, sent_msg.text
    messages = client.get(f"/chats/{chat_id}/messages", headers=auth(bob))
    assert messages.status_code == 200
    assert messages.json()[0]["text"] == "Привет, NEXUS!"
    assert messages.json()[0]["sender_username"] == "alice_test"
    assert client.get(f"/chats/{chat_id}/messages").status_code == 401


def test_chat_membership_is_required():
    alice = register("member_alice")
    outsider = register("outsider_test")
    # Create a group using the owner's token. Outsider is not a member.
    group = client.post("/groups", headers=auth(alice), json={"title": "private group", "members": []})
    assert group.status_code == 200, group.text
    chat_id = group.json()["id"]
    denied = client.get(f"/chats/{chat_id}/messages", headers=auth(outsider))
    assert denied.status_code == 404


def test_image_upload_rejects_invalid_content():
    token = register("image_test")
    group = client.post("/groups", headers=auth(token), json={"title": "photos", "members": []}).json()
    response = client.post(f"/chats/{group['id']}/images", headers=auth(token), files={"file": ("fake.png", b"not an image", "image/png")})
    assert response.status_code == 415


def test_websocket_pushes_message_event_to_subscribed_member():
    alice = register("ws_alice")
    bob = register("ws_bob")
    sent = client.post("/friends/requests", headers=auth(alice), json={"username": "ws_bob"})
    assert sent.status_code == 200
    req = client.get("/friends/requests", headers=auth(bob)).json()[0]
    accepted = client.post(f"/friends/requests/{req['id']}/accept", headers=auth(bob))
    chat_id = accepted.json()["chat_id"]
    with client.websocket_connect("/ws", headers=auth(bob)) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"action": "subscribe", "chat_id": chat_id})
        assert ws.receive_json()["type"] == "subscribed"
        post = client.post(f"/chats/{chat_id}/messages", headers=auth(alice), json={"text": "Instant delivery"})
        assert post.status_code == 200
        event = ws.receive_json()
        assert event["type"] == "new_message"
        assert event["message"]["text"] == "Instant delivery"


def test_uploaded_image_is_private_to_chat_members():
    import io
    from PIL import Image
    alice = register("photo_alice")
    bob = register("photo_bob")
    outsider = register("photo_outsider")
    client.post("/friends/requests", headers=auth(alice), json={"username": "photo_bob"})
    req = client.get("/friends/requests", headers=auth(bob)).json()[0]
    accepted = client.post(f"/friends/requests/{req['id']}/accept", headers=auth(bob)).json()
    chat_id = accepted["chat_id"]
    image_stream = io.BytesIO()
    Image.new("RGB", (2, 2), (30, 220, 90)).save(image_stream, format="PNG")
    image = client.post(f"/chats/{chat_id}/images", headers=auth(alice), files={"file": ("test.png", image_stream.getvalue(), "image/png")})
    assert image.status_code == 200, image.text
    image_path = image.json()["image_url"]
    allowed = client.get(image_path, headers=auth(bob))
    assert allowed.status_code == 200
    denied = client.get(image_path, headers=auth(outsider))
    assert denied.status_code == 404
