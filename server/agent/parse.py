"""Turn verbatim document strings into comparable values. Pure code, no LLM.

Part numbers and account numbers are never converted to numbers: leading zeros and
punctuation matter (`064316`, `1N4749A,133`, `000158380`).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------- numbers


def num(raw: str | None, decimal_comma: bool = False) -> Decimal | None:
    """Parse '260,000', '$0.20630', '1.32000 EA', or with decimal_comma '19.000', '0,45980'."""
    if raw is None:
        return None
    s = re.sub(r"[^\d,.\-]", "", raw)
    if not s or not re.search(r"\d", s):
        return None
    if decimal_comma:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def ties_out(qty: Decimal | None, price: Decimal | None, extended: Decimal | None, tol: Decimal = Decimal("0.011")) -> bool | None:
    if qty is None or price is None or extended is None:
        return None
    return abs(qty * price - extended) <= tol


def detect_decimal_comma(lines: list[tuple[str | None, str | None, str | None]]) -> bool:
    """Pick the number convention under which qty x price = extended holds for more lines.

    `lines` holds raw (qty, unit_price, extended) strings. This is how '19.000' on a German
    PO resolves to nineteen thousand rather than nineteen.
    """
    def score(comma: bool) -> int:
        return sum(1 for q, p, e in lines if ties_out(num(q, comma), num(p, comma), num(e, comma)))

    return score(True) > score(False)


# ---------------------------------------------------------------- dates


def to_date(raw: str | None, day_first: bool = False) -> date | None:
    """Parse '9/14/2026', '06.10.2026' (day first), '2026-09-14', 'Mon, 31 Aug 2026'."""
    if not raw:
        return None
    s = raw.strip()
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        dotted = "." in m.group(0)
        day, month = (a, b) if (day_first or dotted) else (b, a)
        try:
            return date(y, month, day)
        except ValueError:
            return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for fmt in ("%d %b %Y", "%b %d, %Y", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(re.sub(r"^\w{3},\s*", "", s), fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- payment terms

# Codes are the ERP's `term_codes`. Scope this to payment-terms fields only:
# "Prepay & Add" on an ack is a freight term, not a payment term.
_TERMS = [
    (r"\bnet\s*30\b|\bn30\b|30\s*days", "N30"),
    (r"\bnet\s*45\b|\bn45\b|45\s*days", "N45"),
    (r"\bnet\s*60\b|\bn60\b|60\s*days", "N60"),
    (r"cash\s*in\s*advance|\bcia\b|prepay|pre-pay|vorkasse|vorauskasse|advance\s*payment", "CIA"),
    (r"credit\s*card|\bcc\b|card", "CC"),
    (r"\bcod\b|cash\s*on\s*delivery", "COD"),
]


def terms_code(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.lower()
    for pattern, code in _TERMS:
        if re.search(pattern, s):
            return code
    return None


# ---------------------------------------------------------------- carriers, accounts, incoterms

_CARRIERS = [
    (r"fed\s*ex|fdx", "FDX"),
    (r"\bups\b", "UPS"),
    (r"\bdhl\b", "DHL"),
    (r"pick\s*-?\s*up|cpu\b|abholung", "CPU"),
]


def carrier_code(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.lower()
    for pattern, code in _CARRIERS:
        if re.search(pattern, s):
            return code
    return None


def account_number(raw: str | None) -> str | None:
    """The first run of 6+ digits, kept as a string (leading zeros matter)."""
    if not raw:
        return None
    m = re.search(r"(?<!\d)(\d{6,})(?!\d)", raw)
    return m.group(1) if m else None


def incoterm(raw: str | None) -> tuple[str | None, str | None]:
    """Return (term, side): side is 'origin' or 'destination', matching the master's ORG/DST."""
    if not raw:
        return None, None
    s = raw.lower()
    if "ex works" in s or re.search(r"\bexw\b", s):
        return "EXW", "origin"
    if re.search(r"\bfca\b", s):
        return "FCA", "origin"
    if re.search(r"fob\s*(destination|dest\b)", s):
        return "FOB-DEST", "destination"
    if re.search(r"fob\s*(origin|shipping\s*point)", s):
        return "FOB-ORIGIN", "origin"
    if re.search(r"\b(dap|ddp|cpt|cip)\b", s):
        term = re.search(r"\b(dap|ddp|cpt|cip)\b", s).group(1).upper()
        return term, "destination"
    if "customer pickup" in s:
        return "EXW", "origin"
    return None, None


