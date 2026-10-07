"""Email handling without the network: grouping POs in one email, the self-sent gate, the
state store, and the API."""

import pytest

from agent import config, mail, orchestrator, state, triage
from conftest import trial_doc


def po(number="PO-MER26-0037", source="PO.pdf", date_raw="8/31/2026", note=None):
    x = trial_doc("meridian_po")
    x = x.model_copy(deep=True)
    x.doc.header.doc_number.raw = number
    x.doc.header.doc_date.raw = date_raw
    x.source = source
    x.sha256 = f"{number}-{source}-{date_raw}"
    if note:
        x.doc.notes[0].raw = note
    return x


def test_group_pos_separates_orders_and_orders_versions():
    original = po(source="PO PO-MER26-0037.pdf", date_raw="8/31/2026")
    revised = po(source="PO PO-MER26-0037 Rev B.pdf", date_raw="9/3/2026")
    other = po(number="PO-MER26-0040", source="PO PO-MER26-0040.pdf")
    groups = orchestrator.group_pos([revised, other, original])
    by_number = {latest.doc.header.doc_number.raw: (latest, earlier) for latest, earlier in groups}
    assert set(by_number) == {"PO-MER26-0037", "PO-MER26-0040"}
    latest, earlier = by_number["PO-MER26-0037"]
    assert latest.source.endswith("Rev B.pdf") and [e.source for e in earlier] == ["PO PO-MER26-0037.pdf"]
    assert by_number["PO-MER26-0040"][1] == []


def test_group_pos_uses_date_when_no_revision_marker():
    a, b = po(source="a.pdf", date_raw="8/31/2026"), po(source="b.pdf", date_raw="9/5/2026")
    (latest, earlier), = orchestrator.group_pos([b, a])
    assert latest.source == "b.pdf" and earlier[0].source == "a.pdf"


def test_identical_copies_count_once():
    a = po(source="PO.pdf")
    (latest, earlier), = orchestrator.group_pos([a, a.model_copy(deep=True)])
    assert earlier == []


def test_self_sent_mail_is_ignored_without_a_model_call(monkeypatch):
    monkeypatch.setattr(triage.llm, "parse", lambda *a, **k: pytest.fail("no model call expected"))
    e = mail.Email(id="x", subject="[Order agent] HOLD: PO PO-MER26-0037", sender=config.MAILBOX, received="", body="")
    assert triage.classify(e).label == "ignored_self"
    e2 = mail.Email(id="y", subject="Re: order", sender="someone@example.com", received="", body="", headers={"X-Order-Agent": "1"})
    assert triage.classify(e2).label == "ignored_self"


@pytest.fixture
def temp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    return tmp_path


def test_state_and_api(temp_state):
    from fastapi.testclient import TestClient

    from agent.api import app
    from agent.models import Finding, Status
    from agent.verify import VerificationResult

    state.upsert_message("m1", received="2026-10-06T10:00:00Z", sender="buyer@example.com", subject="PO 1", state="done", label="customer_po")
    result = VerificationResult(
        order={"so_number": 503097, "cust_no": "C1002", "customer": "Meridian", "cust_po": "PO-MER26-0037"},
        recommendation="hold", reasons=["x"], counts={"fail": 1}, checklist=[], problems=[],
        findings=[Finding(check_id="H.payment_terms", checklist_ref="1.1", title="Payment terms", status=Status.FAIL, severity="high", message="Net 30 vs Net 45"),
                  Finding(check_id="L.qty", checklist_ref="2.L", title="Quantity", status=Status.PASS, message="ok")])
    state.add_run(result, "m1")
    client = TestClient(app)
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/emails").json()[0]["id"] == "m1"
    assert client.get("/emails/m1").json()["runs"][0]["so_number"] == 503097
    assert client.get("/orders").json()[0]["recommendation"] == "hold"
    issues = client.get("/issues").json()
    assert [i["check_id"] for i in issues] == ["H.payment_terms"]  # passes are not issues
    assert client.get("/status").json()["messages"] == {"done": 1}
    assert client.get("/emails/unknown").status_code == 404
