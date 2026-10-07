"""Vendor & inventory, and dates (checklist sections 4 and 5).

4.1 asks "are there enough parts": stock first, then supplier PO lines bought for this SO,
then unallocated open supplier PO lines. Lines committed to another SO are not available.
The ERP's two stock sources (`itm_inv` and lot balances) disagree on most items, so coverage
is computed under both and a disagreement that changes the answer goes to a person.

5.1 asks "by when": the checklist's rule of 3 days for stock sales, the actual due date of the
supplier PO lines used, or the quoted lead time counted from the order date for anything still
to be bought (that convention is an assumption, so it yields needs_review, not fail).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ..bundle import Bundle
from ..models import Status
from . import check, ev, finding

STOCK_DAYS = 3  # checklist 5.1: "3 days for stock sales"


def _d(x) -> Decimal:
    return Decimal(str(x or 0))


def _day(x) -> date | None:
    return x.date() if hasattr(x, "date") else x


@dataclass
class Cover:
    stock: Decimal = Decimal(0)
    supply: list[tuple[dict, Decimal]] = field(default_factory=list)  # (po line, qty used)
    short: Decimal = Decimal(0)

    @property
    def latest_supply_due(self) -> date | None:
        dues = [_day(p["due_date"]) for p, _ in self.supply if p.get("due_date")]
        return max(dues) if dues else None

    def outcome(self, quoted_stock: bool) -> str:
        if self.short > 0:
            return "quoted_stock_short" if quoted_stock else "needs_sourcing"
        return "covered"


def quote_lead(qline: dict | None) -> tuple[str | None, int | None]:
    """('STOCK', 0) or ('6-8 WKS', 42) from quo_detl.del_date (free text in this ERP)."""
    if not qline or not qline.get("del_date"):
        return None, None
    s = str(qline["del_date"]).strip().upper()
    if "STOCK" in s:
        return s, 0
    m = re.search(r"(\d+)\s*-\s*(\d+)\s*W", s) or re.search(r"(\d+)\s*W", s)
    if m:
        return s, int(m.group(1)) * 7
    m = re.search(r"(\d+)\s*D", s)
    return (s, int(m.group(1))) if m else (s, None)


def _quote_line(b: Bundle, so_line: dict) -> dict | None:
    for m in b.lines:
        if m.so is so_line:
            return m.quote
    return None


def allocate(b: Bundle, key, lines: list[dict], stock: Decimal) -> dict[int, Cover]:
    """Cover each SO line of one item, in need-date order: its own supplier PO lines, then stock,
    then unallocated open supplier PO lines (an exact quantity and cost match first)."""
    free = [p for p in b.open_supply.get(key, []) if p.get("so_order_no") is None]
    remaining_free = {id(p): _d(p["ord_qty"]) - _d(p.get("qty_recvd")) for p in free}
    pool = stock
    out = {}
    for l in sorted(lines, key=lambda l: (l.get("req_date") or l.get("due_date"), l["lin_no"])):
        need = _d(l["ord_qty"])
        c = Cover()
        for p in b.supply:
            if p.get("so_lin_no") == l["lin_no"] and need > 0:
                q = min(need, _d(p["ord_qty"]) - _d(p.get("qty_recvd")))
                c.supply.append((p, q))
                need -= q
        take = min(need, pool)
        c.stock, pool, need = take, pool - take, need - take
        exact = [p for p in free if _d(p["ord_qty"]) == _d(l["ord_qty"]) and _d(p["unit_cost"]) == _d(l["unit_cost"])]
        for p in exact + [p for p in free if p not in exact]:
            if need <= 0:
                break
            q = min(need, remaining_free[id(p)])
            if q > 0:
                c.supply.append((p, q))
                remaining_free[id(p)] -= q
                need -= q
        c.short = max(need, Decimal(0))
        out[l["lin_no"]] = c
    return out


def _stock_sources(b: Bundle, key) -> tuple[Decimal | None, Decimal | None, list]:
    inv = b.inventory.get(key)
    itm = max(_d(inv["qty_on_hnd"]) - _d(inv["qty_commit"]), Decimal(0)) if inv else None
    lots = b.lots.get(key) or []
    lot = sum((max(_d(x["bal_of_lot"]) - _d(x["qty_commit"]), Decimal(0)) for x in lots), Decimal(0)) if lots else None
    return itm, lot, lots


def _by_item(b: Bundle) -> dict:
    groups: dict = {}
    for l in b.so_lines:
        groups.setdefault((l["item_no"], l["manu_no"]), []).append(l)
    return groups


def _fmt(x) -> str:
    return f"{Decimal(x):,.0f}"


def _po_label(p: dict) -> str:
    return f"PO {p['po_no']} line {p['lin_no']}"


@check("S.stock", "4.1", "Stock and supply cover the order")
def stock(b: Bundle):
    if not b.so_lines:
        return []
    out = []
    for key, lines in _by_item(b).items():
        itm, lot, lots = _stock_sources(b, key)
        primary = itm if itm is not None else (lot or Decimal(0))
        alt = lot if itm is not None else None
        cov = allocate(b, key, lines, primary)
        cov_alt = allocate(b, key, lines, alt or Decimal(0)) if alt is not None else None
        for l in lines:
            n, c = l["lin_no"], cov[l["lin_no"]]
            lead_text, _ = quote_lead(_quote_line(b, l))
            quoted_stock = lead_text is not None and "STOCK" in lead_text
            src_inv = f"itm_inv ({key[0]})"
            evidence = [ev("Order qty", _fmt(l["ord_qty"]), f"ord_detl.ord_qty (SO {b.so_no} line {n})"),
                        ev("itm_inv available", None if itm is None else _fmt(itm), src_inv + " qty_on_hnd - qty_commit" if itm is not None else "no itm_inv row"),
                        ev("Lot balance", None if lot is None else _fmt(lot), f"lot_hedr ({len(lots)} lots)" if lots else "no lots")]
            evidence += [ev("Supply", f"{_fmt(q)} due {_day(p['due_date'])}", _po_label(p) + (" (bought for this SO)" if p.get("so_order_no") else " (unallocated)"))
                         for p, q in c.supply]
            if lead_text:
                evidence.append(ev("Quoted lead time", lead_text, "quo_detl.del_date"))
            title = f"Parts available (line {n})"
            o = c.outcome(quoted_stock)
            if cov_alt is not None and cov_alt[n].outcome(quoted_stock) != o:
                out.append(finding("S.stock", "4.1", title, Status.NEEDS_REVIEW,
                                   f"The ERP's stock sources disagree: itm_inv shows {_fmt(itm)} available, lots show {_fmt(lot)}. "
                                   f"With itm_inv the line is {o.replace('_', ' ')}; with lots it is {cov_alt[n].outcome(quoted_stock).replace('_', ' ')}.",
                                   evidence, severity="medium", line=n))
            elif o == "covered":
                parts = []
                if c.stock:
                    parts.append(f"{_fmt(c.stock)} from stock")
                parts += [f"{_fmt(q)} from {_po_label(p)}" for p, q in c.supply]
                out.append(finding("S.stock", "4.1", title, Status.PASS, "Covered: " + ", ".join(parts) + ".", evidence, line=n))
            elif o == "quoted_stock_short":
                out.append(finding("S.stock", "4.1", title, Status.FAIL,
                                   f"Quoted from stock ({lead_text}), but stock and open supply are {_fmt(c.short)} short.", evidence, severity="high", line=n))
            else:
                out.append(finding("S.stock", "4.1", title, Status.MANUAL,
                                   f"Needs sourcing: {_fmt(c.short)} not covered by stock or open supplier POs.", evidence, severity="low", line=n))
    return out


@check("S.ready", "5.1", "Ready date meets the required date")
def ready_date(b: Bundle):
    if not (b.so_lines and b.as_of):
        return []
    out = []
    for key, lines in _by_item(b).items():
        itm, lot, _ = _stock_sources(b, key)
        cov = allocate(b, key, lines, itm if itm is not None else (lot or Decimal(0)))
        inv = b.inventory.get(key) or {}
        for l in lines:
            n, c = l["lin_no"], cov[l["lin_no"]]
            need = _day(l.get("req_date") or l.get("due_date"))
            ack_ship = None
            for m in b.lines:
                if m.so is l and m.ack:
                    from .. import parse
                    ack_ship = parse.to_date(m.ack.ship_date.raw)
            candidates = []  # (date, basis, strength)
            if c.stock:
                candidates.append((b.as_of + timedelta(days=STOCK_DAYS), f"stock: order date + {STOCK_DAYS} days", "rule"))
            if c.supply:
                candidates.append((c.latest_supply_due, "supplier PO due date", "actual"))
            if c.short:
                text, days = quote_lead(_quote_line(b, l))
                if days is not None:
                    candidates.append((b.as_of + timedelta(days=days), f"quoted lead time {text} from the order date", "assumed"))
                elif inv.get("avg_lead_tm"):
                    candidates.append((b.as_of + timedelta(days=int(inv["avg_lead_tm"])), f"average lead time {inv['avg_lead_tm']} days (itm_inv)", "weak"))
                else:
                    candidates.append((None, "no lead time on the quote or item", "weak"))
            ready = max((d for d, _, _ in candidates if d), default=None)
            evidence = [ev("Required", need, f"ord_detl.req_date (SO {b.so_no} line {n})")]
            if ack_ship:
                evidence.append(ev("Ack est ship", ack_ship, "SO ack"))
            evidence += [ev("Ready basis", d, basis) for d, basis, _ in candidates]
            title = f"Ready by the required date (line {n})"
            promised = min(x for x in (need, ack_ship) if x) if (need or ack_ship) else None
            if any(d is None for d, _, _ in candidates) or promised is None:
                out.append(finding("S.ready", "5.1", title, Status.UNVERIFIABLE, "No lead time for the part still to be bought.", evidence, severity="low", line=n))
                continue
            late = [(d, basis, s) for d, basis, s in candidates if d > promised]
            if not late:
                out.append(finding("S.ready", "5.1", title, Status.PASS, f"Ready by {ready}, required {promised}.", evidence, line=n))
                continue
            strengths = {s for _, _, s in late}
            msg = f"Ready {ready} is after the required/promised date {promised} ({'; '.join(basis for _, basis, _ in late)})."
            if strengths & {"actual", "rule"}:
                out.append(finding("S.ready", "5.1", title, Status.FAIL, msg, evidence, severity="high", line=n))
            elif "assumed" in strengths:
                out.append(finding("S.ready", "5.1", title, Status.NEEDS_REVIEW, msg + " Lead time is counted from the order date (assumed).",
                                   evidence, severity="medium", line=n))
            else:
                out.append(finding("S.ready", "5.1", title, Status.UNVERIFIABLE, msg + " Based on an average lead time only.", evidence, severity="low", line=n))
    return out


@check("S.supplier_po", "4.5", "Supplier POs match what the order needs")
def supplier_pos(b: Bundle):
    """Cost of supplier PO lines bought for this SO, and lines on those POs that no SO needs."""
    if not b.so:
        return []
    out = []
    so_by_line = {l["lin_no"]: l for l in b.so_lines}
    for p in b.supply:
        l = so_by_line.get(p.get("so_lin_no"))
        if not l:
            continue
        so_c, po_c = _d(l["unit_cost"]), _d(p["unit_cost"])
        evidence = [ev("SO cost", f"{so_c.normalize():f}", f"ord_detl.unit_cost (SO {b.so_no} line {l['lin_no']})"),
                    ev("Supplier PO cost", f"{po_c.normalize():f}", f"po_detl.unit_cost ({_po_label(p)}, vendor {p['vend_no']})"),
                    ev("SO vendor", l.get("vend_no"), f"ord_detl.vend_no (SO {b.so_no} line {l['lin_no']})")]
        if so_c != po_c:
            vend_note = f" The SO line names vendor {l.get('vend_no')}, the PO is with {p['vend_no']}." if l.get("vend_no") != p["vend_no"] else ""
            out.append(finding("S.cost", "2.6", f"Our cost matches the supplier PO (line {l['lin_no']})", Status.FAIL,
                               f"SO cost {so_c.normalize():f} but {_po_label(p)} buys at {po_c.normalize():f}.{vend_note}",
                               evidence, severity="medium", line=l["lin_no"]))
        else:
            out.append(finding("S.cost", "2.6", f"Our cost matches the supplier PO (line {l['lin_no']})", Status.PASS,
                               f"SO cost equals {_po_label(p)}.", evidence, line=l["lin_no"]))
    linked_pos = sorted({p["po_no"] for p in b.supply})
    for po_no in linked_pos:
        for p in b.supplier_po_lines.get(po_no, []):
            if p.get("so_order_no") is None:
                value = _d(p["ord_qty"]) * _d(p["unit_cost"])
                out.append(finding("S.unrequested", "4.5", f"Supplier PO {po_no} line {p['lin_no']} not tied to an order", Status.NEEDS_REVIEW,
                                   f"PO {po_no}, bought for this SO, also buys {_fmt(p['ord_qty'])} x {p['item_no']} ({p['manu_no']}) worth "
                                   f"${value:,.2f}, which no SO line needs.",
                                   [ev("Supplier PO line", f"{p['item_no']} x {_fmt(p['ord_qty'])} @ {_d(p['unit_cost']).normalize():f}", f"po_detl ({_po_label(p)})")],
                                   severity="medium"))
    return out


@check("S.vendors", "4.4", "Vendors are in good standing")
def vendors(b: Bundle):
    if not (b.so and b.as_of):
        return []
    used = {}
    for l in b.so_lines:
        if l.get("vend_no"):
            used.setdefault(l["vend_no"], []).append(f"SO line {l['lin_no']}")
    for p in b.supply:
        used.setdefault(p["vend_no"], []).append(_po_label(p))
    out = []
    for v, where in sorted(used.items()):
        row = b.vendors.get(v)
        if not row:
            continue
        name = row.get("po_name") or v
        evidence = [ev("Vendor", f"{v} {name}", f"vendor ({v})"), ev("Used on", ", ".join(where), "SO lines and supplier PO lines")]
        if row.get("pay_hold"):
            out.append(finding("S.vendor", "4.4", f"Vendor {v} standing", Status.NEEDS_REVIEW,
                               f"{name} is on payment hold, so purchasing from them may be blocked ({', '.join(where)}).",
                               evidence + [ev("pay_hold", True, f"vendor.pay_hold ({v})")], severity="medium"))
        exp = _day(row.get("iso_cert_exp"))
        if exp and exp < b.as_of:
            out.append(finding("S.vendor", "4.4", f"Vendor {v} certification", Status.NEEDS_REVIEW,
                               f"{name}'s {row.get('iso_cert') or 'ISO'} certificate expired on {exp}, before the order date ({', '.join(where)}).",
                               evidence + [ev("Cert expiry", exp, f"vendor.iso_cert_exp ({v})")], severity="medium"))
        elif exp and exp <= b.as_of + timedelta(days=30):
            out.append(finding("S.vendor", "4.4", f"Vendor {v} certification", Status.INFO,
                               f"{name}'s {row.get('iso_cert') or 'ISO'} certificate expires on {exp}, within 30 days of the order.",
                               evidence + [ev("Cert expiry", exp, f"vendor.iso_cert_exp ({v})")]))
    if not out:
        out.append(finding("S.vendor", "4.4", "Vendors are in good standing", Status.PASS, "No pay holds or expired certificates.", []))
    return out
