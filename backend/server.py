"""
Jarvis PWA Backend Server
─────────────────────────
FastAPI server that handles both modes:
  1. Talk to Jarvis  → /talk     (returns ElevenLabs voice response)
  2. Ambient Record  → /ambient  (fires archive + action pipelines)

Runs on Mac Mini, exposed via Cloudflare tunnel.
"""

import asyncio
import base64
import io
import json
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel

from action_extractor import extract_actions, archive_summarize
from chat_auth import (
    clear_session_cookie,
    is_authenticated,
    issue_session_cookie,
    verify_password,
)
from discord_poster import post_action_items_to_discord
from elevenlabs_tts import synthesize_voice
from jarvis_brain import answer_with_jarvis_context

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
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_JARVIS_CHANNEL_ID = os.getenv("DISCORD_JARVIS_CHANNEL_ID")
DISCORD_VOICE_MEMO_CHANNEL_ID = os.getenv(
    "DISCORD_VOICE_MEMO_CHANNEL_ID", DISCORD_JARVIS_CHANNEL_ID
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("jarvis-pwa")

# ──────────────────────────────────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_memos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     DATETIME NOT NULL,
    duration_seconds INTEGER,
    raw_transcript  TEXT NOT NULL,
    summary         TEXT,
    key_ideas       TEXT,            -- JSON array
    tags            TEXT,            -- JSON array
    mood            TEXT,
    audio_path      TEXT,
    processed       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS action_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memo_id         INTEGER,
    created_at      DATETIME NOT NULL,
    task            TEXT NOT NULL,
    priority        TEXT,
    project_tag     TEXT,
    discord_posted  INTEGER DEFAULT 0,
    FOREIGN KEY (memo_id) REFERENCES voice_memos(id)
);

CREATE TABLE IF NOT EXISTS talk_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    spoken_at       DATETIME NOT NULL,
    user_text       TEXT NOT NULL,
    jarvis_text     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memos_date ON voice_memos(recorded_at);
CREATE INDEX IF NOT EXISTS idx_actions_memo ON action_items(memo_id);

