"""The supplier side: our purchase orders to suppliers, and the suppliers' acknowledgements back.

`verify_supplier_order` is the counterpart of `verify_customer_order`. It checks our PO to a
supplier against the ERP PO, against the sales order lines it buys for and that customer's terms,
and against the vendor's record. When the supplier's acknowledgement is available it also checks
what the supplier confirmed (part, quantity, price, date, origin, date code) against what we
ordered and what the customer needs. Same building blocks: facts with provenance, code
comparisons, a checklist rollup, the same recommendation policy.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from . import config, db, erp, ingest, ledger, parse, report
from .checklist import Item
from .checks import ev, finding, req_evidence
from .checks import flowdown as fd
from .models import DocLine, Extracted, Finding, Requirement, Status

SUPPLIER_CHECKLIST = [
    Item("P1", "Supplier PO", "The PO document matches the ERP purchase order (vendor, terms, lines)."),
    Item("P2", "Supplier PO", "Every PO line buys for a sales order line, with the same part and manufacturer and the right quantity."),
    Item("P3", "Supplier PO", "Due dates meet the customer's required dates."),
    Item("P4", "Supplier PO", "Our cost matches the SO cost, and the order still makes a margin."),
    Item("P5", "Supplier PO", "The vendor is in good standing, the PO uses the vendor's terms, and parts are bought new."),
    Item("P6", "Supplier PO", "The customer's requirements are flowed down (date code, CoC, traceability, DFARS/ITAR)."),
    Item("P7", "Supplier PO", "The vendor's standing PO notes are on the PO."),
    Item("P8", "Supplier PO", "Country of origin is acceptable to the customer."),
    Item("P9", "Supplier acknowledgement", "The supplier confirmed exactly what we ordered (part, quantity, price), by our due date.",
         default=Status.UNVERIFIABLE, manual_reason="no supplier acknowledgement received yet"),
    Item("P10", "Supplier acknowledgement", "What the supplier confirmed (dates, origin, date code) still meets the customer's order.",
         default=Status.UNVERIFIABLE, manual_reason="no supplier acknowledgement received yet"),
]


def _d(x) -> Decimal | None:
    return None if x is None else parse.num(str(x))


def _day(x) -> date | None:
    if x is None:
        return None
    return x.date() if isinstance(x, datetime) else x


@dataclass
class SupplierBundle:
    po: Extracted | None
    ack: Extracted | None
    po_no: int | None = None
    hedr: dict | None = None
    lines: list[dict] = field(default_factory=list)
    vendor: dict | None = None
    sos: dict = field(default_factory=dict)  # so_no -> {"hedr", "lines", "customer", "requirements"}
    conditions: list[Requirement] = field(default_factory=list)  # our conditions printed on the PO
    vendor_notes: list[Requirement] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def src(self, which: str, v) -> str:
        x = self.po if which == "PO" else self.ack
        if x is None:
            return which
        loc = ""
        if v is not None and getattr(v, "loc", None):
            loc = (f" p{v.loc.page}" if v.loc.page else "") + (f" · {v.loc.where}" if v.loc.where else "")
        return f"{x.source}{loc}"

    def so_line(self, so_no, lin_no) -> dict | None:
        s = self.sos.get(so_no)
        return next((l for l in s["lines"] if l["lin_no"] == lin_no), None) if s else None

    @property
    def requirements(self) -> list[Requirement]:
        return [r for s in self.sos.values() for r in s["requirements"]]


def _po_number(po: Extracted | None, ack: Extracted | None, po_no: int | None) -> int | None:
    if po_no:
        return po_no
    for raw in ((po.doc.header.doc_number.raw if po else None), (ack.doc.header.customer_po_ref.raw if ack else None)):
        n = parse.account_number(raw or "") or re.sub(r"\D", "", raw or "")
        if n and n.isdigit():
            return int(n)
    return None


def find_po_document(po_no: int) -> Path | None:
    hits = sorted(config.ACK_DIR.rglob(f"Purchase Order {po_no}.*"))
    return hits[0] if hits else None


def _customer_po_doc(so_no: int) -> Extracted | None:
    """The customer's own PO from the latest verification of that SO, for its order-level clauses."""
    from . import state
    for run in state.runs_for_order(so_no):
        if run.get("report_path"):
            p = Path(run["report_path"]).with_name("po.json")
            if p.exists():
                return Extracted.model_validate_json(p.read_text())
    return None


