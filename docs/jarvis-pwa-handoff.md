# Jarvis PWA — Handoff for the single-chat architecture refactor

You're picking up mid-refactor on the `claude/single-chat-architecture` branch.
**Commit 1 of 6 is done and pushed.** Commits 2–6 are spec'd below. Don't merge
to `main`; the user (Tega) reviews per-commit screenshots/results before
greenlighting the next step.

This document is intentionally long. The user asked for "more than enough
context." Better redundant than ambiguous.

---

## 1. TL;DR

Spencer's personal AI assistant — a Progressive Web App that lives on his
phone. Backend is FastAPI + sqlite, frontend is vanilla JS. It started life
as a 3-mode app (Talk / Record / Memos), grew a 4th mode (Chat with
password auth and a multi-thread sidebar), then a Talk-via-gateway upgrade
that gave Talk full Jarvis-skill access through the OpenClaw gateway.

The current refactor **kills Talk, Record, and Memos entirely. Chat
becomes the whole product surface** — text and voice both happen in the
chat composer (textarea + mic button + send button), persist to the same
single sidebar of conversations, and voice-originated bubbles get a 🎙️
icon and a replay button.

---

## 2. Repo + git state

**Location:** `/home/user/jarvis-pwa` (cloud sandbox; the real deploy
target is Spencer's Mac Mini at `~/jarvis-pwa`).

**Remote:** `tegad123/jarvis-pwa` on GitHub. You have GitHub MCP access
scoped to that one repo (`mcp__github__*` tools).

**Branches:**
- `main` — last commit `f0024ef` ("Talk mode → gateway + unified auth
  across all modes (frontend)"). This is the deployable state Tega has
  been running on the Mini.
- `claude/single-chat-architecture` — **the branch you should be on.**
  Off main, one commit ahead at `dfa39a5` ("Backend: voice-message
  endpoint + audio route + message audio columns"). Already pushed to
  origin.
- `claude/add-chat-mode-fLiHH` — old feature branch, in sync with main
  at `f0024ef`. Ignore.

**Verify your starting point:**
```bash
git branch --show-current     # → claude/single-chat-architecture
git log --oneline -1          # → dfa39a5 Backend: voice-message endpoint ...
git log --oneline main..HEAD  # → exactly one commit (dfa39a5)
```

If you're not there: `git checkout claude/single-chat-architecture`.

---

## 3. File map

```
/home/user/jarvis-pwa/
├── README.md                          (Spencer-facing doc; has a "Known limits" section)
├── DEPLOY.md                          (runbook for shipping to the Mini)
├── .env                               (local, gitignored — see §10)
├── .env.example                       (committed template)
├── backend/
│   ├── server.py                      ★ all FastAPI routes + DB init + helpers
│   ├── chat_auth.py                   HMAC cookie auth (login, logout, require_auth dep)
│   ├── gateway_client.py              mock vs relay routing to OpenClaw
│   ├── jarvis_brain.py                JARVIS_VOICE_SYSTEM + answer_with_voice_brain fallback
│   ├── elevenlabs_tts.py              ★ TTS for voice replies — DON'T TOUCH
│   ├── action_extractor.py            ✂ SLATED FOR DELETE in commit 2 (orphaned post-ambient)
│   ├── discord_poster.py              ✂ SLATED FOR DELETE in commit 2 (orphaned post-ambient)
│   └── requirements.txt
├── frontend/
│   ├── index.html                     ★ has tab-nav + 3 obsolete <section> blocks to delete
│   ├── styles.css                     ★ has obsolete CSS sections to delete (orb, ambient, archive, modal, body.chat-mode)
│   ├── app.js                         ★ has talkSession, talk handlers, ambient handlers, archive handlers to delete
│   ├── sw.js                          service worker — leave alone unless asked
│   ├── manifest.json                  PWA manifest — leave alone
│   ├── icon-192.png, icon-512.png
├── skills/
│   ├── voice_chat/SKILL.md            reference doc (read-only)
│   └── ambient_recording/SKILL.md     reference doc — Tega didn't mention; leave alone
└── data/
    ├── jarvis.db                      sqlite — migrations already ran on this DB
    └── audio_cache/                   webm + mp3 files (some from old ambient, OK to leave)
```

`★` = files you'll be modifying in commits 2–6.
`✂` = files to delete in commit 2.

---

## 4. What's already done (commit 1: `dfa39a5`)

### Schema delta
```sql
ALTER TABLE messages ADD COLUMN has_audio INTEGER DEFAULT 0;
ALTER TABLE messages ADD COLUMN audio_url TEXT;
```
Both ran successfully on the dev DB. Migration is idempotent (checks via
`PRAGMA table_info(messages)` before each ALTER). New installs get the
columns from the CREATE TABLE statement directly.

The `voice_memos`, `action_items`, `talk_history` tables **still exist
on disk** — commit 2 drops them.

### Endpoints in the current routing table
```
GET  /api/health                                open
POST /chat/api/login                            open (issues cookie)
POST /chat/api/logout                           open
GET  /chat/api/auth-status                      open
GET  /chat/api/chats                            require_auth (now returns has_audio aggregate)
POST /chat/api/chats                            require_auth
GET  /chat/api/chats/{id}/messages              require_auth (now returns has_audio + audio_url per row)
POST /chat/api/chats/{id}/message               require_auth (text turn, unchanged shape)
POST /chat/api/chats/{id}/voice-message         require_auth ← NEW
GET  /chat/api/audio/{filename}                 require_auth ← NEW
```

**Deleted:** `/api/talk`, `/api/ambient`, `/api/memos`, `/api/memos/{id}`,
`/chat/api/talk-session/new`. They return 405/404 from the static mount now.

### The new voice-message endpoint (full flow)
```
multipart audio upload
  → audio_bytes = await audio.read()
  → verify chat_id exists (404 if not)
  → Whisper transcribe (400 if empty transcript)
  → write user audio to AUDIO_CACHE_DIR/<uuid>.webm
  → _exchange_turn(chat_id, transcript, system_prompt=JARVIS_VOICE_SYSTEM, voice_fallback=True)
       → inserts user msg, auto-titles chat, loads history, calls gateway,
         inserts assistant msg, returns (user_msg_id, assistant_msg_id, reply_text, ts)
  → UPDATE messages SET has_audio=1, audio_url=... on user msg
  → ElevenLabs synthesize reply_text → mp3 bytes
  → write to AUDIO_CACHE_DIR/<uuid>.mp3
  → UPDATE messages SET has_audio=1, audio_url=... on assistant msg
  → IF ElevenLabs fails: log + continue without assistant audio (audio_url=null)
  → return {user_message: {...}, assistant_message: {...}}
```

### The new audio endpoint
```python
@app.get("/chat/api/audio/{filename}", dependencies=[Depends(require_auth)])
async def chat_audio(filename: str):
    if not _AUDIO_FILENAME_RE.match(filename):  # ^[0-9a-f]{32}\.(webm|mp3)$
        raise HTTPException(400, "invalid filename")
    path = AUDIO_CACHE_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "audio not found")
    media_type = "audio/webm" if filename.endswith(".webm") else "audio/mpeg"
    return FileResponse(str(path), media_type=media_type)
```
Tested: path traversal, uppercase hex, wrong-length hex all 400; missing
file 404; valid file 200 with correct Content-Type.

### `_exchange_turn` signature (important — both text and voice paths use it)
```python
async def _exchange_turn(
    chat_id: str,
    user_content: str,
    system_prompt: Optional[str] = None,
    voice_fallback: bool = False,
) -> tuple[str, str, str, str]:
    """Returns (user_msg_id, assistant_msg_id, reply_text, assistant_now)."""
```
**Critical change in commit 1:** `user_msg_id` is now in the return tuple
(it wasn't before). The voice endpoint needs it to UPDATE the user
message row with audio metadata. The text endpoint uses `_` for it.

---

## 5. Spec — the full architecture-v2 brief Tega sent

This is the source of truth for commits 2–6. Copy/paste verbatim:

> **Architecture v2 — Single Chat mode, voice-or-text composer, delete
> Talk + Record + Memos.**
>
> User decision: Talk mode is being killed. Record + Memos are being
> deleted. Chat becomes the single product surface, with full
> voice-to-voice via a mic button in the composer.
>
> **Spec:**
>
> 1. **UI shell — remove tab navigation entirely**
>    - Delete the 4-tab nav (`01 Talk / 02 Record / 03 Memos / 04 Chat`)
>    - App opens directly to the Chat surface (which is now just "the app")
>    - App header simplified — "JARVIS" branding + logout/menu link, nothing else
>    - Mobile: same layout, hamburger toggle still hides/shows sidebar
>
> 2. **Composer — add mic button next to send button**
>    - Composer becomes: `[textarea] [mic button] [send button]`
>    - Mic button visual: lime fill when idle, red pulse + waveform when actively recording, "transcribing..." spinner during processing
>    - Behavior:
>      - Press AND HOLD mic → recording starts, visual pulse
>      - Release → recording stops, audio sent to backend
>      - Backend: Whisper transcribes → message saved as user turn → gateway processes → assistant text response saved → TTS audio generated → response returned
>      - Frontend: shows transcript as user bubble, shows assistant text bubble, AUTO-PLAYS TTS audio
>      - Audio bubble in chat history has a replay button (tap to re-hear)
>    - Tap mic without holding = error "press and hold to record"
>    - Recording < 1 second = silently discarded (existing Whisper guardrail)
>
> 3. **Message types in chat history**
>    - User text messages: text bubble (existing)
>    - User voice messages: text bubble (transcript) WITH a small 🎙️ icon + audio replay button
>    - Assistant text messages: text bubble (existing)
>    - Assistant voice messages (replies to voice turns): text bubble WITH 🎙️ + audio replay button
>    - Database: messages table gets a new column `has_audio BOOLEAN DEFAULT FALSE` + `audio_url TEXT` for the audio file path
>
> 4. **Conversation threading**
>    - All conversations in single sidebar, sorted by `last_message_at DESC`
>    - Voice-originated conversations (where origin='talk') keep their 🎙️ prefix in sidebar
>    - Mixed conversations (some text, some voice) show 🎙️ if ANY message has audio
>    - "+ New Conversation" button at top of sidebar
>    - No more "continue most recent" vs "new" decision — Spencer always picks: tap a sidebar entry, or hit "+ New"
>    - Auto-title from first user message (text OR transcript) — existing 40-char ellipsis pattern unchanged
>
> 5. **Delete Talk + Record + Memos completely**
>    - Backend: `/api/talk`, `/api/ambient`, `/api/memos`, `/api/memos/{id}` endpoints. `voice_memos`, `action_items`, `talk_history` tables (DROP). Any helpers in jarvis_brain.py specific to ambient/memos.
>    - Frontend: Talk/Record/Memos HTML sections + CSS, `talkSession` module, Record/Memo handlers, tab navigation HTML+CSS, body classes related to mode switching.
>    - Keep: `JARVIS_VOICE_SYSTEM`, `answer_with_voice_brain`, `gateway_client.complete()`, Whisper + ElevenLabs integrations.
>
> 6. **New endpoint: POST `/chat/api/chats/{chat_id}/voice-message`** (see §4 above — done in commit 1)
>
> 7. **Existing endpoint modifications**
>    - `POST /chat/api/chats/{id}/message` (text send) — unchanged
>    - `GET /chat/api/chats/{id}/messages` — return `has_audio` and `audio_url` per message
>    - `GET /chat/api/chats` — unchanged
>    - `POST /chat/api/chats` — unchanged (origin='chat' default; talk-session/new can be deleted)
>
> 8. **Database migrations**
>    ```sql
>    ALTER TABLE messages ADD COLUMN has_audio BOOLEAN DEFAULT FALSE;
>    ALTER TABLE messages ADD COLUMN audio_url TEXT;
>    DROP TABLE IF EXISTS voice_memos;
>    DROP TABLE IF EXISTS action_items;
>    DROP TABLE IF EXISTS talk_history;
>    ```
>    Migration must be idempotent. Handle existing `chats.origin` column gracefully.
>
> 9. **iOS standalone PWA considerations**
>    - Mic button must request microphone permission on first use
>    - Audio playback inline (not pop-out)
>    - Composer respects `env(safe-area-inset-bottom)`
>    - Mic recording must work in standalone PWA mode (Safari has historically had issues here — verify)
>
> 10. **Login screen layout bug**
>     - Login card renders at (0, 0) overlapping iOS status bar
>     - Fix: position: fixed, centered with flex; display: none when body.authenticated; z-index: 1000 / app: 1
>     - Verify on iPhone Safari standalone mode

### Commit strategy from the spec (verbatim)

1. Backend: voice-message endpoint + DB migration + delete Talk/Record/Memos endpoints ← **DONE (`dfa39a5`)**
2. Backend: drop unused tables
3. Frontend: remove tab nav, delete Talk/Record/Memos sections
4. Frontend: add mic button to chat composer + voice handling
5. Frontend: audio replay in message bubbles
6. Bug fix: login card layout on iPhone

"Stop and report when each commit is done. I'll greenlight progression."
**Don't merge to main yet.**

### Decisions locked in this session (Tega's answers to Q1/Q2/Q3)

| Question | Answer | Implication |
|---|---|---|
| Q1 — Audio file serving | **Option A: auth-gated route** | `GET /chat/api/audio/{filename}` exists, gated by `require_auth`, strict UUID regex. Done in commit 1. |
| Q2 — Dead modules | **Option A: full delete** | `backend/action_extractor.py` and `backend/discord_poster.py` get deleted in commit 2. `data/audio_cache/*.webm` from the old ambient mode stay on disk. |
| Q3 — Audio file lifecycle | **Skip cleanup, document in README** | Audio files accumulate forever. Tega's estimate: "~100MB max per year." Add to README known-limitations in commit 2. |

Tega also confirmed: `_exchange_turn` stays — both text and voice paths
call into it. Login bug diagnosis (env(safe-area-inset-top) missing) is
correct. The `display: none !important` on `body.authenticated .shell-login`
is "smart belt-and-suspenders."

---

## 6. What's left — commits 2–6 in detail

### Commit 2 — Backend: drop unused tables + delete dead modules

**Files to edit:**
- `backend/server.py` — add DROP TABLE migration in `init_db()`. Idempotent
  via `IF EXISTS`. Add after the existing ALTER COLUMN migrations.
- `README.md` — append "audio cache TTL" note to known limitations section.

**Files to delete:**
- `backend/action_extractor.py`
- `backend/discord_poster.py`

**Sketch of the migration to add in `init_db()`:**
```python
# After the existing has_audio/audio_url migrations:
for table in ("voice_memos", "action_items", "talk_history"):
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone():
        conn.execute(f"DROP TABLE {table}")
        log.info(f"Migration: dropped legacy table {table}")
```

**Don't accidentally re-add the dropped tables to SCHEMA.** I already
removed their CREATE TABLE statements from SCHEMA in commit 1 — check
the current `server.py` SCHEMA constant to confirm.

**Verification:**
```bash
# After server restart, check sqlite_master:
sqlite3 data/jarvis.db "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
# Expect ONLY: chats, messages, session_map (and sqlite_sequence if present)
# voice_memos, action_items, talk_history should be gone
```

**README addition (Tega's exact phrasing):**
> No audio cache TTL — voice message audio files accumulate in
> `data/audio_cache/`. Single-user usage at expected volume means
> ~100MB max per year. Sweep manually or add a TTL job if volume grows.

**Commit message style** (match what I did in commit 1):
- Subject: `Backend: drop legacy tables + delete orphaned modules`
- Body: enumerate what was dropped, confirm migration is idempotent,
  note that deleting the .py files breaks nothing (no remaining
  callers).

**Stop and report after this commit.**

---

### Commit 3 — Frontend: remove tab nav + delete Talk/Record/Memos sections

This is a big mechanical delete. The frontend currently has 4 modes with
tab switching; after this commit it has 1 (Chat) with no tabs.

**`frontend/index.html` deletions:**
- The `<nav class="tabs">…</nav>` block (4 buttons)
- `<section class="screen screen-talk active" data-screen="talk">…</section>` (orb, mic button, "saving to" indicator, transcript pane)
- `<section class="screen screen-ambient" data-screen="ambient">…</section>` (recorder, waveform, processing pane)
- `<section class="screen screen-archive" data-screen="archive">…</section>` (memo list, refresh button)
- `<div class="modal-backdrop" id="memoModal">…</div>` (memo detail modal)
- The leading `class="screen screen-chat"` on the surviving chat section can probably be simplified (no more "screen" semantics needed since there's only one)

**`frontend/index.html` keep:**
- `<body class="auth-pending">` (auth state machine still relevant)
- `<div class="shell-login" id="shellLogin">…</div>` (login card — commit 6 will fix it for iOS)
- `<div id="app">…</div>` containing the top bar + the chat surface
- `<header class="top-bar">` — simplify per spec §1: just "JARVIS" branding + a logout link. Drop the status indicator? Tega's spec says "JARVIS branding + logout/menu link, nothing else." So yes, remove the `.status` element. (The chat logout button in the sidebar can stay too.)
- The chat surface as-is — sidebar + main pane + composer

**`frontend/styles.css` deletions** (these are entire CSS sections, search for the comment headers):
- `/* ════════ TABS ════════ */` (and the `.tabs`, `.tab`, `.tab-num`, `.tab-label` rules)
- `/* ════════ ORB (TALK MODE) ════════ */` block
- `/* ════════ PRIMARY BUTTON ════════ */` block (only used by Talk + Record buttons)
- `/* ════════ AMBIENT MODE ════════ */` block
- `/* ════════ ARCHIVE MODE ════════ */` block (`.ghost-button`, `.memo-list`, `.memo-card`, etc.)
- `/* ════════ MODAL ════════ */` block
- `/* ════════ TALK "SAVING TO" INDICATOR ════════ */` block
- All `body.chat-mode #app { max-width: ... }` rules and the `body.chat-mode .screen-chat.active { padding: 0; }` rule — Chat is no longer a "mode", it's the whole app. Tweak `#app` styles so chat layout works without the `chat-mode` class.
- The `.transcript-pane` rules (Talk-mode artifact)
- Status indicator CSS if you removed it from the top bar

**`frontend/styles.css` keep:**
- Design tokens (`:root` vars)
- Top bar rules (simplified)
- Shell-login rules (commit 6 will tweak them)
- All chat-* rules (sidebar, bubble, composer, mobile breakpoint)
- Scrollbars
- The mobile `@media (max-width: 767px)` block for chat

**`frontend/app.js` deletions:**
- Anything Talk: `talkBtn`, `talkCap`, `talkOrb`, `talkTranscript`, `ensureTalkStream`, `startTalk`, `stopTalk`, `handleTalkStop`, `appendTalkTurn`, talk button event listeners
- The entire `talkSession` module (mint, clear, updateIndicator)
- Anything Ambient: `recordBtn`, `recorderTime`, `recorderState`, `waveformCanvas`, `procPane`, `ensureAmbientStream`, `startAmbient`, `stopAmbient`, `handleAmbientStop`, `resetProcessingSteps`, `setProcStep`, `startAmbientTimer`, `stopAmbientTimer`, `startWaveform`, `drawWaveform`, `stopWaveform`, record button listener
- Anything Archive: `loadMemos`, `renderMemoCard`, `openMemoDetail`, modal listeners, refresh button listener
- `switchMode` function and the `.tab` click listeners that call it
- The "additive tab listener" block at the bottom that toggles `body.chat-mode`
- `state.talkChatId`, `state.talkRecorder`, `state.talkStream`, `state.talkChunks`, `state.talkPressing`, all `state.ambient*` fields, `state.mode`
- `decodeHeader` (only used by Talk response headers)
- `pickMime` (used by Talk + Ambient MediaRecorder — but you'll re-add it for the chat mic in commit 4, so consider keeping it)

**`frontend/app.js` keep:**
- `API_BASE`, `$`, `$$` shorthands
- `state` object — slim it down to `{ authenticated: false }` plus a small `chat` sub-state
- `setStatus` + `checkHealth` — but Tega removed the status indicator from the top bar per spec, so these may be unused. Either delete or leave for `/api/health` smoke pings. Lean: delete to keep things clean.
- `shellAuth` module (login/logout/applyAuthState) — UNCHANGED
- `chat` namespace (init, onActivate, onDeactivate, loadChats, renderChatList, createChat, openChat, renderMessages, appendMessage, sendMessage, autoResize, toggleSidebar, closeSidebar, relativeTime) — but consider whether `onActivate`/`onDeactivate` still make sense without tabs. Chat is now always active.
- `escapeHtml` utility
- The `gesturestart` zoom-prevent handler

**After this commit, the app should still work** — text-only chat,
no tabs, login → straight into the chat surface. Run Playwright to
verify (see §8 for browser harness).

**Stop and report after this commit** with a screenshot showing
"opens directly to chat, no tabs visible."

---

### Commit 4 — Frontend: mic button + voice handling in the composer

**`frontend/index.html` change:** the composer becomes
```html
<div class="chat-composer">
  <textarea id="chatInput" ...></textarea>
  <button class="chat-mic-button" id="chatMicButton" aria-label="Hold to record">
    <!-- mic icon SVG, e.g. lucide-react style mic glyph -->
  </button>
  <button class="chat-send-button" id="chatSendButton">send</button>
</div>
```

**`frontend/styles.css` additions:**
- `.chat-mic-button` — same height/width as send button (40×40 or 40×~16),
  lime `var(--accent)` background by default
- `.chat-mic-button.recording` — red `var(--red)` background, animated pulse
  (CSS keyframes: scale 1 → 1.05 → 1, opacity dip)
- `.chat-mic-button.processing` — desaturated, with a small spinner or
  "transcribing..." text
- `.chat-mic-button:disabled` — opacity 0.4
- A live-waveform area (optional) above or beside the composer during
  recording. Reuse the AnalyserNode pattern from the old ambient code
  if you want.

**`frontend/app.js` additions** (inside the `chat` namespace):
```javascript
mic: {
  stream: null,         // shared MediaStream
  recorder: null,       // MediaRecorder
  chunks: [],
  pressing: false,
  sending: false,
},

async ensureMicStream() {
  if (this.mic.stream) return this.mic.stream;
  try {
    this.mic.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    return this.mic.stream;
  } catch (e) {
    // mic blocked — show transient error in composer
    throw e;
  }
},

async startRecording() {
  const stream = await this.ensureMicStream();
  this.mic.chunks = [];
  const recorder = new MediaRecorder(stream, { mimeType: pickMime() });
  recorder.ondataavailable = (e) => { if (e.data.size) this.mic.chunks.push(e.data); };
  recorder.onstop = () => this.handleMicStop();
  recorder.start();
  this.mic.recorder = recorder;
  this.el.micButton.classList.add('recording');
  this.mic.pressing = true;
},

stopRecording() {
  if (this.mic.recorder?.state === 'recording') {
    this.mic.recorder.stop();
  }
  this.mic.pressing = false;
  this.el.micButton.classList.remove('recording');
  this.el.micButton.classList.add('processing');
},

async handleMicStop() {
  const blob = new Blob(this.mic.chunks, { type: this.mic.recorder.mimeType });
  this.mic.chunks = [];
  if (blob.size < 1000) {
    // < ~1 second; silently discard per spec
    this.el.micButton.classList.remove('processing');
    return;
  }
  // ensure we have a current chat — create one if not
  if (!this.state.currentChatId) {
    await this.createChat();
    if (!this.state.currentChatId) {
      this.el.micButton.classList.remove('processing');
      return;
    }
  }
  const chatId = this.state.currentChatId;
  try {
    const form = new FormData();
    form.append('audio', blob, 'voice.webm');
    const r = await fetch(`/chat/api/chats/${chatId}/voice-message`, {
      method: 'POST',
      body: form,
    });
    if (r.status === 401) { shellAuth.logout(); return; }
    if (!r.ok) throw new Error(`server ${r.status}`);
    const data = await r.json();
    // Render both bubbles
    this.appendMessage(data.user_message);
    this.appendMessage(data.assistant_message);
    // Auto-play assistant audio
    if (data.assistant_message.audio_url) {
      const audio = new Audio(data.assistant_message.audio_url);
      audio.play().catch(() => {}); // iOS may reject without user gesture
    }
    // Refresh sidebar in case title was auto-set
    await this.loadChats();
  } catch (e) {
    console.error(e);
    // optional: show error toast
  } finally {
    this.el.micButton.classList.remove('processing');
  }
},

// In init():
this.el.micButton = $('#chatMicButton');
this.el.micButton.addEventListener('pointerdown', (e) => {
  e.preventDefault();
  this.startRecording().catch(() => { this.mic.pressing = false; });
});
const endPress = () => {
  if (this.mic.pressing) this.stopRecording();
};
this.el.micButton.addEventListener('pointerup', endPress);
this.el.micButton.addEventListener('pointerleave', endPress);
this.el.micButton.addEventListener('pointercancel', endPress);
```

**Update `appendMessage` to handle audio rendering** — defer the full
audio-replay UI to commit 5, but make `appendMessage` not break on
voice messages. Easiest: just render the transcript as before; commit 5
adds the 🎙️ + replay button.

**Test via Playwright with fake media:**
- Launch Chromium with `--use-fake-ui-for-media-stream --use-fake-device-for-media-stream`
- Or use `page.context.grant_permissions(['microphone'])`
- Won't transcribe to anything meaningful without a real audio source, but
  you can verify the request shape, the UI states, and the response
  rendering with mocked backend audio.
- Or stub the backend voice endpoint with a fixture response and verify
  the frontend renders both bubbles correctly.

**Stop and report after this commit** with a screenshot showing the
composer with mic button visible.

---

### Commit 5 — Frontend: audio replay in message bubbles

**`frontend/app.js` changes:**

Update `chat.appendMessage(m)` to render the audio UI when `m.has_audio`
is true:

```javascript
appendMessage(m) {
  const empty = this.el.messages.querySelector('.chat-empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  div.className = `chat-bubble chat-bubble-${m.role}`;
  if (m.pending) div.classList.add('chat-bubble-pending');
  if (m.has_audio) div.classList.add('chat-bubble-with-audio');

  if (m.has_audio && m.audio_url) {
    const icon = document.createElement('span');
    icon.className = 'chat-bubble-mic-icon';
    icon.textContent = '🎙️';
    div.appendChild(icon);
  }

  const text = document.createElement('span');
  text.className = 'chat-bubble-text';
  text.textContent = m.content;
  div.appendChild(text);

  if (m.has_audio && m.audio_url) {
    const replay = document.createElement('button');
    replay.className = 'chat-bubble-replay';
    replay.setAttribute('aria-label', 'Replay audio');
    replay.textContent = '▶';
    replay.addEventListener('click', () => {
      const audio = new Audio(m.audio_url);
      audio.play().catch(() => {});
    });
    div.appendChild(replay);
  }

  this.el.messages.appendChild(div);
  this.scrollToBottom();
  return div;
}
```

Update `chat.renderChatList(c)` so the 🎙️ prefix triggers on either
`c.origin === 'talk'` **or** `c.has_audio === true`:

```javascript
const isVoice = c.origin === 'talk' || c.has_audio === true;
const prefix = isVoice ? '🎙️ ' : '';
const titleClasses = [
  'chat-list-item-title',
  titleEmpty ? 'empty' : '',
  isVoice ? 'is-voice' : '',  // renamed from 'is-talk'
].filter(Boolean).join(' ');
```

**`frontend/styles.css` additions:**
```css
.chat-bubble-with-audio {
  display: flex;
  align-items: center;
  gap: 8px;
}
.chat-bubble-mic-icon {
  font-size: 13px;
  flex-shrink: 0;
}
.chat-bubble-replay {
  background: transparent;
  border: 1px solid var(--line);
  border-radius: 100px;
  padding: 4px 10px;
  font-size: 11px;
  color: var(--text-dim);
  cursor: pointer;
  flex-shrink: 0;
}
.chat-bubble-replay:hover {
  border-color: var(--accent);
  color: var(--accent);
}
```

**Stop and report after this commit** with screenshots showing voice
bubbles in chat history with replay buttons working.

---

### Commit 6 — Login card iOS layout bug

**`frontend/styles.css` changes:**
```css
.shell-login {
  position: fixed;
  inset: 0;
  z-index: 1000;       /* ← was unset; now explicitly above #app */
  align-items: center;
  justify-content: center;
  padding-top: max(24px, env(safe-area-inset-top));    /* ← respects iOS notch */
  padding-bottom: max(24px, env(safe-area-inset-bottom));
  padding-left: 24px;
  padding-right: 24px;
  /* background unchanged */
}

#app {
  z-index: 1;          /* ← explicit hierarchy */
}

body.authenticated .shell-login {
  display: none !important;   /* ← belt-and-suspenders */
}
```

**Verification:** Playwright with iPhone 14 Pro emulation. Render the
login card and visually confirm it doesn't overlap the notch area.
The emulation provides safe-area-inset values automatically.

**Stop and report after this commit.** Final commit. Don't merge to
main yet — Tega reviews the whole set.

---

## 7. Architecture reference (no reading required)

### Auth flow
- User submits password → `POST /chat/api/login {password}` → server
  compares against `JARVIS_PWA_PASSWORD` env var via constant-time
  `hmac.compare_digest` → on success, sets HttpOnly cookie
  `jarvis_chat_session=<ts>.<hmac_sha256>`, `Path=/`, `Max-Age=2592000`
  (30 days), `SameSite=Lax`.
- HMAC key = `JARVIS_PWA_SESSION_SECRET` env var.
- Every gated endpoint depends on `require_auth(request)` which decodes
  the cookie, recomputes the signature, checks expiry.
- `is_authenticated(request) -> bool` is the non-throwing form used by
  `/chat/api/auth-status`.

### Gateway client (mock vs relay)
- **mock mode** (`JARVIS_GATEWAY_MODE=mock`, default for local dev):
  call Anthropic SDK directly with `claude-sonnet-4-6`. Uses
  `MOCK_SYSTEM_PROMPT` unless caller overrides via `system_prompt=`.
- **relay mode** (`JARVIS_GATEWAY_MODE=relay`, used on the Mini):
  POSTs OpenAI-compatible request to `JARVIS_GATEWAY_URL/v1/chat/completions`
  with model `openclaw:main` and bearer token `JARVIS_GATEWAY_TOKEN`.
  If `system_prompt` is provided, it's prepended as `{role:"system",
  content:...}` in the messages array (OpenClaw may or may not honor it;
  if it doesn't, OpenClaw's own system prompt applies).
- session_id passthrough to relay is NOT wired yet — every chat creates
  a fresh OpenClaw session.

### Two system prompts
- `gateway_client.MOCK_SYSTEM_PROMPT` — typed Chat replies. Paragraphs
  OK. Discloses "mock mode" if asked about specific data.
- `jarvis_brain.JARVIS_VOICE_SYSTEM` — spoken replies. Under 50 words.
  No markdown. Action-confirming. Used by `/chat/api/chats/{id}/voice-message`
  and by the `answer_with_voice_brain` fallback helper.

### `_exchange_turn` (in `server.py`)
The single helper that owns insert-user → load-history → call-gateway →
insert-assistant. Used by both text and voice paths. Returns
`(user_msg_id, assistant_msg_id, reply_text, assistant_now)`.
`voice_fallback=True` swaps the gateway 502 for a direct
`answer_with_voice_brain` call (so Talk-mode-style failures still
produce a spoken reply).

### Frontend module map (currently)
- `shellAuth` — top-of-file. Checks `/chat/api/auth-status` on init,
  swaps `body.auth-pending` for `body.authenticated` or `body.unauthenticated`,
  handles login/logout form submission.
- `talkSession` — to be deleted in commit 3.
- `chat` — the Chat namespace. State: `chats`, `currentChatId`, `sending`,
  `loaded`. Methods: `init`, `onActivate`, `onDeactivate`, `loadChats`,
  `renderChatList`, `createChat`, `openChat`, `renderMessages`,
  `appendMessage`, `scrollToBottom`, `sendMessage`, `autoResize`,
  `toggleSidebar`, `closeSidebar`, `relativeTime`. Will gain `mic`
  state + `startRecording`/`stopRecording`/`handleMicStop` in commit 4.
- Talk/Ambient/Archive procedural code — to be deleted in commit 3.

---

## 8. How to run + test locally

### Server boot
```bash
cd /home/user/jarvis-pwa
/home/user/jarvis-pwa/.venv/bin/python backend/server.py > /tmp/jarvis.log 2>&1 &
echo $! > /tmp/jarvis.pid
sleep 3
cat /tmp/jarvis.log    # check for "Jarvis PWA backend ready."
```

Stop:
```bash
kill $(cat /tmp/jarvis.pid)
```

### .env (already present, gitignored)
```
JARVIS_PWA_PASSWORD=34811
JARVIS_PWA_SESSION_SECRET=F2PbVy-FOZAfpsNZ9nlt0n2FufNdcY9ObDXWhcp0B10
ANTHROPIC_API_KEY=sk-ant-api03-...
JARVIS_GATEWAY_MODE=mock
JARVIS_GATEWAY_URL=http://127.0.0.1:18789
JARVIS_GATEWAY_TOKEN=
```

**Missing locally** (only configured on the Mini):
- `OPENAI_API_KEY` — needed for Whisper. Voice-message endpoint will
  500 without it. Text turns still work fine.
- `ELEVENLABS_API_KEY` — needed for TTS. Voice-message endpoint
  silently falls through with `audio_url=null` on the assistant
  message if missing.

**To test the full voice round-trip locally:** add real Whisper +
ElevenLabs keys to `.env`, or skip and rely on the Mini for end-to-end
voice testing (DEPLOY.md covers it).

### Curl pattern for auth-gated endpoints
```bash
# Login → save cookie jar
curl -X POST http://localhost:8765/chat/api/login \
  -H "Content-Type: application/json" \
  -d '{"password":"34811"}' \
  -c /tmp/jar.jar

# Subsequent requests with -b
curl http://localhost:8765/chat/api/chats -b /tmp/jar.jar
```

### Playwright harness
Chromium is pre-installed at `/opt/pw-browsers/chromium-1194/chrome-linux/chrome`.
Playwright python pinned to 1.49.0 (compatible with that chromium version).

Pattern:
```python
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
            args=["--no-sandbox"],
            # For mic testing (commit 4):
            # args=["--no-sandbox", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
        )
        # Desktop:
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        # Mobile:
        # ctx = await browser.new_context(**pw.devices["iPhone 14 Pro"])
        page = await ctx.new_page()
        await page.goto("http://localhost:8765/", wait_until="networkidle")
        await page.screenshot(path="/tmp/jarvis-shots/something.png")
        # Login if needed:
        # await page.fill("#shellPasswordInput", "34811")
        # await page.click(".shell-login-button")
        # await page.wait_for_function("() => document.body.classList.contains('authenticated')")
        await browser.close()

asyncio.run(main())
```

Deliver screenshots via the `SendUserFile` tool (Claude Code only — for
Codex, just save to disk and tell Tega the path).

---

## 9. Working with Tega — observed preferences

- **Scope discipline.** Confirm scope before building. Flag concerns
  proactively, especially security implications. Lean toward "ask one
  question now" over "build something Tega has to undo."
- **Checkpoint commits.** Each commit is a self-contained, verifiable
  unit. Stop and report after each one. Don't batch.
- **Commit messages.** Long, technical, future-Claude-friendly. Use
  the body to explain WHY. Bullet points OK. No emoji in commit
  subjects. The trailer Claude Code adds (`https://claude.ai/code/session_...`)
  is fine — for Codex, omit it.
- **Reports back.** Quantitative when possible — "7 of 7 auth tests
  passed", "11 of 14 screenshots captured", specific status codes,
  log lines. Tega likes seeing the actual evidence.
- **Visual proof.** Screenshots after any UI change. Mobile + desktop.
- **Security-first.** Auth gates, path traversal, content-type, etc.
  Surface concerns even when not asked.
- **No emoji in code/commits** unless it's a domain-specific signal
  (🎙️ for voice UI is fine — Tega specified it).
- **Fast-forward only.** No merge commits. The history stays linear.
- **Don't push to main without explicit approval** for that specific
  scope. "Push to main" once doesn't mean "always push to main."

Tega's voice: direct, technical, blunt-but-friendly. They write specs
in numbered lists with sub-bullets and exact behaviors. When they're
locking a decision they say "Locked." When asking a question they
mark it `Q1`/`Q2`/`Q3`. When greenlighting they say "Approved on X."

---

## 10. Environment + dependencies

**Python:** 3.11 venv at `/home/user/jarvis-pwa/.venv/`. All backend
deps installed. `requirements.txt`:
```
fastapi==0.115.0
uvicorn[standard]==0.32.0
python-multipart==0.0.12
python-dotenv==1.0.1
httpx==0.27.2
openai==1.54.0
anthropic==0.39.0
```

Note: `playwright==1.49.0` is installed in the venv too (for testing,
not a production dep). Don't add it to `requirements.txt`.

**Node:** `/opt/node22/bin/node` available if you need it. Probably not.

**Chromium:** `/opt/pw-browsers/chromium-1194/chrome-linux/chrome`,
already symlinked into the playwright cache.

**The cloud sandbox vs the Mini:**
- Cloud sandbox = where you're working now. Has Anthropic key, doesn't
  have OpenAI/ElevenLabs keys, can't reach the OpenClaw gateway.
- Mac Mini = production. Has all keys + the gateway. Tega deploys
  manually from main via `DEPLOY.md` — you don't deploy.

---

## 11. Gotchas (things that bit me)

1. **SQLite `ALTER TABLE ADD COLUMN` doesn't support `IF NOT EXISTS`.**
   Use the `PRAGMA table_info(<table>)` → check column set → conditionally
   ALTER pattern. Already in `init_db()` for the `origin` and
   `has_audio`/`audio_url` migrations; reuse the pattern for any new
   migrations.

2. **FastAPI's `Depends(require_auth)` returns None, not the user.**
   That's fine — auth is single-user. Don't expect a user object.

3. **Cookie path matters.** Was `/chat` originally, widened to `/` in
   the unified-auth refactor. Any stale `/chat`-scoped cookies in a
   browser pre-update will linger until the new `/` cookie overwrites
   them on next login. Not a bug, just FYI.

4. **`base64`-encoded header values.** The old `/api/talk` used
   `X-User-Text` and `X-Jarvis-Text` base64 headers. That endpoint is
   gone. The new voice-message endpoint returns JSON, not headers —
   you can ignore the old `decodeHeader` JS helper (delete it in commit 3).

5. **Audio MIME types.** WebM (from browser MediaRecorder) is
   `audio/webm`. ElevenLabs returns `audio/mpeg` (mp3). The audio
   endpoint sniffs by extension and sets Content-Type accordingly.
   Mobile Safari is picky about MIME types for `<audio>` — webm playback
   on iOS is iffy. If user-audio-replay doesn't work on iPhone, it's
   probably a Safari webm limitation; the user transcript is what
   matters anyway, and assistant audio (mp3) plays everywhere.

6. **iOS `Audio.play()` autoplay restrictions.** Safari blocks
   `Audio.play()` not initiated by a user gesture. The voice-message
   response triggers playback after the user pressed-and-released the
   mic — that should count as a gesture. If it doesn't, wrap in a
   try/catch and silently swallow. The replay button (commit 5) always
   counts as a gesture so it'll always work.

7. **`getUserMedia` in PWAs on iOS.** Historically Safari hasn't supported
   `getUserMedia` in standalone "Add to Home Screen" mode. As of iOS 14.5
   it should work, but verify. If it doesn't, that's a real shipping
   blocker for the voice feature and Tega needs to know.

8. **Playwright stop-hook.** Some past sessions had a stop hook that
   yelled about uncommitted changes. Commit + push before letting the
   session end. The standard pattern: do work → run tests → commit →
   push → report results → stop.

9. **The `data/jarvis.db` on disk has historical test data.** It has
   ~10 chats from prior testing, some with origin='talk'. After commit 2
   drops the old tables, this is still a valid Chat-mode DB — those
   talk-origin chats with no messages will just show as empty rows. Fine.

10. **`.gitignore` already covers** `.env`, `*.db`, `data/*.db`,
    `data/audio_cache/`, `__pycache__/`, etc. Don't need to add anything.

11. **`/opt/pw-browsers/chromium-1194` is read-only.** Don't try to
    `playwright install chromium` — it'll fail trying to download from
    Google's CDN (network policy). Use the pre-installed binary via
    `executable_path`.

---

## 12. Suggested verification after each commit

After commit N, before reporting back:

1. `git diff --stat HEAD~1` to summarize what changed.
2. Smoke import: `cd backend && python -c "import server; print('OK')"`.
3. Boot the server, tail logs for clean startup + migration lines.
4. Curl the affected endpoints. Run the regression suite (auth gate
   matrix + a text-turn round-trip).
5. Playwright walkthrough for any frontend commit, mobile + desktop.
6. Send screenshots.
7. Push the branch.
8. Report: what changed, what was verified, any flags.

For commits 4/5 (voice), if real Whisper/TTS keys aren't available
locally, document that the full audio round-trip is Mini-only, and
verify everything *up to* the API call (UI states, request shape,
rendering of mocked responses).

---

## 13. If something's broken when you arrive

- `git status` should be clean. If it isn't, `git diff` to see what's
  uncommitted, decide whether to commit or stash.
- `git log --oneline -5` to confirm you're at `dfa39a5`.
- Server should boot. If it doesn't, check `.env` is present + populated.
- Database should have the new columns. Verify:
  ```bash
  sqlite3 data/jarvis.db "PRAGMA table_info(messages)" | grep -E "has_audio|audio_url"
  ```
- If migrations didn't run, just restart the server — `init_db()` is
  idempotent.

---

## 14. Quick command reference

```bash
# Repo state
git status
git log --oneline -10
git branch -vv

# Server
.venv/bin/python backend/server.py > /tmp/jarvis.log 2>&1 &
echo $! > /tmp/jarvis.pid
kill $(cat /tmp/jarvis.pid)

# Sqlite
sqlite3 data/jarvis.db "SELECT name FROM sqlite_master WHERE type='table'"
sqlite3 data/jarvis.db "PRAGMA table_info(messages)"
sqlite3 data/jarvis.db "SELECT chat_id, title, origin FROM chats LIMIT 10"

# Curl auth-flow
curl -s -X POST http://localhost:8765/chat/api/login \
  -H "Content-Type: application/json" \
  -d '{"password":"34811"}' -c /tmp/jar.jar
curl -s http://localhost:8765/chat/api/chats -b /tmp/jar.jar | python -m json.tool

# Playwright run (write a script to /tmp/script.py first)
.venv/bin/python /tmp/script.py

# Commit + push
git add <files>
git commit -m "Subject: …"  # use HEREDOC for multi-line bodies
git push origin claude/single-chat-architecture
```

---

## 15. Final checklist before you start commit 2

- [ ] On branch `claude/single-chat-architecture`
- [ ] HEAD is `dfa39a5`
- [ ] Working tree is clean
- [ ] Server boots without errors
- [ ] `messages` table has `has_audio` + `audio_url` columns
- [ ] You've read §6 (commits 2–6 detail)
- [ ] You've read §9 (Tega's style)
- [ ] You've read §11 (gotchas)
- [ ] You understand: don't merge to main, stop after each commit

When in doubt, ask Tega. They'd rather answer a clarifying question
than untangle a wrong assumption later.
