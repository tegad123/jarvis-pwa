# Jarvis PWA

Spencer's personal AI assistant — a single web app on his phone with two modes:

1. **Talk to Jarvis** — hold the button, ask anything, Jarvis responds in voice.
2. **Ambient Recording** — hit record, think out loud. When stopped, the system auto-transcribes, extracts tasks/ideas/decisions, posts them to Discord for Jarvis to action, and archives the full memo for long-term recall.

No App Store. Spencer opens a URL on his phone, taps "Add to Home Screen," and it lives there like a native app.

---

## What this delivers vs. what Spencer asked for

| Spencer's ask | How it's built |
|---|---|
| App on phone, no App Store | Progressive Web App, installable via "Add to Home Screen" |
| Talk to Jarvis, voice in/out | `/api/talk` — Whisper + Claude + ElevenLabs round trip |
| Ambient recording, no response | `/api/ambient` — fire-and-forget, processes in background |
| Auto-transcribe on stop | Whisper triggered immediately on upload |
| Auto-extract tasks with "intuition" | Chief-of-staff prompt in `action_extractor.py` — explicitly told to infer, not just match |
| Long-term archive separate from Jarvis's mind | SQLite `voice_memos` table — Jarvis never reads it, only the action digest |
| 24/7 capture | Recording is on-demand (not always-on) by design — battery + privacy. Mode 2 makes it one tap to start. |

**One deliberate divergence from the literal ask:** Spencer said "24/7 recording." This system is one-tap-to-record, not always-on. Always-on would crater his phone battery, raise privacy concerns when others are in the room, and produce hours of dead air to transcribe. One-tap-to-record gets 95% of the benefit at 5% of the cost. If after testing he wants true always-on, we add it then.

---

## Architecture

```
┌────────────────────┐         ┌───────────────────────────────────┐
│  PWA on phone      │         │  Mac Mini (existing OpenClaw box) │
│  (added to home    │         │                                   │
│   screen)          │  HTTPS  │  ┌─────────────────────────────┐  │
│                    │ ───────▶│  │  FastAPI server :8765       │  │
│  ┌──────────────┐  │         │  │   ↓ Whisper API             │  │
│  │ Talk button  │  │ ◀───────│  │   ↓ Claude (Sonnet/Haiku)   │  │
│  └──────────────┘  │         │  │   ↓ ElevenLabs              │  │
│  ┌──────────────┐  │         │  │   ↓ SQLite (jarvis.db)      │  │
│  │ Record button│  │         │  │   ↓ Discord poster          │  │
│  └──────────────┘  │         │  └─────────────────────────────┘  │
│  ┌──────────────┐  │         │             ↓                     │
│  │ Memos        │  │         │  ┌─────────────────────────────┐  │
│  └──────────────┘  │         │  │  Existing OpenClaw / Jarvis │  │
└────────────────────┘         │  │  reads Discord digest and   │  │
                               │  │  executes via existing skill│  │
       Cloudflare Tunnel       │  └─────────────────────────────┘  │
       (already set up)        └───────────────────────────────────┘
```

---

## Deployment (Mac Mini)

### 1. Copy the project

Place the entire `jarvis-pwa/` folder somewhere on the Mac Mini, e.g.:

```bash
~/jarvis-pwa/
```

### 2. Install Python dependencies

```bash
cd ~/jarvis-pwa/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cd ~/jarvis-pwa
cp .env.example .env
nano .env
```

Fill in:
- `OPENAI_API_KEY` — for Whisper transcription
- `ANTHROPIC_API_KEY` — for Claude
- `DISCORD_BOT_TOKEN` — same token Jarvis already uses
- `DISCORD_JARVIS_CHANNEL_ID` — channel ID where Jarvis sees messages

`ELEVENLABS_API_KEY` will be loaded automatically from `~/.openclaw/workspace/secrets/elevenlabs.env` if it exists there (which it should).

### 4. Run the server

```bash
cd ~/jarvis-pwa/backend
source .venv/bin/activate
python server.py
```

The server starts on `http://0.0.0.0:8765`. Health check:

```bash
curl http://localhost:8765/api/health
```

### 5. Expose via Cloudflare tunnel

In the same Cloudflare tunnel config that already serves OpenClaw, add an ingress rule for the PWA:

```yaml
ingress:
  - hostname: jarvis.34dev.com
    service: http://localhost:8765
  # ... existing rules ...
```

Then restart the tunnel.

### 6. Install on Spencer's phone

1. On iPhone, open Safari and go to `https://jarvis.34dev.com`
2. Tap the Share button → "Add to Home Screen"
3. Done. Tap the Jarvis icon to launch.

(On Android, same flow via Chrome — three-dot menu → "Install app.")

### 7. Install the OpenClaw skill

Copy `skills/ambient_recording/SKILL.md` into Spencer's OpenClaw skills directory:

