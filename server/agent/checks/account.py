"""Account-level checks: credit, prepay fees, the Yield flag, export."""
from __future__ import annotations

from ..bundle import Bundle
from ..models import Status
from . import check, ev, finding

NET_TERMS = {"N30", "N45", "N60"}


@check("A.credit", "1.4", "Credit line covers the order")
def credit(b: Bundle):
    if not (b.so and b.customer):
        return []
    c, terms = b.customer, b.so["terms_code"]
    if terms not in NET_TERMS:
        return [finding("A.credit", "1.4", "Credit line covers the order", Status.PASS,
                        f"SO terms are {terms} (prepay), so no credit line is used.", [ev("SO", terms, f"ord_hedr.terms_code (SO {b.so_no})")])]
    limit = float(c["crdt_limit"] or 0)
    exposure = (b.open_orders or 0) + max(b.ar_balance or 0, 0)
    evidence = [
        ev("Credit limit", f"{limit:,.2f}", f"customer.crdt_limit ({c['cust_no']})"),
        ev("Open orders", f"{b.open_orders or 0:,.2f}", "sum of ord_detl qty x price, statuses O/I/H (includes this order)"),
        ev("AR balance", f"{b.ar_balance or 0:,.2f}", "sum of arhist.amount"),
    ]
    if exposure > limit:
        return [finding("A.credit", "1.4", "Credit line covers the order", Status.NEEDS_APPROVAL,
                        f"Exposure {exposure:,.2f} (open orders plus AR) is over the {limit:,.2f} credit limit on net terms.", evidence, severity="high")]
    return [finding("A.credit", "1.4", "Credit line covers the order", Status.PASS,
                    f"Exposure {exposure:,.2f} is within the {limit:,.2f} limit.", evidence)]


@check("A.prepay_fee", "1.2", "Prepay fees")
def prepay_fee(b: Bundle):
    if not b.so:
        return []
    terms = b.so["terms_code"]
    if terms == "CC":
        fee_lines = [l for l in b.so_lines if "fee" in (l.get("item_desc") or "").lower() or "surcharge" in (l.get("item_desc") or "").lower()]
        if not fee_lines:
            return [finding("A.prepay_fee", "1.2", "Prepay fees", Status.FAIL, "SO terms are credit card but no 3.5% card fee line is on the SO.",
                            [ev("SO", terms, f"ord_hedr.terms_code (SO {b.so_no})")], severity="medium")]
    if terms == "CIA":
        return [finding("A.prepay_fee", "1.2", "Prepay fees", Status.UNVERIFIABLE,
                        "Cash in advance: whether payment is by wire ($25 fee) or ACH (no fee) is not recorded.",
                        [ev("SO", terms, f"ord_hedr.terms_code (SO {b.so_no})")], severity="low")]
    return []


@check("A.yield", "3.1", "Yield flag")
def yield_flag(b: Bundle):
    if not b.customer:
        return []
    if any(r.clause_type == "yield_hold" for r in b.requirements):
        return []  # the yield_hold verifier reports it
    return [finding("A.yield", "3.1", "Yield flag", Status.PASS, "The account has no Yield flag.",
                    [ev("Master", b.customer.get("comments") or "(blank)", f"customer.comments ({b.customer['cust_no']})")])]


@check("A.export", "6.1", "Export")
def export(b: Bundle):
    if not (b.customer and b.so):
        return []
    country = (b.customer.get("cust_cntry") or "").upper()
    exporting = country not in ("", "USA", "US", "UNITED STATES")
    out = [finding("A.export", "6.1", "Are we exporting?", Status.INFO,
                   f"{'Yes' if exporting else 'No'}: the customer is in {country or 'an unknown country'}.",
                   [ev("Master", country, f"customer.cust_cntry ({b.customer['cust_no']})")])]
    if not exporting:
        out.append(finding("A.eccn", "6.2", "Export review", Status.PASS, "Not exporting, so no export review is needed.",
                           [ev("Master", country, f"customer.cust_cntry ({b.customer['cust_no']})")]))
        return out
    for l in b.so_lines:
        item = b.items.get((l["item_no"], l["manu_no"]))
        eccn = (item or {}).get("eccn")
        src = f"item.eccn ({l['item_no']} / {l['manu_no']})"
        if not eccn:
            out.append(finding("A.eccn", "6.2", f"ECCN (line {l['lin_no']})", Status.UNVERIFIABLE, "No ECCN on the item master.", [ev("Item", None, src)], line=l["lin_no"]))
        elif eccn.upper() != "EAR99":
            out.append(finding("A.eccn", "6.2", f"ECCN (line {l['lin_no']})", Status.NEEDS_APPROVAL,
                               f"Exporting to {country} with ECCN {eccn}: the Export Review Form and approval are required before shipping.",
                               [ev("Item", eccn, src)], severity="high", line=l["lin_no"]))
        else:
            out.append(finding("A.eccn", "6.2", f"ECCN (line {l['lin_no']})", Status.PASS, "EAR99.", [ev("Item", eccn, src)], line=l["lin_no"]))
    return out