def build(po: Extracted | None, ack: Extracted | None, po_no: int | None) -> SupplierBundle:
    b = SupplierBundle(po=po, ack=ack, po_no=_po_number(po, ack, po_no))
    if not b.po_no:
        b.problems.append("no supplier PO number found")
        return b
    with db.connect() as conn:
        e = db.Erp(conn)
        b.hedr = e.row("select * from po_hedr where po_no = %s", (b.po_no,))
        if not b.hedr:
            b.problems.append(f"PO {b.po_no} not found in the ERP")
            return b
        b.lines = erp.po_lines(e, b.po_no)
        b.vendor = erp.vendor(e, b.hedr["vend_no"])
        so_nos = {l["so_order_no"] for l in b.lines if l.get("so_order_no")}
        printed = parse.account_number(po.doc.header.customer_po_ref.raw or "") if po else None
        if printed and printed.isdigit():
            so_nos.add(int(printed))
        for so_no in sorted(so_nos):
            hedr = erp.sales_order(e, so_no)
            if hedr:
                b.sos[so_no] = {"hedr": hedr, "lines": erp.so_lines(e, so_no), "customer": erp.customer(e, hedr["cust_no"]),
                                "items": erp.items(e, [(l["item_no"], l["manu_no"]) for l in b.lines])}
    # Model steps (clause labelling) after the ERP connection is released.
    for so_no, s in b.sos.items():
        reqs = ledger.from_customer(s["customer"]) if s["customer"] else []
        cust_po = _customer_po_doc(so_no)
        if cust_po:
            reqs += ledger.from_document(cust_po)
        s["requirements"] = ledger.llm_label(reqs)
    if po:
        conds = []
        for v in po.doc.notes:
            conds += ledger.classify(v.raw or "", f"{po.source} · purchase conditions", "distributor")
        b.conditions = ledger.llm_label(conds)
    if b.vendor and b.vendor.get("po_notes"):
        b.vendor_notes = ledger.classify(b.vendor["po_notes"], f"vendor.po_notes ({b.vendor['vend_no']})", "distributor")
    return b


# ---------------------------------------------------------------- checks


def _doc_line_for(doc: Extracted | None, l: dict) -> DocLine | None:
    if not doc:
        return None
    by_no = {int(n): x for x in doc.doc.lines if (n := parse.num(x.line_no.raw)) is not None}
    if l["lin_no"] in by_no and parse.mpn_key(by_no[l["lin_no"]].mpn.raw) == parse.mpn_key(l["item_no"]):
        return by_no[l["lin_no"]]
    cands = [x for x in doc.doc.lines if parse.mpn_key(x.mpn.raw) == parse.mpn_key(l["item_no"])]
    cands.sort(key=lambda x: parse.num(x.qty.raw) != _d(l["ord_qty"]))
    return cands[0] if cands else None


def _cmp(cid, ref, title, vals, severity="high", line=None):
    present = [v for v in vals if v[1] is not None]
    evidence = [ev(lbl, shown, src) for lbl, _, shown, src in vals]
    if len(present) < 2:
        return finding(cid, ref, title, Status.UNVERIFIABLE, "Not enough sources to compare.", evidence, severity="low", line=line)
    if len({v[1] for v in present}) == 1:
        return finding(cid, ref, title, Status.PASS, f"Same on {', '.join(v[0] for v in present)}.", evidence, line=line)
    return finding(cid, ref, title, Status.FAIL, "Mismatch: " + "; ".join(f"{v[0]} {v[2]}" for v in present) + ".", evidence, severity=severity, line=line)


