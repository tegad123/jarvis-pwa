"""Reusable health-contract framework for Jarvis self-verification.

A *contract* is a set of CHECKS that make assertions about STATE, not liveness.

Design law (learned from real failures on 2026-07-20 — every bug that has bitten
this system exited 0):

  * Assert that work HAPPENED and that the RESULT is plausible, not merely that a
    process did not raise.
  * Silence, not crashes, is the failure mode. A check that cannot PROVE its
    assertion must go RED (FAIL), never green. Unknown state is red state.

This module is deliberately implementation-independent. A check reads observable
state (a Sheet, a file, an API response) and returns a verdict. It never imports
the code it is guarding. That is what makes it a trustworthy witness and what
makes it the reference other skills' contracts follow.

Python 3.9-compatible (the Mini runs 3.9.6). No match statements, no PEP-604
runtime unions.
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import json
import sys
import traceback
from typing import Any, Callable, List, Optional


class Severity(enum.Enum):
    """How loud a *failed* assertion is allowed to be."""

    FAIL = "FAIL"  # contract goes RED -> feeds diagnosis/repair downstream
    WARN = "WARN"  # surfaced and counted, but does not turn the contract red


class Status(enum.Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclasses.dataclass
class Assertion:
    """What a check function returns.

    ok=True  -> the assertion holds -> reported PASS.
    ok=False -> the assertion is violated -> reported at the check's Severity.
    A raised exception is NOT an Assertion; it is converted to FAIL (see below),
    because a check that cannot run cannot warn its way past not knowing.
    """

    ok: bool
    detail: str
    evidence: Optional[Any] = None


@dataclasses.dataclass
class Outcome:
    name: str
    status: Status
    detail: str
    evidence: Optional[Any] = None

    def to_dict(self) -> dict:
        d = {"check": self.name, "status": self.status.value, "detail": self.detail}
        if self.evidence is not None:
            d["evidence"] = self.evidence
        return d


@dataclasses.dataclass
class Check:
    name: str
    severity: Severity
    fn: Callable[[Any], Assertion]  # receives shared ctx, returns Assertion
    description: str = ""

    def run(self, ctx: Any) -> Outcome:
        try:
            a = self.fn(ctx)
        except Exception:
            # A check that raises has NOT proven anything. Do not let it pass
            # silently and do not soften it to WARN: unknown state is red.
            return Outcome(
                self.name,
                Status.FAIL,
                "check raised while evaluating (state unknown)",
                evidence=traceback.format_exc().strip().splitlines()[-6:],
            )
        if a.ok:
            return Outcome(self.name, Status.PASS, a.detail, a.evidence)
        status = Status.FAIL if self.severity is Severity.FAIL else Status.WARN
        return Outcome(self.name, status, a.detail, a.evidence)


@dataclasses.dataclass
class ContractReport:
    contract: str
    outcomes: List[Outcome]

    @property
    def overall(self) -> Status:
        if any(o.status is Status.FAIL for o in self.outcomes):
            return Status.FAIL
        if any(o.status is Status.WARN for o in self.outcomes):
            return Status.WARN
        return Status.PASS

    def counts(self) -> dict:
        c = {"PASS": 0, "WARN": 0, "FAIL": 0}
        for o in self.outcomes:
            c[o.status.value] += 1
        return c

    def to_dict(self) -> dict:
        return {
            "contract": self.contract,
            "overall": self.overall.value,
            "counts": self.counts(),
            "checks": [o.to_dict() for o in self.outcomes],
        }


class Contract:
    """An ordered set of checks that share a context object."""

    def __init__(self, name: str, checks: List[Check]):
        self.name = name
        self.checks = checks

    def run(self, ctx: Any) -> ContractReport:
        return ContractReport(self.name, [c.run(ctx) for c in self.checks])


# --------------------------------------------------------------------------- #
# Reporting + CLI glue shared by every skill's contract.
# --------------------------------------------------------------------------- #

_GLYPH = {Status.PASS: "PASS", Status.WARN: "WARN", Status.FAIL: "FAIL"}


def render_text(report: ContractReport) -> str:
    lines = []
    lines.append("contract: {}".format(report.contract))
    width = max((len(o.name) for o in report.outcomes), default=0)
    for o in report.outcomes:
        lines.append("  [{:>4}] {:<{w}}  {}".format(
            _GLYPH[o.status], o.name, o.detail, w=width))
        if o.evidence is not None and o.status is not Status.PASS:
            ev = o.evidence
            if isinstance(ev, (list, tuple)):
                for item in ev[:20]:
                    lines.append("           - {}".format(item))
                if len(ev) > 20:
                    lines.append("           - ... (+{} more)".format(len(ev) - 20))
            else:
                lines.append("           {}".format(ev))
    c = report.counts()
    lines.append("  overall: {}   ({} pass / {} warn / {} fail)".format(
        report.overall.value, c["PASS"], c["WARN"], c["FAIL"]))
    return "\n".join(lines)


def exit_code(report: ContractReport, warn_as_fail: bool = False) -> int:
    """0 = healthy. 1 = contract red.

    WARN exits 0 by default so known-tolerable state (dirty cells, an
    intentionally unscheduled engine) does not page anyone. The downstream
    Discord verifier can pass warn_as_fail=True to be paged on WARN too.
    """
    if report.overall is Status.FAIL:
        return 1
    if report.overall is Status.WARN and warn_as_fail:
        return 1
    return 0


def run_cli(build_ctx: Callable[[argparse.Namespace], Any],
            contract: Contract,
            selftest: Optional[Callable[[], int]] = None,
            argv: Optional[List[str]] = None) -> int:
    """Standard entry point a skill contract can delegate to.

    build_ctx(args) -> the shared context passed to every check (e.g. the fetched
    Sheet). It is only called for a live run, so --selftest never touches the
    network.
    """
    p = argparse.ArgumentParser(description="Run the {} health contract.".format(contract.name))
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p.add_argument("--strict", action="store_true",
                   help="treat WARN as a non-zero exit (for the daily verifier)")
    if selftest is not None:
        p.add_argument("--selftest", action="store_true",
                       help="run the check logic against fixtures; no network")
    args = p.parse_args(argv)

    if selftest is not None and getattr(args, "selftest", False):
        return selftest()

    ctx = build_ctx(args)
    report = contract.run(ctx)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(render_text(report))
    return exit_code(report, warn_as_fail=args.strict)
