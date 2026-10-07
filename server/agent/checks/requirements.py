"""Verifiers for the requirements ledger, one per clause type.

Rules: a clause with an exception returns needs_approval on a hit, not fail. Weak evidence
never passes. Clauses that can only be met later are listed as carry-forward requirements.
A clause type with no verifier is surfaced for human review.
"""
from __future__ import annotations

import re

from ..bundle import Bundle, LineMatch
from ..models import Requirement, Status
from . import ev, finding, req_evidence, verifier


def _first(reqs: list[Requirement]) -> Requirement:
    # Order-scope clauses (the PO itself) are more specific than the customer master.
    return sorted(reqs, key=lambda r: {"order": 0, "customer": 1, "distributor": 2}[r.scope])[0]


@verifier("no_partials", "partials_allowed")
def partials(clause_type: str, reqs: list[Requirement], b: Bundle):
    # Both clause types are judged together, once, under the no_partials group.
    all_reqs = [r for r in b.requirements if r.clause_type in ("no_partials", "partials_allowed")]
    if clause_type == "partials_allowed" and any(r.clause_type == "no_partials" for r in all_reqs):
        return []
    allowed_by_order = [r for r in all_reqs if r.clause_type == "partials_allowed" and r.scope == "order"]
    forbidden = [r for r in all_reqs if r.clause_type == "no_partials"]
    evidence = req_evidence(all_reqs)
    ack_opt = b.ack.doc.header.ship_option if b.ack else None
    if ack_opt is None or not ack_opt.raw:
        return [finding("R.partials", "2.9", "Partial-shipment instruction on the sale", Status.UNVERIFIABLE,
                        "The ack's ship option is not available.", evidence, severity="medium")]
    evidence.append(ev("Ack", ack_opt.raw, b.src("Ack", ack_opt)))
    ships_partials = "partial" in ack_opt.raw.lower()
    if forbidden and not allowed_by_order:
        if ships_partials:
            return [finding("R.partials", "2.9", "Partial-shipment instruction on the sale", Status.FAIL,
                            f"The customer forbids partial shipments ({len(forbidden)} clause(s)), but the sale is set to {ack_opt.raw!r}.",
                            evidence, severity="high")]
        return [finding("R.partials", "2.9", "Partial-shipment instruction on the sale", Status.PASS, f"Sale is set to {ack_opt.raw!r}, as required.", evidence)]
    if allowed_by_order:
        note = " The PO explicitly allows partials, which overrides the master's restriction." if forbidden else ""
        return [finding("R.partials", "2.9", "Partial-shipment instruction on the sale", Status.PASS,
                        f"Partial shipments are acceptable on this PO; the sale is set to {ack_opt.raw!r}.{note}", evidence)]
    return []


@verifier("yield_hold")
def yield_hold(clause_type, reqs, b):
    return [finding("R.yield", "3.1", "Yield flag", Status.NEEDS_APPROVAL,
                    "The account carries a Yield flag: manager approval is required before the order is released.", req_evidence(reqs), severity="high")]


@verifier("customer_pickup")
def customer_pickup(clause_type, reqs, b):
    if not b.ack:
        return []
    v = b.ack.doc.header.ship_via
    ok = "pick" in (v.raw or "").lower()
    status = Status.PASS if ok else Status.FAIL
    msg = "The sale is set to customer pickup." if ok else f"The customer's terms say customer pickup, but the sale ships via {v.raw!r}."
    return [finding("R.pickup", "2.7", "Customer pickup", status, msg, req_evidence(reqs) + [ev("Ack", v.raw, b.src("Ack", v))], severity="medium")]


