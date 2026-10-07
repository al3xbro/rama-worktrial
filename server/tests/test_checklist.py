"""Every item on Demo's order checklist is either covered by a check or explicitly marked manual
with a reason. Nothing on the checklist may be silently skipped."""
import os

import pytest

from agent import verify
from agent.checklist import CHECKLIST
from conftest import trial_doc


@pytest.mark.skipif(not os.getenv("PGHOST"), reason="needs the ERP (PG* variables in .env)")
def test_every_checklist_item_is_checked_or_explained():
    r = verify.verify(trial_doc("meridian_po"), trial_doc("meridian_ack"), write=False)
    covered = {f.checklist_ref for f in r.findings}
    uncovered = [i.ref for i in CHECKLIST if i.ref not in covered and not i.manual_reason]
    assert uncovered == [], f"checklist items with neither a check nor a manual reason: {uncovered}"


def test_manual_items_have_reasons():
    from agent.models import Status
    for i in CHECKLIST:
        if i.manual_reason is not None:
            assert len(i.manual_reason) > 10, i.ref
        assert i.default in (Status.MANUAL, Status.UNVERIFIABLE)
