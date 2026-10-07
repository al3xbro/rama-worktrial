"""The chatbot's tools use the same definitions as the verifier, so the two never disagree."""
import os

import pytest

from agent import chat

pytestmark = pytest.mark.skipif(not os.getenv("PGHOST"), reason="needs the ERP (PG* variables in .env)")


def test_credit_over_limit_agrees_with_verifier():
    over = {c["cust_no"] for c in chat.tool_credit_exposure(None, True)["customers"]}
    # The verifier flags these trial customers as over their credit line (A.credit needs_approval).
    assert {"C1048", "C1107", "C1143"} <= over  # Ospreyfield, Crescent (master CC but a Net 30 order), Sandstone
    assert "C1002" not in over  # Meridian is within its limit


def test_customer_profile_includes_terms_as_read():
    p = chat.tool_customer_profile("C1002")
    assert p["payment_terms"]["code"] == "N30"
    assert any(t["clause"] == "no_partials" for t in p["terms_as_read_by_the_agent"])
