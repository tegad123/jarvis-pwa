#!/usr/bin/env python3
"""Health contract for the Open House Lead Tracker.

This is the REFERENCE implementation of Jarvis self-verification. It reads the
live Google Sheet read-only and asserts about STATE, not liveness:

  * FAIL (contract red): things that mean the engine is silently doing the wrong
    thing -- a scanned tab that won't resolve, a column that has drifted, a
    queued lead with no recipient, an impossible nudge count, a `Dead` lead that
    ARCHIVE failed to move, a malformed/duplicate Lead ID.
  * WARN (surfaced, known-tolerable): a queue that isn't advancing (the "cron
    never ran" detector -- the whole point, since the engine is NOT SCHEDULED),
    dirty lead-email cells, and leads with a blank follow-up date that are
    invisible to the queue.

It NEVER writes to the Sheet. Read-only by construction, so "back up before
write / dry-run before apply" are satisfied trivially.

Run:
    python3 lead_tracker_contract.py            # live, human report, exit code
    python3 lead_tracker_contract.py --json     # machine-readable (for the verifier)
    python3 lead_tracker_contract.py --selftest  # prove the checks; no network
    python3 lead_tracker_contract.py --strict   # WARN also exits non-zero

============================ ASSUMPTIONS TO VERIFY ON THE MINI (rule 6) =========
These are transcribed from CONTEXT.md, not read from the live code/sheet. Confirm
each against reality on the first live run and adjust the constant if wrong:

  1. AUTH: google_token.json is an OAuth "authorized_user" token (matches
     lead_scan.py). If lead_scan.py uses a service account, see _load_credentials.
     BETTER: import lead_scan.py's own sheet-service builder so this reuses the
     already-verified auth path -- wire that once you know the function name.
  2. HEADERS: the column *labels* below are from CONTEXT.md. The contract anchors
     on keywords at fixed positions (robust to wording), but if a live header
     word differs, adjust COL_ANCHORS.
  3. SCHEMA: header on row 2, data from row 3, columns A-S. N (col 14) is the
     ONLY queue trigger; O (col 15) is the only recipient source.
  4. TABS: scanned tabs are Spencer, Martin, Alex, Tega, Blake. Titles may carry
     trailing spaces; we resolve real titles from metadata and key off the
     stripped name. Do NOT rename tabs -- it breaks resolution.
================================================================================
"""

from __future__ import annotations

import datetime
import os
import re
import sys
import warnings
from typing import Dict, List, Optional

# Framework lives beside this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from health_contract import (  # noqa: E402
    Assertion, Check, Contract, Severity, render_text, run_cli,
)

# --------------------------------------------------------------------------- #
# Configuration (see ASSUMPTIONS block).
# --------------------------------------------------------------------------- #

SHEET_ID = os.getenv(
    "JARVIS_SHEET_ID", "1XU_BDhJy6-ABY0ul1DzPUdWCRHSNj8f2BTTC4fMHg2U")
CRED_PATH = os.getenv(
    "JARVIS_GOOGLE_CRED",
    os.path.expanduser("~/.openclaw/workspace/credentials/google_token.json"))

SCANNED_TABS = ["Spencer", "Martin", "Alex", "Tega", "Blake"]

# 0-indexed columns (A=0 ... S=18). Header row is row 2; data rows start at row 3.
COL = {
    "date": 0, "name": 1, "property": 2, "price": 3, "notes": 4,
    "temperature": 5, "followup_status": 6, "status_of_lead": 7,
    "follow_up_next": 8, "assigned_agent": 9, "lead_email": 10, "phone": 11,
    "status_date": 12, "next_followup_date": 13, "agent_email": 14,
    "lead_id": 15, "nudge_count": 16, "last_nudge_date": 17, "thread_id": 18,
}

# Keyword an expected column header must contain, anchored at its index. This
# catches the dangerous case (a column inserted/removed so the engine reads the
# wrong cell) without being brittle about exact wording.
COL_ANCHORS = {
    COL["status_of_lead"]: "status of lead",
    COL["next_followup_date"]: "next follow",
    COL["agent_email"]: "agent email",
    COL["lead_id"]: "lead id",
    COL["nudge_count"]: "nudge",
    COL["last_nudge_date"]: "last nudge",
    COL["thread_id"]: "thread",
}

