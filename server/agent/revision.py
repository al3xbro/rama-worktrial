"""PO revisions: detect them, diff them, and compare findings with the previous run.

Detection is code: a PO is a revision when we have already verified a PO with the same
(customer, PO number) and different bytes. The order-desk agent can also pass a previous
version explicitly (for example when the email says "revised PO" but the number changed).
What to do about a revision is the agent's decision; this module only reports the facts.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from . import config, parse
from .models import DocLine, Extracted, Finding, Status

INDEX = config.STORE_DIR / "po_index.json"

HEADER_FIELDS = ["doc_date", "payment_terms", "incoterm", "ship_via", "carrier_account", "ship_to", "bill_to", "ship_option", "currency"]
LINE_FIELDS = ["mpn", "manufacturer", "customer_pn", "qty", "unit_price", "required_date"]


def _key(cust_no: str, cust_po: str) -> str:
    return f"{cust_no.strip()}|{parse.mpn_key(cust_po)}"


def _load_index() -> dict:
    return json.loads(INDEX.read_text()) if INDEX.exists() else {}


def previous_versions(cust_no: str, cust_po: str, sha256: str) -> list[dict]:
    """Earlier versions of this PO (different bytes), newest first."""
    entries = _load_index().get(_key(cust_no, cust_po), [])
    return sorted([e for e in entries if e["sha256"] != sha256], key=lambda e: e["at"], reverse=True)


_LOCK = threading.Lock()


def record(cust_no: str, cust_po: str, po: Extracted, run_dir: Path) -> None:
    with _LOCK:  # batch runs verify orders in parallel threads
        index = _load_index()
        entries = index.setdefault(_key(cust_no, cust_po), [])
        if not any(e["sha256"] == po.sha256 and e["run_dir"] == str(run_dir) for e in entries):
            entries.append({"sha256": po.sha256, "source": po.source, "run_dir": str(run_dir), "at": datetime.now().isoformat(timespec="seconds")})
        INDEX.parent.mkdir(parents=True, exist_ok=True)
        tmp = INDEX.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=1))
        os.replace(tmp, INDEX)  # atomic, so a concurrent reader never sees half a file


def load_previous(entry: dict) -> tuple[Extracted | None, list[Finding]]:
    run = Path(entry["run_dir"])
    po = Extracted.model_validate_json((run / "po.json").read_text()) if (run / "po.json").exists() else None
    findings = []
    if (run / "result.json").exists():
        findings = [Finding.model_validate(f) for f in json.loads((run / "result.json").read_text())["findings"]]
    return po, findings


def _norm(raw: str | None) -> str:
    return parse.squash(raw)


def _line_key(l: DocLine) -> str | None:
    return parse.mpn_key(l.mpn.raw)


def diff(old: Extracted, new: Extracted) -> list[dict]:
    """Header and line changes between two versions of the same PO."""
    changes = []
    for f in HEADER_FIELDS:
        a, b = getattr(old.doc.header, f), getattr(new.doc.header, f)
        if _norm(a.raw) != _norm(b.raw):
            changes.append({"where": "header", "field": f, "old": a.raw, "new": b.raw})
    remaining = list(enumerate(old.doc.lines, 1))
    for j, nl in enumerate(new.doc.lines, 1):
        cands = [(i, ol) for i, ol in remaining if _line_key(ol) == _line_key(nl)]
        # Same MPN twice (e.g. Norwood): pair on quantity, then price.
        cands.sort(key=lambda c: (_norm(c[1].qty.raw) != _norm(nl.qty.raw), _norm(c[1].unit_price.raw) != _norm(nl.unit_price.raw)))
        if not cands:
            changes.append({"where": f"line {j}", "field": "line", "old": None, "new": f"{nl.qty.raw} x {nl.mpn.raw} @ {nl.unit_price.raw}", "change": "added"})
            continue
        i, ol = cands[0]
        remaining.remove((i, ol))
        for f in LINE_FIELDS:
            a, b = getattr(ol, f).raw, getattr(nl, f).raw
            if _norm(a) != _norm(b):
                changes.append({"where": f"line {j} ({nl.mpn.raw})", "field": f, "old": a, "new": b})
    for i, ol in remaining:
        changes.append({"where": f"old line {i}", "field": "line", "old": f"{ol.qty.raw} x {ol.mpn.raw} @ {ol.unit_price.raw}", "new": None, "change": "removed"})
    old_notes = {_norm(n.raw): n.raw for n in old.doc.notes}
    new_notes = {_norm(n.raw): n.raw for n in new.doc.notes}
    for k in new_notes.keys() - old_notes.keys():
        changes.append({"where": "notes", "field": "note", "old": None, "new": new_notes[k], "change": "added"})
    for k in old_notes.keys() - new_notes.keys():
        changes.append({"where": "notes", "field": "note", "old": old_notes[k], "new": None, "change": "removed"})
    return changes


ISSUE = {Status.FAIL, Status.NEEDS_APPROVAL, Status.NEEDS_REVIEW}


def compare_findings(previous: list[Finding], current: list[Finding]) -> dict:
    key = lambda f: (f.check_id, f.line, f.title)
    prev = {key(f): f for f in previous if f.status in ISSUE}
    curr = {key(f): f for f in current if f.status in ISSUE}
    return {
        "resolved": [f"{f.title}: {f.message}" for k, f in prev.items() if k not in curr],
        "new": [f"{f.title}: {f.message}" for k, f in curr.items() if k not in prev],
        "still_open": [f"{f.title}: {f.message}" for k, f in curr.items() if k in prev],
    }