def check_document(b: SupplierBundle) -> list[Finding]:
    """P1: our PO document against the ERP purchase order."""
    if not (b.po and b.hedr):
        return [finding("P.doc", "P1", "PO document matches the ERP", Status.UNVERIFIABLE, "No PO document to compare.", severity="low")] if b.hedr else []
    h, out = b.po.doc.header, []
    out.append(_cmp("P.terms", "P1", "Payment terms on the PO", [
        ("PO document", parse.terms_code(h.payment_terms.raw), h.payment_terms.raw, b.src("PO", h.payment_terms)),
        ("ERP PO", b.hedr["terms_code"], b.hedr["terms_code"], f"po_hedr.terms_code (PO {b.po_no})")], severity="medium"))
    if b.vendor:
        doc_v, erp_v = parse.company_key(h.addressed_to.raw), parse.company_key(b.vendor.get("po_name"))
        same = bool(doc_v and erp_v and (doc_v == erp_v or doc_v.startswith(erp_v) or erp_v.startswith(doc_v)))
        out.append(finding("P.vendor_name", "P1", "Vendor on the PO", Status.PASS if same else Status.FAIL,
                           "Same vendor." if same else f"The PO is addressed to {h.addressed_to.raw!r}; the ERP PO is with {b.vendor.get('po_name')!r}.",
                           [ev("PO document", h.addressed_to.raw, b.src("PO", h.addressed_to)), ev("ERP vendor", f"{b.hedr['vend_no']} {b.vendor.get('po_name')}", "vendor.po_name")],
                           severity="high"))
    comma = parse.detect_decimal_comma([(l.qty.raw, l.unit_price.raw, l.extended.raw) for l in b.po.doc.lines])
    for l in b.lines:
        dl = _doc_line_for(b.po, l)
        n = l["lin_no"]
        if not dl:
            out.append(finding("P.doc_line", "P1", f"ERP line {n} on the PO document", Status.FAIL,
                               f"ERP PO line {n} ({l['item_no']} x {l['ord_qty']}) is not on the PO document.", severity="high", line=n))
            continue
        src = f"po_detl (PO {b.po_no} line {n})"
        out.append(_cmp("P.doc_qty", "P1", f"Quantity on the PO (line {n})", [("PO document", parse.num(dl.qty.raw, comma), dl.qty.raw, b.src("PO", dl.qty)),
                        ("ERP PO", _d(l["ord_qty"]), l["ord_qty"], src + " ord_qty")], line=n))
        out.append(_cmp("P.doc_cost", "P1", f"Unit cost on the PO (line {n})", [("PO document", parse.num(dl.unit_price.raw, comma), dl.unit_price.raw, b.src("PO", dl.unit_price)),
                        ("ERP PO", _d(l["unit_cost"]), l["unit_cost"], src + " unit_cost")], line=n))
        out.append(_cmp("P.doc_due", "P1", f"Due date on the PO (line {n})", [("PO document", parse.to_date(dl.required_date.raw), dl.required_date.raw, b.src("PO", dl.required_date)),
                        ("ERP PO", _day(l.get("due_date")), _day(l.get("due_date")), src + " due_date")], severity="medium", line=n))
    return out