@verifier("ack_must_state")
def ack_must_state(clause_type, reqs, b):
    fields = sorted({f for r in reqs for f in r.params.get("fields", [])})
    if not b.ack:
        return [finding("R.ack_fields", "3.2", "Ack must state per-line data", Status.UNVERIFIABLE, "No ack to check.", req_evidence(reqs))]
    labels = {"coo": ("country of origin", ("origin", "coo", "country")), "eccn": ("ECCN", ("eccn",)), "hts": ("HTS code", ("hts", "schedule b", "tariff"))}
    missing = []
    for f in fields:
        name, words = labels[f]
        for i, l in enumerate(b.ack.doc.lines, 1):
            text = " ".join((n.raw or "") for n in l.line_notes).lower()
            if not any(w in text for w in words):
                missing.append(name)
                break
    evidence = req_evidence(reqs) + [ev("Ack line notes", " | ".join(n.raw or "" for n in b.ack.doc.lines[0].line_notes) if b.ack.doc.lines else None, b.ack.source)]
    if missing:
        return [finding("R.ack_fields", "3.2", "Ack must state per-line data", Status.FAIL,
                        f"The customer requires the acknowledgement to state {', '.join(missing)} for each line; the ack does not.", evidence, severity="medium")]
    return [finding("R.ack_fields", "3.2", "Ack must state per-line data", Status.PASS, f"The ack states {', '.join(fields)} per line.", evidence)]


def origin(b: Bundle, m: LineMatch) -> tuple[str | None, str | None, str | None]:
    """Country of origin with its source and strength:
    1. our supplier PO line bought for this SO line (strong);
    2. lots in stock, when they cover the quantity and share one origin (strong);
    3. the item default (weak: it disagrees with lot origin on 83% of lots)."""
    sup = [p for p in b.supply if p.get("so_lin_no") == m.so["lin_no"]]
    if sup and sup[0].get("src_cntry"):
        p = sup[0]
        return p["src_cntry"], f"po_detl.src_cntry (PO {p['po_no']} line {p['lin_no']})", "strong"
    key = (m.so["item_no"], m.so["manu_no"])
    stocked = [x for x in b.lots.get(key, []) if (x.get("bal_of_lot") or 0) > 0 and x.get("cnty_of_origin")]
    coos = {x["cnty_of_origin"] for x in stocked}
    if stocked and len(coos) == 1 and sum(x["bal_of_lot"] for x in stocked) >= m.so["ord_qty"]:
        return coos.pop(), f"lot_info.cnty_of_origin ({', '.join(x['lot_no'] for x in stocked)})", "strong"
    item = b.items.get(key)
    if item and item.get("deflt_cnty_of_origin"):
        note = f" (lots in stock: {', '.join(sorted(coos))})" if coos else ""
        return item["deflt_cnty_of_origin"], f"item.deflt_cnty_of_origin ({m.so['item_no']}){note}", "weak"
    return None, None, None


@verifier("prohibited_origin")
def prohibited_origin(clause_type, reqs, b):
    req = _first(reqs)
    countries = {c.upper() for r in reqs for c in r.params.get("countries", [])}
    exception = any(r.params.get("exception") for r in reqs)
    out = []
    for m in b.lines:
        if not m.so:
            continue
        n = m.so["lin_no"]
        coo, src, strength = origin(b, m)
        evidence = req_evidence([req]) + ([ev("Origin", coo, src)] if coo else [])
        title = f"Restricted origin ({', '.join(sorted(countries))}) (line {n})"
        if coo is None:
            out.append(finding("R.origin", "3.2", title, Status.UNVERIFIABLE, "Country of origin unknown.", evidence, severity="medium", line=n))
        elif strength == "weak":
            hit = coo.upper() in countries
            out.append(finding("R.origin", "3.2", title, Status.UNVERIFIABLE,
                               f"Only the item default is known ({coo}); no supplier PO or lot confirms the origin."
                               + (" The default points at a restricted country." if hit else ""),
                               evidence, severity="high" if hit else "low", line=n))
        elif coo.upper() in countries:
            status = Status.NEEDS_APPROVAL if exception else Status.FAIL
            out.append(finding("R.origin", "3.2", title, status,
                               f"Origin is {coo}" + (": the customer's prior written approval is required for this line." if exception else ", which the customer does not accept."),
                               evidence, severity="high", line=n))
        else:
            out.append(finding("R.origin", "3.2", title, Status.PASS, f"Origin is {coo}.", evidence, line=n))
    return out


IN_DOCUMENT = r"\bpage \d|\bbelow\b|overleaf|\breverse\b|\battached\b|\bfollowing\b"


