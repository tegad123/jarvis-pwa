# Jarvis self-verification — health contracts

Reference implementation of CONTEXT.md item 4, step 1: a **health contract per
skill (assertions about state, not liveness)**. The lead tracker is the first,
and the pattern the other 23 skills follow.

    health_contract.py         reusable framework (Check / Severity / Contract / runner)
    lead_tracker_contract.py   the lead-tracker instance + live Sheet adapter + selftest

## Design law (from the 2026-07-20 failure modes)

Every bug that has bitten this system exited 0. So a check asserts that work
**happened** and the **result is plausible** — never merely that a process did
not raise. A check that cannot *prove* its assertion goes **RED (FAIL)**, never
green. Unknown state is red state.

- **FAIL = contract red** → will feed diagnosis/repair downstream.
- **WARN = surfaced, known-tolerable** → shown and counted, exits 0 by default.
  Pass `--strict` (the daily verifier will) to make WARN exit non-zero too.

## Run

    python3 lead_tracker_contract.py            # live, human report + exit code
    python3 lead_tracker_contract.py --json     # machine-readable, for the verifier
    python3 lead_tracker_contract.py --selftest  # prove every check; no network
    python3 lead_tracker_contract.py --strict   # WARN also exits non-zero

`--selftest` runs every check against synthetic good/broken fixtures and asserts
each one fires as designed (operating rule 5). It needs no credentials or
network — run it first, anywhere.

## What it asserts about the live Sheet (read-only)

FAIL: all 5 scanned tabs resolve (trailing-space safe) · key columns (H, N, O,
P, Q, R, S) are where the engine expects them · every queue-eligible row
(non-blank N) has an email-shaped Agent Email (O) · nudge count ∈ 0..3 and
dated · no `Dead` lead lingering in an active tab · Lead IDs well-formed and
unique.

WARN: queue overdue-and-unadvanced (**the "cron never ran" detector** — expected
today since the engine is NOT SCHEDULED; becomes the primary loud signal under
`--strict` once cron is installed) · dirty lead-email cells (`-`, `AGENT INFO:`)
· named leads with a blank follow-up date, invisible to the queue.

## Operating-rule compliance

- **Read-only by construction.** Never writes to the Sheet, so "back up before
  write / dry-run before apply" are satisfied trivially. The only roadmap item
  that writes is the run-ledger (item 2), not built here.
- **Self-tested before shipping** (`--selftest`).
- **Propose, not apply.** These files are authored for review; nothing is
  deployed until you drop them on the Mini.

## Deploy to the Mini

Copy both `.py` files next to the lead tracker (e.g. `~/jarvis/`), then:

    ssh nemoclaw@100.127.55.100 "zsh -l -c 'cd ~/jarvis && python3 lead_tracker_contract.py'"

## Verify on the Mini before trusting it (operating rule 6)

These constants are transcribed from CONTEXT.md, not read from the live
code/Sheet. Confirm on the first live run and adjust if wrong:

1. **Auth** — `_load_credentials` assumes `google_token.json` is an OAuth
   `authorized_user` token. If `lead_scan.py` uses a service account, keep the
   fallback branch instead. **Better:** import `lead_scan.py`'s own sheet-service
   builder so this reuses the already-verified auth path.
2. **Headers** — the contract anchors on keywords at fixed column positions
   (robust to wording). If a live header word differs, adjust `COL_ANCHORS`.
3. **Lead ID shape** — `LEAD_ID_RE` is `^[A-Z]{2,4}-\d{3,4}$` (matches SPN-0001,
   ALX-0003). Loosen if other tabs use a different prefix width.

Expected on today's Sheet: `queue_advancing` and `missing_followup_date` WARN
(the 2 queued Alex nudges are within cadence; ~15 blank-N leads exist), dirty-K
WARN for Martin r6 / Alex r3–r4. If instead a FAIL appears, that's the contract
earning its keep.
