"""The supplier side: our POs to suppliers (real, from Sent Items) and supplier acknowledgements
(synthetic: the trial data has none; built from our PO in synthetic_acks.py). Uses the live ERP."""
import os

import pytest

from agent import supplier
from agent.models import Status
from conftest import supplier_po, synthetic_ack

pytestmark = pytest.mark.skipif(not os.getenv("PGHOST"), reason="needs the ERP (PG* variables in .env)")


def run(po: int, with_ack: bool = False):
    return supplier.verify_supplier_order(supplier_po(po), synthetic_ack(po) if with_ack else None, po, write=False)


def statuses(r, check_id, line=None):
    return [f.status for f in r.findings if f.check_id == check_id and (line is None or f.line == line)]


def test_po_81766_birch():
    r = run(81766)
    assert r.recommendation == "hold"
    assert statuses(r, "P.due", 1) == [Status.FAIL] and statuses(r, "P.due", 3) == [Status.FAIL]  # due 11/26, customer needs 9/7 and 9/10
    assert statuses(r, "P.cost", 3) == [Status.FAIL]  # 10.333 vs SO cost 10.112
    assert statuses(r, "P.unrequested", 2) == [Status.NEEDS_REVIEW]  # 230k Murata, no SO
    assert statuses(r, "P.flow.date_code") == [Status.FAIL]
    assert statuses(r, "P.flow.coc") == [Status.FAIL]
    assert Status.NEEDS_REVIEW in statuses(r, "P.vendor_note")  # "No substitutions" not on the PO


def test_po_81797_sable():
    r = run(81797)
    assert statuses(r, "P.flow.date_code") == [Status.FAIL]  # 36 months vs the customer's 12
    assert statuses(r, "P.unrequested", 2) == [Status.NEEDS_REVIEW]  # $197,688 of Bourns, no SO
    assert statuses(r, "P.due", 1) == [Status.PASS]


def test_po_81793_drake_is_clean_apart_from_a_vendor_note():
    r = run(81793)
    issues = [f for f in r.findings if f.status in (Status.FAIL, Status.NEEDS_APPROVAL)]
    assert issues == []
    assert r.recommendation in ("review", "release")


def test_ack_sable_late_and_old_date_code():
    r = run(81797, with_ack=True)
    assert statuses(r, "A.date", 1) == [Status.FAIL]  # 10/15 vs our due 9/24
    assert statuses(r, "A.customer_date", 1) == [Status.FAIL]  # vs Meridian's 10/10
    assert Status.FAIL in statuses(r, "A.date_code", 1)  # 2318 is ~40 months old
    assert statuses(r, "A.part", 1) == [Status.PASS]


def test_ack_birch_substitution_short_and_price():
    r = run(81766, with_ack=True)
    assert statuses(r, "A.part", 3) == [Status.FAIL]
    assert statuses(r, "A.qty", 3) == [Status.FAIL]
    assert statuses(r, "A.price", 3) == [Status.FAIL]
    assert statuses(r, "A.part", 1) == [Status.PASS] and statuses(r, "A.qty", 1) == [Status.PASS]
    assert statuses(r, "A.extra") == []  # the substituted line is paired with our line 3, not reported as extra


def test_ack_drake_matches():
    r = run(81793, with_ack=True)
    assert [f for f in r.findings if f.check_id.startswith("A.") and f.status == Status.FAIL] == []
