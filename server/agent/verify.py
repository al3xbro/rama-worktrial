"""The `verify_customer_order` tool: one customer PO (plus its SO ack) in, findings out.

This is a fixed pipeline. The order-desk agent calls it and decides what to do with the
result; it cannot change which checks run or what they found.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from . import bundle as bundle_mod
from . import checks, config, db, ingest, judge, ledger, report, resolve, revision
from .models import Extracted, Finding, Requirement, Status

SO_FIELDS = ("order_no", "order_dt", "cust_no", "status", "cust_po", "terms_code", "quote_no", "slsman_1", "create_id")
LINE_FIELDS = ("lin_no", "item_no", "manu_no", "ord_qty", "unit_price", "unit_cost", "vend_no", "cust_itmno", "req_date", "due_date", "status")
CUSTOMER_FIELDS = ("cust_no", "cust_name", "terms_code", "crdt_limit", "ship_via", "fob_code", "cust_addr1", "cust_city", "cust_state",
                   "cust_zip", "cust_cntry", "comments", "ord_notes", "shipinst", "no_partials", "shelf_life", "AS5553_Flg", "coc_st",
                   "prt_coo", "eccn_req", "qc_before_shp", "fa_req")
SUPPLY_FIELDS = ("po_no", "lin_no", "so_lin_no", "vend_no", "item_no", "ord_qty", "qty_recvd", "unit_cost", "due_date", "src_cntry")


def _pick(row: dict | None, fields) -> dict | None:
    return {k: row.get(k) for k in fields} if row else None


def erp_snapshot(b) -> dict:
    """What the checks compared against, kept with the result so a reviewer (or the UI) sees the same data."""
    return {
        "so": _pick(b.so, SO_FIELDS),
        "lines": [_pick(l, LINE_FIELDS) for l in b.so_lines],
        "customer": _pick(b.customer, CUSTOMER_FIELDS),
        "credit": {"open_orders": b.open_orders, "ar_balance": b.ar_balance},
        "quote": _pick(b.quote, ("quote_no", "quote_dt", "valid_days", "terms_code", "ship_via")),
        "supply": [_pick(p, SUPPLY_FIELDS) for p in b.supply],
    }


class VerificationResult(BaseModel):
    order: dict
    recommendation: str
    reasons: list[str]
    counts: dict[str, int]
    checklist: list[dict]
    findings: list[Finding]
    problems: list[str]
    revision: dict | None = None
    report_path: str | None = None
    requirements: list[Requirement] = []  # the customer terms as we read them (the ledger)
    erp: dict | None = None  # snapshot of the ERP data the checks compared against
    run_id: int | None = None  # the state-store run, once recorded

    def for_agent(self, max_items: int = 25) -> dict:
        """A compact view for the order-desk agent: decisions and issues, not every pass."""
        issues = [(i, f) for i, f in enumerate(self.findings) if f.status not in (Status.PASS, Status.INFO, Status.MANUAL)]
        issues.sort(key=lambda x: ({"high": 0, "medium": 1, "low": 2, "info": 3}[x[1].severity], x[1].checklist_ref))
        return {
            "order": self.order,
            "recommendation": self.recommendation,
            "reasons": self.reasons[:10],
            "counts": self.counts,
            "issues": [
                {"index": i, "ref": f.checklist_ref, "title": f.title, "status": f.status.value, "severity": f.severity, "message": f.message,
                 "resolvable_by_agent": resolve.resolvable(f),
                 "evidence": [f"{e.label}: {e.value} ({e.source})" for e in f.evidence][:5]}
                for i, f in issues[:max_items]
            ],
            "carry_forward": sorted({f.title for f in self.findings if f.status == Status.MANUAL}),
            "revision": self.revision,
            "problems": self.problems,
            "report_path": self.report_path,
            "run_id": self.run_id,
        }


def find_ack(so_number: int) -> Path | None:
    """Acks are not stored in the ERP or the mailbox; look in the drop folder by SO number."""
    hits = sorted(config.ACK_DIR.rglob(f"*Acknowledgement-{so_number}.*"))
    return hits[0] if hits else None


def verify(po: Extracted | None, ack: Extracted | None = None, so_number: int | None = None, write: bool = True,
           previous_po: Extracted | None = None, message_id: str | None = None, source: str | None = None,
           references: list | None = None) -> VerificationResult:
    """Verify a customer PO against its SO. If `previous_po` is given, or we have verified an
    earlier version of the same (customer, PO number) before, the result also carries a
    revision section: what changed, and which issues were resolved or are new. `references` are
    documents the PO refers to (its terms), read into the requirements like terms printed in the PO."""
    problems: list[str] = []
    if po and po.doc.header.doc_type != "customer_po":
        problems.append(f"{po.source} does not look like a customer purchase order (classified as {po.doc.header.doc_type}).")
    # ERP reads hold a connection; model calls (ack extraction, clause labelling, supplier PO
    # documents) happen outside it, so parallel agents don't sit on the ERP's 5 connections.
    with db.connect() as conn:
        b = bundle_mod.build(db.Erp(conn), po, ack, so_number)
    if b.so and ack is None:
        path = find_ack(b.so["order_no"])
        if path:
            ack = ingest.extract(ingest.normalize_file(path))
            with db.connect() as conn:
                b = bundle_mod.build(db.Erp(conn), po, ack, b.so["order_no"])
        else:
            problems.append(f"No SO acknowledgement found for SO {b.so['order_no']}; ack-only checks are unverifiable.")
    read: set[str] = set()
    for ref in references or []:
        b.references.append(ref)
        if ref.text not in read:  # one document can hold both the terms and the quality clauses: read it once
            read.add(ref.text)
            b.requirements += ledger.classify(ref.text, ref.source, "customer")
    bundle_mod.enrich(b)
    findings = judge.judge(checks.run(b), po, ack, erp_snapshot(b))
    problems += b.problems
    warnings = (po.warnings if po else []) + (ack.warnings if ack else [])
    decision, reasons = report.recommendation(findings)
    if not b.so:
        decision, reasons = "review", problems or ["SO not found"]
    order = {
        "so_number": b.so_no, "cust_no": b.so["cust_no"] if b.so else None,
        "customer": b.customer["cust_name"] if b.customer else None,
        "cust_po": (po.doc.header.doc_number.raw if po else None) or (b.so["cust_po"] if b.so else None),
        "po_source": po.source if po else None, "ack_source": ack.source if ack else None,
        "as_of": str(b.as_of) if b.as_of else None, "source": source,
        "references": [{"kind": r.kind, "source": r.source, "chars": len(r.text)} for r in b.references],
    }
    result = VerificationResult(order=order, recommendation=decision, reasons=reasons, counts=report.counts(findings),
                                checklist=report.rollup(findings), findings=findings, problems=problems + warnings,
                                requirements=b.requirements, erp=erp_snapshot(b))
    if po and b.so:
        result.revision = _revision(po, b.so["cust_no"], order["cust_po"], previous_po, findings)
    if write:
        run_dir = _write(result, warnings, po, ack)
        result.report_path = str(run_dir / "report.md")
        if po and b.so:
            revision.record(b.so["cust_no"], order["cust_po"], po, run_dir)
        from . import state
        result.run_id = state.add_run(result, message_id)
        # The ERP record is written once the order is settled (erp_write.record_result), not here.
    return result


def _revision(po: Extracted, cust_no: str, cust_po: str, previous_po: Extracted | None, findings: list[Finding]) -> dict | None:
    prev_findings: list[Finding] = []
    how = "given by the caller"
    if previous_po is None:
        earlier = revision.previous_versions(cust_no, cust_po, po.sha256)
        if not earlier:
            return None
        previous_po, prev_findings = revision.load_previous(earlier[0])
        how = f"same customer and PO number as {earlier[0]['source']} verified at {earlier[0]['at']}"
        if previous_po is None:
            return {"detected": True, "how": how, "changes": None, "note": "previous extraction not available"}
    out = {"detected": True, "how": how, "previous_source": previous_po.source, "changes": revision.diff(previous_po, po)}
    if prev_findings:
        out["issues"] = revision.compare_findings(prev_findings, findings)
    return out


def _write(result: VerificationResult, warnings: list[str], po: Extracted | None, ack: Extracted | None) -> Path:
    o = result.order
    name = f"SO{o['so_number']}" if o.get("so_number") else f"PO-{o.get('cust_po') or 'unknown'}"
    out = config.RUNS_DIR / name / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(result.model_dump_json(indent=1))
    if po:
        (out / "po.json").write_text(po.model_dump_json(indent=1))
    if ack:
        (out / "ack.json").write_text(ack.model_dump_json(indent=1))
    md = report.markdown(o, result.findings, result.recommendation, result.reasons, warnings)
    if result.revision:
        md += "\n## Revision\n\n" + f"Detected: {result.revision.get('how')}\n\n"
        for c in result.revision.get("changes") or []:
            md += f"- {c['where']} · {c['field']}: `{c['old']}` → `{c['new']}`\n"
        for k in ("resolved", "new", "still_open"):
            items = (result.revision.get("issues") or {}).get(k) or []
            if items:
                md += f"\n**{k.replace('_', ' ').capitalize()} since the previous version:**\n" + "".join(f"- {i}\n" for i in items)
    (out / "report.md").write_text(md)
    return out


def verify_files(po_path: Path | None, ack_path: Path | None = None, so_number: int | None = None) -> VerificationResult:
    po = ingest.extract(ingest.normalize_file(po_path)) if po_path else None
    ack = ingest.extract(ingest.normalize_file(ack_path)) if ack_path else None
    return verify(po, ack, so_number)


def summary_json(result: VerificationResult) -> str:
    return json.dumps(result.for_agent(), default=str)
