"""Referenced documents (the terms a PO points to): which links may be fetched, how they are read,
and that their clauses are checked like terms printed in the PO."""
import os
from functools import partial

import httpx
import pytest

from agent import config, references, verify
from agent.models import Status
from conftest import trial_doc

TERMS_HTML = b"""<html><head><style>p{}</style><script>var x=1;</script></head><body><nav>Home | About</nav>
<h1>Conditions of Purchase</h1><p>1. Country of origin. No goods of PRC origin may be supplied without Buyer's prior written approval.</p>
<p>2. Governing law. This order is governed by the laws of Japan.</p></body></html>"""


def mock_web(monkeypatch, handler, public=lambda host: host == "terms.example.com"):
    monkeypatch.setattr(references, "_public", public)
    monkeypatch.setattr(references.httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(handler)))


def test_only_links_found_in_the_order_or_email_are_fetched():
    with pytest.raises(references.ReferenceError, match="only links"):
        references.fetch_url("https://terms.example.com/tc", "terms", allowed="Terms at https://other.example.com/tc apply.")


def test_local_and_private_addresses_are_refused():
    for url in ("http://127.0.0.1:8000/status", "http://169.254.169.254/latest/meta-data/", "http://localhost/"):
        with pytest.raises(references.ReferenceError, match="not a public address"):
            references.fetch_url(url, "terms", allowed=url)


def test_a_redirect_to_a_private_address_is_refused(monkeypatch):
    mock_web(monkeypatch, lambda req: httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"}))
    with pytest.raises(references.ReferenceError, match="not a public address"):
        references.fetch_url("https://terms.example.com/tc", "terms", allowed="https://terms.example.com/tc")


def test_a_dead_link_is_reported(monkeypatch):
    mock_web(monkeypatch, lambda req: httpx.Response(404, text="Not Found"))
    with pytest.raises(references.ReferenceError, match="HTTP 404"):
        references.fetch_url("https://terms.example.com/tc", "terms", allowed="https://terms.example.com/tc")


def test_html_terms_are_read_as_text(monkeypatch):
    mock_web(monkeypatch, lambda req: httpx.Response(200, content=TERMS_HTML, headers={"content-type": "text/html"}))
    ref = references.fetch_url("https://terms.example.com/tc", "terms", allowed="see https://terms.example.com/tc")
    assert "prior written approval" in ref.text and "var x" not in ref.text and "Home | About" not in ref.text


@pytest.mark.skipif(not os.getenv("PGHOST"), reason="needs the ERP (PG* variables in .env)")
def test_supplied_terms_are_checked_clause_by_clause(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    po, ack = trial_doc("meridian_po"), trial_doc("meridian_ack")
    ref = references.Reference(kind="terms", source="https://terms.example.com/tc", text=references.html_text(TERMS_HTML.decode()))
    r = verify.verify(po, ack, write=False, references=[ref])
    by_id = {f.check_id: f for f in r.findings}
    assert by_id["R.external_terms"].status == Status.INFO  # was unverifiable without the document
    assert by_id["R.governing_law"].status == Status.NEEDS_REVIEW  # a clause from the supplied terms, checked by code
    assert any(f.check_id == "R.origin" for f in r.findings)
    assert r.order["references"][0]["source"] == "https://terms.example.com/tc"