FOB_SIDE = {"ORG": "origin", "DST": "destination"}


# ---------------------------------------------------------------- manufacturers

# ERP manufacturer codes to the names documents use. Seeded from the 14 SO acks, which print
# the name, matched to `ord_detl.manu_no` on the same lines. Unknown names stay unmapped and
# the line comparison reports them instead of guessing.
MANUFACTURERS = {
    "AGILEN": ["agilent"],
    "AMPHEN": ["amphenol"],
    "BOSCH": ["bosch"],
    "BOURNS": ["bourns"],
    "COIL": ["coilcraft"],
    "CTS": ["cts"],
    "DIODES": ["diodes inc", "diodes incorporated", "diodes"],
    "INFIN": ["infineon"],
    "LTF": ["littelfuse"],
    "MCHP": ["microchip"],
    "MEANWL": ["mean well", "meanwell"],
    "MURATA": ["murata"],
    "MXIC": ["macronix"],
    "NEXP": ["nexperia"],
    "NORDIC": ["nordic semiconductor", "nordic semic onductor", "nordic"],
    "NXP": ["nxp"],
    "ONSEMI": ["onsemi", "on semiconductor"],
    "PHOENX": ["phoenix contact"],
    "SAMSNG": ["samsung"],
    "SKYWKS": ["skyworks"],
    "STM": ["stmicroelectronics", "st microelectronics", "stmicroelect ronics"],
    "WURTH": ["wurth elektronik", "würth elektronik", "wurth"],
}
_MFR_LOOKUP = {alias: code for code, aliases in MANUFACTURERS.items() for alias in aliases}


def mfr_code(raw: str | None) -> str | None:
    if not raw:
        return None
    s = re.sub(r"\s+", " ", raw.lower()).strip()
    if s.upper() in MANUFACTURERS:
        return s.upper()
    return _MFR_LOOKUP.get(s)


# ---------------------------------------------------------------- identifiers and text


def mpn_key(raw: str | None) -> str | None:
    """Matching key only (case and whitespace). The exact-match check uses the raw string."""
    if not raw:
        return None
    return re.sub(r"\s+", "", raw).upper()


_LEGAL_SUFFIX = r"\b(inc|llc|ltd|gmbh|k\.?k\.?|corp|corporation|co|company|s\.?a\.?|b\.?v\.?|plc)\.?$"


def company_key(raw: str | None) -> str | None:
    if not raw:
        return None
    s = re.sub(r"[^\w\s.]", " ", raw.lower())
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(2):
        s = re.sub(_LEGAL_SUFFIX, "", s).strip(" ,.")
    return s


_ADDR_ABBR = {
    "boulevard": "blvd", "drive": "dr", "street": "st", "avenue": "ave", "parkway": "pkwy",
    "road": "rd", "suite": "ste", "building": "bldg", "deutschland": "germany", "netherland": "netherlands",
}


def address_key(raw: str | None) -> str | None:
    """Street number + street + postcode, lowercased and abbreviated, for comparison."""
    if not raw:
        return None
    s = raw.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    words = [_ADDR_ABBR.get(w, w) for w in s.split()]
    return " ".join(words)


def street_line(raw: str | None) -> str | None:
    """The first '<number> <street>' found in an address block."""
    if not raw:
        return None
    m = re.search(r"\b\d{2,6}\s+[A-Za-z][\w .]*?(?:Blvd|Dr|St|Ave|Pkwy|Rd|Way|Lane|Ln|Ct|Pl|Boulevard|Drive|Street|Avenue|Parkway|Road)\b\.?", raw, re.I)
    return address_key(m.group(0)) if m else None


def postcode(raw: str | None) -> str | None:
    if not raw:
        return None
    m = re.findall(r"(?<!\d)(\d{5})(?!\d)", raw)
    return m[-1] if m else None


def squash(raw: str | None) -> str:
    """Whitespace-insensitive form, used for grounding raw values in source text."""
    return re.sub(r"\s+", "", raw or "").lower()
