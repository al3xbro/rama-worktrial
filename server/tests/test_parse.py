from datetime import date
from decimal import Decimal

from agent import parse


def test_numbers_us_and_european():
    assert parse.num("260,000") == Decimal("260000")
    assert parse.num("$0.20630") == Decimal("0.20630")
    assert parse.num("1.32000 EA") == Decimal("1.32000")
    assert parse.num("19.000", decimal_comma=True) == Decimal("19000")
    assert parse.num("0,45980", decimal_comma=True) == Decimal("0.45980")
    assert parse.num("8.736,20", decimal_comma=True) == Decimal("8736.20")


def test_decimal_convention_resolved_by_arithmetic():
    # Oakridge's German PO: 19.000 x 0,45980 = 8.736,20
    german = [("19.000", "0,45980", "8.736,20"), ("6.200", "1,32000", "8.184,00")]
    assert parse.detect_decimal_comma(german) is True
    us = [("260,000", "$0.20630", "$53,638.00")]
    assert parse.detect_decimal_comma(us) is False


def test_dates():
    assert parse.to_date("9/14/2026") == date(2026, 9, 14)
    assert parse.to_date("06.10.2026") == date(2026, 10, 6)  # dotted dates are day first
    assert parse.to_date("2026-09-14") == date(2026, 9, 14)


def test_terms_codes_scoped_to_payment_terms():
    assert parse.terms_code("Net 30") == "N30"
    assert parse.terms_code("Cash in Advance") == "CIA"
    assert parse.terms_code("Vorkasse") == "CIA"
    assert parse.terms_code("Credit Card") == "CC"
    assert parse.terms_code("Net 45 as usual") == "N45"


def test_account_numbers_keep_leading_zeros():
    assert parse.account_number("FedEx Ground on our account 000601844") == "000601844"
    assert parse.account_number("Customer Freight Acct # - 000158380") == "000158380"
    assert parse.account_number("FedEx Ground") is None


def test_incoterms():
    assert parse.incoterm("Ex Works") == ("EXW", "origin")
    assert parse.incoterm("FCA Austin / DHL Express") == ("FCA", "origin")
    assert parse.incoterm("FOB Destination") == ("FOB-DEST", "destination")
    assert parse.incoterm("EXW Austin (Incoterms 2020)") == ("EXW", "origin")


def test_manufacturer_aliases():
    assert parse.mfr_code("Coilcraft") == "COIL"
    assert parse.mfr_code("Diodes Inc") == "DIODES"
    assert parse.mfr_code("STMicroelectronics") == "STM"
    assert parse.mfr_code("Molex") is None  # unknown names are reported, not guessed


def test_xlsx_raw_reader_matches_openpyxl_cells():
    # The fallback used when openpyxl rejects a workbook must still read the cells.
    import io

    import openpyxl

    from agent import ingest
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["B2"], ws["C13"], ws["B13"], ws["E11"] = "PO-TEST-0001", "1N3002B", "CUST-123", 6000
    buf = io.BytesIO()
    wb.save(buf)
    raw = ingest._xlsx_text_raw(buf.getvalue())
    for cell in ("B2=PO-TEST-0001", "C13=1N3002B", "B13=CUST-123", "E11=6000"):
        assert cell in raw


def test_company_and_address_keys():
    assert parse.company_key("Quarry Contract Manufacturing K.K.") == parse.company_key("Quarry Contract Manufacturing")
    assert parse.company_key("Oakridge Imaging GmbH") == parse.company_key("Oakridge Imaging")
    assert parse.street_line("Meridian Medical Devices\n3060 Research Blvd\nMinneapolis, MN 48852") == "3060 research blvd"
    assert parse.postcode("Norfolk, VA 23510\nUSA") == "23510"
