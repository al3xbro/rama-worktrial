"""Line checks: every PO line against its SO line (ERP) and ack line, plus the quote."""
from __future__ import annotations

from decimal import Decimal

from .. import parse
from ..bundle import Bundle, LineMatch
from ..models import Status
from . import check, ev, finding
from .header import compare


def _d(x) -> Decimal | None:
    return None if x is None else parse.num(str(x))


def _so_src(b: Bundle, m: LineMatch, col: str) -> str:
    return f"ord_detl.{col} (SO {b.so_no} line {m.so['lin_no']})"


def _fmt(x) -> str:
    if isinstance(x, Decimal):
        return f"{x.normalize():f}"
    return str(x)


@check("L.lines", "2.L", "PO lines vs SO lines")
def line_checks(b: Bundle):
    out = []
    if not b.lines:
        return [finding("L.lines", "2.L", "PO lines vs SO lines", Status.UNVERIFIABLE, "No lines to compare.", severity="high")]
    for m in b.lines:
        out += _line(b, m)
    return out


def _line(b: Bundle, m: LineMatch):
    out = []
    if m.po and not m.so:
        pl = m.po
        return [finding("L.missing_on_so", "2.L", f"PO line {m.po_index} is not on the SO", Status.FAIL,
                        f"The PO asks for {pl.qty.raw} x {pl.mpn.raw} ({pl.manufacturer.raw}) and no SO line matches it.",
                        [ev("PO", f"{pl.mpn.raw} · {pl.qty.raw} @ {pl.unit_price.raw}", b.src("PO", pl.mpn))], severity="high", line=m.po_index)]
    if m.so and not m.po:
        if not b.po:
            return []
        s = m.so
        return [finding("L.not_on_po", "2.L", f"SO line {s['lin_no']} was not requested on the PO", Status.FAIL,
                        f"The SO has {s['ord_qty']} x {s['item_no']} ({s['manu_no']}) and the PO has no matching line.",
                        [ev("SO", f"{s['item_no']} · {s['ord_qty']} @ {_fmt(_d(s['unit_price']))}", _so_src(b, m, "item_no"))], severity="high", line=s["lin_no"])]

    pl, s, al, n = m.po, m.so, m.ack, m.so["lin_no"]
    tag = f"line {n}"

    # Manufacturer part number, exactly as printed (checklist 2.5).
    vals = [("PO", (pl.mpn.raw or "").strip() or None, pl.mpn.raw, b.src("PO", pl.mpn)),
            ("SO", s["item_no"], s["item_no"], _so_src(b, m, "item_no"))]
    if al:
        vals.append(("Ack", (al.mpn.raw or "").strip() or None, al.mpn.raw, b.src("Ack", al.mpn)))
    f = compare("L.mpn", "2.5", f"Part number exact ({tag})", vals, severity="high", what="manufacturer part number")
    if m.near_miss:
        f.message = (f"Part number differs: PO {pl.mpn.raw} vs SO {s['item_no']}. The lines were paired because quantity and "
                     f"price agree; check whether the SO has the wrong part.")
    f.line = n
    out.append(f)

    # Manufacturer.
    po_code = parse.mfr_code(pl.manufacturer.raw)
    mfr_ev = [ev("PO", pl.manufacturer.raw, b.src("PO", pl.manufacturer)), ev("SO", s["manu_no"], _so_src(b, m, "manu_no"))]
    if pl.manufacturer.raw and po_code is None:
        out.append(finding("L.mfr", "2.L", f"Manufacturer ({tag})", Status.UNVERIFIABLE,
                           f"PO manufacturer {pl.manufacturer.raw!r} is not in the manufacturer alias table; the SO has {s['manu_no']}.",
                           mfr_ev, line=n))
    else:
        mv = [("PO", po_code, pl.manufacturer.raw, b.src("PO", pl.manufacturer)), ("SO", s["manu_no"], s["manu_no"], _so_src(b, m, "manu_no"))]
        if al:
            mv.append(("Ack", parse.mfr_code(al.manufacturer.raw), al.manufacturer.raw, b.src("Ack", al.manufacturer)))
        f = compare("L.mfr", "2.L", f"Manufacturer ({tag})", mv, severity="high", what="manufacturer")
        f.line = n
        out.append(f)

    # Quantity, unit price, required date.
    qv = [("PO", b.po_num(pl.qty.raw), pl.qty.raw, b.src("PO", pl.qty)), ("SO", _d(s["ord_qty"]), s["ord_qty"], _so_src(b, m, "ord_qty"))]
    pv = [("PO", b.po_num(pl.unit_price.raw), pl.unit_price.raw, b.src("PO", pl.unit_price)), ("SO", _d(s["unit_price"]), _fmt(_d(s["unit_price"])), _so_src(b, m, "unit_price"))]
    po_date = parse.to_date(pl.required_date.raw, day_first=b.po_comma)
    so_date = s["req_date"].date() if s.get("req_date") else None
    dv = [("PO", po_date, pl.required_date.raw, b.src("PO", pl.required_date)), ("SO", so_date, so_date, _so_src(b, m, "req_date"))]
    if al:
        qv.append(("Ack", b.ack_num(al.qty.raw), al.qty.raw, b.src("Ack", al.qty)))
        pv.append(("Ack", b.ack_num(al.unit_price.raw), al.unit_price.raw, b.src("Ack", al.unit_price)))
        dv.append(("Ack", parse.to_date(al.required_date.raw), al.required_date.raw, b.src("Ack", al.required_date)))
    for cid, title, vals, what in (("L.qty", "Quantity", qv, "quantity"), ("L.price", "Unit price", pv, "unit price"), ("L.date", "Required date", dv, "required date")):
        f = compare(cid, "2.L", f"{title} ({tag})", vals, severity="high", what=what)
        f.line = n
        out.append(f)

    # Customer part number, when the PO gives one (checklist 2.4). A "customer part number" that is
    # just the manufacturer part number again is not one (the model sometimes copies it on photos).
    if pl.customer_pn.raw and parse.mpn_key(pl.customer_pn.raw) != parse.mpn_key(pl.mpn.raw):
        cv = [("PO", pl.customer_pn.raw.strip(), pl.customer_pn.raw, b.src("PO", pl.customer_pn)),
              ("SO", s.get("cust_itmno") or None, s.get("cust_itmno") or "(blank)", _so_src(b, m, "cust_itmno"))]
        if s.get("cust_itmno"):
            f = compare("L.cust_pn", "2.4", f"Customer part number ({tag})", cv, what="customer part number")
        else:
            f = finding("L.cust_pn", "2.4", f"Customer part number ({tag})", Status.FAIL,
                        f"The PO gives customer part number {pl.customer_pn.raw} but the SO line has none.",
                        [ev(l, sh, src) for l, _, sh, src in cv])
        f.line = n
        out.append(f)

    # Quote (checklist 2.6).
    if m.quote:
        out += _quote(b, m, tag)
    return out