CADENCE_GAP_DAYS = 2      # nudge at 2-day gaps
MAX_NUDGES = 3            # max 3, CC on the 3rd, then stalled
STALE_SLACK_DAYS = 1      # grace before we call an unadvanced queue stale
DEAD_MARK = "Dead"        # exact value a human sets in H to retire a lead

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LEAD_ID_RE = re.compile(r"^[A-Z]{2,4}-\d{3,4}$")


# --------------------------------------------------------------------------- #
# Data model. A check receives this ctx; it is populated live OR by --selftest.
# --------------------------------------------------------------------------- #

class TabData:
    def __init__(self, requested: str, title: str, header: List[str],
                 rows: List[List[str]]):
        self.requested = requested   # stripped name we asked for, e.g. "Alex"
        self.title = title           # real title incl. trailing space, or None
        self.header = header         # row 2 values
        self.rows = rows             # row 3+, each a list of cell strings

    @property
    def resolved(self) -> bool:
        return self.title is not None


class Ctx:
    def __init__(self, tabs: Dict[str, TabData], today: datetime.date,
                 missing: Optional[List[str]] = None):
        self.tabs = tabs                 # stripped name -> TabData
        self.today = today
        self.missing = missing or []     # scanned names that failed to resolve


def cell(row: List[str], idx: int) -> str:
    # Sheets trims trailing empty cells, so short rows are normal.
    return row[idx].strip() if idx < len(row) else ""


def named(row: List[str]) -> bool:
    return bool(cell(row, COL["name"]))


def _parse_date(s: str) -> Optional[datetime.date]:
    s = s.strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _iter_rows(ctx: Ctx):
    for tab in ctx.tabs.values():
        if not tab.resolved:
            continue
        for i, row in enumerate(tab.rows):
            yield tab, i + 3, row  # +3: data starts at spreadsheet row 3


# --------------------------------------------------------------------------- #
# Checks. Each returns Assertion(ok, detail, evidence).
# --------------------------------------------------------------------------- #

def check_tabs_resolved(ctx: Ctx) -> Assertion:
    if ctx.missing:
        return Assertion(False,
                         "scanned tab(s) did not resolve to a real title",
                         evidence=ctx.missing)
    resolved = ["{!r}".format(t.title) for t in ctx.tabs.values()]
    return Assertion(True, "all {} scanned tabs resolved".format(len(ctx.tabs)),
                     evidence=resolved)


def check_header_alignment(ctx: Ctx) -> Assertion:
    drift = []
    for tab in ctx.tabs.values():
        if not tab.resolved:
            continue
        for idx, kw in COL_ANCHORS.items():
            got = tab.header[idx].strip().lower() if idx < len(tab.header) else ""
            if kw not in got:
                drift.append("{}: col {} expected ~{!r}, got {!r}".format(
                    tab.requested, _a1(idx), kw, got))
    if drift:
        return Assertion(False, "column layout has drifted from the schema",
                         evidence=drift)
    return Assertion(True, "key columns anchored in all tabs")


def check_queued_have_recipient(ctx: Ctx) -> Assertion:
    bad = []
    for tab, rownum, row in _iter_rows(ctx):
        if cell(row, COL["next_followup_date"]):  # queue-eligible
            o = cell(row, COL["agent_email"])
            if not EMAIL_RE.match(o):
                bad.append("{} r{}: queued but Agent Email (O)={!r}".format(
                    tab.requested, rownum, o))
    if bad:
        return Assertion(False, "queued lead(s) have no valid recipient in O",
                         evidence=bad)
    return Assertion(True, "every queued lead has an email-shaped Agent Email")


def check_nudge_count_sane(ctx: Ctx) -> Assertion:
    bad = []
    for tab, rownum, row in _iter_rows(ctx):
        q = cell(row, COL["nudge_count"])
        r = cell(row, COL["last_nudge_date"])
        n_val = None
        if q:
            try:
                n_val = int(q)
            except ValueError:
                bad.append("{} r{}: Nudge Count (Q)={!r} not an integer".format(
                    tab.requested, rownum, q))
                continue
            if n_val < 0 or n_val > MAX_NUDGES:
                bad.append("{} r{}: Nudge Count (Q)={} outside 0..{}".format(
                    tab.requested, rownum, n_val, MAX_NUDGES))
        if n_val and n_val > 0:
            if not r or _parse_date(r) is None:
                bad.append("{} r{}: Q={} but Last Nudge Date (R)={!r} "
                           "missing/unparseable".format(tab.requested, rownum, n_val, r))
    if bad:
        return Assertion(False, "nudge bookkeeping is inconsistent", evidence=bad)
    return Assertion(True, "nudge counts within 0..{} and dated".format(MAX_NUDGES))