def check_lines(b: SupplierBundle) -> list[Finding]:
    """P2-P4, P8: each PO line against the sales order line it buys for."""
    out = []
    for l in b.lines:
        n, src = l["lin_no"], f"po_detl (PO {b.po_no} line {l['lin_no']})"
        value = (_d(l["ord_qty"]) or 0) * (_d(l["unit_cost"]) or 0)
        if not l.get("so_order_no"):
            out.append(finding("P.unrequested", "P2", f"PO line {n} not tied to an order", Status.NEEDS_REVIEW,
                               f"Line {n} buys {l['ord_qty']:,} x {l['item_no']} ({l['manu_no']}), worth ${value:,.2f}, and no sales order line needs it.",
                               [ev("PO line", f"{l['item_no']} x {l['ord_qty']} @ {l['unit_cost']}", src)], severity="medium", line=n))
            continue
        s = b.so_line(l["so_order_no"], l["so_lin_no"])
        if not s:
            out.append(finding("P.so_line", "P2", f"Sales order line for PO line {n}", Status.FAIL,
                               f"PO line {n} points to SO {l['so_order_no']} line {l['so_lin_no']}, which does not exist.", severity="high", line=n))
            continue
        so_src = f"ord_detl (SO {l['so_order_no']} line {l['so_lin_no']})"
        tag = f"line {n} → SO {l['so_order_no']}/{l['so_lin_no']}"
        out.append(_cmp("P.part", "P2", f"Part number ({tag})", [("PO", l["item_no"], l["item_no"], src), ("SO", s["item_no"], s["item_no"], so_src)], line=n))
        out.append(_cmp("P.mfr", "P2", f"Manufacturer ({tag})", [("PO", l["manu_no"], l["manu_no"], src), ("SO", s["manu_no"], s["manu_no"], so_src)], line=n))
        pq, sq = _d(l["ord_qty"]), _d(s["ord_qty"])
        qty_ev = [ev("PO qty", l["ord_qty"], src), ev("SO qty", s["ord_qty"], so_src)]
        if pq == sq:
            out.append(finding("P.qty", "P2", f"Quantity ({tag})", Status.PASS, "Buys exactly what the SO line needs.", qty_ev, line=n))
        elif pq < sq:
            out.append(finding("P.qty", "P2", f"Quantity ({tag})", Status.INFO, f"Buys {pq:,} of the {sq:,} needed; the rest must come from stock or another PO.", qty_ev, line=n))
        else:
            out.append(finding("P.qty", "P2", f"Quantity ({tag})", Status.NEEDS_REVIEW, f"Buys {pq:,}, more than the {sq:,} the SO line needs.", qty_ev, severity="medium", line=n))
        due, need = _day(l.get("due_date")), _day(s.get("req_date"))
        date_ev = [ev("PO due", due, src + " due_date"), ev("Customer required", need, so_src + " req_date")]
        if due and need:
            ok = due <= need
            out.append(finding("P.due", "P3", f"Due date meets the customer's date ({tag})", Status.PASS if ok else Status.FAIL,
                               f"Due {due}, customer needs {need}." if ok else f"Due {due}, after the customer's required date {need} ({(due - need).days} days late).",
                               date_ev, severity="high", line=n))
        pc, sc, sp = _d(l["unit_cost"]), _d(s["unit_cost"]), _d(s["unit_price"])
        cost_ev = [ev("PO cost", pc, src + " unit_cost"), ev("SO cost", sc, so_src + " unit_cost"), ev("SO price", sp, so_src + " unit_price")]
        if pc is not None and sc is not None and pc != sc:
            out.append(finding("P.cost", "P4", f"Cost matches the SO ({tag})", Status.FAIL,
                               f"PO buys at {pc} but the SO was costed at {sc}" + (f"; SO line vendor {s.get('vend_no')}, PO vendor {b.hedr['vend_no']}" if s.get("vend_no") != b.hedr["vend_no"] else "") + ".",
                               cost_ev, severity="medium", line=n))
        else:
            out.append(finding("P.cost", "P4", f"Cost matches the SO ({tag})", Status.PASS, "PO cost equals the SO cost.", cost_ev, line=n))
        if pc is not None and sp is not None and sp <= pc:
            out.append(finding("P.margin", "P4", f"Margin ({tag})", Status.FAIL, f"SO price {sp} does not cover the PO cost {pc}.", cost_ev, severity="high", line=n))
        coo = l.get("src_cntry")
        reqs = [r for r in b.sos[l["so_order_no"]]["requirements"] if r.clause_type == "prohibited_origin"]
        if reqs and coo:
            banned = {c.upper() for r in reqs for c in r.params.get("countries", [])}
            exception = any(r.params.get("exception") for r in reqs)
            hit = coo.upper() in banned
            out.append(finding("P.origin", "P8", f"Country of origin ({tag})",
                               (Status.NEEDS_APPROVAL if exception else Status.FAIL) if hit else Status.PASS,
                               f"Origin {coo}" + (": the customer needs to approve this line in writing." if hit and exception else (", which the customer does not accept." if hit else ".")),
                               req_evidence(reqs) + [ev("Origin", coo, src + " src_cntry")], severity="high" if hit else "info", line=n))
    return out


