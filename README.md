# NEXUS Messenger 2.0

NEXUS is an Android/Kivy client with a FastAPI backend, PostgreSQL production storage, Docker deployment files, and GitHub Actions APK build workflow. The client is not a standalone server: for real multi-device messaging, deploy `server/` to a publicly reachable HTTPS host first.

## What is implemented

- Username/password registration and login. Passwords are salted and hashed with `scrypt`.
- Seven-day HMAC-signed bearer tokens; protected API calls require `Authorization: Bearer ...`.
- Private one-to-one chats created after accepting a friend request.
- User search, friend requests, accept/decline, friend list, direct chat opening.
- Group creation and membership rules (other members must be friends).
- Text messages with history and online presence heartbeat.
- Authenticated WebSocket handshake with chat subscription and server-side message events. Client automatically reconnects and falls back to HTTP polling if WebSocket is unavailable.
- JPG, PNG and WebP uploads up to 8 MiB, image signature/decoder checks, dimension limit and chat-membership-protected downloads. Images are not exposed via a public static directory.
- Per-IP in-process request limits, basic browser security headers, production secret/database checks.
- SQLite for local backend development, PostgreSQL in production.
- A native Android image picker path, with a local file chooser fallback.

## File layout

```text
NEXUS/
├── main.py                       # Kivy Android client; test this first in Pydroid
├── buildozer.spec                # Android packaging settings
├── .github/workflows/build-apk.yml
├── deploy/Caddyfile.example      # HTTPS reverse-proxy example
└── server/
    ├── app.py                    # FastAPI API + WebSocket hub
    ├── requirements.txt
    ├── requirements-dev.txt
    ├── Dockerfile
    ├── docker-compose.yml
    └── tests/test_api.py
```

## 1. Run the Android client in Pydroid 3

1. Install Pydroid 3 and its Kivy support.
2. Copy `main.py` to the phone and open it in Pydroid.
3. Install its HTTP/WebSocket client packages from Pydroid's terminal:

   ```bash
   pip install requests websocket-client
   ```

4. Run `main.py`. Enter the API address on the login screen. The address must be your own running server, for example `https://chat.example.com`.
5. Create an account with a 3–32-character Latin-letter/digit/underscore username and a password of at least 10 characters.

The server URL is saved in the app's local data directory. The bearer token is currently held in memory only, so after restarting the app you must sign in again. There is no fake/offline server: a placeholder address will not register accounts or send messages.

### Test the backend locally first

From `server/`, install dependencies and run a development API backed by SQLite:

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -r requirements-dev.txt
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

- Health: `http://127.0.0.1:8000/health`
- Development API docs: `http://127.0.0.1:8000/docs`
- Test suite: `pytest -q`

For a phone and development computer on the same Wi-Fi, use the computer's LAN IP in the client. Plain HTTP is intended only for local testing; Android networking policy may also vary by build. Use HTTPS for remotely accessible servers.

## 2. Deploy the production backend

You need a Linux VPS or managed container host, a domain name pointing at it, Docker Compose, and HTTPS termination. On the server, enter the repository's `server/` directory:

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"
```

Put the generated random value in `NEXUS_SECRET_KEY`. Set `POSTGRES_PASSWORD` to a different strong database password; using `secrets.token_hex(32)` is convenient because it avoids URL-reserved characters in the database URL. **Never commit `.env` or publish these secrets.**

Start the API and database:

```bash
docker compose up --build -d
docker compose logs -f api
```

The API binds to `127.0.0.1:8000` on the host, rather than publicly exposing an unencrypted port. Configure a reverse proxy on that host with a valid TLS certificate. For Caddy installed directly on the host, copy `deploy/Caddyfile.example` to your Caddy config, replace `nexus.example.com` with your real domain, then reload Caddy. Ensure DNS points to your VPS and ports 80/443 are reachable.

After TLS is working, check `https://YOUR-DOMAIN/health`, then enter `https://YOUR-DOMAIN` in NEXUS. Check `docker compose logs -f api` for server-side errors. Back up the Postgres volume and `nexus_uploads` volume; both account/chat records and uploaded images matter.

### Important deployment notes

- The included request limiter is an in-process safety layer. With multiple server workers/replicas, use Redis or a reverse-proxy/API gateway for shared limits.
- The WebSocket hub is in-process. One API instance supports realtime delivery to its connected clients. For horizontally scaled instances, add Redis Pub/Sub (or another broker) and shared connection/event routing.
- Database tables are created on startup for the starter project. For future schema changes on an already deployed database, add and run Alembic migrations; do not assume `create_all()` upgrades old tables.
- Use HTTPS in production. Do not expose port 8000 directly to the public internet.

## 3. Build APK from GitHub

1. Create a new GitHub repository called `NEXUS`.
2. Upload the *contents* of this folder so `main.py` and `buildozer.spec` are in the repository root and the workflow lives at `.github/workflows/build-apk.yml`. Keep `server/` alongside them.
3. Commit and push to `main` or `master`.
4. Open **Actions → Build NEXUS APK** and choose **Run workflow**. A push changing `main.py`, `buildozer.spec`, or the workflow also starts a build.
5. When the run succeeds, open its artifacts and download **NEXUS-debug-apk**. Extract the archive to get the APK and install it on a test phone.
6. Enter the deployed HTTPS API address on the login screen.

This workflow creates a **debug APK** for testing, not a signed Play Store release. The first build downloads the Android SDK/NDK and can take a long time. Upstream Buildozer/python-for-android recipes may change; use the failed workflow log if a toolchain step breaks. To publish to an app store, configure a release build and protect signing keys with GitHub Secrets.

## API routes

- `GET /health`
- `POST /auth/register`, `POST /auth/login`, `GET /me`
- `POST /presence/heartbeat`
- `GET /users/search?q=...`
- `POST /friends/requests`, `GET /friends/requests`
- `POST /friends/requests/{id}/accept`, `POST /friends/requests/{id}/decline`, `GET /friends`
- `POST /chats/direct/{username}`, `POST /groups`, `GET /chats`
- `GET /chats/{id}/messages`, `POST /chats/{id}/messages`
- `POST /chats/{id}/images`, `GET /chats/{id}/images/{filename}` (chat membership required)
- `GET /ws` (WebSocket; send bearer token in the `Authorization` header, then `{"action":"subscribe","chat_id":123}`)

## Still not included

NEXUS now has a real multi-user API path and event-driven WebSocket delivery, but is still a starter project rather than an audited consumer service. It does **not** yet have end-to-end encryption, push notifications, read receipts, typing indicators, message edit/delete, account recovery, admin/moderation tooling, spam detection, media CDN, or Alembic migrations. In-memory rate limiting and WebSocket fan-out are designed for a single API instance. Complete a security review and add operational monitoring, backups, migration handling, abuse reporting, and shared infrastructure before accepting public users.
