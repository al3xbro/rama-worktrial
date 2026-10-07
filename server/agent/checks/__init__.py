"""The check registry and runner.

Every registered check runs on every order, in registration order. Then each clause type in
the order's requirements ledger is handed to its verifier. No model decides what runs.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from ..bundle import Bundle
from ..models import Evidence, Finding, Requirement, Status


@dataclass
class Check:
    id: str
    ref: str
    title: str
    fn: Callable[[Bundle], list[Finding]]


CHECKS: list[Check] = []
VERIFIERS: dict[str, Callable[[str, list[Requirement], Bundle], list[Finding]]] = {}


def check(id: str, ref: str, title: str):
    def deco(fn):
        CHECKS.append(Check(id, ref, title, fn))
        return fn
    return deco


def verifier(*clause_types: str):
    def deco(fn):
        for ct in clause_types:
            VERIFIERS[ct] = fn
        return fn
    return deco


def finding(check_id: str, ref: str, title: str, status: Status, message: str, evidence=(), severity="medium", line=None) -> Finding:
    if status in (Status.PASS, Status.INFO):
        severity = "info"
    return Finding(check_id=check_id, checklist_ref=ref, title=title, status=status, severity=severity,
                   message=message, evidence=list(evidence), line=line)


def ev(label: str, value, source: str) -> Evidence:
    return Evidence(label=label, value=None if value is None else str(value), source=source)


def req_evidence(reqs: list[Requirement]) -> list[Evidence]:
    return [ev("Clause", r.quote, r.source) for r in reqs]


def run(b: Bundle) -> list[Finding]:
    findings: list[Finding] = []
    for c in CHECKS:
        try:
            findings += c.fn(b) or []
        except Exception as e:  # a broken check must show up, not vanish
            findings.append(finding(c.id, c.ref, c.title, Status.UNVERIFIABLE, f"check failed to run: {e!r}", severity="high"))
    groups: dict[str, list[Requirement]] = defaultdict(list)
    for r in b.requirements:
        groups[r.clause_type].append(r)
    for clause_type, reqs in groups.items():
        fn = VERIFIERS.get(clause_type, VERIFIERS["__unknown__"])
        try:
            findings += fn(clause_type, reqs, b) or []
        except Exception as e:
            findings.append(finding(f"R.{clause_type}", "3.2", f"Requirement: {clause_type}", Status.UNVERIFIABLE,
                                    f"verifier failed to run: {e!r}", req_evidence(reqs), severity="high"))
    return findings


# Registration happens on import.
from . import header, lines, account, requirements, supply, flowdown  # noqa: E402,F401
