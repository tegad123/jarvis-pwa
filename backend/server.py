"""
Jarvis PWA Backend Server
─────────────────────────
Single-product surface: Chat. Text and voice in one composer, persisted
to a single sqlite chat thread per conversation. The previous Talk /
Record / Memos endpoints and their tables are gone — Chat does it all.

Endpoints:
  /api/health                                     open status probe
  POST /chat/api/login                            single-password gate
  POST /chat/api/logout
  GET  /chat/api/auth-status
  GET  /chat/api/chats                            sidebar list (auth)
  POST /chat/api/chats                            mint new chat (auth)
  GET  /chat/api/chats/{id}/messages              chat history (auth)
  POST /chat/api/chats/{id}/message               text turn (auth)
  POST /chat/api/chats/{id}/voice-message         voice turn (auth)
  GET  /chat/api/audio/{filename}                 stream cached audio (auth)

Runs on Mac Mini, exposed via Cloudflare tunnel.
"""

import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel

from chat_auth import (
    clear_session_cookie,
    is_authenticated,
    issue_session_cookie,
    require_auth,
    verify_password,
)
from elevenlabs_tts import synthesize_voice_stream

# ──────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
load_dotenv(PROJECT_ROOT / ".env")
# Also try the existing OpenClaw secrets location
openclaw_secrets = Path.home() / ".openclaw" / "workspace" / "secrets" / "elevenlabs.env"
if openclaw_secrets.exists():
    load_dotenv(openclaw_secrets, override=False)

DB_PATH = os.getenv("JARVIS_DB_PATH", str(PROJECT_ROOT / "data" / "jarvis.db"))
FRONTEND_DIR = PROJECT_ROOT / "frontend"
AUDIO_CACHE_DIR = PROJECT_ROOT / "data" / "audio_cache"
AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "9IzcwKmvwJcw58h3KnlH")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
BRIDGE_SCRIPT = Path(
    os.getenv(
        "JARVIS_BRIDGE_SCRIPT",
        str(Path.home() / ".openclaw" / "workspace" / "skills" / "universal-bridge" / "handle_request.py"),
    )
)
BRIDGE_TIMEOUT_SECONDS = int(os.getenv("JARVIS_BRIDGE_TIMEOUT_SECONDS", "120"))
BRIDGE_SENDER = os.getenv("JARVIS_BRIDGE_SENDER", "spencerhuck@34dev.com")
BRIDGE_PYTHON = os.getenv("JARVIS_BRIDGE_PYTHON", "python3")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("jarvis-pwa")

NO_CACHE_STATIC_PATHS = {
    "/",
    "/index.html",
    "/styles.css",
    "/app.js",
    "/manifest.json",
    "/sw.js",
}


class JarvisStaticFiles(StaticFiles):
    """Static frontend with revalidation headers for deploy-sensitive assets."""

    def file_response(self, full_path, stat_result, scope, status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        if scope.get("path") in NO_CACHE_STATIC_PATHS:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

# ──────────────────────────────────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────────────────────────────────
#
# Three tables that matter post-refactor: chats, messages, session_map.
# Pre-refactor installs may still have voice_memos / action_items /
# talk_history on disk; init_db drops those legacy tables idempotently.

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id         TEXT PRIMARY KEY,
    title           TEXT,
    created_at      TEXT,
    last_message_at TEXT,
    origin          TEXT DEFAULT 'chat'
);

CREATE TABLE IF NOT EXISTS messages (
    message_id  TEXT PRIMARY KEY,
    chat_id     TEXT,
    role        TEXT CHECK(role IN ('user','assistant')),
    content     TEXT,
    created_at  TEXT,
    has_audio   INTEGER DEFAULT 0,
    audio_url   TEXT,
    FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
);