def check_vendor(b: SupplierBundle) -> list[Finding]:
    """P5: vendor standing, terms, new parts."""
    if not (b.vendor and b.hedr):
        return []
    v, out = b.vendor, []
    vev = [ev("Vendor", f"{v['vend_no']} {v.get('po_name')}", f"vendor ({v['vend_no']})")]
    po_dt = _day(b.hedr.get("po_dt"))
    if v.get("pay_hold"):
        out.append(finding("P.vendor_hold", "P5", "Vendor payment hold", Status.NEEDS_REVIEW, f"{v.get('po_name')} is on payment hold.", vev, severity="medium"))
    exp = _day(v.get("iso_cert_exp"))
    if exp and po_dt and exp < po_dt:
        out.append(finding("P.vendor_cert", "P5", "Vendor certification", Status.NEEDS_REVIEW, f"{v.get('iso_cert') or 'ISO'} certificate expired {exp}, before the PO date {po_dt}.",
                           vev + [ev("Cert expiry", exp, "vendor.iso_cert_exp")], severity="medium"))
    out.append(_cmp("P.vendor_terms", "P5", "PO uses the vendor's terms", [("PO", b.hedr["terms_code"], b.hedr["terms_code"], f"po_hedr.terms_code (PO {b.po_no})"),
                    ("Vendor", v.get("terms_code"), v.get("terms_code"), f"vendor.terms_code ({v['vend_no']})")], severity="low"))
    used = [l for l in b.lines if (l.get("condition") or "NEW").upper() != "NEW"]
    out.append(finding("P.condition", "P5", "Parts bought new", Status.FAIL if used else Status.PASS,
                       ("Lines bought in another condition: " + ", ".join(f"{l['lin_no']} ({l['condition']})" for l in used)) if used else "All lines are bought new.",
                       [ev("Condition", l.get("condition"), f"po_detl.condition (PO {b.po_no} line {l['lin_no']})") for l in b.lines], severity="high"))
    if not out or all(f.status == Status.PASS for f in out if f.check_id in ("P.vendor_hold", "P.vendor_cert")):
        pass
    return out


def check_flowdown(b: SupplierBundle) -> list[Finding]:
    """P6: the customer's requirements on our PO; P7: the vendor's standing notes on our PO."""
    out = []
    if b.sos:
        conditions = b.conditions if b.po else b.vendor_notes
        out += fd.compare(b.requirements, conditions, b.po_no, from_doc=b.po is not None, ref="P6", prefix="P.flow")
    if b.po and b.vendor_notes:
        printed = [parse.squash(n.raw) for n in b.po.doc.notes]
        printed_types = {r.clause_type for r in b.conditions}
        for r in b.vendor_notes:
            covered = (r.clause_type != "unclassified" and r.clause_type in printed_types) or any(
                difflib.SequenceMatcher(None, parse.squash(r.quote), p).ratio() > 0.6 for p in printed)
            out.append(finding("P.vendor_note", "P7", "Vendor's standing PO note on the PO", Status.PASS if covered else Status.NEEDS_REVIEW,
                               ("On the PO: " if covered else "Not on the PO: ") + repr(r.quote),
                               [ev("Vendor note", r.quote, r.source)] + [ev("PO condition", n.raw, b.src("PO", n)) for n in b.po.doc.notes][:4],
                               severity="medium"))
    return out


def _date_code_age_months(raw: str | None, as_of: date) -> int | None:
    """'2318' / '23+' / '2023' as year-week; months between that and `as_of`."""
    if not raw:
        return None
    m = re.search(r"\b(\d{2})(\d{2})\b", raw)
    if m and 1 <= int(m.group(2)) <= 53:
        year, week = 2000 + int(m.group(1)), int(m.group(2))
        d = date.fromisocalendar(year, week, 1)
        return (as_of.year - d.year) * 12 + (as_of.month - d.month)
    return None


def pair_ack_lines(b: SupplierBundle) -> dict[int, DocLine]:
    """Pair each PO line with one acknowledgement line, one-to-one. First the same line number when the
    part is the same or similar (a similar part on the same line is a substitution), then the same part
    number among what is left."""
    ack_lines = list(b.ack.doc.lines) if b.ack else []
    free = list(ack_lines)
    pairs: dict[int, DocLine] = {}
    for l in b.lines:
        same_no = [x for x in free if (k := parse.num(x.line_no.raw)) is not None and int(k) == l["lin_no"]]
        for x in same_no:
            sim = difflib.SequenceMatcher(None, parse.mpn_key(x.mpn.raw) or "", parse.mpn_key(l["item_no"]) or "").ratio()
            if sim >= 0.7:
                pairs[l["lin_no"]] = x
                free.remove(x)
                break
    for l in b.lines:
        if l["lin_no"] in pairs:
            continue
        cands = [x for x in free if parse.mpn_key(x.mpn.raw) == parse.mpn_key(l["item_no"])]
        cands.sort(key=lambda x: parse.num(x.qty.raw) != _d(l["ord_qty"]))
        if cands:
            pairs[l["lin_no"]] = cands[0]
            free.remove(cands[0])
    return pairs


