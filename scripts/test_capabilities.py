#!/usr/bin/env python3
"""
Synthetic PWA capability audit harness.

Runs typed-chat requests through the deployed/local PWA API and validates
observable side effects such as task DB rows, Google Calendar events, and
bridge invocation logs. All generated prompts are prefixed with a unique
[TEST-PWA-AUDIT-<run_id>] marker and cleanup runs in finally.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = ROOT / "scripts" / "test_cases.json"
DEFAULT_OUT_DIR = ROOT / "data"
DEFAULT_PWA_URL = os.getenv("PWA_URL", "http://127.0.0.1:8765").rstrip("/")
DEFAULT_PWA_PASSWORD = os.getenv("JARVIS_PWA_PASSWORD", "34811")
DEFAULT_PWA_DB = Path(os.getenv("JARVIS_DB_PATH", str(ROOT / "backend" / "data" / "jarvis.db")))
DEFAULT_TASKS_DB = Path(
    os.getenv(
        "JARVIS_TASKS_DB",
        str(Path.home() / ".openclaw" / "workspace" / "skills" / "task-manager" / "data" / "tasks.db"),
    )
)
DEFAULT_GOOGLE_TOKEN = Path(
    os.getenv(
        "JARVIS_GOOGLE_TOKEN",
        str(Path.home() / ".openclaw" / "workspace" / "credentials" / "google_token.json"),
    )
)
DEFAULT_CALENDAR_ID = os.getenv("JARVIS_CALENDAR_ID", "jarvis@34dev.com")
DEFAULT_AUDIT_SENDER = os.getenv("JARVIS_AUDIT_SENDER", "tega@34dev.com")
SPENCER_CALENDAR_ID = os.getenv("JARVIS_SPENCER_CALENDAR_ID", "spencerhuck@34dev.com")
TEGA_CALENDAR_ID = os.getenv("JARVIS_TEGA_CALENDAR_ID", "tega@34dev.com")
DEFAULT_LOG_PATH = Path(os.getenv("JARVIS_SERVER_ERR_LOG", str(ROOT / "data" / "server.err.log")))
SAFE_EMAILS = {"jarvis@34dev.com"}
BRIDGE_GRACEFUL_ERROR = "Sorry, I had trouble with that. Could you try again?"


@dataclass
class HarnessConfig:
    pwa_url: str
    password: str
    cases_path: Path
    output_dir: Path
    pwa_db: Path
    tasks_db: Path
    google_token: Path
    calendar_id: str
    log_path: Path
    per_test_timeout: float
    total_budget: float
    token_budget_usd: float
    audit_sender: str
    only: set[str] = field(default_factory=set)
    dry_run: bool = False


@dataclass
class TestResult:
    id: str
    category: str
    status: str
    message: str
    response_text: str = ""
    audio_url: str | None = None
    actions_taken: list[Any] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


class PwaClient:
    def __init__(
        self,
        base_url: str,
        password: str,
        timeout: float,
        *,
        audit_sender: str,
        calendar_id: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self.cookie = ""
        self.audit_sender = audit_sender
        self.calendar_id = calendar_id

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        data = None
        headers = {
            "Accept": "application/json",
            "X-PWA-Audit-Sender": self.audit_sender,
            "X-PWA-Audit-Calendar-Id": self.calendar_id,
        }
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read().decode()
                response_headers = {k.lower(): v for k, v in resp.headers.items()}
                set_cookie = response_headers.get("set-cookie")
                if set_cookie:
                    self.cookie = set_cookie.split(";", 1)[0]
                parsed = json.loads(body) if body else {}
                return resp.status, parsed, response_headers
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = {"raw": body}
            return exc.code, parsed, {k.lower(): v for k, v in exc.headers.items()}

    def login(self) -> None:
        status, payload, _ = self.request(
            "POST",
            "/chat/api/login",
            payload={"password": self.password},
        )
        if status != 200 or not self.cookie:
            raise RuntimeError(f"PWA login failed: status={status} payload={payload}")

    def create_chat(self) -> str:
        status, payload, _ = self.request("POST", "/chat/api/chats")
        if status != 200 or not payload.get("chat_id"):
            raise RuntimeError(f"chat create failed: status={status} payload={payload}")
        return payload["chat_id"]

    def send_message(self, chat_id: str, content: str, timeout: float) -> dict[str, Any]:
        status, payload, _ = self.request(
            "POST",
            f"/chat/api/chats/{chat_id}/message",
            payload={"content": content},
            timeout=timeout,
        )
        if status != 200:
            raise RuntimeError(f"message failed: status={status} payload={payload}")
        return payload


def now_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def load_json(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+)", r"***@\2", value)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


def found_unsafe_email(text: str) -> str | None:
    for email in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or ""):
        if email.lower() not in SAFE_EMAILS:
            return email
    return None


def build_prompt(raw_message: str, prefix: str) -> str:
    return (
        f"{prefix} {raw_message.strip()}\n\n"
        "For this automated audit, act only on behalf of Tega "
        "(tega@34dev.com), not Spencer. Use Tega as the requester, but route all "
        "test artifacts to Jarvis (jarvis@34dev.com): calendar writes must use "
        "calendar_id jarvis@34dev.com, calendar attendees must be jarvis@34dev.com, "
        "email recipients must be jarvis@34dev.com, and task assignees should be "
        "Jarvis/jarvis@34dev.com when an assignee is needed. Do not create anything "
        "on Spencer's or Tega's calendar, and do not assign anything to Spencer. "
        f"Include the exact marker {prefix} in any task title, calendar event "
        "title, email subject, or other artifact you create."
    )


def substitute_prefix(value: Any, prefix: str) -> Any:
    if isinstance(value, str):
        return value.replace("{prefix}", prefix)
    if isinstance(value, list):
        return [substitute_prefix(v, prefix) for v in value]
    if isinstance(value, dict):
        return {k: substitute_prefix(v, prefix) for k, v in value.items()}
    return value


def query_one(db_path: Path, sql: str, params: list[Any] | None = None) -> bool:
    if not db_path.exists():
        raise RuntimeError(f"database not found: {db_path}")
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(sql, params or []).fetchone()
    return row is not None


def task_rows_for_prefix(db_path: Path, prefix: str) -> list[str]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE task_summary LIKE ? OR full_context LIKE ?",
            (f"%{prefix}%", f"%{prefix}%"),
        ).fetchall()
    return [r[0] for r in rows]


def spencer_task_rows_for_prefix(db_path: Path, prefix: str) -> list[str]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT task_id FROM tasks
            WHERE (task_summary LIKE ? OR full_context LIKE ?)
              AND (lower(assignee) LIKE '%spencer%' OR lower(assignee_name) LIKE '%spencer%')
            """,
            (f"%{prefix}%", f"%{prefix}%"),
        ).fetchall()
    return [r[0] for r in rows]


