from agent import revision
from agent.models import Finding, Status
from conftest import trial_doc


def meridian():
    return trial_doc("meridian_po")


def test_identical_versions_have_no_changes():
    assert revision.diff(meridian(), meridian()) == []


def test_diff_reports_header_line_and_note_changes():
    old, new = meridian(), meridian()
    new.doc.header.payment_terms.raw = "Net 45"
    new.doc.lines[0].qty.raw = "250,000"
    new.doc.lines = new.doc.lines[:2]  # line 3 dropped
    new.doc.notes = new.doc.notes[1:]  # "Ship complete..." removed
    changes = revision.diff(old, new)
    fields = {(c["field"], c.get("change")) for c in changes}
    assert ("payment_terms", None) in fields
    assert ("qty", None) in fields
    assert ("line", "removed") in fields
    assert ("note", "removed") in fields


def test_compare_findings_resolved_and_new():
    f = lambda cid, status: Finding(check_id=cid, checklist_ref="1.1", title=cid, status=status, message="m")
    prev = [f("H.payment_terms", Status.FAIL), f("R.partials", Status.FAIL)]
    curr = [f("H.payment_terms", Status.PASS), f("R.partials", Status.FAIL), f("L.qty", Status.FAIL)]
    out = revision.compare_findings(prev, curr)
    assert [x.split(":")[0] for x in out["resolved"]] == ["H.payment_terms"]
    assert [x.split(":")[0] for x in out["still_open"]] == ["R.partials"]
    assert [x.split(":")[0] for x in out["new"]] == ["L.qty"]
