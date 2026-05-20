"""
Discord Poster
──────────────
Posts structured voice-memo digests to a Discord channel where the existing
OpenClaw Jarvis instance is listening. Jarvis then handles the action items
through its existing skill set (calendar, email, deal tracking, etc.).

Message format is intentionally clean so Jarvis can parse it easily.
"""

import logging
from typing import Optional

import httpx

log = logging.getLogger("jarvis-pwa.discord")


def _format_duration(seconds: int) -> str:
    if not seconds:
        return ""
    m, s = divmod(seconds, 60)
    return f"{m}m {s:02d}s"


def _format_message(
    memo_id: int,
    duration_seconds: int,
    actions: list,
    ideas: list,
    decisions: list,
    summary: Optional[str],
) -> str:
    """Build the Discord message text."""
    parts = [f"🎙️ **Voice Memo #{memo_id}**"]
    if duration_seconds:
        parts[0] += f" · {_format_duration(duration_seconds)}"

    if summary:
        parts.append(f"\n_{summary}_")

    if actions:
        parts.append("\n📋 **TASKS**")
        for a in actions:
            prio = a.get("priority", "").upper()
            prio_str = f" `{prio}`" if prio else ""
            proj = a.get("project")
            proj_str = f" — *{proj}*" if proj else ""
            parts.append(f"→ {a.get('task')}{prio_str}{proj_str}")

    if ideas:
        parts.append("\n💡 **IDEAS FLAGGED**")
        for i in ideas:
            parts.append(f"→ {i.get('idea')}")

    if decisions:
        parts.append("\n📌 **DECISIONS LOGGED**")
        for d in decisions:
            parts.append(f"→ {d.get('decision')}")

    if not (actions or ideas or decisions):
        parts.append("\n_(No actionable items detected — archived only.)_")

    # Trigger phrase so Jarvis knows to process this
    parts.append("\n\n`jarvis: process voice memo actions`")
    return "\n".join(parts)


async def post_action_items_to_discord(
    bot_token: str,
    channel_id: str,
    memo_id: int,
    duration_seconds: int,
    actions: list,
    ideas: list,
    decisions: list,
    summary: Optional[str] = None,
) -> bool:
    """Post the digest to the Discord channel."""
    if not bot_token or not channel_id:
        log.warning("Discord not configured — skipping post")
        return False

    content = _format_message(memo_id, duration_seconds, actions, ideas, decisions, summary)

    # Discord caps messages at 2000 chars
    if len(content) > 1950:
        content = content[:1950] + "\n…(truncated)"

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, headers=headers, json={"content": content})
        if r.status_code not in (200, 201):
            log.error(f"Discord post failed {r.status_code}: {r.text[:300]}")
            return False
    return True
