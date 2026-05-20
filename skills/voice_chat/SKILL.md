---
name: voice_chat_pwa
description: Reference documentation for the Jarvis PWA two-way voice chat endpoint. This is not a skill you invoke directly — it documents how the PWA's /api/talk endpoint integrates with the broader Jarvis system, so other skills understand the architecture.
---

# Voice Chat (PWA)

The Jarvis PWA runs an independent FastAPI server on the Mac Mini at
port 8765, exposed via the same Cloudflare tunnel as the rest of the
Jarvis infrastructure.

## Architecture

```
Spencer's phone (PWA)
    ↓  hold-to-talk
    ↓  records audio
    ↓
POST /api/talk  (multipart audio)
    ↓
[FastAPI] → OpenAI Whisper
    ↓ transcript
[FastAPI] → Claude Sonnet 4.6
            (system prompt = Jarvis identity, business context,
             last 6 turns of talk history)
    ↓ reply text
[FastAPI] → ElevenLabs (voice 9IzcwKmvwJcw58h3KnlH)
    ↓ mp3 bytes
PWA plays audio
```

## Important boundaries

- This endpoint does NOT have access to Jarvis's full Discord-based
  skill set. It is a fast voice-only Q&A surface.
- For real action execution, Spencer uses the PWA's Ambient
  Recording mode (Mode 2), which posts structured task digests to
  Discord where the full Jarvis instance processes them via the
  `process_voice_memo_actions` skill.
- Talk mode is for: quick questions, status checks, brainstorming,
  acknowledging tasks. NOT for: sending emails, modifying pipelines,
  touching the deal database directly.

## When this skill should be referenced

You shouldn't invoke this — it's a reference doc so you understand
the system. If Spencer asks "why doesn't Jarvis-on-phone do X?", the
answer is: "Talk mode is fast voice Q&A. For X, use ambient recording
or Discord directly."