def _quote_tolerance(b: Bundle) -> tuple[float, bool]:
    for r in b.requirements:
        if r.clause_type == "price_matches_quote":
            return r.params.get("tolerance_pct", 0.0), True
    return 0.0, False


def _quote(b: Bundle, m: LineMatch, tag: str):
    s, qd, n = m.so, m.quote, m.so["lin_no"]
    src_q = f"quo_detl (quote {qd['quote_no']} line {qd['lin_no']})"
    out = []
    tol, customer_clause = _quote_tolerance(b)
    so_p, q_p = _d(s["unit_price"]), _d(qd["unit_price"])
    po_p = b.po_num(m.po.unit_price.raw) if m.po else None
    evidence = [ev("SO", _fmt(so_p), _so_src(b, m, "unit_price")), ev("Quote", _fmt(q_p), src_q + " unit_price")]
    if m.po:
        evidence.insert(0, ev("PO", m.po.unit_price.raw, b.src("PO", m.po.unit_price)))
    if so_p is not None and q_p:
        pct = float((so_p - q_p) / q_p * 100)
        if so_p == q_p:
            out.append(finding("Q.price", "2.6", f"Price matches the quote ({tag})", Status.PASS, "SO price equals the quoted price.", evidence, line=n))
        elif abs(pct) <= tol:
            out.append(finding("Q.price", "2.6", f"Price matches the quote ({tag})", Status.PASS,
                               f"SO price is {pct:+.1f}% from the quote, within the customer's {tol:g}% tolerance.", evidence, line=n))
        else:
            status = Status.NEEDS_APPROVAL if customer_clause else Status.FAIL
            extra = f" The customer requires buyer approval above {tol:g}%." if customer_clause else ""
            po_note = f" The PO price ({m.po.unit_price.raw}) matches the quote." if po_p == q_p else ""
            out.append(finding("Q.price", "2.6", f"Price matches the quote ({tag})", status,
                               f"SO price {_fmt(so_p)} is {pct:+.1f}% from the quoted {_fmt(q_p)}.{extra}{po_note}", evidence, severity="high", line=n))
    so_c, q_c = _d(s["unit_cost"]), _d(qd["unit_cost"])
    if so_c is not None and q_c is not None:
        st = Status.PASS if so_c == q_c else Status.FAIL
        msg = "SO cost equals the quoted cost." if st == Status.PASS else f"SO cost {_fmt(so_c)} differs from the quoted cost {_fmt(q_c)}."
        out.append(finding("Q.cost", "2.6", f"Our cost matches the quote ({tag})", st, msg,
                           [ev("SO", _fmt(so_c), _so_src(b, m, "unit_cost")), ev("Quote", _fmt(q_c), src_q + " unit_cost")], line=n))
    return out


