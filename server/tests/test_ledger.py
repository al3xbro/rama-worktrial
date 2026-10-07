from agent import ledger


def types(text):
    return [r.clause_type for r in ledger.classify(text, "test", "order")]


def test_partials():
    assert types("Ship complete. No partial shipments without written approval.") == ["no_partials"]
    assert types("Partial shipments acceptable.") == ["partials_allowed"]


def test_master_ord_notes_sentences():
    notes = ("No partial shipments without written buyer approval. Date code must be no older than 12 months at time of "
             "shipment. Supplier must comply with SAE AS5553 counterfeit avoidance; parts sourced from OCM or authorized "
             "distribution only unless pre-approved.")
    rows = ledger.classify(notes, "customer.ord_notes", "customer")
    assert [r.clause_type for r in rows] == ["no_partials", "date_code_max_age", "counterfeit_avoidance"]
    assert rows[1].params == {"months": 12, "basis": "shipment"}


def test_quarry_conditions():
    assert "prohibited_origin" in types("5. Country of origin. Buyer does not accept goods of People's Republic of China origin without its prior written approval, line by line.")
    row = next(r for r in ledger.classify("3. Documents. The order acknowledgement shall state, for each line, the country of origin, the ECCN and the HTS code.", "t", "order")
               if r.clause_type == "ack_must_state")
    assert row.params["fields"] == ["coo", "eccn", "hts"]


def test_german_ack_requirement():
    rows = ledger.classify("Ursprungsland und ECCN je Position sind in der Auftragsbestätigung anzugeben.", "t", "order")
    assert rows[0].clause_type == "ack_must_state" and rows[0].params["fields"] == ["coo", "eccn"]


def test_specific_patterns_do_not_swallow_other_clauses():
    assert types("Seller shall provide a first article inspection report for each new part number.") == ["first_article"]
    assert types("9. Inspection. Buyer may inspect within 30 days of receipt and reject nonconforming goods at Seller's cost.") == ["inspection_rights"]


def test_unknown_sentences_surface_for_review():
    assert types("Parts must be delivered in purple boxes.") == ["unclassified"]
    assert types("Buyer: Dana Kowalski") == []
