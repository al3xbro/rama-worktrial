"""The fallback for clauses no check recognises: a model reads the clause against the order.

The dictionary and the labeller map most clauses to a clause type with a code verifier. What is
left is an `R.unclassified` finding (needs_review). The judge reads each one against the
customer PO, our ack and the ERP data, and code decides what its answer is worth:

- pass only when the judge says the clause is met, says it is certain, and every quote it gives
  appears verbatim in the order data, with at least one quote from something other than the clause;
- fail only when it reports a conflict and grounds it the same way;
- anything else stays needs_review, with the judge's reading attached for the person.

So the model can clear a clause only when it can point at the order data that proves it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from . import config, parse
from .models import Extracted, Finding, Status


class _Quote(BaseModel):
    quote: str
    source: str


class _Judgment(BaseModel):
    index: int
    verdict: Literal["met", "conflict", "unsure"]
    certain: bool
    reasoning: str
    evidence: list[_Quote]


class _Judgments(BaseModel):
    items: list[_Judgment]


INSTRUCTIONS = """You check customer purchase-order clauses for an electronic component distributor. Each numbered
clause is one our rule-based checks did not recognise. For each clause, decide from the ORDER DATA alone whether our
order (the customer PO, our sales-order acknowledgement and our ERP records) meets it:
- met: the order data shows the clause is satisfied. Quote the exact text or values that show it.
- conflict: the order data shows something that breaks the clause. Quote the exact text or values.
- unsure: anything else, including clauses that can only be met later (when sourcing or shipping), legal or
  commercial terms a person has to accept, and clauses the data says nothing about.
Set certain=true only if you would stake the order on it; when in doubt, answer unsure. Evidence quotes must be
copied exactly from the order data (a value, a phrase), with where it came from. Quoting the clause itself does not
count as evidence. Do not guess, do not assume defaults, do not infer from what is usual in the industry."""


def _val(v) -> str | None:
    return v.raw if v is not None else None


def _doc_view(x: Extracted | None) -> dict | None:
    if x is None:
        return None
    h = x.doc.header
    return {
        "source": x.source,
        "header": {k: _val(getattr(h, k)) for k in h.model_fields if k != "doc_type" and _val(getattr(h, k))},
        "lines": [{k: _val(getattr(l, k)) for k in l.model_fields if k != "line_notes" and _val(getattr(l, k))}
                  | ({"notes": [n.raw for n in l.line_notes if n.raw]} if l.line_notes else {}) for l in x.doc.lines],
        "notes": [n.raw for n in x.doc.notes if n.raw],
        "references": [r.raw for r in x.doc.references if r.raw],
    }


def order_data(po: Extracted | None, ack: Extracted | None, erp: dict) -> str:
    return json.dumps({"customer_po": _doc_view(po), "our_ack": _doc_view(ack), "erp": erp}, indent=1, default=str)


def _grounded(j: _Judgment, clause: str, data: str) -> bool:
    """Every quote is in the order data, and at least one is not just the clause."""
    corpus, own = parse.squash(data), parse.squash(clause)
    quotes = [parse.squash(e.quote) for e in j.evidence if e.quote.strip()]
    return bool(quotes) and all(q in corpus for q in quotes) and any(q not in own for q in quotes)


def _ask(clauses: list[str], data: str) -> list[_Judgment | None]:
    from . import llm

    cache_dir = config.STORE_DIR / "cache" / "judge"
    key = hashlib.sha256(json.dumps([clauses, data, INSTRUCTIONS, config.JUDGE_MODEL]).encode()).hexdigest()[:24]
    path = cache_dir / f"{key}.json"
    if path.exists():
        out = _Judgments.model_validate_json(path.read_text())
    else:
        text = "ORDER DATA:\n" + data + "\n\nCLAUSES:\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(clauses))
        out = llm.parse(config.JUDGE_MODEL, INSTRUCTIONS, [llm.text_part(text)], _Judgments)
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(out.model_dump_json())
    by_index = {j.index: j for j in out.items}
    return [by_index.get(i) for i in range(len(clauses))]


def judge(findings: list[Finding], po: Extracted | None, ack: Extracted | None, erp: dict) -> list[Finding]:
    """Judge the unrecognised clauses among `findings` in one model call. Returns the findings with
    those items updated; on a model error they stay needs_review."""
    todo = [i for i, f in enumerate(findings) if f.check_id == "R.unclassified" and f.status == Status.NEEDS_REVIEW]
    if not todo:
        return findings
    clauses = [findings[i].evidence[0].value or "" for i in todo]
    data = order_data(po, ack, erp)
    try:
        judgments = _ask(clauses, data)
    except Exception as e:  # the fallback failing leaves the items with a person, as before
        for i in todo:
            findings[i].message += f" (The clause judge could not run: {e!r}.)"
        return findings
    for i, clause, j in zip(todo, clauses, judgments):
        f = findings[i]
        if j is None:
            continue
        grounded = _grounded(j, clause, data)
        if j.verdict == "met" and j.certain and grounded:
            status, message = Status.PASS, f"Judged met from the order data: {j.reasoning}"
        elif j.verdict == "conflict" and grounded:
            status, message = Status.FAIL, f"Judged in conflict with the order data: {j.reasoning}"
        else:
            why = {"met": "the judge thinks it is met but is not certain or could not quote order data that shows it",
                   "conflict": "the judge suspects a conflict but could not quote order data that shows it",
                   "unsure": "the judge could not settle it from the order data"}[j.verdict]
            status, message = Status.NEEDS_REVIEW, f"{f.message} The judge's reading ({why}): {j.reasoning}"
        f.original_status = f.status
        f.resolution = {"by": "llm_judge", "verdict": j.verdict, "certain": j.certain, "grounded": grounded,
                        "reasoning": j.reasoning, "evidence": [e.model_dump() for e in j.evidence],
                        "model": config.JUDGE_MODEL, "at": datetime.now().isoformat(timespec="seconds")}
        f.status, f.message = status, message
        if status == Status.PASS:
            f.severity = "info"
    return findings
