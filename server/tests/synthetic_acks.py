"""Synthetic supplier acknowledgements.

The trial data has our POs to suppliers but no acknowledgements back. These are built from our
real PO (read from data/): every line confirmed as ordered, except for the changes below, so no
trial values live in the repository.
- 81793: confirmed as ordered (the clean case).
- 81797: line 1 three weeks late, with a date code from 2023.
- 81766: line 3 substituted with an "-01" part, 100 short, about 1% dearer and two weeks late.

  uv run python tests/synthetic_acks.py OUT_DIR   # also write them as PDFs, to upload or email
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the server package, when run as a script

from agent import parse  # noqa: E402
from agent.models import DocHeader, DocLine, Extracted, OrderDoc, Totals, Val  # noqa: E402

# Per PO: changes by line number (part_suffix, qty_delta, price_factor, days_late, coo, date_code) and notes.
CASES: dict[int, dict] = {
    81793: {"lines": {1: {"coo": "US", "date_code": "2531"}, 2: {"coo": "CN", "date_code": "2528"}},
            "notes": ["Parts ship with manufacturer CoC and full traceability to the OCM."]},
    81797: {"lines": {1: {"days_late": 21, "coo": "CN", "date_code": "2318"}, 2: {"coo": "TH", "date_code": "2532"}},
            "notes": ["Delivery dates are estimates. Line 1 ships from distributor stock."]},
    81766: {"lines": {1: {"coo": "US", "date_code": "2530"}, 2: {"coo": "CN", "date_code": "2533"},
                      3: {"part_suffix": "-01", "qty_delta": -100, "price_factor": 1.0113, "days_late": 14, "coo": "US", "date_code": "2530"}},
            "notes": ["Line 3 supplied as an equivalent part. Quantity limited by availability."]},
}


def _v(raw) -> Val:
    return Val(raw=None if raw is None else str(raw), loc=None)


def _day(d) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def build(po: Extracted, po_no: int) -> Extracted:
    """The supplier's acknowledgement of our PO `po`, with the changes in CASES[po_no]."""
    case = CASES[po_no]
    lines, total = [], 0.0
    for l in po.doc.lines:
        c = case["lines"].get(int(l.line_no.raw), {})
        qty = int(parse.num(l.qty.raw)) + c.get("qty_delta", 0)
        price = round(float(parse.num(l.unit_price.raw)) * c.get("price_factor", 1), 5)
        ship = parse.to_date(l.required_date.raw) + timedelta(days=c.get("days_late", 0))
        total += qty * price
        lines.append(DocLine(line_no=_v(l.line_no.raw), customer_pn=_v(None), mpn=_v(l.mpn.raw + c.get("part_suffix", "")),
                             manufacturer=_v(l.manufacturer.raw), description=_v(None), qty=_v(f"{qty:,}"), uom=_v(None),
                             unit_price=_v(f"{price:.5f}"), extended=_v(f"{qty * price:,.2f}"), required_date=_v(None),
                             ship_date=_v(_day(ship)), country_of_origin=_v(c.get("coo")), date_code=_v(c.get("date_code")), line_notes=[]))
    h = po.doc.header
    none = {k: _v(None) for k in ("customer_id", "addressed_to", "buyer_name", "buyer_email", "incoterm", "carrier_account",
                                  "freight_terms", "ship_option", "currency")}
    header = DocHeader(doc_type="supplier_ack", doc_number=_v(f"ACK-{po_no}"), doc_date=_v(h.doc_date.raw), customer_po_ref=_v(str(po_no)),
                       customer_name=_v(h.customer_name.raw), bill_to=_v(h.bill_to.raw), ship_to=_v(h.ship_to.raw),
                       payment_terms=_v(h.payment_terms.raw), ship_via=_v(h.ship_via.raw), **none)
    doc = OrderDoc(header=header, lines=lines, notes=[_v(n) for n in case["notes"]], references=[],
                   totals=Totals(subtotal=_v(None), tax=_v(None), total=_v(f"{total:,.2f}")))
    return Extracted(doc=doc, source=f"synthetic acknowledgement of PO {po_no}", sha256=f"synthetic-ack-{po_no}", method="fixture")


def write_pdf(ack: Extracted, supplier: str, path: Path) -> None:
    import pymupdf

    h, rows = ack.doc.header, "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in (l.line_no.raw, l.mpn.raw, l.manufacturer.raw, l.qty.raw, l.unit_price.raw, l.extended.raw,
                                                    l.ship_date.raw, l.country_of_origin.raw or "", l.date_code.raw or "")) + "</tr>"
        for l in ack.doc.lines)
    html = f"""<h1>{supplier}</h1><h1>ORDER ACKNOWLEDGEMENT</h1>
<table><tr><th>Our order #</th><th>Date</th><th>Your PO #</th><th>Terms</th><th>Ship via</th></tr>
<tr><td>{h.doc_number.raw}</td><td>{h.doc_date.raw}</td><td>{h.customer_po_ref.raw}</td><td>{h.payment_terms.raw}</td><td>{h.ship_via.raw}</td></tr></table>
<p>Ship to: {h.ship_to.raw or h.customer_name.raw}</p>
<table><tr><th>Ln</th><th>Part number</th><th>Mfr</th><th>Qty</th><th>Unit price</th><th>Extended</th><th>Promised ship</th><th>COO</th><th>Date code</th></tr>
{rows}<tr><td colspan="5"></td><td><b>{ack.doc.totals.total.raw}</b></td><td colspan="3"></td></tr></table>
<p>{' '.join(n.raw for n in ack.doc.notes)}</p>"""
    css = "body{font-family:sans-serif;font-size:9pt} h1{font-size:15pt;margin:0 0 4pt 0} table{border-collapse:collapse;width:100%;margin-top:8pt} td,th{border:1px solid #999;padding:3pt;text-align:left} th{background:#eee}"
    doc = pymupdf.open()
    doc.new_page(width=612, height=792).insert_htmlbox(pymupdf.Rect(36, 36, 576, 756), html, css=css)
    doc.save(path)


if __name__ == "__main__":
    from conftest import supplier_po

    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    for po_no in CASES:
        po = supplier_po(po_no)
        supplier = (po.doc.header.addressed_to.raw or "Supplier").split("\n")[0]
        path = out / f"synthetic-ack-{po_no}.pdf"
        write_pdf(build(po, po_no), supplier, path)
        print("wrote", path)