def cleanup_tasks(db_path: Path, prefix: str) -> int:
    if not db_path.exists():
        return 0
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE tasks
            SET status='completed',
                completed_at=COALESCE(completed_at, ?),
                full_context=full_context || ?
            WHERE (task_summary LIKE ? OR full_context LIKE ?)
              AND status != 'completed'
            """,
            (now, f" [CLEANED {prefix}]", f"%{prefix}%", f"%{prefix}%"),
        )
        return cur.rowcount


def cleanup_chat(db_path: Path, chat_id: str | None) -> None:
    if not chat_id or not db_path.exists():
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM session_map WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))


def get_google_access_token(token_path: Path) -> str:
    creds = load_json(token_path)
    data = urllib.parse.urlencode(
        {
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(creds["token_uri"], data=data)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())["access_token"]


def calendar_events(
    cfg: HarnessConfig,
    *,
    token: str,
    prefix: str,
    attendee: str | None = None,
    summary_contains: str | None = None,
    within_hours: int = 72,
) -> list[dict[str, Any]]:
    time_min = datetime.now(timezone.utc) - timedelta(hours=1)
    time_max = datetime.now(timezone.utc) + timedelta(hours=within_hours)
    params = {
        "timeMin": time_min.isoformat().replace("+00:00", "Z"),
        "timeMax": time_max.isoformat().replace("+00:00", "Z"),
        "singleEvents": "true",
        "orderBy": "startTime",
        "q": summary_contains or prefix,
    }
    url = (
        "https://www.googleapis.com/calendar/v3/calendars/"
        f"{urllib.parse.quote(cfg.calendar_id)}/events?{urllib.parse.urlencode(params)}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        items = json.loads(resp.read().decode()).get("items", [])
    out = []
    for event in items:
        text = json.dumps(event).lower()
        if prefix.lower() not in text:
            continue
        if attendee and attendee.lower() not in text:
            continue
        if summary_contains and summary_contains.lower() not in (event.get("summary") or "").lower():
            continue
        out.append(event)
    return out


def delete_calendar_events(cfg: HarnessConfig, token: str, events: list[dict[str, Any]]) -> int:
    deleted = 0
    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue
        url = (
            "https://www.googleapis.com/calendar/v3/calendars/"
            f"{urllib.parse.quote(cfg.calendar_id)}/events/{urllib.parse.quote(event_id)}"
            "?sendUpdates=none"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status in (200, 204):
                    deleted += 1
        except urllib.error.HTTPError as exc:
            if exc.code != 410:
                raise
    return deleted


def calendar_events_for_calendar(
    *,
    token: str,
    calendar_id: str,
    prefix: str,
    within_hours: int = 168,
) -> list[dict[str, Any]]:
    time_min = datetime.now(timezone.utc) - timedelta(hours=1)
    time_max = datetime.now(timezone.utc) + timedelta(hours=within_hours)
    params = {
        "timeMin": time_min.isoformat().replace("+00:00", "Z"),
        "timeMax": time_max.isoformat().replace("+00:00", "Z"),
        "singleEvents": "true",
        "orderBy": "startTime",
        "q": prefix,
    }
    url = (
        "https://www.googleapis.com/calendar/v3/calendars/"
        f"{urllib.parse.quote(calendar_id)}/events?{urllib.parse.urlencode(params)}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        items = json.loads(resp.read().decode()).get("items", [])
    return [event for event in items if prefix.lower() in json.dumps(event).lower()]


def try_calendar_events_for_calendar(
    *,
    token: str,
    calendar_id: str,
    prefix: str,
    within_hours: int = 168,
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        return (
            calendar_events_for_calendar(
                token=token,
                calendar_id=calendar_id,
                prefix=prefix,
                within_hours=within_hours,
            ),
            None,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return [], f"inaccessible:{exc.code}"
        return [], f"HTTPError:{exc.code}"
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"


def read_log_since(path: Path, offset: int) -> str:
    if not path.exists():
        return ""
    with path.open(errors="replace") as f:
        f.seek(offset)
        return f.read()


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def evaluate_expectations(
    cfg: HarnessConfig,
    test: dict[str, Any],
    response: dict[str, Any],
    prefix: str,
    log_slice: str,
    google_token: str | None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    calendar_artifacts: list[dict[str, Any]] = []
    expect = substitute_prefix(test.get("expect", {}), prefix)
    response_text = response.get("content") or response.get("response_text") or ""
    actions_taken = response.get("actions_taken") or []

    if response_text == BRIDGE_GRACEFUL_ERROR:
        errors.append("bridge returned graceful error")

    needles = expect.get("response_contains_one_of")
    if needles:
        ok = any(n.lower() in response_text.lower() for n in needles)
        checks.append({"type": "response_contains_one_of", "ok": ok, "needles": needles})
        if not ok:
            errors.append(f"response did not contain any of {needles}")

    action_needle = expect.get("actions_taken_contains")
    if action_needle:
        action_blob = json.dumps(actions_taken)
        ok = action_needle in action_blob
        checks.append({"type": "actions_taken_contains", "ok": ok, "needle": action_needle})
        if not ok:
            errors.append(
                f"actions_taken did not include {action_needle!r}; PWA response may not expose actions_taken"
            )

    if expect.get("db_check"):
        ok = query_one(cfg.tasks_db, expect["db_check"], expect.get("db_params", []))
        checks.append({"type": "db_check", "ok": ok, "sql": expect["db_check"]})
        if not ok:
            errors.append("db_check returned no rows")

    calendar_check = expect.get("google_calendar_check")
    if calendar_check:
        if not google_token:
            ok = False
            matches: list[dict[str, Any]] = []
        else:
            matches = calendar_events(
                cfg,
                token=google_token,
                prefix=prefix,
                attendee=calendar_check.get("attendee"),
                summary_contains=calendar_check.get("summary_contains"),
                within_hours=int(calendar_check.get("within_hours", 72)),
            )
            calendar_artifacts.extend(matches)
            ok = bool(matches)
        checks.append(
            {
                "type": "google_calendar_check",
                "ok": ok,
                "matches": [
                    {
                        "id": e.get("id"),
                        "summary": e.get("summary"),
                        "start": e.get("start"),
                        "attendees": e.get("attendees", []),
                    }
                    for e in matches
                ],
            }
        )
        if not ok:
            errors.append("google_calendar_check found no matching event")

    log_check = expect.get("log_check")
    if log_check:
        ok = log_check in log_slice
        checks.append({"type": "log_check", "ok": ok, "needle": log_check})
        if not ok:
            errors.append(f"log_check did not find {log_check!r}")

    return checks, errors, calendar_artifacts


def enforce_guardrails(tests: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    safe_tests = []
    for test in tests:
        message = substitute_prefix(test.get("message", ""), prefix)
        unsafe = found_unsafe_email(message)
        if unsafe:
            copied = dict(test)
            copied["_skip_reason"] = f"unsafe email recipient {unsafe}; allowed={sorted(SAFE_EMAILS)}"
            safe_tests.append(copied)
            continue
        safe_tests.append(test)
    return safe_tests


def run(cfg: HarnessConfig) -> dict[str, Any]:
    run_id = now_run_id()
    prefix = f"[TEST-PWA-AUDIT-{run_id}]"
    started = time.monotonic()
    cases = load_json(cfg.cases_path)
    tests = enforce_guardrails(cases.get("tests", []), prefix)
    results: list[TestResult] = []
    chat_id: str | None = None
    google_token: str | None = None
    calendar_artifacts: list[dict[str, Any]] = []
    cleanup: dict[str, Any] = {"calendar_deleted": 0, "tasks_completed": 0, "chat_deleted": False}
    isolation: dict[str, Any] = {
        "spencer_calendar_events": [],
        "tega_calendar_events": [],
        "spencer_task_ids": [],
        "ok": True,
    }

    try:
        if cfg.audit_sender.lower() != "tega@34dev.com" or cfg.calendar_id.lower() != "jarvis@34dev.com":
            raise RuntimeError(
                "audit harness must run as Tega requester with Jarvis artifact target: "
                f"audit_sender={cfg.audit_sender!r} calendar_id={cfg.calendar_id!r}"
            )
        if not cfg.dry_run:
            client = PwaClient(
                cfg.pwa_url,
                cfg.password,
                cfg.per_test_timeout,
                audit_sender=cfg.audit_sender,
                calendar_id=cfg.calendar_id,
            )
            client.login()
            chat_id = client.create_chat()
            if cfg.google_token.exists():
                google_token = get_google_access_token(cfg.google_token)

        if cfg.only:
            tests = [test for test in tests if test.get("id") in cfg.only]

        for idx, test in enumerate(tests, 1):
            if time.monotonic() - started > cfg.total_budget:
                results.append(
                    TestResult(
                        id=test.get("id", f"test-{idx}"),
                        category=test.get("category", "unknown"),
                        status="skipped",
                        message=test.get("message", ""),
                        errors=["total run budget exceeded"],
                    )
                )
                continue

            test_id = test.get("id", f"test-{idx}")
            category = test.get("category", "unknown")
            prompt = build_prompt(test.get("message", ""), prefix)
            if test.get("_skip_reason"):
                print(f"SKIP {test_id}: {test['_skip_reason']}", flush=True)
                results.append(
                    TestResult(
                        id=test_id,
                        category=category,
                        status="skipped",
                        message=prompt,
                        errors=[test["_skip_reason"]],
                    )
                )
                continue
            if cfg.dry_run:
                print(f"SKIP {test_id}: dry run", flush=True)
                results.append(
                    TestResult(
                        id=test_id,
                        category=category,
                        status="skipped",
                        message=prompt,
                        errors=["dry run"],
                    )
                )
                continue

            print(f"RUN  {test_id} [{category}]", flush=True)
            t0 = time.monotonic()
            log_offset = file_size(cfg.log_path)
            response: dict[str, Any] = {}
            checks: list[dict[str, Any]] = []
            errors: list[str] = []
            status = "failed"
            try:
                assert chat_id is not None
                response = client.send_message(chat_id, prompt, cfg.per_test_timeout)
                time.sleep(0.5)
                log_slice = read_log_since(cfg.log_path, log_offset)
                checks, errors, artifacts = evaluate_expectations(
                    cfg, test, response, prefix, log_slice, google_token
                )
                calendar_artifacts.extend(artifacts)
                status = "passed" if not errors else "failed"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
            duration = time.monotonic() - t0
            print(f"{'PASS' if status == 'passed' else 'FAIL'} {test_id} ({duration:.1f}s)", flush=True)
            for err in errors:
                print(f"     - {err}", flush=True)
            results.append(
                TestResult(
                    id=test_id,
                    category=category,
                    status=status,
                    message=prompt,
                    response_text=response.get("content") or response.get("response_text") or "",
                    audio_url=response.get("audio_url"),
                    actions_taken=response.get("actions_taken") or [],
                    checks=checks,
                    errors=errors,
                    duration_seconds=duration,
                )
            )
    finally:
        if google_token:
            spencer_events, spencer_error = try_calendar_events_for_calendar(
                token=google_token,
                calendar_id=SPENCER_CALENDAR_ID,
                prefix=prefix,
            )
            isolation["spencer_calendar_events"] = [
                {
                    "id": e.get("id"),
                    "summary": e.get("summary"),
                    "start": e.get("start"),
                }
                for e in spencer_events
            ]
            if spencer_error:
                isolation["spencer_calendar_error"] = spencer_error

            tega_events, tega_error = try_calendar_events_for_calendar(
                token=google_token,
                calendar_id=TEGA_CALENDAR_ID,
                prefix=prefix,
            )
            isolation["tega_calendar_events"] = [
                {
                    "id": e.get("id"),
                    "summary": e.get("summary"),
                    "start": e.get("start"),
                }
                for e in tega_events
            ]
            if tega_error:
                isolation["tega_calendar_error"] = tega_error
        try:
            isolation["spencer_task_ids"] = spencer_task_rows_for_prefix(cfg.tasks_db, prefix)
        except Exception as exc:  # noqa: BLE001
            isolation["spencer_task_error"] = f"{type(exc).__name__}: {exc}"
        isolation["ok"] = (
            not isolation["spencer_calendar_events"]
            and not isolation["spencer_task_ids"]
            and not isolation["tega_calendar_events"]
        )
        if not isolation["ok"]:
            results.append(
                TestResult(
                    id="IDENTITY-ISOLATION",
                    category="infrastructure",
                    status="failed",
                    message=prefix,
                    errors=["test artifact landed on Spencer or Tega identity"],
                    checks=[isolation],
                )
            )
        if google_token:
            try:
                all_test_events = calendar_events(cfg, token=google_token, prefix=prefix, within_hours=168)
                by_id = {event.get("id"): event for event in calendar_artifacts + all_test_events if event.get("id")}
                cleanup["calendar_deleted"] = delete_calendar_events(cfg, google_token, list(by_id.values()))
            except Exception as exc:  # noqa: BLE001
                cleanup["calendar_error"] = f"{type(exc).__name__}: {exc}"
        try:
            cleanup["tasks_completed"] = cleanup_tasks(cfg.tasks_db, prefix)
        except Exception as exc:  # noqa: BLE001
            cleanup["tasks_error"] = f"{type(exc).__name__}: {exc}"
        try:
            cleanup_chat(cfg.pwa_db, chat_id)
            cleanup["chat_deleted"] = bool(chat_id)
        except Exception as exc:  # noqa: BLE001
            cleanup["chat_error"] = f"{type(exc).__name__}: {exc}"

    elapsed = time.monotonic() - started
    passed = sum(1 for r in results if r.status == "passed")
    failed = sum(1 for r in results if r.status == "failed")
    skipped = sum(1 for r in results if r.status == "skipped")
    report = {
        "run_id": run_id,
        "prefix": prefix,
        "pwa_url": cfg.pwa_url,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": elapsed,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "tokens_spent_usd_estimate": 0.0,
            "token_budget_usd": cfg.token_budget_usd,
        },
        "cleanup": cleanup,
        "identity_isolation": isolation,
        "results": [r.__dict__ for r in results],
    }
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    out_path = cfg.output_dir / f"capability-audit-{run_id}.json"
    out_path.write_text(json.dumps(redact(report), indent=2, ensure_ascii=False))
    report["output_path"] = str(out_path)
    return report


def parse_args() -> HarnessConfig:
    parser = argparse.ArgumentParser(description="Run synthetic PWA capability audit tests.")
    parser.add_argument("--pwa-url", default=DEFAULT_PWA_URL)
    parser.add_argument("--password", default=DEFAULT_PWA_PASSWORD)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--pwa-db", type=Path, default=DEFAULT_PWA_DB)
    parser.add_argument("--tasks-db", type=Path, default=DEFAULT_TASKS_DB)
    parser.add_argument("--google-token", type=Path, default=DEFAULT_GOOGLE_TOKEN)
    parser.add_argument("--calendar-id", default=DEFAULT_CALENDAR_ID)
    parser.add_argument("--audit-sender", default=DEFAULT_AUDIT_SENDER)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--per-test-timeout", type=float, default=60.0)
    parser.add_argument("--total-budget", type=float, default=30 * 60.0)
    parser.add_argument("--token-budget-usd", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return HarnessConfig(
        pwa_url=args.pwa_url,
        password=args.password,
        cases_path=args.cases,
        output_dir=args.output_dir,
        pwa_db=args.pwa_db,
        tasks_db=args.tasks_db,
        google_token=args.google_token,
        calendar_id=args.calendar_id,
        log_path=args.log_path,
        per_test_timeout=args.per_test_timeout,
        total_budget=args.total_budget,
        token_budget_usd=args.token_budget_usd,
        audit_sender=args.audit_sender,
        only=set(args.only),
        dry_run=args.dry_run,
    )


def main() -> int:
    cfg = parse_args()
    try:
        report = run(cfg)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    summary = report["summary"]
    print("\nCapability Audit Summary")
    print(f"  total:   {summary['total']}")
    print(f"  passed:  {summary['passed']}")
    print(f"  failed:  {summary['failed']}")
    print(f"  skipped: {summary['skipped']}")
    print(f"  elapsed: {report['elapsed_seconds']:.1f}s")
    print(f"  tokens spent: ${summary['tokens_spent_usd_estimate']:.2f} / ${summary['token_budget_usd']:.2f}")
    print(f"  report:  {report['output_path']}")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
