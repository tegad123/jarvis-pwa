# Jarvis PWA — Deploy Runbook

How to deploy a Chat-mode update (or any update) to the Mac Mini.

The PWA backend runs on the Mini as a `launchd` agent listening on
`127.0.0.1:8765` and is exposed publicly via the existing Cloudflare
tunnel at `https://app.34jarvis.uk`. The OpenClaw gateway runs locally
on the same Mini at `127.0.0.1:18789`.

---

## 0 — Prereqs (one-time, already done in v1)

- Tailscale / SSH access to the Mini configured.
- `~/jarvis-pwa` cloned on the Mini.
- `launchd` plist `com.34dev.jarvis-pwa.plist` installed under
  `~/Library/LaunchAgents/` and loaded.
- Cloudflare tunnel route `app.34jarvis.uk → http://127.0.0.1:8765`
  active.
- OpenClaw gateway running on `127.0.0.1:18789` (separate service,
  not managed by this runbook).

---

## 1 — Pull latest from main

Use the deploy script for normal deploys. It pulls `main`, stamps
frontend asset URLs with the app commit SHA being deployed, commits and
pushes that stamp, restarts launchd, and verifies the live CSS is fresh.

```bash
ssh nemoclaw@nemo-mac-mini.<tailnet>.ts.net    # or via Tailscale Magic DNS
cd ~/jarvis-pwa
./scripts/deploy.sh
```

Do **not** use the old shortcut (`git pull && launchctl kickstart`) for
frontend deploys. Cloudflare can cache `/styles.css` and `/app.js`
long enough to produce a mixed deploy: fresh HTML/JS with stale CSS.
The deploy script prevents that by cache-busting asset URLs.

Verify the latest commits look right:

```bash
git log --oneline -5
```

You should see the app commit you intended to deploy plus, when the
asset stamp changed, a deploy commit like:

```
Deploy: cache-bust assets for <sha>
```

### What `scripts/deploy.sh` does

1. Refuses to run if tracked files are dirty.
2. Fetches and fast-forwards `main`.
3. Reads the current app commit SHA. If `HEAD` is already a
   `Deploy: cache-bust assets for <sha>` commit, it reuses that recorded
   SHA so repeated deploys are idempotent.
4. Runs `scripts/cache-bust.sh <sha>` to update:
   - `frontend/index.html` asset URLs (`/styles.css?v=<sha>`,
     `/app.js?v=<sha>`, `/manifest.json?v=<sha>`)
   - `frontend/styles.css` `asset-version: <sha>` proof comment
   - `frontend/sw.js` cache name and shell asset URLs
5. Commits and pushes the asset stamp if it changed.
6. Restarts `com.34dev.jarvis-pwa` with `launchctl kickstart`.
7. Verifies `https://app.34jarvis.uk/styles.css?v=<sha>` contains
   `asset-version: <sha>`.

The verification uses the versioned CSS URL because the app itself uses
that URL. A stale bare `/styles.css` object in Cloudflare is harmless
once `index.html` points at `/styles.css?v=<sha>`.

---

## 2 — Update `.env` on the Mini

The Chat-mode release adds six new env vars. The existing
Talk/Record/Memos vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`ELEVENLABS_*`, `DISCORD_*`) stay as-is.

Append (or replace) these in `~/jarvis-pwa/.env`:

```bash
# Chat mode — Mode 4
JARVIS_PWA_PASSWORD=34811
JARVIS_PWA_SESSION_SECRET=<the value from the local-dev .env, or generate a fresh 32-byte url-safe string>

# Gateway — point at the local OpenClaw gateway in relay mode
JARVIS_GATEWAY_MODE=relay
JARVIS_GATEWAY_URL=http://127.0.0.1:18789
JARVIS_GATEWAY_TOKEN=<bearer token from ~/.openclaw/config/openclaw.json or wherever the gateway stores it>
```

To generate a fresh `JARVIS_PWA_SESSION_SECRET` if you'd rather not
reuse the local-dev one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Verify the values are set:

```bash
grep -E '^JARVIS_(PWA|GATEWAY)_' .env
```