@verifier("external_terms", "quality_clause_codes")
def external_terms(clause_type, reqs, b):
    links = b.po.links if b.po else []
    what = "quality clause codes" if clause_type == "quality_clause_codes" else "referenced terms and conditions"
    from ..references import KINDS
    supplied = [r for r in b.references if KINDS[r.kind] == clause_type]
    if supplied:
        n = sum(1 for r in b.requirements if any(r.source.startswith(s.source) for s in supplied))
        return [finding(f"R.{clause_type}", "3.2", f"Customer {what}", Status.INFO,
                        f"The {what} were supplied from {', '.join(s.source for s in supplied)} and read clause by clause "
                        f"({n} clause(s) found); each is checked under its own item.", req_evidence(reqs) + [ev("Supplied", s.source, s.source) for s in supplied])]
    # "Governed by the Conditions of Purchase on page 2": the terms are in the document and were read clause by clause.
    if clause_type == "external_terms" and all(re.search(IN_DOCUMENT, r.quote.lower()) for r in reqs):
        return [finding(f"R.{clause_type}", "3.2", f"Customer {what}", Status.INFO,
                        "The referenced terms are printed in the document itself, and each clause was read and checked.", req_evidence(reqs))]
    return [finding(f"R.{clause_type}", "3.2", f"Customer {what}", Status.UNVERIFIABLE,
                    f"The PO incorporates {what} that are not available to the agent, so they could not be reviewed."
                    + (f" Link: {', '.join(links)}" if links else ""),
                    req_evidence(reqs), severity="medium")]


@verifier("payment_fees")
def payment_fees(clause_type, reqs, b):
    seller_pays = [r for r in reqs if re.search(r"borne by (?:the )?(?:seller|supplier|vendor)|at (?:the )?seller'?s (?:cost|expense)", r.quote.lower())]
    if seller_pays:
        return [finding("R.payment_fees", "1.2", "Card fees vs our prepay fee policy", Status.NEEDS_REVIEW,
                        "The customer's terms put card fees on us, but our policy charges the customer 3.5% for card payments (checklist 1.2). "
                        "Agree the fee with the customer or accept the cost.", req_evidence(seller_pays), severity="medium")]
    return [finding("R.payment_fees", "1.2", "Payment fees clause", Status.INFO, "Noted.", req_evidence(reqs))]


US_STATES = {"alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida", "georgia", "hawaii",
             "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
             "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
             "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
             "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming", "united states"}


@verifier("governing_law")
def governing_law(clause_type, reqs, b):
    text = " ".join(r.quote for r in reqs)
    # The place is the capitalised name after "laws of (the State of)": "Colorado" in "The laws of the
    # State of Colorado govern this order", "Japan" in "governed by the laws of Japan; ...".
    m = re.search(r"(?i:laws? of (?:the )?(?:(?:state|commonwealth) of )?)([A-Z][\w-]*(?: (?:of )?[A-Z][\w-]*)*)", text)
    place = m.group(1).strip() if m else ""
    if place and place.lower() not in US_STATES:
        return [finding("R.governing_law", "3.2", "Governing law and jurisdiction", Status.NEEDS_REVIEW,
                        f"The customer's terms put the order under the laws of {place}"
                        + (" with exclusive foreign jurisdiction" if "exclusive jurisdiction" in text.lower() else "")
                        + ". A person (legal) should accept this before we confirm.", req_evidence(reqs), severity="medium")]
    return [finding("R.governing_law", "3.2", "Governing law", Status.INFO, "Noted.", req_evidence(reqs))]


@verifier("ack_deadline")
def ack_deadline(clause_type, reqs, b):
    from .. import parse
    hours = max(r.params.get("hours") or 0 for r in reqs)
    po_d = parse.to_date(b.po.doc.header.doc_date.raw, day_first=b.po_comma) if b.po else None
    ack_d = parse.to_date(b.ack.doc.header.doc_date.raw) if b.ack else None
    evidence = req_evidence(reqs)
    if not (po_d and ack_d):
        return [finding("R.ack_deadline", "3.2", f"Acknowledge within {hours} hours", Status.UNVERIFIABLE, "PO or ack date missing.", evidence)]
    days = (ack_d - po_d).days
    ok = days * 24 <= hours
    return [finding("R.ack_deadline", "3.2", f"Acknowledge within {hours} hours", Status.PASS if ok else Status.FAIL,
                    f"PO dated {po_d}, ack dated {ack_d} ({days} day(s)).", evidence, severity="low")]