def check_no_dead_in_active(ctx: Ctx) -> Assertion:
    lingering = []
    for tab, rownum, row in _iter_rows(ctx):
        if cell(row, COL["status_of_lead"]) == DEAD_MARK:
            lingering.append("{} r{}: H=={!r} still in active tab (ARCHIVE "
                             "should have moved it)".format(tab.requested, rownum, DEAD_MARK))
    if lingering:
        return Assertion(False, "Dead lead(s) not archived", evidence=lingering)
    return Assertion(True, "no Dead leads lingering in active tabs")


def check_lead_id_wellformed_unique(ctx: Ctx) -> Assertion:
    seen = {}
    problems = []
    for tab, rownum, row in _iter_rows(ctx):
        if not named(row):
            continue
        pid = cell(row, COL["lead_id"])
        if not pid:
            continue  # missing IDs handled (as WARN) by check_lead_id_present
        if not LEAD_ID_RE.match(pid):
            problems.append("{} r{}: Lead ID (P)={!r} malformed".format(
                tab.requested, rownum, pid))
            continue
        where = "{} r{}".format(tab.requested, rownum)
        if pid in seen:
            problems.append("Lead ID {!r} duplicated: {} and {}".format(
                pid, seen[pid], where))
        else:
            seen[pid] = where
    if problems:
        return Assertion(False, "Lead ID integrity violated", evidence=problems)
    return Assertion(True, "all present Lead IDs well-formed and unique")


# ---- WARN-severity checks (surfaced, known-tolerable) --------------------- #

def check_queue_advancing(ctx: Ctx) -> Assertion:
    """The 'cron never ran' detector.

    A queue-eligible lead whose Next Follow-Up Date is overdue by more than the
    cadence gap, has not exhausted its nudges, and is not Dead, should have been
    advanced by a run. If such rows pile up, the engine is not executing. Today
    this is EXPECTED (NOT SCHEDULED) -> WARN; once cron is installed, --strict
    turns this into the primary loud signal that the engine has silently stopped.
    """
    overdue = []
    threshold = ctx.today - datetime.timedelta(days=CADENCE_GAP_DAYS + STALE_SLACK_DAYS)
    for tab, rownum, row in _iter_rows(ctx):
        n = _parse_date(cell(row, COL["next_followup_date"]))
        if n is None:
            continue
        q = cell(row, COL["nudge_count"])
        try:
            qn = int(q) if q else 0
        except ValueError:
            qn = 0
        h = cell(row, COL["status_of_lead"])
        if n < threshold and qn < MAX_NUDGES and h != DEAD_MARK:
            overdue.append("{} r{}: due {} (>{}d ago), nudges={}".format(
                tab.requested, rownum, n.isoformat(),
                CADENCE_GAP_DAYS + STALE_SLACK_DAYS, qn))
    if overdue:
        return Assertion(False,
                         "{} queued lead(s) overdue and unadvanced -- engine may "
                         "not be running".format(len(overdue)), evidence=overdue)
    return Assertion(True, "no overdue-unadvanced leads; queue is moving")


def check_dirty_lead_email(ctx: Ctx) -> Assertion:
    dirty = []
    for tab, rownum, row in _iter_rows(ctx):
        k = cell(row, COL["lead_email"])
        if k and not EMAIL_RE.match(k):
            dirty.append("{} r{}: Email (K)={!r}".format(tab.requested, rownum, k))
    if dirty:
        return Assertion(False, "lead-email cells hold non-email text (manual "
                         "cleanup outstanding)", evidence=dirty)
    return Assertion(True, "no dirty lead-email cells")


def check_missing_followup_date(ctx: Ctx) -> Assertion:
    invisible = []
    for tab, rownum, row in _iter_rows(ctx):
        if named(row) and not cell(row, COL["next_followup_date"]):
            invisible.append("{} r{}: {!r} has blank N (invisible to queue)".format(
                tab.requested, rownum, cell(row, COL["name"])))
    if invisible:
        return Assertion(False, "{} named lead(s) have no follow-up date and are "
                         "invisible to the engine".format(len(invisible)),
                         evidence=invisible)
    return Assertion(True, "every named lead has a follow-up date")


