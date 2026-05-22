"""
ElevenLabs Text-to-Speech wrapper.
"""

import logging
from typing import AsyncIterator

import httpx

log = logging.getLogger("jarvis-pwa.tts")


async def synthesize_voice(
    text: str,
    api_key: str,
    voice_id: str = "9IzcwKmvwJcw58h3KnlH",
    model: str = "eleven_turbo_v2_5",
) -> bytes:
    """Convert text to mp3 audio bytes via ElevenLabs."""
    if not api_key:
        raise RuntimeError("Missing ELEVENLABS_API_KEY")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": 0.45,
            "similarity_boost": 0.8,
            "style": 0.15,
            "use_speaker_boost": True,
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            log.error(f"ElevenLabs error {r.status_code}: {r.text[:400]}")
            r.raise_for_status()
        return r.content


async def synthesize_voice_stream(
    text: str,
    api_key: str,
    voice_id: str = "9IzcwKmvwJcw58h3KnlH",
    model: str = "eleven_turbo_v2_5",
) -> AsyncIterator[bytes]:
    """Stream mp3 audio chunks from ElevenLabs as they are generated."""
    if not api_key:
        raise RuntimeError("Missing ELEVENLABS_API_KEY")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": 0.45,
            "similarity_boost": 0.8,
            "style": 0.15,
            "use_speaker_boost": True,
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode("utf-8", errors="replace")
                log.error(f"ElevenLabs streaming error {r.status_code}: {body[:400]}")
                r.raise_for_status()
            async for chunk in r.aiter_bytes():
                if chunk:
                    yield chunk