Expect six lines back, all non-empty (except `JARVIS_GATEWAY_TOKEN`
which you'll need to paste in by hand).

---

## 3 — Restart the PWA backend

The deploy script already restarts the backend. Use this manual command
only for emergency restarts that do not change frontend assets:

```bash
launchctl kickstart -k gui/$(id -u)/com.34dev.jarvis-pwa
```

Tail the log to confirm clean boot:

```bash
tail -n 30 ~/jarvis-pwa/data/jarvis-pwa.log    # or wherever the plist sends stdout
```

Look for:

```
Database ready at /Users/nemoclaw/jarvis-pwa/data/jarvis.db
Jarvis PWA backend ready.
Application startup complete.
Uvicorn running on http://0.0.0.0:8765
```

You may also see existing warnings for `OPENAI_API_KEY` /
`ELEVENLABS_API_KEY` etc. if those aren't set — that's fine, Chat
mode doesn't need them.

---

## 4 — Smoke tests (from the Mini or from your laptop)

### 4a. Cache-bust verification

The deploy script performs this automatically. To run it by hand, replace
`<sha>` with the asset version printed by the deploy script:

```bash
curl -fsSL "https://app.34jarvis.uk/styles.css?v=<sha>" | grep "asset-version: <sha>"
```

If this fails, the likely culprit is Cloudflare or a browser/PWA
service-worker cache serving stale CSS. Re-run `./scripts/deploy.sh` and
confirm the versioned URL in `frontend/index.html` changed.

Static deploy-sensitive assets (`/index.html`, `/styles.css`,
`/app.js`, `/manifest.json`, `/sw.js`) are served with:

```
Cache-Control: no-cache, must-revalidate
```

This is intentionally conservative. The app is small, and avoiding stale
frontend deploys matters more than shaving a few milliseconds from CSS/JS
loads. Versioned URLs still let browsers and Cloudflare store assets, but
they must revalidate when the same URL is requested again.

### 4b. Health + unauthenticated chat status

```bash
curl https://app.34jarvis.uk/api/health
# → {"ok":true,"openai":true,"anthropic":true,...}

curl https://app.34jarvis.uk/chat/api/auth-status
# → {"authenticated":false}
```

### 4c. Login

```bash
curl -i -X POST https://app.34jarvis.uk/chat/api/login \
  -H "Content-Type: application/json" \
  -d '{"password":"34811"}' \
  -c /tmp/jar.jar
# → HTTP 200, Set-Cookie: jarvis_chat_session=...; HttpOnly; ...

curl https://app.34jarvis.uk/chat/api/auth-status -b /tmp/jar.jar
# → {"authenticated":true}
```

Wrong password should reject:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  https://app.34jarvis.uk/chat/api/login \
  -H "Content-Type: application/json" \
  -d '{"password":"wrong"}'
# → 401
```

### 4d. End-to-end: create chat → send message → confirm relay path hit

```bash
# Create
CHAT_ID=$(curl -s -X POST https://app.34jarvis.uk/chat/api/chats \
  -b /tmp/jar.jar | python3 -c "import json,sys;print(json.load(sys.stdin)['chat_id'])")
echo "chat_id=$CHAT_ID"

# Send a message
curl -s -X POST "https://app.34jarvis.uk/chat/api/chats/${CHAT_ID}/message" \
  -H "Content-Type: application/json" \
  -d '{"content":"hello from prod"}' \
  -b /tmp/jar.jar | python3 -m json.tool
```

While that's running, tail the PWA log on the Mini:

```bash
tail -f ~/jarvis-pwa/data/jarvis-pwa.log
```

You should see a line like:

```
[gateway:relay] 1 msgs -> openclaw:main @ http://127.0.0.1:18789
```

**If you see `[gateway:mock]` instead**, `JARVIS_GATEWAY_MODE` is still
set to `mock` (or unset). Re-check step 2 and re-kickstart.

**If the request hangs or returns 502 "gateway error"**, the OpenClaw
gateway probably isn't running or the bearer token is wrong. Check:

```bash
curl -i http://127.0.0.1:18789/v1/chat/completions \
  -H "Authorization: Bearer ${JARVIS_GATEWAY_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"model":"openclaw:main","messages":[{"role":"user","content":"ping"}]}'
```

A working gateway returns `200` with a chat completion JSON. `401`
means token mismatch; connection refused means the gateway isn't up.

### 4e. PWA on phone

1. Open `https://app.34jarvis.uk` on Spencer's phone.
2. Enter `34811` → chat UI.
3. Confirm the composer shows textarea + mic + SEND.
4. "+ New Chat" → type a message → Jarvis (real one, via the
   gateway) responds.

---

## 5 — Rollback

If the new release breaks something, roll back to the previous commit:

```bash
cd ~/jarvis-pwa
git log --oneline -5             # find the previous deploy SHA
git checkout <previous-sha> -- .  # restore prior tree
# or, full reset:
git reset --hard <previous-sha>

launchctl kickstart -k gui/$(id -u)/com.34dev.jarvis-pwa
```

The Chat-mode tables (`chats`, `messages`, `session_map`) are
additive — leaving them in `jarvis.db` after a rollback does not
affect Talk/Record/Memos. No DB migration to reverse.

---

## 6 — What to deploy next

When the gateway integration is confirmed end-to-end, follow up by
wiring the OpenClaw session id from `session_map` into the relay
request (see `backend/gateway_client.py` docstring + README "Known
Limitations"). Until then, every PWA chat starts a fresh gateway
session, which is acceptable for v1.
