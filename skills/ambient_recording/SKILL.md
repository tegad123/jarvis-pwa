---
name: process_voice_memo_actions
description: Process action items posted by the Jarvis PWA voice memo system. Triggered when a message in Discord contains the phrase "jarvis: process voice memo actions" — that message is the structured digest of tasks/ideas/decisions extracted from a recording, and you should route each item to the appropriate existing skill (calendar, email, deal tracker, etc.) or store it for Spencer's review.
---

# Process Voice Memo Actions

## Trigger

A message in any channel that contains the literal phrase:

```
jarvis: process voice memo actions
```

The PWA backend posts these. Each message follows this format:

```
🎙️ **Voice Memo #<id>** · <duration>
_<one-or-two-sentence summary>_

📋 **TASKS**
→ <task text> `<priority>` — *<project tag>*
→ <task text> ...

💡 **IDEAS FLAGGED**
→ <idea text>

📌 **DECISIONS LOGGED**
→ <decision text>

jarvis: process voice memo actions
```

## What to do

For each item in the digest:

### Tasks
- If a task involves scheduling, calendar, or showings → route to the `calendar` skill.
- If a task involves drafting an email → route to `spencer_email_voice`.
- If a task references a deal property → check the relevant `#jarvis-deals-*` channel and post a follow-up note there.
- Otherwise → reply in the same channel with a confirmation:
  `✅ Logged: <task>` and add it to Spencer's pending task list.

### Ideas
- Do NOT auto-action ideas. They are flagged for Spencer to review later.
- Reply once at the end: `💡 <N> idea(s) logged for review.`

### Decisions
- Acknowledge them by reacting with 📌 — no further action.

### High-priority items
- If any task is marked `HIGH` and is time-sensitive (today or tomorrow),
  send Spencer a direct ping in `#jarvis-assistant` even if the digest was
  posted elsewhere.

## Style of reply

- Keep responses short — one or two lines per item, total reply under
  10 lines whenever possible.
- Don't repeat the digest back to Spencer. He already saw it on his phone.
- If a task is ambiguous, ask ONE clarifying question. Don't ask more.
- Never reject a task. If you can't action it, log it and say so plainly.

## Examples

### Example input
```
🎙️ Voice Memo #42 · 3m 18s
_Spencer worked through pricing strategy for the Heights lot split and flagged a follow-up with Martin._

📋 TASKS
→ Follow up with Martin on Heights Deal Machine export — confirm Tuesday delivery `HIGH` — *heights*
→ Pull comps for 815 Lawrence St `MED` — *deal-815-lawrence*

💡 IDEAS FLAGGED
→ Lot split economics breakdown for new investor deck

jarvis: process voice memo actions
```

### Example reply
```
✅ Martin follow-up queued (HIGH). Drafting message in #jarvis-assistant.
✅ 815 Lawrence comps queued — will pull tonight.
💡 1 idea logged for review.
```

## What NOT to do

- Don't second-guess the extraction. The PWA already filtered.
- Don't post a long acknowledgment of every line. Be terse.
- Don't action ideas. Only tasks.
- Don't ask Spencer to repeat himself.