def _a1(idx: int) -> str:
    return chr(ord("A") + idx)


CONTRACT = Contract("lead_tracker", [
    Check("tabs_resolved", Severity.FAIL, check_tabs_resolved,
          "all scanned tabs resolve to a real title"),
    Check("header_alignment", Severity.FAIL, check_header_alignment,
          "key columns are where the engine expects them"),
    Check("queued_have_recipient", Severity.FAIL, check_queued_have_recipient,
          "queued leads have a valid Agent Email"),
    Check("nudge_count_sane", Severity.FAIL, check_nudge_count_sane,
          "nudge counts are within 0..MAX and dated"),
    Check("no_dead_in_active", Severity.FAIL, check_no_dead_in_active,
          "Dead leads have been archived"),
    Check("lead_id_integrity", Severity.FAIL, check_lead_id_wellformed_unique,
          "Lead IDs well-formed and unique"),
    Check("queue_advancing", Severity.WARN, check_queue_advancing,
          "the queue is actually moving (cron liveness-of-outcome)"),
    Check("dirty_lead_email", Severity.WARN, check_dirty_lead_email,
          "lead-email cells are clean"),
    Check("missing_followup_date", Severity.WARN, check_missing_followup_date,
          "named leads have a follow-up date"),
])


# --------------------------------------------------------------------------- #
# Live sheet adapter. Lazy-imports google libs so --selftest needs no network.
# --------------------------------------------------------------------------- #

def _load_credentials(cred_path: str):
    # ASSUMPTION (verify on Mini): authorized_user OAuth token, read-only scope.
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(cred_path, scopes)
        if not creds.valid and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        return creds
    except Exception:
        # Fallback: service-account key. Remove whichever branch is wrong once
        # you confirm the token type against lead_scan.py.
        from google.oauth2.service_account import Credentials as SA
        return SA.from_service_account_file(cred_path, scopes=scopes)


def build_live_ctx(args) -> Ctx:
    warnings.filterwarnings("ignore")  # Py3.9.6 + google libs emit FutureWarnings
    from googleapiclient.discovery import build

    creds = _load_credentials(CRED_PATH)
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    # Resolve real titles (trailing-space safe) by stripped name.
    by_stripped = {}
    for s in meta.get("sheets", []):
        title = s["properties"]["title"]
        by_stripped[title.strip()] = title

    tabs: Dict[str, TabData] = {}
    missing: List[str] = []
    for want in SCANNED_TABS:
        title = by_stripped.get(want)
        if title is None:
            missing.append(want)
            tabs[want] = TabData(want, None, [], [])
            continue
        rng = "'{}'!A2:S".format(title)
        vals = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=rng).execute().get("values", [])
        header = vals[0] if vals else []
        rows = vals[1:] if len(vals) > 1 else []
        tabs[want] = TabData(want, title, header, rows)
    return Ctx(tabs, today=datetime.date.today(), missing=missing)


# --------------------------------------------------------------------------- #
# Self-test (operating rule 5: prove the fix before shipping it). No network.
# --------------------------------------------------------------------------- #

def _good_header() -> List[str]:
    h = [""] * 19
    labels = {
        COL["status_of_lead"]: "Status Of Lead",
        COL["next_followup_date"]: "Next Follow-Up Date",
        COL["agent_email"]: "Agent Email",
        COL["lead_id"]: "Lead ID",
        COL["nudge_count"]: "Nudge Count",
        COL["last_nudge_date"]: "Last Nudge Date",
        COL["thread_id"]: "Thread ID",
        COL["name"]: "Lead Name",
        COL["lead_email"]: "Email",
    }
    for i, v in labels.items():
        h[i] = v
    return h


def _row(name="", n_date="", agent_email="agent@cqhouston.com", nudge="",
         last_nudge="", status="", lead_id="ALX-0001", lead_email="lead@x.com"):
    r = [""] * 19
    r[COL["name"]] = name
    r[COL["next_followup_date"]] = n_date
    r[COL["agent_email"]] = agent_email
    r[COL["nudge_count"]] = nudge
    r[COL["last_nudge_date"]] = last_nudge
    r[COL["status_of_lead"]] = status
    r[COL["lead_id"]] = lead_id
    r[COL["lead_email"]] = lead_email
    return r


