"""Parallel email processing, inline images, and dry-run ERP writes."""
import os
import threading
import time

import pytest

from agent import config, erp_write, mail, state, watcher


@pytest.fixture
def temp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    return tmp_path


def test_at_most_five_agents_and_same_sender_keeps_order(monkeypatch):
    monkeypatch.setattr(watcher, "_executor", None)
    monkeypatch.setattr(config, "WATCH_WORKERS", 5)
    running, peak, order = 0, 0, []
    lock = threading.Lock()

    def fake_process(mid, force=False, folder=None):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
            order.append(mid)
        time.sleep(0.05)
        with lock:
            running -= 1

    monkeypatch.setattr(watcher, "process_message", fake_process)
    futures = []
    for i in range(12):  # 12 different senders, all at once
        futures.append(watcher.submit(f"m{i}", f"buyer{i}@example.com"))
    for mid in ("orig", "rev1", "rev2"):  # one sender: an original and two revisions
        futures.append(watcher.submit(mid, "same@example.com"))
    watcher.wait_futures([f for f in futures if f])
    assert peak <= 5, peak
    assert len(order) == 15
    same = [m for m in order if m in ("orig", "rev1", "rev2")]
    assert same == ["orig", "rev1", "rev2"]  # a revision never overtakes its original


def test_inline_images_large_enough_are_documents():
    small_logo = mail.Attachment("image001.png", "image/png", 6000, True, b"x")
    pasted_po = mail.Attachment("image002.png", "image/png", 180000, True, b"x")
    pdf = mail.Attachment("PO.pdf", "application/pdf", 3000, False, b"x")
    assert not small_logo.is_document
    assert pasted_po.is_document
    assert pdf.is_document


@pytest.mark.skipif(not os.getenv("PGHOST"), reason="needs the ERP for the current order status")
def test_hold_is_planned_not_executed_in_dry_run(temp_state, monkeypatch):
    monkeypatch.setattr(config, "ERP_DRY_RUN", True)
    out = erp_write.hold_order(503097, "payment terms and partial shipments differ from the PO", "m1")
    assert out["dry_run"] is True and out["statements"] == 2
    action = state.erp_actions(503097)[0]
    assert action["kind"] == "hold_order" and action["dry_run"] is True
    assert "set status = 'H'" in action["detail"]["statements"][0]["sql"]
    from agent import db
    with db.connect() as conn:  # the order is untouched
        assert db.Erp(conn).row("select trim(status) s from ord_hedr where order_no = 503097")["s"] == "O"


@pytest.mark.skipif(not os.getenv("PGHOST"), reason="needs the ERP for the current order status")
def test_hold_only_from_open_status(temp_state):
    out = erp_write.hold_order(503179, "x")  # already on hold (H)
    assert "skipped" in out


# ---- the check record: written once the order is settled, carrying every check and its evidence


def settled_result():
    """A stored result as the agent leaves it: one check passed, one failed, one it resolved."""
    ev = [{"label": "PO", "value": "Net 30", "source": "PO p1 · Terms"}, {"label": "SO", "value": "CC", "source": "ord_hedr.terms_code"}]
    return {
        "order": {"so_number": 503097, "cust_no": "C1001", "cust_po": "PO-MER26-0037", "customer": "Meridian", "source": None},
        "recommendation": "hold", "reasons": ["Payment terms differ"], "counts": {"fail": 1, "pass": 2},
        "requirements": [{"clause_type": "no_partials", "quote": "Ship complete.", "source": "PO p1", "scope": "order", "params": {}}],
        "findings": [
            {"check_id": "H.payment_terms", "checklist_ref": "1.2", "title": "Payment terms", "status": "fail", "severity": "high",
             "message": "PO Net 30, SO CC", "evidence": ev, "line": None},
            {"check_id": "L.qty", "checklist_ref": "2.3", "title": "Quantity", "status": "pass", "severity": "info",
             "message": "260,000 = 260000", "evidence": [], "line": 1},
            {"check_id": "Q.link", "checklist_ref": "2.L", "title": "Approved quote", "status": "pass", "severity": "info", "message": "",
             "evidence": [], "line": None, "original_status": "unverifiable",
             "resolution": {"by": "agent", "verdict": "met", "reasoning": "price matches", "evidence": []}},
        ],
    }


