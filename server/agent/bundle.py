"""The order bundle: everything known about one order, built once in code before any check runs.

The bundle is the evidence. The checklist is the list of questions. Findings are the answers.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from . import erp as q
from . import ledger, parse
from .db import Erp
from .models import DocLine, Extracted, Requirement, Val


@dataclass
class LineMatch:
    """One order line seen across the PO, the ERP SO line, the ack line and the quote line."""

    po: DocLine | None = None
    po_index: int | None = None  # 1-based position on the PO
    so: dict | None = None
    ack: DocLine | None = None
    quote: dict | None = None
    near_miss: bool = False  # paired despite a different MPN (same qty and price)

    @property
    def label(self) -> str:
        if self.so:
            return f"SO line {self.so['lin_no']}"
        return f"PO line {self.po_index}"


@dataclass
class Bundle:
    po: Extracted | None
    ack: Extracted | None
    so: dict | None = None
    so_lines: list[dict] = field(default_factory=list)
    customer: dict | None = None
    quote: dict | None = None
    quote_lines: list[dict] = field(default_factory=list)
    supply: list[dict] = field(default_factory=list)  # supplier PO lines linked to this SO
    items: dict = field(default_factory=dict)
    inventory: dict = field(default_factory=dict)  # (item, mfr) -> itm_inv row or None
    lots: dict = field(default_factory=dict)  # (item, mfr) -> lot rows
    open_supply: dict = field(default_factory=dict)  # (item, mfr) -> open supplier PO lines
    supplier_po_lines: dict = field(default_factory=dict)  # po_no -> all lines of supplier POs feeding this SO
    vendors: dict = field(default_factory=dict)  # vend_no -> vendor row
    supplier_pos: dict = field(default_factory=dict)  # po_no -> Extracted supplier PO document, when we have the PDF
    supplier_requirements: dict = field(default_factory=dict)  # po_no -> our conditions to the supplier, as Requirements
    open_orders: float | None = None
    ar_balance: float | None = None
    requirements: list[Requirement] = field(default_factory=list)
    references: list = field(default_factory=list)  # documents the PO refers to, supplied by the agent (references.Reference)
    lines: list[LineMatch] = field(default_factory=list)
    po_comma: bool = False
    ack_comma: bool = False
    problems: list[str] = field(default_factory=list)  # bundle-level issues (e.g. SO not found)

    @property
    def as_of(self) -> date | None:
        """Checks are judged as of the order date, not the date the code runs."""
        return self.so["order_dt"].date() if self.so and self.so.get("order_dt") else None

    @property
    def so_no(self) -> int | None:
        return self.so["order_no"] if self.so else None

    # -- provenance helpers ---------------------------------------------------------

    def src(self, which: str, v: Val | None) -> str:
        x = self.po if which == "PO" else self.ack
        if x is None:
            return which
        loc = ""
        if v is not None and v.loc:
            loc = f" p{v.loc.page}" if v.loc.page else ""
            loc += f" · {v.loc.where}" if v.loc.where else ""
        return f"{x.source}{loc}"

    def po_num(self, raw: str | None) -> Decimal | None:
        return parse.num(raw, self.po_comma)

    def ack_num(self, raw: str | None) -> Decimal | None:
        return parse.num(raw, self.ack_comma)


def _decimal_comma(x: Extracted | None) -> bool:
    if x is None:
        return False
    return parse.detect_decimal_comma([(l.qty.raw, l.unit_price.raw, l.extended.raw) for l in x.doc.lines])


def resolve_so(erp: Erp, po: Extracted | None, ack: Extracted | None, so_number: int | None) -> tuple[dict | None, list[str]]:
    """Find the SO. Prefer an explicit number, then the ack's order number, then the PO number
    plus customer. PO numbers alone are not unique across customers."""
    problems = []
    if so_number is None and ack and ack.doc.header.doc_number.raw:
        digits = parse.account_number(ack.doc.header.doc_number.raw) or ack.doc.header.doc_number.raw
        if digits.isdigit():
            so_number = int(digits)
    if so_number is not None:
        so = q.sales_order(erp, so_number)
        if so is None:
            problems.append(f"SO {so_number} not found in the ERP")
        return so, problems
    if po is None or not po.doc.header.doc_number.raw:
        return None, ["no SO number and no PO number to look one up"]
    candidates = q.sales_orders_for_po(erp, po.doc.header.doc_number.raw)
    if len(candidates) == 1:
        return candidates[0], problems
    name = parse.company_key(po.doc.header.customer_name.raw)
    named = [c for c in candidates if name and parse.company_key(c["cust_name"]) == name]
    if len(named) == 1:
        return named[0], problems
    if not candidates:
        problems.append(f"no SO carries PO number {po.doc.header.doc_number.raw}")
    else:
        problems.append(
            f"PO number {po.doc.header.doc_number.raw} matches {len(candidates)} SOs for different customers "
            f"({', '.join(str(c['order_no']) + ' ' + c['cust_name'] for c in candidates)}); customer could not be resolved"
        )
    return None, problems


def _score(po_line: DocLine, so_line: dict, comma: bool) -> int:
    s = 0
    if parse.num(po_line.qty.raw, comma) == parse.num(str(so_line["ord_qty"])):
        s += 2
    if parse.num(po_line.unit_price.raw, comma) == parse.num(str(so_line["unit_price"])):
        s += 2
    d = parse.to_date(po_line.required_date.raw, day_first=comma)
    if d and so_line.get("req_date") and d == so_line["req_date"].date():
        s += 1
    return s


def match_lines(b: Bundle) -> list[LineMatch]:
    """Pair PO lines with SO lines on MPN, using qty, price and date to separate duplicate MPNs.
    A PO line with no MPN match is paired with an unmatched SO line only when qty and price
    both agree, and flagged as a near miss so the MPN difference is reported, not hidden."""
    po_lines = list(enumerate(b.po.doc.lines, 1)) if b.po else []
    so_free = list(b.so_lines)
    matches: list[LineMatch] = []
    unmatched_po = []
    for idx, pl in po_lines:
        key = parse.mpn_key(pl.mpn.raw)
        cands = [s for s in so_free if parse.mpn_key(s["item_no"]) == key]
        if cands:
            best = max(cands, key=lambda s: _score(pl, s, b.po_comma))
            so_free.remove(best)
            matches.append(LineMatch(po=pl, po_index=idx, so=best))
        else:
            unmatched_po.append((idx, pl))
    for idx, pl in unmatched_po:
        cands = [s for s in so_free if _score(pl, s, b.po_comma) >= 4]
        if cands:
            best = max(cands, key=lambda s: difflib.SequenceMatcher(None, parse.mpn_key(pl.mpn.raw) or "", parse.mpn_key(s["item_no"])).ratio())
            so_free.remove(best)
            matches.append(LineMatch(po=pl, po_index=idx, so=best, near_miss=True))
        else:
            matches.append(LineMatch(po=pl, po_index=idx))
    matches += [LineMatch(so=s) for s in so_free]

    # Ack lines: the ack prints the SO line number.
    if b.ack:
        by_no = {}
        for al in b.ack.doc.lines:
            n = parse.num(al.line_no.raw)
            if n is not None:
                by_no[int(n)] = al
        for m in matches:
            if m.so and m.so["lin_no"] in by_no:
                m.ack = by_no[m.so["lin_no"]]

    quote_by_line = {ql["lin_no"]: ql for ql in b.quote_lines}
    for m in matches:
        if m.so and m.so.get("gen_quot_lin") in quote_by_line:
            m.quote = quote_by_line[m.so["gen_quot_lin"]]
    matches.sort(key=lambda m: (m.so["lin_no"] if m.so else 999, m.po_index or 0))
    return matches


def _load_supplier_terms(b: Bundle) -> None:
    """Our conditions on each supplier PO bought for this SO: from the PO document when we have
    it (Sent Items copies live with the order documents), else the vendor's standing PO notes."""
    from . import config, ingest

    for po_no in sorted({p["po_no"] for p in b.supply}):
        hits = sorted(config.ACK_DIR.rglob(f"Purchase Order {po_no}.*"))
        if hits:
            x = ingest.extract(ingest.normalize_file(hits[0]))
            b.supplier_pos[po_no] = x
            reqs = []
            for v in x.doc.notes:
                reqs += ledger.classify(v.raw or "", f"{x.source} · purchase conditions", "distributor")
            b.supplier_requirements[po_no] = ledger.llm_label(reqs)
        else:
            vend = next((p["vend_no"] for p in b.supply if p["po_no"] == po_no), None)
            notes = (b.vendors.get(vend) or {}).get("po_notes") if vend else None
            b.supplier_requirements[po_no] = ledger.classify(notes, f"vendor.po_notes ({vend})", "distributor") if notes else []


