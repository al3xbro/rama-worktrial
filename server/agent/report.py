"""Roll findings up to Demo's checklist, decide a recommendation, and render reports."""
from __future__ import annotations

from collections import defaultdict

from .checklist import CHECKLIST
from .models import STATUS_ORDER, Finding, Status

ICON = {
    Status.FAIL: "FAIL", Status.NEEDS_APPROVAL: "NEEDS APPROVAL", Status.NEEDS_REVIEW: "NEEDS REVIEW",
    Status.UNVERIFIABLE: "UNVERIFIABLE", Status.MANUAL: "MANUAL", Status.INFO: "INFO", Status.PASS: "PASS",
}


def worst(statuses) -> Status:
    statuses = list(statuses)
    for s in STATUS_ORDER:
        if s in statuses:
            return s
    return Status.PASS


def rollup(findings: list[Finding], checklist=None) -> list[dict]:
    by_ref = defaultdict(list)
    for f in findings:
        by_ref[f.checklist_ref].append(f)
    out = []
    for item in checklist or CHECKLIST:
        fs = by_ref.get(item.ref, [])
        status = worst(f.status for f in fs) if fs else item.default
        out.append({"ref": item.ref, "section": item.section, "text": item.text, "added": item.added,
                    "status": status.value, "findings": len(fs), "automated": bool(fs), "manual_reason": item.manual_reason})
    return out


def recommendation(findings: list[Finding]) -> tuple[str, list[str]]:
    """Policy: fails hold the order, approvals hold it until granted, unknowns go to a person."""
    fails = [f for f in findings if f.status == Status.FAIL and f.severity in ("high", "medium")]
    approvals = [f for f in findings if f.status == Status.NEEDS_APPROVAL]
    # Release only when everything that matters was verified: unread customer terms or a required
    # but missing quote send the order to a person.
    reviews = [f for f in findings if f.status == Status.NEEDS_REVIEW or (f.status == Status.UNVERIFIABLE and f.severity in ("high", "medium"))]
    if fails:
        return "hold", [f"{f.title}: {f.message}" for f in fails]
    if approvals:
        return "needs_approval", [f"{f.title}: {f.message}" for f in approvals]
    if reviews:
        return "review", [f"{f.title}: {f.message}" for f in reviews]
    return "release", ["No failures, approvals or open questions."]


def counts(findings: list[Finding]) -> dict[str, int]:
    c = defaultdict(int)
    for f in findings:
        c[f.status.value] += 1
    return dict(c)


def _evidence_lines(f: Finding) -> list[str]:
    return [f"    - {e.label}: `{e.value}` — {e.source}" for e in f.evidence]


def markdown(header: dict, findings: list[Finding], decision: str, reasons: list[str], warnings: list[str], checklist=None) -> str:
    title = f"SO {header.get('so_number') or '?'} · PO {header.get('cust_po') or '?'} · {header.get('customer') or '?'}"
    out = [f"# Order check: {title}", "", f"**Recommendation: {decision.replace('_', ' ').upper()}**", ""]
    out += [f"- {r}" for r in reasons[:12]] + [""]
    out += [f"Sources: PO `{header.get('po_source')}`, ack `{header.get('ack_source')}`, ERP schema `worktrial`. As of {header.get('as_of')}.", ""]

    issues = [f for f in findings if f.status in (Status.FAIL, Status.NEEDS_APPROVAL, Status.NEEDS_REVIEW)
              or (f.status == Status.UNVERIFIABLE and f.severity in ("high", "medium"))]
    issues.sort(key=lambda f: (STATUS_ORDER.index(f.status), f.checklist_ref, f.line or 0))
    out += ["## Issues", ""]
    if not issues:
        out.append("None.")
    for f in issues:
        out.append(f"- **{ICON[f.status]}** [{f.checklist_ref}] {f.title} — {f.message}")
        out += _evidence_lines(f)
    out += ["", "## Checklist", "", "| Ref | Item | Status | Checks |", "|---|---|---|---|"]
    for row in rollup(findings, checklist):
        auto = row["findings"] if row["automated"] else f"not automated: {row['manual_reason'] or 'no check applies to this order'}"
        out.append(f"| {row['ref']} | {row['text']}{' *(added)*' if row['added'] else ''} | {row['status'].replace('_', ' ')} | {auto} |")
    carry = [f for f in findings if f.status == Status.MANUAL]
    if carry:
        out += ["", "## Carry-forward requirements", ""]
        for f in carry:
            out.append(f"- {f.title} — {f.message}")
            out += _evidence_lines(f)
    low = [f for f in findings if f.status in (Status.UNVERIFIABLE, Status.INFO) and f not in issues]
    if low:
        out += ["", "## Notes", ""]
        for f in low:
            out.append(f"- [{f.checklist_ref}] {f.title} — {f.message}")
    passed = [f for f in findings if f.status == Status.PASS]
    out += ["", f"## Passed ({len(passed)})", ""]
    for f in passed:
        out.append(f"- [{f.checklist_ref}] {f.title} — {f.message}")
    if warnings:
        out += ["", "## Extraction warnings", ""] + [f"- {w}" for w in warnings]
    return "\n".join(out) + "\n"
