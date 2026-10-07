"""Header checks: the PO vs the SO (ERP) vs the ack vs the customer master."""
from __future__ import annotations

import re

from .. import parse
from ..bundle import Bundle
from ..models import Finding, Status
from . import check, ev, finding


def compare(check_id, ref, title, values, severity="medium", what="value") -> Finding:
    """values: (label, comparable, shown, source). Missing comparables are reported, not compared."""
    present = [v for v in values if v[1] is not None]
    evidence = [ev(label, shown, source) for label, _, shown, source in values]
    if len(present) < 2:
        missing = ", ".join(label for label, comp, _, _ in values if comp is None) or "sources"
        return finding(check_id, ref, title, Status.UNVERIFIABLE, f"Not enough sources to compare the {what} (missing: {missing}).", evidence, severity="low")
    distinct = {v[1] for v in present}
    if len(distinct) == 1:
        return finding(check_id, ref, title, Status.PASS, f"Same {what} on {', '.join(v[0] for v in present)}.", evidence)
    parts = "; ".join(f"{label} {shown!s}" for label, comp, shown, _ in present)
    return finding(check_id, ref, title, Status.FAIL, f"Mismatch in {what}: {parts}.", evidence, severity=severity)


def _po_src(b: Bundle, v) -> str:
    return b.src("PO", v)


def _ack_src(b: Bundle, v) -> str:
    return b.src("Ack", v)


@check("H.po_number", "2.3", "Customer PO number on the SO")
def po_number(b: Bundle):
    key = lambda s: re.sub(r"\s+", "", s).upper() if s else None
    vals = []
    if b.po:
        v = b.po.doc.header.doc_number
        vals.append(("PO", key(v.raw), v.raw, _po_src(b, v)))
    if b.so:
        vals.append(("SO", key(b.so["cust_po"]), b.so["cust_po"], f"ord_hedr.cust_po (SO {b.so_no})"))
    if b.ack:
        v = b.ack.doc.header.customer_po_ref
        vals.append(("Ack", key(v.raw), v.raw, _ack_src(b, v)))
    return [compare("H.po_number", "2.3", "Customer PO number on the SO", vals, severity="high", what="PO number")]


@check("H.customer", "2.1", "Customer matches the PO")
def customer(b: Bundle):
    out = []
    master = b.customer
    vals = []
    if b.po:
        v = b.po.doc.header.customer_name
        vals.append(("PO", parse.company_key(v.raw), v.raw, _po_src(b, v)))
    if master:
        vals.append(("Master", parse.company_key(master["cust_name"]), master["cust_name"], f"customer.cust_name ({master['cust_no']})"))
    if b.ack:
        v = b.ack.doc.header.customer_name
        vals.append(("Ack", parse.company_key(v.raw), v.raw, _ack_src(b, v)))
    out.append(compare("H.customer_name", "2.1", "Customer name matches the PO", vals, severity="high", what="customer name"))
    if b.ack and b.so:
        v = b.ack.doc.header.customer_id
        out.append(compare("H.customer_id", "2.1", "Ack customer id matches the SO", [
            ("Ack", (v.raw or "").strip() or None, v.raw, _ack_src(b, v)),
            ("SO", b.so["cust_no"], b.so["cust_no"], f"ord_hedr.cust_no (SO {b.so_no})"),
        ], severity="high", what="customer id"))
    return out


def _addr(raw):
    return (parse.street_line(raw), parse.postcode(raw))


def _addr_compare(check_id, title, values):
    """values: (label, raw, source). Addresses agree if streets agree (when both known) and postcodes agree (when both known)."""
    keyed = [(label, _addr(raw), raw, src) for label, raw, src in values]
    evidence = [ev(label, raw, src) for label, _, raw, src in keyed]
    usable = [k for k in keyed if k[1][0] or k[1][1]]
    if len(usable) < 2:
        return finding(check_id, "2.2", title, Status.UNVERIFIABLE, "Not enough address data to compare.", evidence, severity="low")
    diffs = []
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            (la, (sa, pa), ra, _), (lb, (sb, pb), rb, _) = usable[i], usable[j]
            if (sa and sb and sa != sb) or (pa and pb and pa != pb):
                diffs.append(f"{la} vs {lb}")
    if diffs:
        return finding(check_id, "2.2", title, Status.FAIL, f"Address differs ({'; '.join(diffs)}).", evidence, severity="high")
    return finding(check_id, "2.2", title, Status.PASS, f"Address agrees across {', '.join(u[0] for u in usable)}.", evidence)


def _master_address(c: dict) -> str:
    return ", ".join(x for x in (c.get("cust_addr1"), c.get("cust_city"), c.get("cust_state"), c.get("cust_zip"), c.get("cust_cntry")) if x)


@check("H.addresses", "2.2", "Billing and shipping addresses match the PO")
def addresses(b: Bundle):
    out = []
    master = b.customer
    ship = []
    if b.po:
        v = b.po.doc.header.ship_to
        ship.append(("PO ship-to", v.raw, _po_src(b, v)))
    if b.ack:
        v = b.ack.doc.header.ship_to
        ship.append(("Ack ship-to", v.raw, _ack_src(b, v)))
    if master:
        ship.append(("Master address", _master_address(master), f"customer.cust_addr1/city/zip ({master['cust_no']})"))
    out.append(_addr_compare("H.ship_to", "Ship-to address matches the PO", ship))
    bill = []
    if b.po and b.po.doc.header.bill_to.raw:
        v = b.po.doc.header.bill_to
        bill.append(("PO bill-to", v.raw, _po_src(b, v)))
    if b.ack:
        v = b.ack.doc.header.bill_to
        bill.append(("Ack bill-to", v.raw, _ack_src(b, v)))
    if master:
        bill.append(("Master address", _master_address(master), f"customer.cust_addr1/city/zip ({master['cust_no']})"))
    out.append(_addr_compare("H.bill_to", "Bill-to address matches the customer", bill))
    return out


