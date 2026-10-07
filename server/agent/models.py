"""Shared data models: extracted order documents, requirements and findings.

The extraction models (Loc, Val, DocLine, DocHeader, Totals, OrderDoc) are also the
structured-output schema the LLM fills, so they have no defaults: every field is required
and nullable, which is what strict JSON schema mode expects.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, model_validator


# ---------------------------------------------------------------- extraction schema


class Loc(BaseModel):
    """Where a value was read: page number plus a short anchor (a label, line or cell ref)."""

    page: int | None
    where: str | None


class Val(BaseModel):
    """A value copied verbatim from the document, exactly as printed, with its location."""

    raw: str | None
    loc: Loc | None


class DocLine(BaseModel):
    line_no: Val
    customer_pn: Val
    mpn: Val
    manufacturer: Val
    description: Val
    qty: Val
    uom: Val
    unit_price: Val
    extended: Val
    required_date: Val
    ship_date: Val
    country_of_origin: Val
    date_code: Val
    line_notes: list[Val]

    @model_validator(mode="before")
    @classmethod
    def _older_extractions(cls, data):
        # Extractions saved before these fields existed load with them empty. (A before-validator
        # keeps the strict schema the model fills: every field still required.)
        if isinstance(data, dict):
            for k in ("country_of_origin", "date_code"):
                data.setdefault(k, {"raw": None, "loc": None})
        return data


class DocHeader(BaseModel):
    doc_type: Literal["customer_po", "so_ack", "supplier_po", "supplier_ack", "other"]
    doc_number: Val
    doc_date: Val
    customer_po_ref: Val
    customer_name: Val
    customer_id: Val
    addressed_to: Val
    buyer_name: Val
    buyer_email: Val
    bill_to: Val
    ship_to: Val
    payment_terms: Val
    incoterm: Val
    ship_via: Val
    carrier_account: Val
    freight_terms: Val
    ship_option: Val
    currency: Val


class Totals(BaseModel):
    subtotal: Val
    tax: Val
    total: Val


class OrderDoc(BaseModel):
    header: DocHeader
    lines: list[DocLine]
    notes: list[Val]
    references: list[Val]
    totals: Totals


class Extracted(BaseModel):
    """An extracted document plus what code knows about its source."""

    doc: OrderDoc
    source: str  # file name or "email body"
    sha256: str
    method: Literal["text", "vision", "text+vision", "fixture"]
    links: list[str] = []
    warnings: list[str] = []


# ---------------------------------------------------------------- requirements


class Requirement(BaseModel):
    """One clause from the customer's terms, the order itself, or our own rules."""

    clause_type: str
    params: dict = {}
    quote: str
    source: str
    scope: Literal["customer", "order", "distributor"]


# ---------------------------------------------------------------- findings


class Status(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_REVIEW = "needs_review"  # a person must read it (unrecognised clause, judgment call)
    UNVERIFIABLE = "unverifiable"  # the evidence needed is missing or too weak
    MANUAL = "manual"  # a process step or a requirement to carry forward, not checkable from data
    INFO = "info"


# Worst first. The rollup takes the worst status of the findings under a checklist item.
STATUS_ORDER = [Status.FAIL, Status.NEEDS_APPROVAL, Status.NEEDS_REVIEW, Status.UNVERIFIABLE, Status.MANUAL, Status.INFO, Status.PASS]


class Evidence(BaseModel):
    label: str  # "PO", "Ack", "SO", "Master", "Quote", ...
    value: str | None
    source: str  # where it came from, e.g. "PO PO-MER26-0037.pdf p1 · Terms"


class Finding(BaseModel):
    check_id: str
    checklist_ref: str
    title: str
    status: Status
    severity: Literal["high", "medium", "low", "info"] = "medium"
    message: str
    evidence: list[Evidence] = []
    line: int | None = None
    # Set when the order-desk agent investigated a needs_review / unverifiable item and gave a verdict.
    resolution: dict | None = None
    original_status: Status | None = None
