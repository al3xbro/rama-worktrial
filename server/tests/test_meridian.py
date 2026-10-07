"""End to end on Meridian (order 01) with the trial documents (read through the extraction cache) and the live ERP.

Also mutation tests: change one value on the PO the way a real discrepancy would, and the
right check must catch it. Skipped when no database is configured.
"""
import os

import pytest

from agent import verify
from agent.models import Status
from conftest import trial_doc

pytestmark = pytest.mark.skipif(not os.getenv("PGHOST"), reason="needs the ERP (PG* variables in .env)")


def run(po=None, ack=None):
    po = po or trial_doc("meridian_po")
    ack = ack or trial_doc("meridian_ack")
    return verify.verify(po, ack, write=False)


def statuses(result, check_id, line=None):
    return [f.status for f in result.findings if f.check_id == check_id and (line is None or f.line == line)]


def test_meridian_known_issues():
    r = run()
    assert r.recommendation == "hold"
    assert r.order["so_number"] == 503097
    assert statuses(r, "H.payment_terms") == [Status.FAIL]  # PO Net 30, SO Net 45
    assert statuses(r, "R.partials") == [Status.FAIL]  # forbidden, but ack says Ship Partials
    assert statuses(r, "H.carrier_account") == [Status.PASS]
    for n in (1, 2, 3):
        for cid in ("L.mpn", "L.qty", "L.price", "L.date", "L.mfr"):
            assert statuses(r, cid, n) == [Status.PASS], (cid, n)
    assert statuses(r, "A.credit") == [Status.PASS]


def mutated(path, fn):
    x = trial_doc(path)
    fn(x.doc)
    return x


def test_mutation_quantity():
    r = run(po=mutated("meridian_po", lambda d: setattr(d.lines[0].qty, "raw", "250,000")))
    assert statuses(r, "L.qty", 1) == [Status.FAIL]


def test_mutation_account_digit_swap():
    r = run(po=mutated("meridian_po", lambda d: setattr(d.header.carrier_account, "raw", "000601484")))
    assert statuses(r, "H.carrier_account") == [Status.FAIL]


def test_mutation_part_number_near_miss():
    r = run(po=mutated("meridian_po", lambda d: setattr(d.lines[2].mpn, "raw", "2DD1766R-18")))
    assert Status.FAIL in statuses(r, "L.mpn", 3)


def test_mutation_unrequested_line():
    r = run(po=mutated("meridian_po", lambda d: d.lines.pop(1)))
    assert statuses(r, "L.not_on_po") == [Status.FAIL]


def test_mutation_ship_to_changed():
    r = run(po=mutated("meridian_po", lambda d: setattr(d.header.ship_to, "raw", "Meridian Medical Devices\n2200 Harbor Point Dr\nNorfolk, VA 23510")))
    assert statuses(r, "H.ship_to") == [Status.FAIL]