def check_ack(b: SupplierBundle) -> list[Finding]:
    """P9-P10: the supplier's acknowledgement against our PO and the customer's needs."""
    if not b.ack:
        return []
    pairs = pair_ack_lines(b)
    out, h = [], b.ack.doc.header
    out.append(_cmp("A.po_ref", "P9", "Acknowledgement references our PO", [
        ("Supplier ack", (parse.account_number(h.customer_po_ref.raw or "") or (h.customer_po_ref.raw or "").strip() or None), h.customer_po_ref.raw, b.src("Ack", h.customer_po_ref)),
        ("Our PO", str(b.po_no), b.po_no, "po_hedr.po_no")]))
    if h.payment_terms.raw:
        out.append(_cmp("A.terms", "P9", "Confirmed payment terms", [("Supplier ack", parse.terms_code(h.payment_terms.raw), h.payment_terms.raw, b.src("Ack", h.payment_terms)),
                        ("Our PO", b.hedr["terms_code"] if b.hedr else None, b.hedr["terms_code"] if b.hedr else None, f"po_hedr.terms_code (PO {b.po_no})")], severity="medium"))
    comma = parse.detect_decimal_comma([(l.qty.raw, l.unit_price.raw, l.extended.raw) for l in b.ack.doc.lines])
    ack_date = parse.to_date(h.doc_date.raw) or date.today()
    matched = set()
    for l in b.lines:
        n, src = l["lin_no"], f"po_detl (PO {b.po_no} line {l['lin_no']})"
        al = pairs.get(n)
        if al is None:
            out.append(finding("A.missing", "P9", f"PO line {n} confirmed", Status.FAIL, f"The supplier did not confirm line {n} ({l['item_no']}).",
                               [ev("Our PO", f"{l['item_no']} x {l['ord_qty']}", src)], severity="high", line=n))
            continue
        if parse.mpn_key(al.mpn.raw) != parse.mpn_key(l["item_no"]):
            no_subs = any(r.clause_type == "no_substitutions" for r in b.vendor_notes + b.conditions)
            out.append(finding("A.part", "P9", f"Part confirmed (line {n})", Status.FAIL,
                               f"The supplier confirmed {al.mpn.raw!r} instead of {l['item_no']!r}: a substitution" + (", which the vendor terms forbid without our written approval." if no_subs else "."),
                               [ev("Supplier ack", al.mpn.raw, b.src("Ack", al.mpn)), ev("Our PO", l["item_no"], src + " item_no")], severity="high", line=n))
        else:
            out.append(finding("A.part", "P9", f"Part confirmed (line {n})", Status.PASS, "Same part number.",
                               [ev("Supplier ack", al.mpn.raw, b.src("Ack", al.mpn)), ev("Our PO", l["item_no"], src + " item_no")], line=n))
        matched.add(id(al))
        out.append(_cmp("A.qty", "P9", f"Quantity confirmed (line {n})", [("Supplier ack", parse.num(al.qty.raw, comma), al.qty.raw, b.src("Ack", al.qty)),
                        ("Our PO", _d(l["ord_qty"]), l["ord_qty"], src + " ord_qty")], line=n))
        out.append(_cmp("A.price", "P9", f"Price confirmed (line {n})", [("Supplier ack", parse.num(al.unit_price.raw, comma), al.unit_price.raw, b.src("Ack", al.unit_price)),
                        ("Our PO", _d(l["unit_cost"]), l["unit_cost"], src + " unit_cost")], line=n))
        promised = parse.to_date(al.ship_date.raw) or parse.to_date(al.required_date.raw)
        due = _day(l.get("due_date"))
        pv = al.ship_date if al.ship_date.raw else al.required_date
        if promised and due:
            late = promised > due
            out.append(finding("A.date", "P9", f"Confirmed date meets our due date (line {n})", Status.FAIL if late else Status.PASS,
                               f"Supplier confirms {promised}, after our due date {due}." if late else f"Confirmed {promised}, due {due}.",
                               [ev("Supplier ack", pv.raw, b.src("Ack", pv)), ev("Our PO due", due, src + " due_date")], severity="medium", line=n))
        s = b.so_line(l.get("so_order_no"), l.get("so_lin_no")) if l.get("so_order_no") else None
        if s and promised:
            need = _day(s.get("req_date"))
            if need:
                late = promised > need
                out.append(finding("A.customer_date", "P10", f"Confirmed date meets the customer's date (line {n} → SO {l['so_order_no']}/{l['so_lin_no']})",
                                   Status.FAIL if late else Status.PASS,
                                   f"Supplier confirms {promised}, after customer's required {need}: SO {l['so_order_no']} line {l['so_lin_no']} will be late." if late else f"Confirmed {promised}, customer needs {need}.",
                                   [ev("Supplier ack", pv.raw, b.src("Ack", pv)), ev("Customer required", need, f"ord_detl.req_date (SO {l['so_order_no']} line {l['so_lin_no']})")],
                                   severity="high", line=n))
        if al.country_of_origin.raw:
            coo = al.country_of_origin.raw.strip().upper()[:2]
            out.append(_cmp("A.origin", "P10", f"Confirmed origin (line {n})", [("Supplier ack", coo, al.country_of_origin.raw, b.src("Ack", al.country_of_origin)),
                            ("Our PO", (l.get("src_cntry") or "").upper() or None, l.get("src_cntry"), src + " src_cntry")], severity="medium", line=n))
            if s:
                reqs = [r for r in b.sos[l["so_order_no"]]["requirements"] if r.clause_type == "prohibited_origin"]
                if reqs and coo in {c.upper() for r in reqs for c in r.params.get("countries", [])}:
                    out.append(finding("A.origin_customer", "P10", f"Confirmed origin acceptable to the customer (line {n})", Status.NEEDS_APPROVAL,
                                       f"Supplier confirms origin {coo}, which the customer only accepts with written approval.",
                                       req_evidence(reqs) + [ev("Supplier ack", al.country_of_origin.raw, b.src("Ack", al.country_of_origin))], severity="high", line=n))
        if al.date_code.raw:
            age = _date_code_age_months(al.date_code.raw, ack_date)
            limits = [(r.params.get("months"), r) for r in (b.sos[l["so_order_no"]]["requirements"] if s else []) + b.conditions
                      if r.clause_type == "date_code_max_age" and r.params.get("months")]
            for months, r in limits:
                if age is not None:
                    whose = "the customer's" if r.scope in ("customer", "order") else "our PO's"
                    ok = age <= months
                    out.append(finding("A.date_code", "P10", f"Confirmed date code within {whose} limit (line {n})", Status.PASS if ok else Status.FAIL,
                                       f"Date code {al.date_code.raw} is about {age} months old; {whose} limit is {months} months.",
                                       [ev("Supplier ack", al.date_code.raw, b.src("Ack", al.date_code)), ev("Limit", f"{months} months", r.source)],
                                       severity="high" if r.scope != "distributor" else "medium", line=n))
    for x in b.ack.doc.lines:
        if id(x) not in matched:
            out.append(finding("A.extra", "P9", "Line confirmed that we did not order", Status.FAIL,
                               f"The supplier confirmed {x.qty.raw} x {x.mpn.raw}, which is not on our PO.", [ev("Supplier ack", x.mpn.raw, b.src("Ack", x.mpn))], severity="high"))
    return out


