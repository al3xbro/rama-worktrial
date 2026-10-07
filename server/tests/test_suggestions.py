"""The agent's suggestions past the checks: recorded only with evidence it saw, capped per email,
attached to the order they concern, and replaced when the email is processed again."""
import pytest

from agent import config, orchestrator, state

HISTORY = '{"rows": [{"vend_no": "V1135", "last_po": "2024-10-30", "min_cost": 0.04852, "coo": "MX,PH"}], "count": 1}'


@pytest.fixture
def agent_ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    orchestrator._current_message.set("m1")
    orchestrator._seen.set([HISTORY])
    with state.db() as c:
        rid = c.execute("insert into runs (message_id, so_number, order_key) values ('m1', 503105, 'SO503105')").lastrowid
    orchestrator._runs.set([rid])
    return rid


def suggest(**kw):
    args = {"kind": "sourcing", "priority": "high", "title": "Buy 14D22110201D from V1135", "detail": "Short 715,800.",
            "action": "Buyer: RFQ V1135.", "evidence": [{"quote": '"vend_no": "V1135"', "source": "query_erp"}],
            "so_number": 503105, "po_number": None} | kw
    return orchestrator.tool_add_suggestion(**args)


def test_a_grounded_suggestion_is_recorded_and_attached_to_its_order(agent_ctx):
    out = suggest()
    assert out["recorded"] and out["attached_to_run"] == agent_ctx
    [s] = state.suggestions(message_id="m1")
    assert s["status"] == "open" and s["evidence"][0]["source"] == "query_erp"


def test_quotes_the_agent_never_saw_are_rejected(agent_ctx):
    out = suggest(evidence=[{"quote": "V2000 has 1M pcs in stock at $0.03", "source": "query_erp"}])
    assert "error" in out and not state.suggestions(message_id="m1")


def test_a_suggestion_needs_evidence(agent_ctx):
    assert "error" in suggest(evidence=[])


def test_at_most_six_per_email(agent_ctx):
    for i in range(orchestrator.MAX_SUGGESTIONS):
        assert "recorded" in suggest(title=f"idea {i}")
    assert "error" in suggest(title="one too many")


def test_reprocessing_replaces_open_suggestions_but_keeps_acted_on_ones(agent_ctx):
    done = suggest(title="already ordered")["recorded"]
    suggest(title="still open")
    state.set_suggestion_status(done, "done")
    state.clear_open_suggestions("m1")
    assert [s["title"] for s in state.suggestions(message_id="m1")] == ["already ordered"]


def test_status_must_be_known(agent_ctx):
    sid = suggest()["recorded"]
    with pytest.raises(ValueError):
        state.set_suggestion_status(sid, "maybe")
    assert state.set_suggestion_status(sid, "dismissed")["status"] == "dismissed"
