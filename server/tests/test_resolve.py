"""The agent resolving review items: allowed only for needs_review / unverifiable items, only with
evidence it actually saw, and the recommendation is recalculated."""
import os

import pytest

from agent import config, resolve, verify
from agent.models import Status
from conftest import trial_doc

pytestmark = pytest.mark.skipif(not os.getenv("PGHOST"), reason="needs the ERP (PG* variables in .env)")


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr("agent.revision.INDEX", tmp_path / "po_index.json")
    monkeypatch.setattr(config, "ERP_DRY_RUN", True)
    return verify.verify(trial_doc("meridian_po"), trial_doc("meridian_ack"))


def index_of(r, check_id, status=None):
    return next(i for i, f in enumerate(r.findings) if f.check_id == check_id and (status is None or f.status == status))


def test_resolving_an_unverifiable_item_with_real_evidence(run):
    i = index_of(run, "Q.link")  # no quote linked to the SO: unverifiable
    out = resolve.resolve(run.run_id, i, "met", "The PO price matches the price on the SO, so the missing quote link does not matter here.",
                          [{"quote": "$0.20630", "source": "PO line 1"}], seen=[])
    assert out["applied"] and out["status"] == "pass"


def test_quotes_the_agent_never_saw_are_rejected(run):
    i = index_of(run, "Q.link")
    out = resolve.resolve(run.run_id, i, "met", "made up", [{"quote": "Quote 99999 approved at $0.20630 by Dana", "source": "?"}], seen=[])
    assert "error" in out


def test_evidence_from_a_tool_output_counts(run):
    i = index_of(run, "Q.link")
    seen = ['{"rows": [{"quote_no": 20250115, "unit_price": 0.2063}]}']
    out = resolve.resolve(run.run_id, i, "met", "quote found", [{"quote": "\"quote_no\": 20250115", "source": "query_erp"}], seen=seen)
    assert out["applied"]


def test_failures_cannot_be_resolved_by_the_agent(run):
    i = index_of(run, "H.payment_terms", Status.FAIL)
    out = resolve.resolve(run.run_id, i, "met", "terms are fine", [{"quote": "Net 30", "source": "PO"}], seen=[])
    assert "error" in out and "failures" in out["error"]


def test_not_met_on_a_review_item_turns_into_a_failure_and_recommendation_follows(run):
    i = index_of(run, "S.unrequested")
    out = resolve.resolve(run.run_id, i, "not_met", "The extra line is not tied to any order.",
                          [{"quote": "81797", "source": "ERP supply"}], seen=[])
    assert out["status"] == "fail" and out["recommendation"] == "hold"


def test_missing_evidence_cannot_become_a_failure(run):
    # Unverifiable means the evidence is missing; the agent saying "not met" leaves it with a person.
    i = index_of(run, "R.external_terms")
    out = resolve.resolve(run.run_id, i, "not_met", "The referenced terms were not found anywhere.",
                          [{"quote": "This order is subject to our Supplier Terms and Conditions.", "source": "PO notes"}], seen=[])
    assert out["status"] == "needs_review"


def test_accepting_a_term_is_not_the_agents_call(run):
    # Governing law and card fees ask a person to accept a term; the agent can only add notes.
    from agent import resolve as r_mod
    from agent.models import Finding
    f = Finding(check_id="R.governing_law", checklist_ref="3.2", title="Governing law", status=Status.NEEDS_REVIEW,
                severity="medium", message="laws of Japan", evidence=[], line=None)
    assert not r_mod.resolvable(f)


def test_settled_items_cannot_be_resolved_again(run):
    i = index_of(run, "Q.link")
    resolve.resolve(run.run_id, i, "met", "price matches", [{"quote": "$0.20630", "source": "PO line 1"}], seen=[])
    out = resolve.resolve(run.run_id, i, "not_met", "changed my mind", [{"quote": "$0.20630", "source": "PO line 1"}], seen=[])
    assert "error" in out