def test_the_record_carries_every_check_with_its_evidence(temp_state, monkeypatch):
    monkeypatch.setattr(config, "ERP_DRY_RUN", True)
    out = erp_write.record_result(settled_result(), run_id=7, message_id="m1", agent_summary="Held: terms differ.", on_hold=True)
    assert out["dry_run"] and out["statements"] == 4  # the result row plus one row per check, passes included
    action = state.erp_actions(503097)[0]
    head, *items = action["detail"]["statements"]
    assert head["row"]["recommendation"] == "hold" and head["row"]["on_hold"] is True and head["row"]["run_id"] == 7
    assert head["row"]["reasons"] == ["Payment terms differ"] and head["row"]["requirements"][0]["quote"] == "Ship complete."
    assert [i["row"]["status"] for i in items] == ["fail", "pass", "pass"]
    assert items[0]["row"]["evidence"][1] == {"label": "SO", "value": "CC", "source": "ord_hedr.terms_code"}
    assert items[2]["row"]["resolved_by"] == "agent" and items[2]["row"]["original_status"] == "unverifiable"
    for s in action["detail"]["statements"]:  # every placeholder has a value
        assert s["sql"].count("%s") == len(s["params"])


def test_supplier_results_are_recorded_by_po_number(temp_state, monkeypatch):
    monkeypatch.setattr(config, "ERP_DRY_RUN", True)
    r = settled_result() | {"order": {"kind": "supplier", "po_number": 81766, "so_number": 503097, "vendor": "Arrow"}}
    assert erp_write.record_result(r, run_id=8)["dry_run"]
    assert erp_write.record_result(settled_result() | {"order": {"kind": "supplier", "po_number": None}})["skipped"]


def test_only_the_settled_run_of_each_order_is_recorded(temp_state, monkeypatch, tmp_path):
    """Re-verifying with more inputs supersedes the first run; the agent's hold is carried."""
    import json
    from agent import orchestrator
    monkeypatch.setattr(config, "ERP_DRY_RUN", True)
    ids = []
    for n in (1, 2):
        d = tmp_path / f"run{n}"
        d.mkdir()
        (d / "result.json").write_text(json.dumps(settled_result() | {"recommendation": ["review", "hold"][n - 1]}))
        with state.db() as c:
            ids.append(c.execute("insert into runs (message_id, so_number, report_path, order_key) values ('m1', 503097, ?, 'SO503097')",
                                 (str(d / "report.md"),)).lastrowid)
    orchestrator._runs.set(ids)
    log = {"final": "Held.", "steps": [{"tool": "hold_order", "args": {"so_number": 503097}, "result": {"dry_run": True}}]}
    out = orchestrator.record_outcomes("m1", log)
    assert len(out) == 1
    row = state.erp_actions(503097)[0]["detail"]["statements"][0]["row"]
    assert row["run_id"] == ids[1] and row["recommendation"] == "hold" and row["on_hold"] is True


def test_rerun_requeues_a_processed_email_once(temp_state, monkeypatch):
    """The rerun button: the email is processed again with force, queued like new mail; a second click
    while it is still queued does nothing."""
    monkeypatch.setattr(watcher, "_executor", None)
    started, release, calls = threading.Event(), threading.Event(), []

    def fake_process(mid, force=False, folder=None):
        calls.append((mid, force, folder))
        started.set()
        release.wait(5)

    monkeypatch.setattr(watcher, "process_message", fake_process)
    state.upsert_message("m1", sender="buyer@example.com", state="done", folder="inbox")
    assert watcher.rerun("m1") == {"queued": "m1"}
    assert state.get_message("m1")["state"] == "queued"
    started.wait(5)
    assert "skipped" in watcher.rerun("m1")  # still processing
    release.set()
    watcher._pool().shutdown(wait=True)
    assert calls == [("m1", True, "inbox")]
    with pytest.raises(KeyError):
        watcher.rerun("never-seen")