CHECKS = [check_document, check_lines, check_vendor, check_flowdown, check_ack]


def run_checks(b: SupplierBundle) -> list[Finding]:
    out = []
    for fn in CHECKS:
        try:
            out += fn(b)
        except Exception as e:  # a broken check must show up, not vanish
            out.append(finding(f"P.{fn.__name__}", "P1", fn.__name__, Status.UNVERIFIABLE, f"check failed to run: {e!r}", severity="high"))
    return out


# ---------------------------------------------------------------- the tool


def snapshot(b: SupplierBundle) -> dict:
    keep_line = ("lin_no", "item_no", "manu_no", "ord_qty", "qty_recvd", "unit_cost", "due_date", "src_cntry", "condition", "so_order_no", "so_lin_no")
    return {
        "po": {k: b.hedr.get(k) for k in ("po_no", "po_dt", "vend_no", "terms_code", "buyer", "status")} if b.hedr else None,
        "po_lines": [{k: l.get(k) for k in keep_line} for l in b.lines],
        "vendor": {k: b.vendor.get(k) for k in ("vend_no", "po_name", "terms_code", "pay_hold", "iso_cert", "iso_cert_exp", "po_notes")} if b.vendor else None,
        "sales_orders": [{"so": {k: s["hedr"].get(k) for k in ("order_no", "cust_no", "cust_po", "terms_code", "status")},
                          "customer": {k: (s["customer"] or {}).get(k) for k in ("cust_no", "cust_name", "terms_code")},
                          "lines": [{k: l.get(k) for k in ("lin_no", "item_no", "manu_no", "ord_qty", "unit_price", "unit_cost", "req_date", "vend_no")} for l in s["lines"]]}
                         for s in b.sos.values()],
    }


