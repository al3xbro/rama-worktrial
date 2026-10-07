"""The requirements ledger: clauses from the customer master, the order's own documents,
and our own rules, as typed rows a verifier can check.

Flags map to clauses in code. Free text goes through a dictionary of the clause sentences
seen in this ERP and these POs first. Sentences the dictionary does not recognise become
`unclassified` rows, which always surface for human review (LLM classification of long T&Cs
comes later and feeds the same rows).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel

from .models import Extracted, Requirement

# (pattern, clause_type, params builder). Patterns run on lowercased sentences.
RULES: list[tuple[str, str, callable]] = [
    (r"no partial shipments?", "no_partials", lambda m, s: {"exception": "written_approval" if "approval" in s else None}),
    (r"^ship complete\b", "no_partials", lambda m, s: {"exception": None}),
    (r"partial shipments? (?:are )?(?:acceptable|allowed|permitted|ok)", "partials_allowed", lambda m, s: {}),
    (r"date code (?:must be |shall be )?(?:no older than|within) (\d+) months", "date_code_max_age",
     lambda m, s: {"months": int(m.group(1)), "basis": "po_date" if "po date" in s else "shipment"}),
    (r"as\s?5553", "counterfeit_avoidance", lambda m, s: {"standard": "AS5553", "sourcing": "OCM or authorized" if "ocm" in s else None}),
    (r"unit price must match the quoted price.*?(\d+(?:\.\d+)?)\s?%", "price_matches_quote", lambda m, s: {"tolerance_pct": float(m.group(1))}),
    (r"late delivery.*?(\d+(?:\.\d+)?)\s?% of (?:the )?line value per week.*?capped at (\d+(?:\.\d+)?)\s?%", "late_delivery_penalty",
     lambda m, s: {"pct_per_week": float(m.group(1)), "cap_pct": float(m.group(2))}),
    (r"dfars", "dfars_flowdown", lambda m, s: {}),
    (r"\bitar\b", "itar_flowdown", lambda m, s: {}),
    (r"ship on customer (\w+) account (\d+)", "ship_on_customer_account", lambda m, s: {"carrier": m.group(1), "account": m.group(2)}),
    (r"customer pick\s?-?up", "customer_pickup", lambda m, s: {"notice_hours": int(n.group(1)) if (n := re.search(r"(\d+)\s*h", s)) else None}),
    (r"yield:.*manager approval", "yield_hold", lambda m, s: {}),
    (r"past-?due history", "credit_history_note", lambda m, s: {}),
    (r"no deliveries more than (\d+) days before", "early_delivery_limit", lambda m, s: {"days": int(m.group(1))}),
    (r"acknowledge this order within (\d+) hours", "ack_deadline", lambda m, s: {"hours": int(m.group(1))}),
    (r"(?:purchase order|po) number (?:must )?(?:appear )?on all|show our po number|include our purchase order number|unsere bestellnummer", "po_number_on_docs", lambda m, s: {}),
    (r"(?:people'?s republic of china|\bprc\b|china) origin|goods of people'?s republic of china", "prohibited_origin",
     lambda m, s: {"countries": ["CN"], "exception": "prior_written_approval" if "approval" in s else None}),
    (r"(?:order acknowledg\w+|auftragsbest\w+).*(?:country of origin|ursprungsland|eccn|hts)|(?:country of origin|ursprungsland).*(?:order acknowledg\w+|auftragsbest\w+)",
     "ack_must_state", lambda m, s: {"fields": [f for f, k in (("coo", r"country of origin|ursprungsland"), ("eccn", r"eccn"), ("hts", r"\bhts\b")) if re.search(k, s)]}),
    (r"certificate of conformance|\bcoc\b", "coc_required", lambda m, s: {}),
    (r"traceability", "traceability", lambda m, s: {}),
    (r"original manufacturer packaging|dry packed|moisture sensitive", "packaging", lambda m, s: {}),
    (r"export (?:control|authori[sz]ation)|not ear99|licence exception|license exception", "export_review", lambda m, s: {}),
    (r"quality clauses?:\s*([\w ,]+)", "quality_clause_codes", lambda m, s: {"codes": [c.strip().upper() for c in m.group(1).split(",") if c.strip()]}),
    (r"terms and conditions|conditions of purchase|einkaufsbedingungen|nsa-scm", "external_terms", lambda m, s: {}),
    (r"payment by company credit card|payment\.? payment by", "payment_method", lambda m, s: {"method": "CC"}),
    (r"governed by the laws of|exclusive jurisdiction|governing law", "governing_law", lambda m, s: {}),
    (r"card fees|wire fee|bank fees", "payment_fees", lambda m, s: {}),
    (r"moved receiving|ship (?:this|it|them|the order) to|new (?:ship(?:ping)?|delivery) address|deliver(?:y)? to:|lieferanschrift",
     "ship_to_instruction", lambda m, s: {}),
    (r"^terms\b.*\b(?:net|cia|cod|card)|\bas usual\b.*(?:fedex|ups|dhl)", "commercial_terms_restated", lambda m, s: {}),
    # Specific patterns only: the dictionary runs before the LLM fallback, so a loose match is never second-guessed.
    (r"first article", "first_article", lambda m, s: {}),
    (r"(?:buyer|we) may inspect|right to inspect|inspect(?:ion)? within \d+ days|reject nonconforming", "inspection_rights", lambda m, s: {}),
    (r"seller'?s own terms do not apply|acknowledgement or shipment is acceptance", "terms_acceptance", lambda m, s: {}),
    (r"notify buyer immediately of any change", "change_notification", lambda m, s: {}),
    (r"no substitutions?|alternate part numbers?", "no_substitutions", lambda m, s: {"exception": "written_approval" if "approval" in s else None}),
    (r"warranty", "warranty", lambda m, s: {"months": int(n.group(1)) if (n := re.search(r"(\d+)\s*months", s)) else None}),
]

# Sentences that are not requirements on the order (greetings, contact details, sign-offs).
NOT_REQUIREMENTS = [
    r"^buyer:", r"^authori[sz]ed by", r"^date:", r"^thanks", r"^thank you", r"^regards", r"^best\b", r"^(hi|hello|dear)\b",
    r"^please confirm receipt", r"^send the acknowledgement", r"^please (?:take|find|see) (?:this|attached|the attached)",
    r"[\w.+-]+@[\w-]+\.[\w.]+.*\(\d{3}\)", r"^[\w .'-]+,\s*(?:purchasing|procurement|buyer)\b",
]


def sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    parts = [p.strip() for p in re.split(r"(?<=[.;!?])\s+(?=(?:\d+\.\s)?[A-Z])", text) if p.strip()]
    # Clause headings ("1. Acceptance.", "Payment.") belong to the sentence that follows them.
    out, carry = [], ""
    for p in parts:
        if re.fullmatch(r"(?:\d+\.\s*)?[A-Z][\w'’ -]{0,30}\.", p) and len(p.split()) <= 4:
            carry += p + " "
            continue
        out.append(carry + p)
        carry = ""
    if carry:
        out.append(carry.strip())
    return [p for p in out if len(p) > 3]


def classify(text: str, source: str, scope: str) -> list[Requirement]:
    out: list[Requirement] = []
    for sent in sentences(text):
        s = sent.lower()
        s = re.sub(r"^\d+\.\s*", "", s)
        hits = []
        for pattern, clause_type, params in RULES:
            m = re.search(pattern, s)
            if m and clause_type not in {h.clause_type for h in hits}:  # one row per clause type per sentence
                hits.append(Requirement(clause_type=clause_type, params=params(m, s), quote=sent, source=source, scope=scope))
        if hits:
            out += hits
        elif not any(re.search(p, s) for p in NOT_REQUIREMENTS):
            out.append(Requirement(clause_type="unclassified", quote=sent, source=source, scope=scope))
    return out


# Customer-master flags. The column names are the ERP's (note the mixed-case AS5553_Flg).
FLAGS = [
    ("no_partials", lambda v: bool(v), "no_partials", lambda v: {"exception": None}),
    ("shelf_life", lambda v: (v or 0) > 0, "shelf_life_months", lambda v: {"months": int(v)}),
    ("AS5553_Flg", lambda v: bool(v), "counterfeit_avoidance", lambda v: {"standard": "AS5553"}),
    ("coc_st", lambda v: bool(v), "coc_required", lambda v: {}),
    # Most likely "print on the shipping documents", not "state on the acknowledgement": carry forward.
    ("prt_coo", lambda v: bool(v), "print_coo_on_docs", lambda v: {}),
    ("eccn_req", lambda v: bool(v), "print_eccn_on_docs", lambda v: {}),
    ("lic_req", lambda v: bool(v), "export_license_required", lambda v: {}),
    ("qc_before_shp", lambda v: bool(v), "qc_before_ship", lambda v: {}),
    ("fa_req", lambda v: bool(v), "first_article", lambda v: {}),
    ("blind_ship", lambda v: bool(v), "blind_ship", lambda v: {}),
]


def from_customer(cust: dict) -> list[Requirement]:
    cno = cust.get("cust_no")
    out = []
    for col, on, clause_type, params in FLAGS:
        v = cust.get(col)
        if on(v):
            out.append(Requirement(clause_type=clause_type, params=params(v), quote=f"{col} = {v}", source=f"customer.{col} ({cno})", scope="customer"))
    for col in ("ord_notes", "comments", "shipinst"):
        if cust.get(col):
            out += classify(cust[col], f"customer.{col} ({cno})", "customer")
    return out


# ---------------------------------------------------------------- LLM fallback for unrecognised sentences

LLM_TYPES = sorted({t for _, t, _ in RULES} | {"shelf_life_months", "first_article", "qc_before_ship", "blind_ship", "export_license_required"})


class _Label(BaseModel):
    index: int
    label: Literal[tuple(LLM_TYPES + ["not_a_requirement", "other_requirement"])]  # type: ignore[valid-type]
    months: int | None
    percent: float | None
    hours: int | None
    countries: list[str]
    note: str


class _Labels(BaseModel):
    items: list[_Label]


LABEL_INSTRUCTIONS = """You label sentences from purchase orders, customer terms and customer-master notes for an
electronic component distributor. For each numbered sentence choose exactly one label:
- one of the clause types listed, if the sentence states that requirement;
- not_a_requirement for greetings, headings, contact details, or text that only restates the order;
- other_requirement for a real requirement that fits none of the clause types.
Fill months / percent / hours / countries (ISO 3166 alpha-2 codes) only when the sentence states them. In `note`,
give a few words on what the sentence requires. Do not judge whether the order complies."""


def _params(label: _Label, quote: str) -> dict:
    t, s = label.label, quote.lower()
    if t in ("date_code_max_age", "shelf_life_months"):
        return {"months": label.months, "basis": "unspecified"} if t == "date_code_max_age" else {"months": label.months}
    if t == "price_matches_quote":
        return {"tolerance_pct": label.percent or 0.0}
    if t == "late_delivery_penalty":
        return {"pct_per_week": label.percent, "cap_pct": None}
    if t == "ack_deadline":
        return {"hours": label.hours}
    if t == "prohibited_origin":
        return {"countries": [c.upper() for c in label.countries], "exception": "prior_written_approval" if "approval" in s else None}
    if t == "ack_must_state":
        return {"fields": [f for f, k in (("coo", r"origin"), ("eccn", r"eccn"), ("hts", r"\bhts\b|tariff")) if re.search(k, s)]}
    return {}


def llm_label(reqs: list[Requirement]) -> list[Requirement]:
    """Label `unclassified` rows into known clause types. The verifiers stay code; this only reads."""
    from . import config, llm  # local import: the ledger itself works without a model

    todo = [r for r in reqs if r.clause_type == "unclassified"]
    if not todo:
        return reqs
    cache_dir = config.STORE_DIR / "cache" / "ledger"
    key = lambda r: hashlib.sha256((r.quote + "|" + LABEL_INSTRUCTIONS).encode()).hexdigest()[:20]
    labels: dict[str, dict] = {}
    missing = []
    for r in todo:
        p = cache_dir / f"{key(r)}.json"
        if p.exists():
            labels[key(r)] = json.loads(p.read_text())
        else:
            missing.append(r)
    if missing:
        text = "Clause types: " + ", ".join(LLM_TYPES) + "\n\n" + "\n".join(f"{i}. {r.quote}" for i, r in enumerate(missing))
        out = llm.parse(config.TRIAGE_MODEL, LABEL_INSTRUCTIONS, [llm.text_part(text)], _Labels)
        cache_dir.mkdir(parents=True, exist_ok=True)
        for item in out.items:
            if 0 <= item.index < len(missing):
                r = missing[item.index]
                labels[key(r)] = item.model_dump()
                (cache_dir / f"{key(r)}.json").write_text(json.dumps(labels[key(r)]))
    result = []
    for r in reqs:
        lab = labels.get(key(r)) if r.clause_type == "unclassified" else None
        if lab is None or lab["label"] == "other_requirement":
            if lab:
                r = r.model_copy(update={"params": {"llm_note": lab["note"]}})
            result.append(r)
            continue
        label = _Label.model_validate(lab)
        clause_type = label.label
        result.append(Requirement(clause_type=clause_type, params=_params(label, r.quote) if clause_type != "not_a_requirement" else {"llm_note": label.note},
                                  quote=r.quote, source=r.source + " (labelled by LLM)", scope=r.scope))
    return result


def from_document(x: Extracted) -> list[Requirement]:
    """Order-scope clauses from a customer PO's notes and header instructions."""
    out = []
    for v in x.doc.notes:
        page = f" p{v.loc.page}" if v.loc and v.loc.page else ""
        out += classify(v.raw or "", f"{x.source}{page} · notes", "order")
    so = x.doc.header.ship_option
    if so.raw and not any(so.raw.strip() in (n.raw or "") for n in x.doc.notes):
        out += classify(so.raw, f"{x.source} · ship option", "order")
    return out
