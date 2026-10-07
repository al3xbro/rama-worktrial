"""The fallback judge for unrecognised clauses: a pass only when the model is certain and quotes
order data that shows it; everything short of that stays with a person. The model is replaced by
canned answers here, so these tests check the rules code applies to its answer."""
from types import SimpleNamespace

import pytest

from agent import judge
from agent.checks import ev, finding
from agent.checks import requirements as reqs_mod
from agent.models import Requirement, Status

CLAUSE = "All parts must ship from our Dallas warehouse."
ERP = {"so": {"order_no": 12345, "whse": "DAL"}, "lines": [{"lin_no": 1, "item_no": "ABC-1", "ord_qty": 100}]}


def unclassified():
    return [finding("R.unclassified", "3.2", "Unrecognised clause", Status.NEEDS_REVIEW, f"A person should read it: {CLAUSE!r}",
                    [ev("Clause", CLAUSE, "PO p2")], severity="medium")]


def run_with(monkeypatch, verdict, certain, quotes):
    answer = judge._Judgment(index=0, verdict=verdict, certain=certain, reasoning="because",
                             evidence=[judge._Quote(quote=q, source="erp") for q in quotes])
    monkeypatch.setattr(judge, "_ask", lambda clauses, data: [answer])
    return judge.judge(unclassified(), None, None, ERP)[0]


def test_certain_and_grounded_passes(monkeypatch):
    f = run_with(monkeypatch, "met", True, ['"whse": "DAL"'])
    assert f.status == Status.PASS and f.resolution["by"] == "llm_judge" and f.original_status == Status.NEEDS_REVIEW


def test_not_certain_stays_with_a_person(monkeypatch):
    assert run_with(monkeypatch, "met", False, ['"whse": "DAL"']).status == Status.NEEDS_REVIEW


def test_quotes_not_in_the_order_data_stay_with_a_person(monkeypatch):
    assert run_with(monkeypatch, "met", True, ["Ships from Dallas, confirmed by Dana"]).status == Status.NEEDS_REVIEW


def test_quoting_only_the_clause_is_not_evidence(monkeypatch):
    # The clause is part of the order data, so it would ground itself; it must not count.
    f = run_with(monkeypatch, "met", True, ["ship from our Dallas warehouse"])
    assert f.status == Status.NEEDS_REVIEW


def test_no_evidence_stays_with_a_person(monkeypatch):
    assert run_with(monkeypatch, "met", True, []).status == Status.NEEDS_REVIEW


def test_grounded_conflict_fails(monkeypatch):
    assert run_with(monkeypatch, "conflict", True, ['"ord_qty": 100']).status == Status.FAIL


def test_unsure_keeps_the_reading_for_the_reviewer(monkeypatch):
    f = run_with(monkeypatch, "unsure", False, [])
    assert f.status == Status.NEEDS_REVIEW and "because" in f.message


def test_a_model_error_leaves_the_item_for_review(monkeypatch):
    def boom(*a):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(judge, "_ask", boom)
    f = judge.judge(unclassified(), None, None, ERP)[0]
    assert f.status == Status.NEEDS_REVIEW and "could not run" in f.message


# ---- Quarry's conditions of purchase: the clauses that used to be only noted


def req(clause_type, quote):
    return Requirement(clause_type=clause_type, quote=quote, source="PO p2", scope="order")


def test_card_fees_on_the_seller_conflict_with_our_prepay_fee():
    r = req("payment_fees", "Card fees are borne by Seller.")
    assert reqs_mod.payment_fees("payment_fees", [r], None)[0].status == Status.NEEDS_REVIEW


def test_foreign_governing_law_goes_to_a_person():
    r = req("governing_law", "This order is governed by the laws of Japan; the Tokyo District Court has exclusive jurisdiction.")
    f = reqs_mod.governing_law("governing_law", [r], None)[0]
    assert f.status == Status.NEEDS_REVIEW and "Japan" in f.message


@pytest.mark.parametrize("quote", ["This order is governed by the laws of the State of Texas.",
                                   "Governing law The laws of the State of Colorado govern this order.",
                                   "The laws of the State of New York, without regard to conflict of laws, apply."])
def test_us_governing_law_is_noted(quote):
    assert reqs_mod.governing_law("governing_law", [req("governing_law", quote)], None)[0].status == Status.INFO


def test_foreign_law_named_mid_sentence():
    r = req("governing_law", "The laws of England and Wales govern this order.")
    assert "England" in reqs_mod.governing_law("governing_law", [r], None)[0].message


@pytest.mark.parametrize("quote,status", [
    ("This order is governed by the Conditions of Purchase on page 2.", Status.INFO),
    ("This order is subject to our Supplier Terms and Conditions.", Status.UNVERIFIABLE),
])
def test_terms_printed_in_the_document_are_not_flagged_as_missing(quote, status):
    b = SimpleNamespace(po=SimpleNamespace(links=[]), references=[], requirements=[])
    assert reqs_mod.external_terms("external_terms", [req("external_terms", quote)], b)[0].status == status
