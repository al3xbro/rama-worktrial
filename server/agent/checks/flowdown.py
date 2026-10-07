"""Flow-down: do our supplier POs carry the customer's sourcing requirements?

Evidence is the supplier PO document when we have it (its printed conditions), otherwise the
vendor's standing PO notes in the ERP. The second is weaker, so gaps found that way are
needs_review rather than fail. The same comparison serves the customer-order check (4.5) and
the supplier-PO check (P6).
"""
from __future__ import annotations

from ..bundle import Bundle
from ..models import Finding, Requirement, Status
from . import check, ev, finding, req_evidence


def customer_requirements(reqs: list[Requirement], clause_type: str) -> list[Requirement]:
    return [r for r in reqs if r.clause_type == clause_type and r.scope in ("customer", "order")]


def compare(customer: list[Requirement], supplier: list[Requirement], po_no, from_doc: bool, ref: str, prefix: str = "F") -> list[Finding]:
    """The customer's date-code, CoC, traceability and DFARS/ITAR requirements against our conditions on one supplier PO."""
    date_reqs = customer_requirements(customer, "date_code_max_age")
    coc_reqs = customer_requirements(customer, "coc_required")
    trace_reqs = customer_requirements(customer, "counterfeit_avoidance") + customer_requirements(customer, "traceability")
    defense = customer_requirements(customer, "dfars_flowdown") + customer_requirements(customer, "itar_flowdown")
    packaging = customer_requirements(customer, "packaging")
    basis = f"supplier PO {po_no} document" if from_doc else f"vendor standing PO notes (no PO {po_no} document)"
    gap = Status.FAIL if from_doc else Status.NEEDS_REVIEW
    sup_ev = [ev("Supplier PO clause", r.quote, r.source) for r in supplier] or [ev("Supplier PO clauses", "(none relevant)", basis)]
    out = []

    if date_reqs:
        cust_months = min(r.params.get("months") or 999 for r in date_reqs)
        sup_dates = [r for r in supplier if r.clause_type == "date_code_max_age"]
        title = f"Date code limit flowed down to PO {po_no}"
        if not sup_dates:
            out.append(finding(f"{prefix}.date_code", ref, title, gap,
                               f"The customer requires date codes no older than {cust_months} months; {basis} sets no date-code limit.",
                               req_evidence(date_reqs) + sup_ev, severity="medium"))
        else:
            sup_months = min(r.params.get("months") or 999 for r in sup_dates)
            bases = {r.params.get("basis") for r in sup_dates} | {r.params.get("basis") for r in date_reqs}
            note = " (measured from different dates: " + ", ".join(sorted(x for x in bases if x)) + ")" if len(bases) > 1 else ""
            evidence = req_evidence(date_reqs) + [ev("Supplier PO clause", r.quote, r.source) for r in sup_dates]
            if sup_months > cust_months:
                out.append(finding(f"{prefix}.date_code", ref, title, gap,
                                   f"The customer requires date codes no older than {cust_months} months, but PO {po_no} allows {sup_months} months{note}.",
                                   evidence, severity="high"))
            else:
                out.append(finding(f"{prefix}.date_code", ref, title, Status.PASS, f"PO {po_no} limits date codes to {sup_months} months{note}.", evidence))

    for reqs, kinds, cid, label, what in ((coc_reqs, ("coc_required",), "coc", "Certificate of conformance", "a certificate of conformance"),
                                          (trace_reqs, ("traceability", "counterfeit_avoidance"), "trace", "Traceability / counterfeit avoidance",
                                           "counterfeit avoidance / traceability to the manufacturer"),
                                          (defense, ("dfars_flowdown", "itar_flowdown"), "defense", "DFARS / ITAR", "the DFARS / ITAR flow-down"),
                                          (packaging, ("packaging",), "packaging", "Packaging", "the packaging requirement (e.g. sealed original packaging, dry pack)")):
        if not reqs:
            continue
        has = [r for r in supplier if r.clause_type in kinds]
        title = f"{label} flowed down to PO {po_no}"
        if has:
            out.append(finding(f"{prefix}.{cid}", ref, title, Status.PASS, f"PO {po_no} carries it.",
                               req_evidence(reqs) + [ev("Supplier PO clause", r.quote, r.source) for r in has]))
        else:
            status = gap if cid == "coc" else Status.NEEDS_REVIEW
            out.append(finding(f"{prefix}.{cid}", ref, title, status, f"The customer requires {what}; {basis} does not ask the supplier for it.",
                               req_evidence(reqs) + sup_ev, severity="medium"))
    return out


@check("F.flowdown", "4.5", "Supplier POs flow down the customer's requirements")
def flowdown(b: Bundle):
    out = []
    for po_no in sorted({p["po_no"] for p in b.supply}):
        out += compare(b.requirements, b.supplier_requirements.get(po_no, []), po_no, po_no in b.supplier_pos, "4.5")
    return out