def verify_supplier_order(po: Extracted | None = None, ack: Extracted | None = None, po_number: int | None = None,
                          message_id: str | None = None, write: bool = True, source: str | None = None):
    from .verify import VerificationResult

    problems = []
    b = build(po, ack, po_number)
    if b.po is None and b.po_no:
        path = find_po_document(b.po_no)
        if path:
            b = build(ingest.extract(ingest.normalize_file(path)), ack, b.po_no)
        else:
            problems.append(f"No PO {b.po_no} document found; checked against the ERP PO and vendor notes only.")
    findings = run_checks(b)
    decision, reasons = report.recommendation(findings)
    if not b.hedr:
        decision, reasons = "review", b.problems or ["PO not found"]
    order = {
        "kind": "supplier", "po_number": b.po_no, "so_number": min(b.sos) if b.sos else None,
        "so_numbers": sorted(b.sos), "vend_no": b.hedr["vend_no"] if b.hedr else None,
        "vendor": b.vendor.get("po_name") if b.vendor else None,
        "customer": ", ".join(sorted({(s["customer"] or {}).get("cust_name") or "" for s in b.sos.values()})) or None,
        "cust_po": ", ".join(sorted({s["hedr"].get("cust_po") or "" for s in b.sos.values()})) or None,
        "po_source": b.po.source if b.po else None, "ack_source": b.ack.source if b.ack else None,
        "as_of": str(_day(b.hedr["po_dt"])) if b.hedr else None, "source": source,
    }
    warnings = (b.po.warnings if b.po else []) + (b.ack.warnings if b.ack else [])
    result = VerificationResult(order=order, recommendation=decision, reasons=reasons, counts=report.counts(findings),
                                checklist=report.rollup(findings, SUPPLIER_CHECKLIST), findings=findings,
                                problems=problems + b.problems + warnings, requirements=b.requirements + b.conditions, erp=snapshot(b))
    if write:
        out = config.RUNS_DIR / f"PO{b.po_no or 'unknown'}" / datetime.now().strftime("%Y%m%d-%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        result.report_path = str(out / "report.md")
        (out / "result.json").write_text(result.model_dump_json(indent=1))
        if b.po:
            (out / "po.json").write_text(b.po.model_dump_json(indent=1))
        if b.ack:
            (out / "ack.json").write_text(b.ack.model_dump_json(indent=1))
        title = {"so_number": f"PO {b.po_no} ({order['vendor']})", "cust_po": f"for SO {', '.join(map(str, order['so_numbers'])) or '—'}",
                 "customer": order["customer"], "po_source": order["po_source"], "ack_source": order["ack_source"], "as_of": order["as_of"]}
        (out / "report.md").write_text(report.markdown(title, findings, decision, reasons, warnings, SUPPLIER_CHECKLIST))
        from . import state
        result.run_id = state.add_run(result, message_id)
    return result
