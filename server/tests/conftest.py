"""Shared test helpers.

Tests read the trial documents from data/ (not in the repository) through the extraction cache,
so after the first run they make no model calls. They skip when the trial data is not there.
Supplier acknowledgements, which the trial data does not have, are built from our real PO
(synthetic_acks.py).
"""
import os
from pathlib import Path

import pytest

DATA = Path(os.getenv("TRIAL_DATA_DIR") or Path(__file__).resolve().parents[2] / "data" / "order_docs")

TRIAL_DOCS = {
    "meridian_po": "01-meridian-medical/PO PO-MER26-0037.pdf",
    "meridian_ack": "01-meridian-medical/Sales Order Acknowledgement-503097.pdf",
}


def _read(path: Path):
    from agent import ingest

    if not path.exists():
        pytest.skip(f"trial document {path.name} not present (data/ is not in the repository)")
    return ingest.extract(ingest.normalize_file(path))


def trial_doc(name: str):
    """A trial document as the extractor reads it (cached after the first run)."""
    return _read(DATA / TRIAL_DOCS[name])


def supplier_po(po_no: int):
    """Our PO to a supplier, as sent (the Sent Items copy in the trial data)."""
    hits = sorted(DATA.rglob(f"Purchase Order {po_no}.pdf")) if DATA.exists() else []
    return _read(hits[0] if hits else DATA / f"Purchase Order {po_no}.pdf")


def synthetic_ack(po_no: int):
    """The supplier's acknowledgement of our PO, synthetic (see synthetic_acks.py)."""
    from synthetic_acks import build

    return build(supplier_po(po_no), po_no)
