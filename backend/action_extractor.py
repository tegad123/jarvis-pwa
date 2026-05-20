"""
Action Extractor
────────────────
Two Claude calls run in parallel on each transcript:

  1. extract_actions()    → tasks, ideas, decisions, follow-ups
                            written with INTUITION not literalism
  2. archive_summarize()  → 3-sentence summary + key ideas + tags + mood
                            for long-term storage and future search

The extraction prompt is intentionally aggressive about inferring
downstream implications. Spencer asked for "intuition, not systems".
"""

import json
import logging
from typing import Optional

from anthropic import AsyncAnthropic

log = logging.getLogger("jarvis-pwa.extractor")

EXTRACTION_MODEL = "claude-haiku-4-5-20251001"

# ──────────────────────────────────────────────────────────────────────────
# ACTION EXTRACTION  ("the chief of staff prompt")
# ──────────────────────────────────────────────────────────────────────────

ACTION_SYSTEM_PROMPT = """You are Spencer Huck's chief of staff.

Spencer is a Houston-based real estate developer (CQ Houston / Thirty Four Ventures LLC).
He buys distressed lots, splits them, and builds new construction. He moves fast.

He has just finished thinking out loud into a voice memo. You will read the transcript
and extract everything he needs handled. You will read it the way a sharp chief of staff
would — not a literal transcriptionist.

CORE RULES:

1. INFER, don't just match keywords.
   If Spencer says "the Heights market is heating up," that's not a fact — it's an
   instruction to track competitor activity, pull recent comps, or accelerate offers
   in that submarket. Create the implied task.

2. Capture commitments he made to himself, not just to others.
   "I really need to fix our follow-up process" is a task. He didn't say "create
   a task" — he doesn't have to.

3. Separate three categories:
     - actions:    things to DO  (verb-first, specific, time-bound when possible)
     - ideas:      things to EXPLORE  (worth developing, not yet a task)
     - decisions:  things he DECIDED  (log it so it's not relitigated)

4. Don't invent things he didn't actually imply. If you're not sure, leave it out.
   It's better to miss one task than to create five fake ones.

5. Be specific. "Follow up with Martin" is bad. "Follow up with Martin on the
   Heights Deal Machine export — confirm Tuesday delivery" is good.

6. Priority calibration:
     - high:   time-sensitive, money on the line, a person is waiting on Spencer
     - med:    important but not blocking
     - low:    background, exploratory, or housekeeping

7. Project tagging — use Spencer's existing projects when relevant:
     - heights, shady-acres, lot-split, cold-calling,
       jarvis, har-pipeline, zapier, website, deal-{address}

Return STRICT JSON only. No prose. No markdown. No explanation outside the JSON.

Schema:
{
  "actions": [
    {"task": "...", "priority": "high|med|low", "project": "tag-or-null", "rationale": "one sentence — why you extracted this"}
  ],
  "ideas": [
    {"idea": "...", "rationale": "one sentence — why it's worth exploring"}
  ],
  "decisions": [
    {"decision": "...", "context": "one sentence"}
  ]
}
"""

async def extract_actions(client: AsyncAnthropic, transcript: str) -> dict:
    """Pull tasks/ideas/decisions from a voice memo transcript."""
    if not client:
        log.warning("No Anthropic client — returning empty actions")
        return {"actions": [], "ideas": [], "decisions": []}

    resp = await client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=2000,
        system=ACTION_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"<transcript>\n{transcript}\n</transcript>",
            }
        ],
    )
    raw = resp.content[0].text.strip()
    raw = _strip_code_fences(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.exception(f"Failed to parse action JSON: {raw[:400]}")
        return {"actions": [], "ideas": [], "decisions": []}

    # Normalize
    return {
        "actions": data.get("actions", []) or [],
        "ideas": data.get("ideas", []) or [],
        "decisions": data.get("decisions", []) or [],
    }

# ──────────────────────────────────────────────────────────────────────────
# ARCHIVE SUMMARIZATION  ("the memory prompt")
# ──────────────────────────────────────────────────────────────────────────

ARCHIVE_SYSTEM_PROMPT = """You are the archivist for Spencer Huck's personal voice memo system.

Your job is NOT to act on what Spencer said. Your job is to make it searchable and
meaningful years from now. Spencer plans to look back on these recordings as a
record of his thinking, the way someone might keep a journal — or eventually edit
into a documentary.

You will produce four things from the transcript:

1. summary       — 2 or 3 sentences. Plain prose. What was Spencer actually
                   thinking about? What's the through-line of the recording?

2. key_ideas     — up to 5 short bullets capturing the most important thoughts.
                   Phrase them as Spencer's own statements, not your descriptions.

3. tags          — 3 to 8 short tags for search. Use kebab-case. Examples:
                   heights, lot-split, jarvis-build, frustration, vision,
                   competitor-watch, capital-strategy, family.

4. mood          — one of: focused, brainstorming, frustrated, confident,
                   uncertain, reflective, energized, tired. Just pick the closest.

Return STRICT JSON. No prose outside the JSON.

Schema:
{
  "summary": "...",
  "key_ideas": ["...", "..."],
  "tags": ["...", "..."],
  "mood": "..."
}
"""

async def archive_summarize(client: AsyncAnthropic, transcript: str) -> dict:
    """Produce the long-term archive record."""
    if not client:
        return {"summary": None, "key_ideas": [], "tags": [], "mood": None}

    resp = await client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=1000,
        system=ARCHIVE_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"<transcript>\n{transcript}\n</transcript>",
            }
        ],
    )
    raw = _strip_code_fences(resp.content[0].text.strip())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.exception(f"Failed to parse archive JSON: {raw[:400]}")
        return {"summary": None, "key_ideas": [], "tags": [], "mood": None}

    return {
        "summary": data.get("summary"),
        "key_ideas": data.get("key_ideas", []) or [],
        "tags": data.get("tags", []) or [],
        "mood": data.get("mood"),
    }

# ──────────────────────────────────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────────────────────────────────

def _strip_code_fences(text: str) -> str:
    """Strip ```json ... ``` if Claude wrapped the response."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        # Drop the opening fence line and trailing fence
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t