-- ── Chat mode (Mode 4) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chats (
    chat_id         TEXT PRIMARY KEY,
    title           TEXT,
    created_at      TEXT,
    last_message_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id  TEXT PRIMARY KEY,
    chat_id     TEXT,
    role        TEXT CHECK(role IN ('user','assistant')),
    content     TEXT,
    created_at  TEXT,
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
    with db() as conn:
        conn.executescript(SCHEMA)
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

def save_audio_cache(audio_bytes: bytes, prefix: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = AUDIO_CACHE_DIR / f"{prefix}_{ts}.webm"
    path.write_bytes(audio_bytes)
    return path

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
        "discord": bool(DISCORD_BOT_TOKEN and DISCORD_JARVIS_CHANNEL_ID),
        "time": datetime.utcnow().isoformat(),
    }

# ── MODE 1: TALK TO JARVIS ────────────────────────────────────────────────

@app.post("/api/talk")
async def talk(audio: UploadFile = File(...)):
    """
    Two-way conversation. Spencer speaks → Jarvis responds in voice.
    Returns: mp3 audio of Jarvis's reply (+ headers with text).
    """
    t0 = time.time()
    audio_bytes = await audio.read()
    log.info(f"[TALK] received {len(audio_bytes)} bytes")

    transcript = await transcribe_audio(audio_bytes, filename=audio.filename or "audio.webm")
    log.info(f"[TALK] heard: {transcript!r}")
    if not transcript:
        raise HTTPException(400, "Empty transcript")

    reply_text = await answer_with_jarvis_context(
        anthropic_client,
        user_message=transcript,
        recent_history=_recent_talk_history(),
    )
    log.info(f"[TALK] reply: {reply_text!r}")

    voice_bytes = await synthesize_voice(
        text=reply_text,
        api_key=ELEVENLABS_API_KEY,
        voice_id=ELEVENLABS_VOICE_ID,
        model=ELEVENLABS_MODEL,
    )

    with db() as conn:
        conn.execute(
            "INSERT INTO talk_history (spoken_at, user_text, jarvis_text) VALUES (?,?,?)",
            (datetime.utcnow().isoformat(), transcript, reply_text),
        )

    log.info(f"[TALK] round-trip {time.time()-t0:.2f}s")

    return Response(
        content=voice_bytes,
        media_type="audio/mpeg",
        headers={
            "X-User-Text": base64.b64encode(transcript.encode()).decode(),
            "X-Jarvis-Text": base64.b64encode(reply_text.encode()).decode(),
        },
    )

def _recent_talk_history(limit: int = 6) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT user_text, jarvis_text FROM talk_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    history = []
    for row in reversed(rows):
        history.append({"role": "user", "content": row["user_text"]})
        history.append({"role": "assistant", "content": row["jarvis_text"]})
    return history

# ── MODE 2: AMBIENT RECORDING ─────────────────────────────────────────────

@app.post("/api/ambient")
async def ambient(
    audio: UploadFile = File(...),
    duration_seconds: int = Form(0),
):
    """
    One-way recording. Spencer thinks out loud → pipelines fire async.
    Returns immediately with memo_id once transcript is captured.
    """
    t0 = time.time()
    audio_bytes = await audio.read()
    log.info(f"[AMBIENT] received {len(audio_bytes)} bytes, duration={duration_seconds}s")

    transcript = await transcribe_audio(audio_bytes, filename=audio.filename or "audio.webm")
    if not transcript:
        raise HTTPException(400, "Empty transcript")
    log.info(f"[AMBIENT] transcribed {len(transcript)} chars in {time.time()-t0:.2f}s")

    audio_path = save_audio_cache(audio_bytes, "ambient")

    # Insert memo shell so we have an ID to attach actions to
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO voice_memos
               (recorded_at, duration_seconds, raw_transcript, audio_path)
               VALUES (?, ?, ?, ?)""",
            (datetime.utcnow().isoformat(), duration_seconds, transcript, str(audio_path)),
        )
        memo_id = cur.lastrowid
    log.info(f"[AMBIENT] memo_id={memo_id}")

    # Fire both pipelines in parallel, don't block response
    asyncio.create_task(_process_ambient_pipelines(memo_id, transcript))

    return {
        "ok": True,
        "memo_id": memo_id,
        "transcript_preview": transcript[:200],
        "duration_seconds": duration_seconds,
    }

async def _process_ambient_pipelines(memo_id: int, transcript: str):
    """Run archive + action pipelines in parallel after response is sent."""
    try:
        log.info(f"[PIPELINE {memo_id}] starting parallel pipelines")
        archive_task = asyncio.create_task(archive_summarize(anthropic_client, transcript))
        action_task = asyncio.create_task(extract_actions(anthropic_client, transcript))

        archive_result, action_result = await asyncio.gather(
            archive_task, action_task, return_exceptions=True
        )

        # ── Archive branch ─────────────────────────────
        if isinstance(archive_result, Exception):
            log.exception(f"[PIPELINE {memo_id}] archive failed", exc_info=archive_result)
        else:
            with db() as conn:
                conn.execute(
                    """UPDATE voice_memos
                       SET summary=?, key_ideas=?, tags=?, mood=?, processed=1
                       WHERE id=?""",
                    (
                        archive_result.get("summary"),
                        json.dumps(archive_result.get("key_ideas", [])),
                        json.dumps(archive_result.get("tags", [])),
                        archive_result.get("mood"),
                        memo_id,
                    ),
                )
            log.info(f"[PIPELINE {memo_id}] archive saved")

        # ── Action branch ──────────────────────────────
        if isinstance(action_result, Exception):
            log.exception(f"[PIPELINE {memo_id}] actions failed", exc_info=action_result)
            return

        actions = action_result.get("actions", [])
        ideas = action_result.get("ideas", [])
        decisions = action_result.get("decisions", [])

        with db() as conn:
            for a in actions:
                conn.execute(
                    """INSERT INTO action_items
                       (memo_id, created_at, task, priority, project_tag)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        memo_id,
                        datetime.utcnow().isoformat(),
                        a.get("task"),
                        a.get("priority"),
                        a.get("project"),
                    ),
                )

        # Post structured digest to Discord for Jarvis to action
        if DISCORD_BOT_TOKEN and DISCORD_VOICE_MEMO_CHANNEL_ID:
            await post_action_items_to_discord(
                bot_token=DISCORD_BOT_TOKEN,
                channel_id=DISCORD_VOICE_MEMO_CHANNEL_ID,
                memo_id=memo_id,
                duration_seconds=_get_memo_duration(memo_id),
                actions=actions,
                ideas=ideas,
                decisions=decisions,
                summary=archive_result.get("summary") if isinstance(archive_result, dict) else None,
            )
            log.info(f"[PIPELINE {memo_id}] posted to Discord")

    except Exception:
        log.exception(f"[PIPELINE {memo_id}] unexpected failure")

def _get_memo_duration(memo_id: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT duration_seconds FROM voice_memos WHERE id=?", (memo_id,)
        ).fetchone()
    return row["duration_seconds"] if row else 0

# ── ARCHIVE ENDPOINTS (for Spencer to browse later) ───────────────────────

@app.get("/api/memos")
async def list_memos(limit: int = 50):
    with db() as conn:
        rows = conn.execute(
            """SELECT id, recorded_at, duration_seconds, summary, key_ideas, tags, mood
               FROM voice_memos
               ORDER BY recorded_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "recorded_at": r["recorded_at"],
            "duration_seconds": r["duration_seconds"],
            "summary": r["summary"],
            "key_ideas": json.loads(r["key_ideas"] or "[]"),
            "tags": json.loads(r["tags"] or "[]"),
            "mood": r["mood"],
        }
        for r in rows
    ]

@app.get("/api/memos/{memo_id}")
async def memo_detail(memo_id: int):
    with db() as conn:
        memo = conn.execute(
            "SELECT * FROM voice_memos WHERE id=?", (memo_id,)
        ).fetchone()
        if not memo:
            raise HTTPException(404, "Not found")
        actions = conn.execute(
            "SELECT task, priority, project_tag FROM action_items WHERE memo_id=?",
            (memo_id,),
        ).fetchall()
    return {
        "id": memo["id"],
        "recorded_at": memo["recorded_at"],
        "duration_seconds": memo["duration_seconds"],
        "raw_transcript": memo["raw_transcript"],
        "summary": memo["summary"],
        "key_ideas": json.loads(memo["key_ideas"] or "[]"),
        "tags": json.loads(memo["tags"] or "[]"),
        "mood": memo["mood"],
        "actions": [dict(a) for a in actions],
    }

# ──────────────────────────────────────────────────────────────────────────
# MODE 4: CHAT — AUTH ENDPOINTS
# ──────────────────────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────────────────────
# STATIC FRONTEND
# ──────────────────────────────────────────────────────────────────────────

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8765, reload=False)