def _statuses(report):
    return {o.name: o.status.value for o in report.outcomes}


def selftest() -> int:
    today = datetime.date(2026, 7, 20)
    fails = []

    # ---- GOOD fixture: everything valid -> no FAIL, no WARN. ----
    good = TabData("Alex", "Alex ", _good_header(), [
        _row(name="Jane", n_date="2026-07-21", nudge="1",
             last_nudge="2026-07-19", lead_id="ALX-0001"),
        _row(name="Bob", n_date="2026-07-22", nudge="0",
             last_nudge="", lead_id="ALX-0002"),
    ])
    good_ctx = Ctx({"Alex": good}, today=today)
    good_rep = CONTRACT.run(good_ctx)
    if good_rep.overall.value != "PASS":
        fails.append("GOOD fixture should be all-PASS, got {}\n{}".format(
            good_rep.overall.value, render_text(good_rep)))

    # ---- BROKEN fixture: one defect per check. ----
    broken_header = _good_header()
    broken_header[COL["agent_email"]] = "Some Other Column"  # column drift
    broken = TabData("Alex", "Alex ", broken_header, [
        # queued but no recipient  -> queued_have_recipient FAIL
        _row(name="NoRcpt", n_date="2026-07-25", agent_email="", lead_id="ALX-0010"),
        # nudge count impossible   -> nudge_count_sane FAIL
        _row(name="Over", n_date="2026-07-25", nudge="5", last_nudge="2026-07-01",
             lead_id="ALX-0011"),
        # Q>0 with no date         -> nudge_count_sane FAIL (same check)
        _row(name="NoDate", n_date="2026-07-25", nudge="2", last_nudge="",
             lead_id="ALX-0012"),
        # Dead still in tab        -> no_dead_in_active FAIL
        _row(name="Ghost", n_date="", status="Dead", lead_id="ALX-0013"),
        # malformed id             -> lead_id_integrity FAIL
        _row(name="BadId", n_date="", lead_id="ALX-1"),
        # duplicate id             -> lead_id_integrity FAIL
        _row(name="Dup1", n_date="", lead_id="ALX-0099"),
        _row(name="Dup2", n_date="", lead_id="ALX-0099"),
        # overdue + unadvanced     -> queue_advancing WARN
        _row(name="Stale", n_date="2026-07-01", nudge="1", last_nudge="2026-07-01",
             lead_id="ALX-0014"),
        # dirty lead email         -> dirty_lead_email WARN
        _row(name="DirtyK", n_date="", lead_email="AGENT INFO: call first",
             lead_id="ALX-0015"),
        # blank N on a named lead  -> missing_followup_date WARN
        _row(name="Invisible", n_date="", lead_id="ALX-0016"),
    ])
    broken_ctx = Ctx({"Alex": broken}, today=today)
    broken_rep = CONTRACT.run(broken_ctx)
    st = _statuses(broken_rep)

    expect = {
        "header_alignment": "FAIL",
        "queued_have_recipient": "FAIL",
        "nudge_count_sane": "FAIL",
        "no_dead_in_active": "FAIL",
        "lead_id_integrity": "FAIL",
        "queue_advancing": "WARN",
        "dirty_lead_email": "WARN",
        "missing_followup_date": "WARN",
    }
    for name, want in expect.items():
        if st.get(name) != want:
            fails.append("BROKEN: check {!r} expected {}, got {}".format(
                name, want, st.get(name)))

    # ---- Unresolved-tab fixture -> tabs_resolved FAIL. ----
    miss_ctx = Ctx({"Blake": TabData("Blake", None, [], [])},
                   today=today, missing=["Blake"])
    if _statuses(CONTRACT.run(miss_ctx)).get("tabs_resolved") != "FAIL":
        fails.append("missing-tab fixture should FAIL tabs_resolved")

    if fails:
        print("SELFTEST FAILED:")
        for f in fails:
            print("  - {}".format(f))
        return 1
    print("SELFTEST PASSED: {} checks proven on good + broken fixtures".format(
        len(CONTRACT.checks)))
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(build_live_ctx, CONTRACT, selftest=selftest))
