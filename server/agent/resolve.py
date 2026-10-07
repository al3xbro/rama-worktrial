"""The order-desk agent resolving review items after investigating them.

Only items the code could not settle are open to the agent: `needs_review` and `unverifiable`.
Code-verified failures and approvals that policy reserves for a person are not. A verdict needs
evidence the agent actually saw: every quoted piece of evidence must appear in the verification
result, the order documents, or the output of a tool the agent called while handling this email.
The verdict changes the item's status (met -> pass, not met -> fail, can't tell -> unchanged), the
recommendation is recalculated, and the item stays marked as resolved by the agent.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import parse, report, state
from .models import Finding, Status

RESOLVABLE = {Status.NEEDS_REVIEW, Status.UNVERIFIABLE}
VERDICTS = {"met": Status.PASS, "not_met": Status.FAIL, "cannot_tell": None}
# Items that ask a person to accept a term, not to establish a fact. The agent can add what it found
# (cannot_tell), but the decision is the person's.
ACCEPTANCE = {"R.governing_law": "legal", "R.payment_fees": "sales"}


def resolvable(f: Finding) -> bool:
    return f.status in RESOLVABLE and f.check_id not in ACCEPTANCE


def _result_path(run_id: int) -> Path:
    run = state.get_run(run_id)
    if not run or not run.get("report_path"):
        raise ValueError(f"run {run_id} not found")
    return Path(run["report_path"]).with_name("result.json")


def _corpus(folder: Path, result: dict) -> str:
    """Everything a quote may come from that the verification itself holds: findings' evidence, the
    customer's terms, the ERP snapshot, and the order documents."""
    parts = [json.dumps(result.get("findings", [])), json.dumps(result.get("requirements", [])), json.dumps(result.get("erp"), default=str)]
    for name in ("po.json", "ack.json"):
        p = folder / name
        if p.exists():
            parts.append(p.read_text())
    return parse.squash(" ".join(parts))


def resolve(run_id: int, index: int, verdict: str, reasoning: str, evidence: list[dict], seen: list[str]) -> dict:
    """Apply the agent's verdict on one finding. `seen` holds the outputs of tools the agent called."""
    if verdict not in VERDICTS:
        return {"error": f"verdict must be one of {sorted(VERDICTS)}"}
    path = _result_path(run_id)
    result = json.loads(path.read_text())
    findings = [Finding.model_validate(f) for f in result["findings"]]
    if not 0 <= index < len(findings):
        return {"error": f"no finding {index} in run {run_id}"}
    f = findings[index]
    if f.status not in RESOLVABLE:
        return {"error": f"finding {index} is {f.status.value}: only needs_review and unverifiable items can be resolved by the agent; "
                         "failures, approvals and items already settled stay as they are"}
    current = f.original_status or f.status
    if f.check_id in ACCEPTANCE and verdict != "cannot_tell":
        return {"error": f"finding {index} asks a person ({ACCEPTANCE[f.check_id]}) to accept a term; it is not a fact you can settle. "
                         "Use cannot_tell to add what you found, and say in the reply who has to accept it."}
    if verdict != "cannot_tell" and not evidence:
        return {"error": "a met / not_met verdict needs evidence: quote the exact text or values you relied on, with where they came from"}
    corpus = _corpus(path.parent, result) + parse.squash(" ".join(seen))
    unseen = [e.get("quote", "") for e in evidence if parse.squash(e.get("quote", "")) not in corpus]
    if unseen:
        return {"error": "these quotes do not appear in the order documents, the verification result or your tool outputs, "
                         f"so the verdict was not applied: {unseen}. Quote exactly what you saw."}
    new_status = VERDICTS[verdict]
    if current == Status.UNVERIFIABLE and new_status == Status.FAIL:
        # Missing evidence is not proof of a problem: the agent can clear a can't-verify item by finding the
        # evidence, but cannot turn it into a failure. A person decides, with the agent's view attached.
        new_status = Status.NEEDS_REVIEW
    f.original_status = current
    f.resolution = {"by": "agent", "verdict": verdict, "reasoning": reasoning, "evidence": evidence,
                    "at": datetime.now().isoformat(timespec="seconds"), "previous": f.resolution}
    if new_status is not None:
        f.status = new_status
        if new_status == Status.FAIL and f.severity in ("low", "info"):
            f.severity = "medium"
    findings[index] = f
    decision, reasons = report.recommendation(findings)
    if result.get("order", {}).get("kind") == "supplier":
        from .supplier import SUPPLIER_CHECKLIST as checklist
    else:
        from .checklist import CHECKLIST as checklist
    result["findings"] = [x.model_dump(mode="json") for x in findings]
    result["recommendation"], result["reasons"] = decision, reasons
    result["counts"] = report.counts(findings)
    result["checklist"] = report.rollup(findings, checklist)
    path.write_text(json.dumps(result, indent=1, default=str))
    state.update_run(run_id, decision, result["counts"], findings)
    return {"applied": True, "finding": f.title, "status": f.status.value, "recommendation": decision}