@check("H.payment_terms", "1.1", "Payment terms")
def payment_terms(b: Bundle):
    vals = []
    if b.po:
        v = b.po.doc.header.payment_terms
        vals.append(("PO", parse.terms_code(v.raw), v.raw, _po_src(b, v)))
    if b.so:
        vals.append(("SO", b.so["terms_code"], b.so["terms_code"], f"ord_hedr.terms_code (SO {b.so_no})"))
    if b.ack:
        v = b.ack.doc.header.payment_terms
        vals.append(("Ack", parse.terms_code(v.raw), v.raw, _ack_src(b, v)))
    if b.customer:
        vals.append(("Master", b.customer["terms_code"], b.customer["terms_code"], f"customer.terms_code ({b.customer['cust_no']})"))
    if b.quote:
        vals.append(("Quote", b.quote["terms_code"], b.quote["terms_code"], f"quote.terms_code (quote {b.quote['quote_no']})"))
    return [compare("H.payment_terms", "1.1", "Payment terms", vals, severity="high", what="payment terms")]


_SERVICE = r"ground|express|priority|overnight|2\s?day|economy|standard|next day|saver"


def _service(raw):
    m = re.search(_SERVICE, (raw or "").lower())
    return m.group(0).replace(" ", "") if m else None


@check("H.ship_via", "2.7", "Carrier and shipping speed")
def ship_via(b: Bundle):
    vals, services = [], []
    if b.po:
        v = b.po.doc.header.ship_via
        vals.append(("PO", parse.carrier_code(v.raw), v.raw, _po_src(b, v)))
        services.append(("PO", _service(v.raw), v.raw, _po_src(b, v)))
    if b.ack:
        v = b.ack.doc.header.ship_via
        vals.append(("Ack", parse.carrier_code(v.raw), v.raw, _ack_src(b, v)))
        services.append(("Ack", _service(v.raw), v.raw, _ack_src(b, v)))
    if b.customer:
        vals.append(("Master", b.customer["ship_via"], b.customer["ship_via"], f"customer.ship_via ({b.customer['cust_no']})"))
    out = [compare("H.carrier", "2.7", "Carrier matches the PO", vals, what="carrier")]
    if sum(1 for s in services if s[1]) >= 2:
        out.append(compare("H.service", "2.7", "Shipping speed matches the PO", services, what="shipping speed"))
    return out


@check("H.carrier_account", "2.8", "Shipping account")
def carrier_account(b: Bundle):
    vals = []
    if b.po:
        v = b.po.doc.header.carrier_account
        acct = parse.account_number(v.raw) or parse.account_number(b.po.doc.header.ship_via.raw)
        vals.append(("PO", acct, v.raw or b.po.doc.header.ship_via.raw, _po_src(b, v)))
    if b.ack:
        v = b.ack.doc.header.carrier_account
        acct = parse.account_number(v.raw) or parse.account_number(b.ack.doc.header.freight_terms.raw)
        vals.append(("Ack", acct, v.raw or b.ack.doc.header.freight_terms.raw, _ack_src(b, v)))
    for r in b.requirements:
        if r.clause_type == "ship_on_customer_account":
            vals.append(("Master", r.params["account"], r.quote, r.source))
    if not [v for v in vals if v[1]]:
        return [finding("H.carrier_account", "2.8", "Shipping account", Status.INFO, "No customer shipping account on the PO, ack or master.",
                        [ev(l, s, src) for l, _, s, src in vals])]
    f = compare("H.carrier_account", "2.8", "Shipping account matches the PO and is the customer's", vals, severity="high", what="shipping account")
    return [f]


@check("H.incoterm", "2.7", "Shipping terms (Incoterm / FOB)")
def incoterm_check(b: Bundle):
    vals = []
    if b.po:
        v = b.po.doc.header.incoterm
        term, _ = parse.incoterm(v.raw)
        if term is None:  # some POs put it in the ship-via text ("FCA Austin / DHL Express")
            term, _ = parse.incoterm(b.po.doc.header.ship_via.raw)
        vals.append(("PO", term, v.raw or b.po.doc.header.ship_via.raw, _po_src(b, v)))
    if b.ack:
        v = b.ack.doc.header.incoterm
        vals.append(("Ack", parse.incoterm(v.raw)[0], v.raw, _ack_src(b, v)))
    if b.po and vals and vals[0][1] is None:
        out = [finding("H.incoterm", "2.7", "Shipping terms match the PO", Status.INFO, "The PO does not state shipping terms.",
                       [ev(l, s, src) for l, _, s, src in vals])]
    else:
        out = [compare("H.incoterm", "2.7", "Shipping terms match the PO", vals, what="shipping terms")]
    if b.customer and b.ack:
        side = parse.FOB_SIDE.get(b.customer.get("fob_code"))
        ack_side = parse.incoterm(b.ack.doc.header.incoterm.raw)[1]
        if side and ack_side:
            out.append(compare("H.fob_side", "2.7", "Shipping terms match the customer's FOB", [
                ("Ack", ack_side, b.ack.doc.header.incoterm.raw, _ack_src(b, b.ack.doc.header.incoterm)),
                ("Master", side, b.customer.get("fob_code"), f"customer.fob_code ({b.customer['cust_no']})"),
            ], severity="low", what="FOB point (origin or destination)"))
    return out
