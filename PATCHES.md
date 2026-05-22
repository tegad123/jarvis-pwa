# Local OpenClaw Patches

## Session Store Snapshot Deduplication

Date applied: 2026-05-22

Purpose: prevent `~/.openclaw/agents/main/sessions/sessions.json` from growing by about 30KB per new OpenClaw session. The root cause was that each session index entry stored full `skillsSnapshot` and `systemPromptReport` objects. Those objects are mostly identical across sessions and belong in a content-addressed snapshot store, not repeated inside the hot session index.

Patched file:

`/opt/homebrew/lib/node_modules/openclaw/dist/store-C5UK26Ce.js`

Backup:

`/opt/homebrew/lib/node_modules/openclaw/dist/store-C5UK26Ce.js.bak-20260522-181153`

Original locations in the backup:

- `loadSessionStore()` starts at line 996.
- `saveSessionStoreUnlocked()` starts at line 1097.
- Original disk serialization was line 1192: `const json = JSON.stringify(store, null, 2);`

New behavior:

- On save, `skillsSnapshot` is written once to:
  `~/.openclaw/agents/main/snapshots/skillsSnapshot-<sha256>.json`
- On save, `systemPromptReport` is written once to:
  `~/.openclaw/agents/main/snapshots/systemPromptReport-<sha256>.json`
- `sessions.json` stores only:
  `skillsSnapshotHash`
  `systemPromptReportHash`
- On load, hashes are hydrated back into full in-memory objects before callers use the session entry.
- Missing snapshot files log a warning and omit the hydrated field rather than crashing.

Recovery:

```bash
cp /opt/homebrew/lib/node_modules/openclaw/dist/store-C5UK26Ce.js.bak-20260522-181153 \
  /opt/homebrew/lib/node_modules/openclaw/dist/store-C5UK26Ce.js
launchctl kickstart -k gui/$(id -u)/ai.openclaw.gateway
```

Versioning note:

OpenClaw upgrades can overwrite `/opt/homebrew/lib/node_modules/openclaw/dist/store-C5UK26Ce.js`. Reapply this patch after upgrading OpenClaw unless upstream adds equivalent session snapshot deduplication.
