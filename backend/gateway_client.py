"""
Gateway client for Chat mode.

Two modes, selected via JARVIS_GATEWAY_MODE:
  - mock   (default): call the Anthropic SDK directly with a stub system
                      prompt. Used for local development without the
                      full Jarvis stack running.
  - relay            : POST OpenAI-compatible /v1/chat/completions to the
                      Jarvis gateway. The gateway owns Spencer's skills
                      (add_task, query_tasks, send_email, etc.).

The PWA stays a thin client: it never sees the tool calls, just the
final assistant text.
"""

import logging
import os
from typing import Optional

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

log = logging.getLogger("jarvis-pwa.gateway")

MOCK_SYSTEM_PROMPT = (
    "You are Jarvis, Spencer's chief of staff. Direct, no filler. Respond "
    "as if you have access to Spencer's task and email systems (you'd "
    "reference them naturally), but explain you're in mock mode if asked "
    "about specific data."
)

MOCK_MODEL = "claude-sonnet-4-6"
RELAY_MODEL = "openclaw:main"
MAX_TOKENS = 1024


def current_mode() -> str:
    return (os.getenv("JARVIS_GATEWAY_MODE") or "mock").strip().lower()


async def complete(
    messages: list[dict],
    anthropic_client: Optional[AsyncAnthropic] = None,
) -> str:
    """
    Send a conversation to whichever backend is configured.

    `messages` is a list of {role: 'user'|'assistant', content: str}.
    Returns the assistant's reply text.

    Note: session_id passing to the relay gateway is intentionally
    omitted in v1 — the gateway will spin up a fresh session per call.
    The session_map table is populated for future use, but not wired
    through to the relay request yet.
    """
    mode = current_mode()
    if mode == "mock":
        return await _mock_complete(messages, anthropic_client)
    if mode == "relay":
        return await _relay_complete(messages)
    raise RuntimeError(f"Unknown JARVIS_GATEWAY_MODE: {mode!r}")


async def _mock_complete(
    messages: list[dict],
    anthropic_client: Optional[AsyncAnthropic],
) -> str:
    client = anthropic_client
    if client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return (
                "Mock mode is configured but ANTHROPIC_API_KEY is missing. "
                "Set it in .env and restart the server."
            )
        client = AsyncAnthropic(api_key=api_key)

    log.info(f"[gateway:mock] {len(messages)} msgs -> {MOCK_MODEL}")
    resp = await client.messages.create(
        model=MOCK_MODEL,
        max_tokens=MAX_TOKENS,
        system=MOCK_SYSTEM_PROMPT,
        messages=messages,
    )
    return resp.content[0].text.strip()


async def _relay_complete(messages: list[dict]) -> str:
    base_url = os.getenv("JARVIS_GATEWAY_URL", "http://127.0.0.1:18789").rstrip("/")
    token = os.getenv("JARVIS_GATEWAY_TOKEN", "")
    if not token:
        return (
            "Relay mode is configured but JARVIS_GATEWAY_TOKEN is missing. "
            "Set it in .env and restart the server."
        )

    client = AsyncOpenAI(base_url=f"{base_url}/v1", api_key=token)
    log.info(f"[gateway:relay] {len(messages)} msgs -> {RELAY_MODEL} @ {base_url}")
    resp = await client.chat.completions.create(
        model=RELAY_MODEL,
        messages=messages,
        max_tokens=MAX_TOKENS,
    )
    return (resp.choices[0].message.content or "").strip()
