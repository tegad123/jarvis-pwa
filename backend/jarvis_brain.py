"""
Jarvis Brain — voice tone constants + direct-Anthropic fallback for Talk mode.

History: in v1 the /api/talk endpoint called `answer_with_jarvis_context`
here as the primary brain. As of Mode-4-v2 (Talk via gateway + persistence),
/api/talk routes through the OpenClaw gateway like Chat does, and this
module provides the two voice-specific pieces that don't belong in the
gateway client:

  - JARVIS_VOICE_SYSTEM: short-form (under-50-words) system prompt that
    /api/talk prepends to every gateway call. Spencer hears these
    replies; long answers are punishment.
  - answer_with_voice_brain(): direct Anthropic fallback for when the
    gateway is unreachable (relay-mode crash, network blip, etc.). Same
    prompt, no skill access — Spencer still hears a sensible reply.

Why a separate prompt from gateway_client.MOCK_SYSTEM_PROMPT:
  - Mock prompt is for typed Chat replies — paragraphs OK, can disclose
    "mock mode" verbosely.
  - Voice prompt is for spoken replies — under 50 words, no markdown,
    no bullets, action-confirming.
"""

import logging

from anthropic import AsyncAnthropic

log = logging.getLogger("jarvis-pwa.brain")

JARVIS_VOICE_MODEL = "claude-haiku-4-5"
JARVIS_VOICE_MAX_TOKENS = 200

JARVIS_VOICE_SYSTEM = (
    "You are Jarvis, Spencer's chief of staff. Respond in under 50 words. "
    "Speak naturally, like a verbal reply — short sentences, no bullet "
    "points, no markdown. If Spencer asks for an action (create task, "
    "send email, etc.), execute it via your tools AND confirm verbally "
    "what you did. Be direct, no filler — no 'great question,' no "
    "'happy to help.' If you don't know, say so plainly."
)


async def answer_with_voice_brain(
    client: AsyncAnthropic,
    history: list[dict],
) -> str:
    """
    Direct-Anthropic fallback for Talk mode when the gateway is down.

    No skill access — produces a short voice-ready reply from the
    conversation history. Used by /api/talk in its except branch.
    """
    if not client:
        return "I can't reach the brain right now. Try again in a moment."
    resp = await client.messages.create(
        model=JARVIS_VOICE_MODEL,
        max_tokens=JARVIS_VOICE_MAX_TOKENS,
        system=JARVIS_VOICE_SYSTEM,
        messages=history,
    )
    return resp.content[0].text.strip()