@verifier("terms_acceptance", "inspection_rights")
def conflicting_terms(clause_type, reqs, b):
    ncnr = b.ack and any("ncnr" in (n.raw or "").lower() or "non-cancel" in (n.raw or "").lower() for n in b.ack.doc.notes)
    if ncnr:
        return [finding(f"R.{clause_type}", "3.2", "Customer terms vs our NCNR terms", Status.NEEDS_REVIEW,
                        "The customer's terms may conflict with the NCNR statement on our acknowledgement.", req_evidence(reqs), severity="medium")]
    return [finding(f"R.{clause_type}", "3.2", "Customer terms", Status.INFO, "Noted.", req_evidence(reqs))]


# Clauses that can only be met when sourcing or shipping. Checked by flow-down to our supplier
# PO in a later milestone; for now they are listed so nobody misses them.
CARRY_FORWARD = {
    "date_code_max_age": "Date code limit", "shelf_life_months": "Shelf life", "counterfeit_avoidance": "Counterfeit avoidance / sourcing",
    "coc_required": "Certificate of conformance", "traceability": "Traceability", "packaging": "Packaging",
    "dfars_flowdown": "DFARS flow-down", "itar_flowdown": "ITAR flow-down", "qc_before_ship": "QC before shipment",
    "first_article": "First article", "blind_ship": "Blind ship", "export_license_required": "Export license",
    "late_delivery_penalty": "Late delivery penalty", "early_delivery_limit": "Early delivery limit",
    "print_coo_on_docs": "Country of origin on shipping documents", "print_eccn_on_docs": "ECCN on shipping documents",
    "export_review": "Export authorisation confirmed to the buyer in writing before shipping",
}


@verifier(*CARRY_FORWARD)
def carry_forward(clause_type, reqs, b):
    return [finding(f"R.{clause_type}", "3.2", CARRY_FORWARD[clause_type], Status.MANUAL,
                    "Carry forward: must be met when sourcing and shipping, and flowed down to the supplier PO.", req_evidence(reqs), severity="low")]


# Clause types another check already covers, or that are not order-entry checks.
COVERED = {
    "ship_on_customer_account": "H.carrier_account", "price_matches_quote": "Q.link / Q.price", "payment_method": "H.payment_terms",
    "ship_to_instruction": "H.addresses", "commercial_terms_restated": "H.payment_terms / H.ship_via",
}
NOTED = {
    "po_number_on_docs": ("3.2", "PO number on all documents"), "change_notification": ("3.2", "Notify the buyer of changes"),
    "credit_history_note": ("1.4", "Credit history note"), "no_substitutions": ("3.2", "No substitutions"), "warranty": ("3.2", "Warranty"),
}


@verifier(*COVERED)
def covered(clause_type, reqs, b):
    return []


@verifier(*NOTED)
def noted(clause_type, reqs, b):
    ref, title = NOTED[clause_type]
    return [finding(f"R.{clause_type}", ref, title, Status.INFO, "Noted.", req_evidence(reqs))]


@verifier("not_a_requirement")
def not_a_requirement(clause_type, reqs, b):
    # Kept visible so a wrong "not a requirement" label can be spotted, never silently dropped.
    return [finding("R.not_requirement", "3.2", "Read, judged not a requirement", Status.INFO, f"{r.quote!r} ({r.params.get('llm_note', '')})",
                    [ev("Clause", r.quote, r.source)]) for r in reqs]


@verifier("unclassified", "__unknown__")
def unknown(clause_type, reqs, b):
    return [finding("R.unclassified", "3.2", "Unrecognised clause", Status.NEEDS_REVIEW,
                    f"A requirement no check covers{': ' + r.params['llm_note'] if r.params.get('llm_note') else ''}. A person should read it: {r.quote!r}",
                    [ev("Clause", r.quote, r.source)], severity="medium")
            for r in reqs]