CREATE TABLE IF NOT EXISTS session_map (
    chat_id              TEXT PRIMARY KEY,
    openclaw_session_id  TEXT NOT NULL,
    created_at           TEXT,
    FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_chats_last_message_at ON chats(last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);
"""


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """
    Idempotent schema bootstrap + migration. Safe to run repeatedly.
    Adds columns to existing installs that predate them; new installs
    get the columns from the CREATE TABLE statements above.
    """
    with db() as conn:
        conn.executescript(SCHEMA)

        # chats.origin — added in the Talk-via-gateway change.
        chat_cols = {r["name"] for r in conn.execute("PRAGMA table_info(chats)").fetchall()}
        if "origin" not in chat_cols:
            conn.execute("ALTER TABLE chats ADD COLUMN origin TEXT DEFAULT 'chat'")
            log.info("Migration: added chats.origin column")

        # messages.has_audio / messages.audio_url — added in the single-Chat
        # voice-in-composer change.
        msg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "has_audio" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN has_audio INTEGER DEFAULT 0")
            log.info("Migration: added messages.has_audio column")
        if "audio_url" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN audio_url TEXT")
            log.info("Migration: added messages.audio_url column")

        for table in ("voice_memos", "action_items", "talk_history"):
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone():
                conn.execute(f"DROP TABLE {table}")
                log.info(f"Migration: dropped legacy table {table}")

    log.info(f"Database ready at {DB_PATH}")


# ──────────────────────────────────────────────────────────────────────────
# SHARED CLIENTS
# ──────────────────────────────────────────────────────────────────────────

openai_client: Optional[AsyncOpenAI] = None
anthropic_client: Optional[AsyncAnthropic] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global openai_client, anthropic_client
    init_db()
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY missing — Whisper transcription will fail")
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY missing — Claude calls will fail")
    if not ELEVENLABS_API_KEY:
        log.warning("ELEVENLABS_API_KEY missing — voice synthesis will fail")

    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
    log.info("Jarvis PWA backend ready.")
    yield
    log.info("Shutting down.")


app = FastAPI(lifespan=lifespan, title="Jarvis PWA Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────────────────────────────────
# CORE HELPERS
# ──────────────────────────────────────────────────────────────────────────

# Audio filenames are <uuid4hex>.<webm|mp3>. Strict regex so path
# components / ".." can't slip through GET /chat/api/audio/{filename}.
_AUDIO_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.(webm|mp3)$")
_TTS_STREAM_TASKS: set[asyncio.Task] = set()
TTS_FIRST_CHUNK_TIMEOUT_SECONDS = 15.0
TTS_STREAM_POLL_SECONDS = 0.1
TTS_STREAM_IDLE_TIMEOUT_SECONDS = 90.0


async def transcribe_audio(audio_bytes: bytes, filename: str = "audio.webm") -> str:
    """Run audio through OpenAI Whisper, return text."""
    if not openai_client:
        raise HTTPException(500, "OpenAI client not configured")
    file_obj = io.BytesIO(audio_bytes)
    file_obj.name = filename
    transcript = await openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=file_obj,
        response_format="text",
    )
    return str(transcript).strip()


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _make_title(text: str, limit: int = 40) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _get_or_create_openclaw_session_id(conn: sqlite3.Connection, chat_id: str) -> str:
    session_row = conn.execute(
        "SELECT openclaw_session_id FROM session_map WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    if session_row and session_row["openclaw_session_id"]:
        return session_row["openclaw_session_id"]

    openclaw_session_id = f"pwa:chat:{chat_id}"
    conn.execute(
        "INSERT OR REPLACE INTO session_map (chat_id, openclaw_session_id, created_at) "
        "VALUES (?, ?, ?)",
        (chat_id, openclaw_session_id, _now_iso()),
    )
    return openclaw_session_id


def _call_universal_bridge(
    *,
    chat_id: str,
    user_content: str,
    channel: str,
    openclaw_session_id: str,
    sender: str,
    calendar_id: Optional[str] = None,
) -> dict[str, Any]:
    if not BRIDGE_SCRIPT.is_file():
        raise RuntimeError(f"bridge script not found: {BRIDGE_SCRIPT}")

    context = {
        "session_id": openclaw_session_id,
        "chat_id": chat_id,
        "sender": sender,
    }
    if calendar_id:
        context["calendar_id"] = calendar_id
    cmd = [
        BRIDGE_PYTHON,
        str(BRIDGE_SCRIPT),
        "--message",
        user_content,
        "--channel",
        channel,
        "--session-id",
        openclaw_session_id,
        "--context-json",
        json.dumps(context),
    ]
    log.info(
        f"[bridge-invoke] chat={chat_id} channel={channel} "
        f"session={openclaw_session_id}"
    )
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=BRIDGE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"bridge exited {result.returncode}; stderr={result.stderr[-1000:]!r}; "
            f"stdout={result.stdout[-1000:]!r}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bridge returned malformed JSON: {result.stdout[-1000:]!r}") from exc

    errors = payload.get("errors") or []
    response_text = (payload.get("response_text") or "").strip()
    if errors or not response_text:
        raise RuntimeError(f"bridge errors={errors!r}; response_empty={not response_text}")
    payload["response_text"] = response_text
    if not isinstance(payload.get("actions_taken"), list):
        payload["actions_taken"] = []
    return payload


def _audit_bridge_context(request: Request) -> tuple[str, Optional[str]]:
    sender = (request.headers.get("x-pwa-audit-sender") or "").strip().lower()
    calendar_id = (request.headers.get("x-pwa-audit-calendar-id") or "").strip().lower()
    if sender == "tega@34dev.com":
        return sender, calendar_id or sender
    return BRIDGE_SENDER, None


async def _exchange_turn(
    chat_id: str,
    user_content: str,
    channel: str,
    sender: str = BRIDGE_SENDER,
    calendar_id: Optional[str] = None,
) -> tuple[str, str, str, str, list[dict[str, Any]]]:
    """
    Append one user → assistant turn to an existing chat.

    Steps: insert user msg, auto-title chat if title is null, invoke the
    universal bridge with this chat's OpenClaw session id, insert assistant
    msg, bump last_message_at.

    Returns (user_msg_id, assistant_msg_id, assistant_text,
    assistant_created_at, actions_taken). The voice-message endpoint uses
    user_msg_id + assistant_msg_id to UPDATE both rows with audio metadata
    after the gateway round-trip.

    Raises 404 if chat_id does not exist. Bridge failures are logged with
    [bridge-error] and return a graceful user-facing response.
    """
    with db() as conn:
        chat_row = conn.execute(
            "SELECT title FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if not chat_row:
            raise HTTPException(404, "chat not found")

        user_msg_id = uuid.uuid4().hex
        user_now = _now_iso()
        conn.execute(
            "INSERT INTO messages (message_id, chat_id, role, content, created_at) "
            "VALUES (?, ?, 'user', ?, ?)",
            (user_msg_id, chat_id, user_content, user_now),
        )
        if chat_row["title"] is None:
            conn.execute(
                "UPDATE chats SET title = ?, last_message_at = ? WHERE chat_id = ?",
                (_make_title(user_content), user_now, chat_id),
            )
        else:
            conn.execute(
                "UPDATE chats SET last_message_at = ? WHERE chat_id = ?",
                (user_now, chat_id),
            )

        openclaw_session_id = _get_or_create_openclaw_session_id(conn, chat_id)

    try:
        bridge_payload = await asyncio.to_thread(
            _call_universal_bridge,
            chat_id=chat_id,
            user_content=user_content,
            channel=channel,
            openclaw_session_id=openclaw_session_id,
            sender=sender,
            calendar_id=calendar_id,
        )
        reply_text = bridge_payload["response_text"]
        actions_taken = bridge_payload.get("actions_taken") or []
    except subprocess.TimeoutExpired:
        log.exception(
            f"[bridge-error] bridge timed out for chat={chat_id} "
            f"channel={channel} session={openclaw_session_id}"
        )
        reply_text = "Sorry, I had trouble with that. Could you try again?"
        actions_taken = []
    except Exception:
        log.exception(
            f"[bridge-error] bridge failed for chat={chat_id} "
            f"channel={channel} session={openclaw_session_id}"
        )
        reply_text = "Sorry, I had trouble with that. Could you try again?"
        actions_taken = []

    assistant_msg_id = uuid.uuid4().hex
    assistant_now = _now_iso()
    with db() as conn:
        conn.execute(
            "INSERT INTO messages (message_id, chat_id, role, content, created_at) "
            "VALUES (?, ?, 'assistant', ?, ?)",
            (assistant_msg_id, chat_id, reply_text, assistant_now),
        )
        conn.execute(
            "UPDATE chats SET last_message_at = ? WHERE chat_id = ?",
            (assistant_now, chat_id),
        )

    return user_msg_id, assistant_msg_id, reply_text, assistant_now, actions_taken


def _set_message_audio(message_id: str, audio_url: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE messages SET has_audio = 1, audio_url = ? WHERE message_id = ?",
            (audio_url, message_id),
        )


def _done_marker(path: Path) -> Path:
    return path.with_name(path.name + ".done")


def _error_marker(path: Path) -> Path:
    return path.with_name(path.name + ".error")


async def _write_streaming_voice_file(
    *,
    path: Path,
    chat_id: str,
    text: str,
    first_chunk_ready: asyncio.Event,
    error_holder: dict[str, Exception],
) -> None:
    done_path = _done_marker(path)
    error_path = _error_marker(path)
    for marker in (done_path, error_path):
        try:
            marker.unlink()
        except FileNotFoundError:
            pass

    bytes_written = 0
    try:
        with path.open("wb") as out:
            async for chunk in synthesize_voice_stream(
                text=text,
                api_key=ELEVENLABS_API_KEY,
                voice_id=ELEVENLABS_VOICE_ID,
                model=ELEVENLABS_MODEL,
            ):
                out.write(chunk)
                out.flush()
                bytes_written += len(chunk)
                if not first_chunk_ready.is_set():
                    log.info(
                        f"[VOICE {chat_id}] TTS first streaming chunk "
                        f"{len(chunk)} bytes -> {path.name}"
                    )
                    first_chunk_ready.set()
        done_path.touch()
        log.info(f"[VOICE {chat_id}] TTS stream complete {bytes_written} bytes -> {path.name}")
    except Exception as exc:
        error_holder["exception"] = exc
        try:
            error_path.write_text(repr(exc))
        except Exception:
            pass
        log.exception(f"[VOICE {chat_id}] streaming TTS failed after {bytes_written} bytes")
    finally:
        if not first_chunk_ready.is_set():
            first_chunk_ready.set()


def _track_tts_task(task: asyncio.Task) -> None:
    _TTS_STREAM_TASKS.add(task)
    task.add_done_callback(_TTS_STREAM_TASKS.discard)


async def _iter_growing_audio_file(path: Path) -> AsyncIterator[bytes]:
    pos = 0
    last_progress = time.monotonic()
    done_path = _done_marker(path)
    error_path = _error_marker(path)

    while True:
        if path.is_file():
            size = path.stat().st_size
            if size > pos:
                with path.open("rb") as f:
                    f.seek(pos)
                    data = f.read(size - pos)
                pos = size
                last_progress = time.monotonic()
                yield data
                continue

        if done_path.exists() or error_path.exists():
            break
        if time.monotonic() - last_progress > TTS_STREAM_IDLE_TIMEOUT_SECONDS:
            log.warning(f"streaming audio timed out while waiting for {path.name}")
            break
        await asyncio.sleep(TTS_STREAM_POLL_SECONDS)


# ──────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ──────────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "openai": bool(openai_client),
        "anthropic": bool(anthropic_client),
        "elevenlabs": bool(ELEVENLABS_API_KEY),
        "time": datetime.utcnow().isoformat(),
    }


# ── AUTH ──────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


@app.post("/chat/api/login")
async def chat_login(req: LoginRequest, response: Response):
    if not verify_password(req.password):
        raise HTTPException(401, "invalid password")
    issue_session_cookie(response)
    return {"ok": True}


@app.post("/chat/api/logout")
async def chat_logout(response: Response):
    clear_session_cookie(response)
    return {"ok": True}


@app.get("/chat/api/auth-status")
async def chat_auth_status(request: Request):
    return {"authenticated": is_authenticated(request)}


# ── CHAT CRUD ─────────────────────────────────────────────────────────────

class MessageRequest(BaseModel):
    content: str


@app.get("/chat/api/chats", dependencies=[Depends(require_auth)])
async def chat_list_chats():
    """
    Returns the sidebar listing. `has_audio` is an aggregate — true if
    any message in this chat has audio (so the frontend can flag mixed
    conversations with the 🎙️ icon, in addition to origin='talk' chats
    that came from the deprecated Talk surface).
    """
    with db() as conn:
        rows = conn.execute(
            """SELECT c.chat_id, c.title, c.created_at, c.last_message_at,
                      c.origin,
                      EXISTS (
                          SELECT 1 FROM messages m
                          WHERE m.chat_id = c.chat_id AND m.has_audio = 1
                      ) AS has_audio
               FROM chats c
               ORDER BY COALESCE(c.last_message_at, c.created_at) DESC"""
        ).fetchall()
    return [
        {**dict(r), "has_audio": bool(r["has_audio"])}
        for r in rows
    ]


@app.post("/chat/api/chats", dependencies=[Depends(require_auth)])
async def chat_create_chat():
    chat_id = uuid.uuid4().hex
    now = _now_iso()
    openclaw_session_id = f"pwa:chat:{chat_id}"
    with db() as conn:
        conn.execute(
            "INSERT INTO chats (chat_id, title, created_at, last_message_at) "
            "VALUES (?, NULL, ?, NULL)",
            (chat_id, now),
        )
        conn.execute(
            "INSERT INTO session_map (chat_id, openclaw_session_id, created_at) "
            "VALUES (?, ?, ?)",
            (chat_id, openclaw_session_id, now),
        )
    return {"chat_id": chat_id, "title": None}


@app.get("/chat/api/chats/{chat_id}/messages", dependencies=[Depends(require_auth)])
async def chat_get_messages(chat_id: str):
    with db() as conn:
        chat = conn.execute(
            "SELECT 1 FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if not chat:
            raise HTTPException(404, "chat not found")
        rows = conn.execute(
            """SELECT message_id, role, content, created_at, has_audio, audio_url
               FROM messages
               WHERE chat_id = ?
               ORDER BY created_at ASC, message_id ASC""",
            (chat_id,),
        ).fetchall()
    return [
        {**dict(r), "has_audio": bool(r["has_audio"])}
        for r in rows
    ]


@app.post("/chat/api/chats/{chat_id}/message", dependencies=[Depends(require_auth)])
async def chat_send_message(chat_id: str, req: MessageRequest, request: Request):
    """Text turn. No audio attachments produced."""
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(400, "content required")

    sender, calendar_id = _audit_bridge_context(request)
    _, assistant_msg_id, reply_text, assistant_now, actions_taken = await _exchange_turn(
        chat_id=chat_id,
        user_content=content,
        channel="pwa-chat",
        sender=sender,
        calendar_id=calendar_id,
    )
    return {
        "message_id": assistant_msg_id,
        "chat_id": chat_id,
        "role": "assistant",
        "content": reply_text,
        "created_at": assistant_now,
        "has_audio": False,
        "audio_url": None,
        "actions_taken": actions_taken,
    }


@app.post("/chat/api/chats/{chat_id}/voice-message", dependencies=[Depends(require_auth)])
async def chat_voice_message(
    request: Request,
    chat_id: str,
    audio: UploadFile = File(...),
):
    """
    Voice turn. User holds the composer mic, releases, and audio lands
    here. We Whisper-transcribe, persist the user turn (with the raw
    audio file URL), run the gateway with the short-form voice prompt,
    persist the assistant turn, ElevenLabs-synthesize the reply, save
    that file, attach its URL to the assistant message, and return both
    message rows.

    Frontend renders user + assistant bubbles (each with a 🎙️ + replay
    button) and auto-plays the assistant audio.
    """
    audio_bytes = await audio.read()
    log.info(f"[VOICE {chat_id}] received {len(audio_bytes)} bytes")

    # Verify the chat exists before doing any expensive work.
    with db() as conn:
        if not conn.execute(
            "SELECT 1 FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone():
            raise HTTPException(404, "chat not found")

    transcript = await transcribe_audio(
        audio_bytes, filename=audio.filename or "voice.webm"
    )
    log.info(f"[VOICE {chat_id}] heard: {transcript!r}")
    if not transcript:
        # Whisper returned empty — likely sub-second / silent audio. The
        # frontend already silently discards <1s clips, so a 400 here is
        # a defensive fallback rather than a user-visible error.
        raise HTTPException(400, "Empty transcript")

    # Save the user's raw audio so the bubble can replay it later.
    user_audio_name = f"{uuid.uuid4().hex}.webm"
    (AUDIO_CACHE_DIR / user_audio_name).write_bytes(audio_bytes)
    user_audio_url = f"/chat/api/audio/{user_audio_name}"

    # Persist user → universal bridge → assistant.
    sender, calendar_id = _audit_bridge_context(request)
    user_msg_id, assistant_msg_id, reply_text, assistant_now, actions_taken = await _exchange_turn(
        chat_id=chat_id,
        user_content=transcript,
        channel="pwa-voice",
        sender=sender,
        calendar_id=calendar_id,
    )
    _set_message_audio(user_msg_id, user_audio_url)

    # ElevenLabs streaming TTS — return the stream URL as soon as the first
    # MP3 bytes are on disk so the browser can buffer while generation
    # continues. If TTS fails before the first chunk, the assistant message
    # still exists without playback.
    assistant_audio_url: Optional[str] = None
    assistant_audio_name = f"{uuid.uuid4().hex}.mp3"
    assistant_audio_path = AUDIO_CACHE_DIR / assistant_audio_name
    first_chunk_ready = asyncio.Event()
    tts_error: dict[str, Exception] = {}
    tts_task = asyncio.create_task(
        _write_streaming_voice_file(
            path=assistant_audio_path,
            chat_id=chat_id,
            text=reply_text,
            first_chunk_ready=first_chunk_ready,
            error_holder=tts_error,
        )
    )
    _track_tts_task(tts_task)
    try:
        await asyncio.wait_for(
            first_chunk_ready.wait(),
            timeout=TTS_FIRST_CHUNK_TIMEOUT_SECONDS,
        )
        if tts_error:
            raise tts_error["exception"]
        if assistant_audio_path.is_file() and assistant_audio_path.stat().st_size > 0:
            assistant_audio_url = f"/chat/api/audio-stream/{assistant_audio_name}"
            _set_message_audio(assistant_msg_id, assistant_audio_url)
    except asyncio.TimeoutError:
        tts_task.cancel()
        log.exception(f"[VOICE {chat_id}] TTS first chunk timed out; assistant text only")
    except Exception:
        log.exception(f"[VOICE {chat_id}] TTS failed before first chunk; assistant text only")

    return {
        "user_message": {
            "message_id": user_msg_id,
            "chat_id": chat_id,
            "role": "user",
            "content": transcript,
            "has_audio": True,
            "audio_url": user_audio_url,
        },
        "assistant_message": {
            "message_id": assistant_msg_id,
            "chat_id": chat_id,
            "role": "assistant",
            "content": reply_text,
            "created_at": assistant_now,
            "has_audio": bool(assistant_audio_url),
            "audio_url": assistant_audio_url,
            "actions_taken": actions_taken,
        },
    }


@app.get("/chat/api/audio/{filename}", dependencies=[Depends(require_auth)])
async def chat_audio(filename: str):
    """
    Stream a cached audio file. Filename must match the strict
    <uuid4hex>.<webm|mp3> shape so a malicious caller can't traverse
    out of AUDIO_CACHE_DIR via path components or `..`.
    """
    if not _AUDIO_FILENAME_RE.match(filename):
        raise HTTPException(400, "invalid filename")
    path = AUDIO_CACHE_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "audio not found")
    media_type = "audio/webm" if filename.endswith(".webm") else "audio/mpeg"
    return FileResponse(str(path), media_type=media_type)


@app.get("/chat/api/audio-stream/{filename}", dependencies=[Depends(require_auth)])
async def chat_audio_stream(filename: str):
    """
    Stream a cached MP3 while the ElevenLabs writer is still appending to it.
    Once the sidecar .done file exists, this behaves like a normal finite
    chunked MP3 response.
    """
    if not _AUDIO_FILENAME_RE.match(filename) or not filename.endswith(".mp3"):
        raise HTTPException(400, "invalid filename")
    path = AUDIO_CACHE_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "audio not found")
    return StreamingResponse(
        _iter_growing_audio_file(path),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ──────────────────────────────────────────────────────────────────────────
# STATIC FRONTEND
# ──────────────────────────────────────────────────────────────────────────

if FRONTEND_DIR.exists():
    app.mount("/", JarvisStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8765, reload=False)