@check("Q.link", "2.6", "Approved quote on file")
def quote_link(b: Bundle):
    if not b.so:
        return []
    if b.quote:
        return [finding("Q.link", "2.6", "Approved quote on file", Status.PASS, f"SO links to quote {b.quote['quote_no']}.",
                        [ev("SO", b.so.get("quote_no"), f"ord_hedr.quote_no (SO {b.so_no})")])]
    _, required = _quote_tolerance(b)
    return [finding("Q.link", "2.6", "Approved quote on file", Status.UNVERIFIABLE,
                    "No quote is linked to this SO, so price and cost cannot be checked against an approved quote."
                    + (" The customer requires the price to match the quote." if required else ""),
                    [ev("SO", "(blank)", f"ord_hedr.quote_no (SO {b.so_no})")], severity="medium" if required else "low")]


def _arith(b: Bundle, which: str):
    x = b.po if which == "PO" else b.ack
    if x is None:
        return []
    comma = b.po_comma if which == "PO" else b.ack_comma
    bad, total = [], Decimal(0)
    for i, l in enumerate(x.doc.lines, 1):
        q, p, e = parse.num(l.qty.raw, comma), parse.num(l.unit_price.raw, comma), parse.num(l.extended.raw, comma)
        if e is not None:
            total += e
        if parse.ties_out(q, p, e) is False:
            bad.append(f"line {i}: {l.qty.raw} x {l.unit_price.raw} != {l.extended.raw}")
    t = x.doc.totals
    stated = parse.num(t.subtotal.raw, comma) or ((parse.num(t.total.raw, comma) or Decimal(0)) - (parse.num(t.tax.raw, comma) or Decimal(0)) if t.total.raw else None)
    if stated is not None and abs(stated - total) > Decimal("0.011"):
        bad.append(f"lines sum to {total} but the document states {stated}")
    evidence = [ev(which, t.total.raw, b.src(which, t.total))]
    if bad:
        return [finding(f"T.{which.lower()}", "2.T", f"{which} arithmetic", Status.FAIL, "; ".join(bad), evidence, severity="medium")]
    return [finding(f"T.{which.lower()}", "2.T", f"{which} arithmetic", Status.PASS, "Line extensions and total tie out.", evidence)]


@check("T.arithmetic", "2.T", "Document arithmetic")
def arithmetic(b: Bundle):
    return _arith(b, "PO") + _arith(b, "Ack")