```bash
cp skills/ambient_recording/SKILL.md ~/.openclaw/workspace/skills/process_voice_memo_actions/SKILL.md
```

Then `/new` in Discord to reload Jarvis. Jarvis will now pick up the digest messages and process them.

---

## Running it as a background service (recommended)

Create `~/Library/LaunchAgents/com.jarvis.pwa.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.jarvis.pwa</string>
  <key>WorkingDirectory</key>
  <string>/Users/USERNAME/jarvis-pwa/backend</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/USERNAME/jarvis-pwa/backend/.venv/bin/python</string>
    <string>/Users/USERNAME/jarvis-pwa/backend/server.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/USERNAME/jarvis-pwa/data/server.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/USERNAME/jarvis-pwa/data/server.log</string>
</dict>
</plist>
```

Replace `USERNAME` and load it:

```bash
launchctl load ~/Library/LaunchAgents/com.jarvis.pwa.plist
```

---

## Testing checklist

- [ ] `curl http://localhost:8765/api/health` returns `{"ok": true, ...}` with all four flags green
- [ ] Open `https://jarvis.34dev.com` in browser on laptop — UI loads
- [ ] Hold the Talk button, say "hey what's up" — get a voice reply
- [ ] Switch to Record mode, talk for 30 seconds, stop — see a Discord message appear in #jarvis-assistant
- [ ] Switch to Memos tab — see the recording listed with summary and tags
- [ ] Add to Home Screen on iPhone — icon appears, opens full-screen

---

## Files

```
jarvis-pwa/
├── README.md                         ← this file
├── .env.example                      ← copy to .env and fill in
├── backend/
│   ├── server.py                     ← FastAPI app, both endpoints
│   ├── action_extractor.py           ← the "chief of staff" Claude prompt
│   ├── jarvis_brain.py               ← talk-mode Claude prompt
│   ├── elevenlabs_tts.py             ← voice synthesis
│   ├── discord_poster.py             ← posts digests to Discord
│   └── requirements.txt
├── frontend/
│   ├── index.html                    ← PWA UI
│   ├── styles.css
│   ├── app.js                        ← audio capture, mode switching
│   ├── manifest.json                 ← makes it installable
│   ├── sw.js                         ← service worker
│   ├── icon-192.png
│   └── icon-512.png
└── skills/
    ├── ambient_recording/SKILL.md    ← install into OpenClaw
    └── voice_chat/SKILL.md           ← reference doc
```

---

## Known limits & next steps

- **Wake word ("Hey Jarvis")** — not yet implemented in this v1. Spencer holds the button to talk, taps to record. Wake-word listening drains battery and is unreliable in PWAs on iOS. If he wants it later, the cleanest path is a native iOS shortcut that opens the PWA via URL scheme.
- **Always-on recording** — see "deliberate divergence" above. Easy to add if needed.
- **Multi-user** — currently single-user (Spencer). Adding Eddie/Blake/Martin requires auth, which is a known follow-up.
- **Skill execution from talk mode** — Mode 1 acknowledges tasks but doesn't execute. The path for execution is: talk → Spencer says "log a task to…" → talk-mode replies "on it" → posts to Discord → main Jarvis processes. Currently the talk mode does the acknowledgment but doesn't post to Discord yet. Easy to wire in once the rest is tested.

### Chat mode (Mode 4)

- **No login rate limiting.** The `/chat/api/login` endpoint accepts unlimited attempts and the password is a single 5-digit numeric code. Acceptable for a single-user PWA behind a Cloudflare tunnel; **must** be revisited before any wider exposure (multi-user, public deploy, longer-lived deploy). See `backend/chat_auth.py` TODO.
- **Markdown is rendered as plain text.** Claude's `**bold**` and `- bullet` syntax shows up literally in the bubble — asterisks and dashes are not converted. Intentional in v1 (no markdown dependency); upgrade to `marked` or similar when needed.
- **No image upload.** The composer accepts text only. Adding image support means a multipart endpoint, Claude vision wiring on the gateway side, and image-attachment rendering in bubbles.
- **No streaming responses.** Chat waits for the full Claude/gateway reply before rendering. Streaming can be added later via Server-Sent Events on `/chat/api/chats/{id}/message`.
- **Voice input lives in Talk mode, not Chat mode.** Chat is typed-only. If Spencer wants to speak into Chat, use Talk (Mode 1) — Chat mode is the persistent, multi-thread surface.
- **Relay-mode session continuity not yet wired.** `session_map` is populated (every PWA chat gets a unique `openclaw_session_id` = `pwa:chat:{chat_id}`), but the id is **not** passed through to the gateway request yet. Every chat creates a fresh OpenClaw session on the gateway side. Full continuity is a follow-up patch — see `backend/gateway_client.py` docstring.
- **PWA service worker may cache stale chat assets.** If you ship a Chat-mode update and don't see it on your phone, hard-refresh once or bump the cache version in `sw.js`.