def build(erp: Erp, po: Extracted | None, ack: Extracted | None, so_number: int | None = None) -> Bundle:
    b = Bundle(po=po, ack=ack, po_comma=_decimal_comma(po), ack_comma=_decimal_comma(ack))
    b.so, b.problems = resolve_so(erp, po, ack, so_number)
    if b.so:
        b.so_lines = q.so_lines(erp, b.so["order_no"])
        b.customer = q.customer(erp, b.so["cust_no"])
        quote_no = (b.so.get("quote_no") or "").strip()
        if quote_no.isdigit():
            b.quote = q.quote(erp, int(quote_no))
            b.quote_lines = q.quote_lines(erp, int(quote_no))
        b.supply = q.linked_po_lines(erp, b.so["order_no"])
        keys = sorted({(l["item_no"], l["manu_no"]) for l in b.so_lines})
        b.items = q.items(erp, keys)
        for k in keys:
            b.inventory[k] = q.inventory(erp, *k)
            b.lots[k] = q.lots(erp, *k)
            b.open_supply[k] = q.open_po_lines_for_item(erp, *k)
        po_nos = sorted({p["po_no"] for p in b.supply} | {p["po_no"] for rows in b.open_supply.values() for p in rows
                                                            if p.get("so_order_no") in (None, b.so["order_no"])})
        for po_no in po_nos:
            b.supplier_po_lines[po_no] = q.po_lines(erp, po_no)
        vend_nos = {l["vend_no"] for l in b.so_lines if l.get("vend_no")} | {p["vend_no"] for rows in b.supplier_po_lines.values() for p in rows}
        for v in sorted(vend_nos):
            b.vendors[v] = q.vendor(erp, v)
    if b.customer:
        b.open_orders = q.open_order_value(erp, b.customer["cust_no"])
        b.ar_balance = q.ar_balance(erp, b.customer["cust_no"])
        b.requirements += ledger.from_customer(b.customer)
    if po:
        b.requirements += ledger.from_document(po)
    b.lines = match_lines(b)
    return b


def enrich(b: Bundle) -> Bundle:
    """The model steps, run after the ERP connection is released: label clauses the dictionary
    did not recognise, and read our conditions on the supplier POs bought for this order."""
    b.requirements = ledger.llm_label(b.requirements)
    if b.so:
        _load_supplier_terms(b)
    return b
