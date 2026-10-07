"""The order-desk agent: an LLM tool loop that receives an email and decides what to do.

Tools now: read the email, verify the customer order, draft a reply. It cannot change what
the verification tool found; it decides what to do about it. Later: ERP lookups, supplier-PO
actions, sending without dry-run.
"""
from __future__ import annotations

import contextvars
import json
import re
from datetime import date, datetime
from pathlib import Path

import httpx

from . import config, ingest, llm, mail, parse, verify
from . import state as state_mod
from .models import Extracted

INSTRUCTIONS = f"""You are the order desk agent for Demo Distributor, an electronic component distributor.
You receive one incoming email at a time from the order desk mailbox.

Your job:
1. Read the email (get_email). Decide whether it contains a customer purchase order (in an attachment of any
   format, or typed in the body) or a revision of one. If it does not (RFQs, notifications, replies, tests,
   anything else), do nothing and say briefly why.
2. For a customer PO, call verify_customer_order. It runs Demo Distributor's full order checklist against the
   sales order in the ERP and the customer's terms, and returns one result per order in the email, each with a
   recommendation (release, hold, needs_approval, review), issues and the evidence for each. Several versions
   of the same PO in one email are treated as a revision: the latest is verified and diffed against the
   earlier one. If the email says the PO is revised or updated, say so in `revision_note`; the tool also
   detects revisions of POs it has seen before. Cover every order in the email in your reply.
3. Decide what to do next, then draft a reply with draft_reply. The reply goes to the order desk
   ({config.REPLY_TO}), not to the customer. Write it for the sales rep: the recommendation first, then each
   issue in one line with the values and where they came from, then what is needed to release the order
   (a revised PO from the customer, the customer's written acceptance, a manager's approval, or an SO
   correction). For a revision, say what changed and which earlier issues are now resolved or still open.

Before deciding, work the issues the code could not settle (resolvable_by_agent=true: needs_review and unverifiable).
First, give the tool what it was missing, and let the code check again: when you find a missing input, call
verify_customer_order again with it and work from the new result (new run_id):
- the SO was not found or the wrong SO was matched: find it with query_erp (ord_hedr by cust_no and cust_po), then pass
  so_number;
- the PO refers to terms or quality clauses it does not contain: if the PO gives a link, pass it in referenced_documents
  (kind terms or quality); if the customer sent them by email, search_mailbox and pass the message_id and attachment.
  Pass only a document that is clearly the one the PO refers to.
Then, for what is still open, investigate and call resolve_review_item. Examples:
- a requirement checked against vendor notes because we had no supplier PO document: search_mailbox for the PO number,
  then verify_supplier_order on that email to read the PO's actual conditions;
- no approved quote linked: query_erp the quote tables (quote, quo_detl) for this customer and these parts;
- stock sources disagree or a part looks short: lookup_inventory;
- a supplier PO line not tied to an order: query_erp open sales order lines (ord_detl joined to ord_hedr with status
  O, I or H) for that part;
- an unrecognised clause or a terms conflict: read_order_documents and judge it against the order.
Quote exact evidence you saw. not_met means you found positive evidence of a problem (a document or record says
something that breaks the requirement); when evidence is simply missing (no quote found, no document found, origin
unknown), use cannot_tell. Leave items that need a person's judgement or access you do not have (for example a link
that does not open). Failures and approvals are not yours to resolve. Keep it proportionate: a few tool calls per item.

After resolving, if an order's recommendation is hold or needs_approval, put it on hold with hold_order, giving the main
reason in one line. Do not hold orders the tool recommends releasing or sends to review only. In the reply, say which
items you resolved and how.

Last, before the reply, look past the checklist and record what the order desk should act on with add_suggestion: at
most 5, only what is worth a person's time, each resting on data you queried. Do not restate issues the tool reported;
build on them (the tool says a line is short, you find who can supply it). Look for:
- a line we cannot cover from stock or open POs: who has sold us this part before (po_detl joined to po_hedr on po_no,
  by item_no and manu_no: vend_no, latest po_dt, unit_cost, src_cntry), whether that vendor is usable (vendor.active,
  pay_hold, iso_cert_exp), and whether we have an open PO to them (po_hedr.status 'O') the line could be added to. If
  nobody has sold us the part, the vendors we buy that manufacturer's parts from;
- supply we could use: open supplier PO lines for the part not tied to an order (so_order_no null, qty_recvd < ord_qty),
  or stock in another warehouse (itm_inv by whse_cd);
- pricing: the SO's unit_cost against what we last paid, the margin, a cheaper recent source;
- the customer: other open orders for the same parts (buy once), or anything on the account worth a call;
- master data that looks wrong (for example an item's default country of origin that no lot or PO line agrees with).
For a supplier PO, the same: an at-risk customer order and how to cover it. Mention the suggestions briefly in the reply.

Supplier side: if the email is our own purchase order to a supplier (usually in Sent Items) or a supplier's
acknowledgement / order confirmation of one of our POs, call verify_supplier_order instead. Reply to the order desk
with what is wrong on our PO or in what the supplier confirmed (wrong or substituted part, quantity, price, a later
date, origin, date code), and name any customer sales order that will be affected. Do not use hold_order for
supplier POs; say in the reply which customer orders are at risk.

You may investigate before replying, read-only: lookup_inventory (stock, lots, open supplier POs for a part) and
query_erp (one SELECT against the ERP). Use them to suggest next steps, for example stock or an open supplier PO
that could cover a short line, or what else is open for the customer. They never override the tool's findings.

ERP notes for query_erp (Postgres, schema worktrial): text columns are space padded, so use trim(); order_no and
po_no are integers. Sales orders: ord_hedr (order_no, cust_no, cust_po, terms_code, status, quote_no) and ord_detl
(order_no, lin_no, item_no, manu_no, ord_qty, unit_price, unit_cost, vend_no, req_date). Customers: customer
(cust_no, cust_name, terms_code, crdt_limit, ...). Items: item (item_no, manu_no, alt_item_no, last_cost, avg_cost,
min_ord, ncnr). Stock: itm_inv (whse_cd, item_no, manu_no, qty_on_hnd, qty_commit, qty_on_ord, avg_lead_tm), lot_hedr /
lot_info (joined on li_int_id). Supplier POs: po_hedr (po_no, vend_no, po_dt, whse_cd, status O open / C closed) and
po_detl (po_no, lin_no, item_no, manu_no, ord_qty, qty_recvd, unit_cost, due_date, src_cntry, vend_item, so_order_no,
so_lin_no). Vendors: vendor (vend_no, po_name, active, pay_hold, iso_cert_exp, min_ord_val, last_purdt, po_notes,
source_notes). Quotes: quote (quote_no, quote_dt, cust_no,
cust_ref, status, valid_days, terms_code, ship_via) and quo_detl (quote_no, lin_no, item_no, manu_no, quantity,
unit_price, unit_cost, status, del_date, req_date, cust_itmno); a sales order's quote is ord_hedr.quote_no (text).
Customer PO numbers repeat across customers: always filter on cust_no too.

Rules: never invent or soften findings. Report what the tool found, including checks it could not verify.
Do not claim a check passed unless the tool says so. Keep the reply short and plain."""

TOOLS = [
    {"type": "function", "name": "get_email", "description": "Read an email from the order desk mailbox: sender, subject, body and attachment list.",
     "parameters": {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"], "additionalProperties": False}},
    {"type": "function", "name": "verify_customer_order",
     "description": "Verify the customer PO in an email (or in local files) against its sales order, the ERP and the customer's terms. "
                    "Returns the recommendation, issues with evidence, carry-forward requirements and any revision diff.",
     "parameters": {"type": "object", "properties": {
         "message_id": {"type": ["string", "null"], "description": "The email holding the PO."},
         "po_path": {"type": ["string", "null"], "description": "A local PO file, when not verifying an email."},
         "ack_path": {"type": ["string", "null"], "description": "A local SO acknowledgement file, if available."},
         "revision_note": {"type": ["string", "null"], "description": "Why you think this is a revised PO, if you do."},
         "so_number": {"type": ["integer", "null"],
                       "description": "Our sales order number, when the tool could not find the SO or matched the wrong one and you found the "
                                      "right one (query_erp). Only for an email with one order."},
         "referenced_documents": {
             "type": ["array", "null"],
             "description": "Documents the PO refers to but does not contain, which you found: its terms and conditions (kind 'terms') or "
                            "its quality clause list (kind 'quality'). Give a url that appears in the PO or an email you read, or a "
                            "message_id plus attachment name (attachment null for the email body). They are read clause by clause "
                            "like terms printed in the PO.",
             "items": {"type": "object", "properties": {
                 "kind": {"type": "string", "enum": ["terms", "quality"]},
                 "url": {"type": ["string", "null"]}, "message_id": {"type": ["string", "null"]}, "attachment": {"type": ["string", "null"]}},
                 "required": ["kind", "url", "message_id", "attachment"], "additionalProperties": False}}},
         "required": ["message_id", "po_path", "ack_path", "revision_note", "so_number", "referenced_documents"], "additionalProperties": False}},
    {"type": "function", "name": "verify_supplier_order",
     "description": "Verify our purchase order to a supplier and/or the supplier's acknowledgement of it: against the ERP PO, the sales order "
                    "lines it buys for and that customer's terms, the vendor's record, and (for an acknowledgement) what we ordered. "
                    "Returns one result per PO in the email.",
     "parameters": {"type": "object", "properties": {
         "message_id": {"type": ["string", "null"], "description": "The email holding our PO or the supplier's acknowledgement."},
         "po_path": {"type": ["string", "null"]}, "ack_path": {"type": ["string", "null"]},
         "po_number": {"type": ["integer", "null"], "description": "Our PO number, if known."}},
         "required": ["message_id", "po_path", "ack_path", "po_number"], "additionalProperties": False}},
    {"type": "function", "name": "search_mailbox",
     "description": "Search the Inbox and Sent Items, for example for a PO number (our supplier PO, a supplier's acknowledgement, a customer's email).",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}},
    {"type": "function", "name": "read_order_documents",
     "description": "The documents and ERP data behind one verification (run_id from verify_*): PO and acknowledgement text, lines, notes, "
                    "links, the ERP snapshot and the customer's terms. Use it to read and quote evidence.",
     "parameters": {"type": "object", "properties": {"run_id": {"type": "integer"}}, "required": ["run_id"], "additionalProperties": False}},
    {"type": "function", "name": "resolve_review_item",
     "description": "Give a verdict on one issue the code could not settle (status needs_review or unverifiable, resolvable_by_agent=true), "
                    "after investigating it. verdict: met (the concern is cleared), not_met (it is a real problem), cannot_tell. "
                    "Evidence must quote exact text or values you saw in the documents, the verification or your tool outputs. "
                    "Failures and approvals cannot be resolved this way. Returns the order's new recommendation.",
     "parameters": {"type": "object", "properties": {
         "run_id": {"type": "integer"}, "index": {"type": "integer", "description": "The issue's index from verify_*."},
         "verdict": {"type": "string", "enum": ["met", "not_met", "cannot_tell"]},
         "reasoning": {"type": "string", "description": "One or two sentences on how you decided."},
         "evidence": {"type": "array", "items": {"type": "object", "properties": {"quote": {"type": "string"}, "source": {"type": "string"}},
                                                  "required": ["quote", "source"], "additionalProperties": False}}},
         "required": ["run_id", "index", "verdict", "reasoning", "evidence"], "additionalProperties": False}},
    {"type": "function", "name": "add_suggestion",
     "description": "Record one suggestion for the order desk: something worth doing past the checks, found by your own queries "
                    "(e.g. a part we cannot cover and a vendor who has sold it to us before). Advisory only: it does not change "
                    "any check or the recommendation. Every evidence quote must be copied exactly from a tool output you saw.",
     "parameters": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["sourcing", "allocation", "pricing", "customer", "supplier", "data_fix", "other"]},
         "priority": {"type": "string", "enum": ["high", "medium", "low"]},
         "title": {"type": "string", "description": "One line, specific: part, quantity, vendor, order."},
         "detail": {"type": "string", "description": "What you found and why it matters, with the numbers."},
         "action": {"type": "string", "description": "The concrete next step and who would take it (buyer, sales rep, credit, ...)."},
         "so_number": {"type": ["integer", "null"]}, "po_number": {"type": ["integer", "null"]},
         "evidence": {"type": "array", "items": {"type": "object", "properties": {"quote": {"type": "string"}, "source": {"type": "string"}},
                                                 "required": ["quote", "source"], "additionalProperties": False}}},
         "required": ["kind", "priority", "title", "detail", "action", "so_number", "po_number", "evidence"], "additionalProperties": False}},
    {"type": "function", "name": "lookup_inventory",
     "description": "Read-only: stock (itm_inv), lots with origin and date code, and open supplier PO lines for a part.",
     "parameters": {"type": "object", "properties": {
         "item_no": {"type": "string", "description": "Manufacturer part number as on the SO."},
         "manufacturer": {"type": ["string", "null"], "description": "ERP manufacturer code or name; null for all."}},
         "required": ["item_no", "manufacturer"], "additionalProperties": False}},
    {"type": "function", "name": "query_erp",
     "description": "Read-only: run one SELECT (or WITH ... SELECT) against the ERP. Returns at most 200 rows.",
     "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"], "additionalProperties": False}},
    {"type": "function", "name": "hold_order",
     "description": "Put an open sales order on hold in the ERP (status O or I -> H), with a one-line reason. "
                    "Dry-run until ERP writes are switched on: the action is planned and logged, not executed.",
     "parameters": {"type": "object", "properties": {"so_number": {"type": "integer"}, "reason": {"type": "string"}},
                    "required": ["so_number", "reason"], "additionalProperties": False}},
    {"type": "function", "name": "draft_reply",
     "description": f"Draft the reply to the order desk. It is only ever addressed to {config.REPLY_TO}.",
     "parameters": {"type": "object", "properties": {"subject": {"type": "string"}, "body": {"type": "string"},
                    "in_reply_to": {"type": ["string", "null"], "description": "The message id being answered."}},
                    "required": ["subject", "body", "in_reply_to"], "additionalProperties": False}},
]

# Files a local run can stand in for an email with.
_LOCAL: dict[str, dict] = {}
# The email being handled by this thread's agent (agents run in parallel, one per email).
_current_message: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_message", default=None)
# Outputs of the tools this agent called: evidence it quotes when resolving an item must appear here
# or in the verification itself.
_seen: contextvars.ContextVar[list[str]] = contextvars.ContextVar("seen", default=[])
# Verification runs made while handling this email, oldest first.
_runs: contextvars.ContextVar[list[int]] = contextvars.ContextVar("runs", default=[])


def _email_docs(email: mail.Email) -> tuple[list[Extracted], list[Extracted], list[str]]:
    """Extract every document in the email: all customer POs and all SO acks."""
    docs, notes = _all_docs(email)
    pos = [x for x in docs if x.doc.header.doc_type == "customer_po" and x.doc.lines]
    acks = [x for x in docs if x.doc.header.doc_type == "so_ack"]
    # If the body restates a PO that is also attached, the attachment is the PO.
    attached = {parse.mpn_key(x.doc.header.doc_number.raw) for x in pos if x.source != "email body"}
    pos = [x for x in pos if not (x.source == "email body" and parse.mpn_key(x.doc.header.doc_number.raw) in attached)]
    return pos, acks, notes


_DOC_CACHE: dict[str, tuple[list[Extracted], list[str]]] = {}


def _all_docs(email: mail.Email) -> tuple[list[Extracted], list[str]]:
    """Every document in the email (attachments, plus the body when it carries one), extracted once."""
    if email.id in _DOC_CACHE:
        return _DOC_CACHE[email.id]
    docs, notes = [], []
    candidates = [ingest.normalize_bytes(_named(a), a.data) for a in email.attachments
                  if a.data and a.is_document and _supported(_named(a))]
    if email.body.strip():
        candidates.append(ingest.normalize_email_body("email body", email.sender, email.subject, email.body))
    for norm in candidates:
        x = ingest.extract(norm)
        notes.append(f"{norm.name}: {x.doc.header.doc_type}, {len(x.doc.lines)} lines")
        # A cover note that only mentions a PO number is not a document: documents have lines.
        if x.doc.lines:
            docs.append(x)
    if len(_DOC_CACHE) > 200:  # extractions are cached on disk anyway; this only saves re-reading within a run
        _DOC_CACHE.clear()
    _DOC_CACHE[email.id] = (docs, notes)
    return docs, notes


_REV = re.compile(r"\brev(?:ision|ised)?\b\.?\s*([a-z0-9]*)|\bamend(?:ed|ment)?\b|\bupdated\b|\breplacement\b|\bv(\d+)\b", re.I)


def _version_key(x: Extracted):
    """Order versions of the same PO: revision markers in the file name or the document, then PO date."""
    text = " ".join([x.source] + [n.raw or "" for n in x.doc.notes] + [x.doc.header.doc_number.raw or ""])
    m = _REV.search(text)
    marker = (1, (m.group(1) or m.group(2) or "").upper()) if m else (0, "")
    d = parse.to_date(x.doc.header.doc_date.raw) or date.min
    return (marker, d)


def group_pos(pos: list[Extracted]) -> list[tuple[Extracted, list[Extracted]]]:
    """Group the POs in one email by PO number. Several versions of one PO (the task notes revised POs
    are often attached next to the original) become (latest, [earlier versions]). Different PO numbers
    are separate orders."""
    groups: dict[str, list[Extracted]] = {}
    for x in pos:
        groups.setdefault(parse.mpn_key(x.doc.header.doc_number.raw) or x.sha256, []).append(x)
    out = []
    for docs in groups.values():
        docs = sorted({d.sha256: d for d in docs}.values(), key=_version_key)  # identical copies count once
        out.append((docs[-1], docs[:-1]))
    return out


def _ack_for(po: Extracted, acks: list[Extracted], single: bool) -> Extracted | None:
    key = parse.mpn_key(po.doc.header.doc_number.raw)
    for a in acks:
        if parse.mpn_key(a.doc.header.customer_po_ref.raw) == key:
            return a
    return acks[0] if single and len(acks) == 1 else None


def _named(a: mail.Attachment) -> str:
    """Inline images are often named 'image001' with no extension; take it from the content type."""
    if Path(a.name).suffix:
        return a.name
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get((a.content_type or "").lower(), "")
    return a.name + ext


def _supported(name: str) -> bool:
    return Path(name).suffix.lower() in {".pdf", ".xlsx", ".xlsm", ".docx", ".eml", *ingest.IMAGE_TYPES}


def tool_get_email(message_id: str) -> dict:
    if message_id in _LOCAL:
        return _LOCAL[message_id]["view"]
    e = mail.get_email(message_id)
    return {"from": e.sender, "subject": e.subject, "received": e.received, "body": e.body[:4000],
            "attachments": [{"name": a.name, "type": a.content_type, "size": a.size, "inline": a.inline} for a in e.attachments]}


def _references(specs: list[dict] | None, pos: list[Extracted]) -> list:
    """Fetch the referenced documents the agent named. Links must appear in the order documents
    or in what the agent has read (its tool outputs)."""
    from . import references

    out = []
    allowed = " ".join([x.model_dump_json() for x in pos] + _seen.get())
    for s in specs or []:
        try:
            if s.get("url"):
                out.append(references.fetch_url(s["url"], s["kind"], allowed))
            elif s.get("message_id"):
                out.append(references.from_email(s["message_id"], s.get("attachment"), s["kind"]))
            else:
                raise references.ReferenceError("give a url, or a message_id (with an attachment name, or null for the body)")
        except (references.ReferenceError, httpx.HTTPError) as e:
            raise ValueError(f"referenced document {s.get('url') or s.get('attachment') or s.get('message_id')}: {e}") from None
    return out


def tool_verify(message_id=None, po_path=None, ack_path=None, revision_note=None, so_number=None, referenced_documents=None) -> dict:
    notes = []
    record_id = message_id
    if message_id and message_id in _LOCAL:
        po_path, ack_path = _LOCAL[message_id]["po_path"], _LOCAL[message_id]["ack_path"]
        message_id = None
    if message_id:
        pos, acks, notes = _email_docs(mail.get_email(message_id))
    else:
        pos = [ingest.extract(ingest.normalize_file(Path(po_path)))] if po_path else []
        acks = [ingest.extract(ingest.normalize_file(Path(ack_path)))] if ack_path else []
    if not pos:
        return {"result": "no customer PO found", "documents": notes}
    groups = group_pos(pos)
    if so_number and len(groups) > 1:
        return {"error": f"so_number applies to an email with one order; this one has {len(groups)}"}
    try:
        refs = _references(referenced_documents, pos)
    except ValueError as e:
        return {"error": str(e), "note": "nothing was verified; call again without that document, or with another source"}
    orders = []
    for latest, earlier in groups:
        r = verify.verify(latest, _ack_for(latest, acks, single=len(groups) == 1), so_number=so_number,
                          previous_po=earlier[-1] if earlier else None, message_id=record_id, references=refs)
        if r.run_id:
            _runs.get().append(r.run_id)
        o = r.for_agent()
        if earlier:
            o["versions_in_email"] = [x.source for x in earlier] + [latest.source]
        orders.append(o)
    out = {"orders": orders, "documents": notes}
    if revision_note:
        out["revision_note"] = revision_note
    return out


def tool_verify_supplier(message_id=None, po_path=None, ack_path=None, po_number=None) -> dict:
    """Our PO to a supplier and/or the supplier's acknowledgement, grouped by our PO number."""
    from . import supplier

    record_id, notes = message_id, []
    if message_id and message_id in _LOCAL:
        po_path, ack_path = _LOCAL[message_id]["po_path"], _LOCAL[message_id]["ack_path"]
        message_id = None
    if message_id:
        docs, notes = _all_docs(mail.get_email(message_id))
    else:
        docs = [ingest.extract(ingest.normalize_file(Path(p))) for p in (po_path, ack_path) if p]
        notes = [f"{x.source}: {x.doc.header.doc_type}" for x in docs]
    pos = [x for x in docs if x.doc.header.doc_type == "supplier_po"]
    acks = [x for x in docs if x.doc.header.doc_type == "supplier_ack"]
    numbers: dict[int | None, dict] = {}
    for x in pos:
        n = supplier._po_number(x, None, None)
        numbers.setdefault(n, {})["po"] = x
    for x in acks:
        n = supplier._po_number(None, x, None)
        numbers.setdefault(n, {})["ack"] = x
    if po_number and not numbers:
        numbers[int(po_number)] = {}
    if not numbers:
        return {"result": "no supplier PO or supplier acknowledgement found", "documents": notes}
    orders = []
    for n, d in numbers.items():
        r = supplier.verify_supplier_order(d.get("po"), d.get("ack"), n, message_id=record_id)
        if r.run_id:
            _runs.get().append(r.run_id)
        orders.append(r.for_agent())
    return {"orders": orders, "documents": notes}


def tool_draft_reply(subject: str, body: str, in_reply_to: str | None = None) -> dict:
    return mail.send(subject, body, in_reply_to)


def tool_lookup_inventory(item_no: str, manufacturer: str | None = None) -> dict:
    from . import db, erp, parse

    code = (parse.mfr_code(manufacturer) or manufacturer.strip().upper()) if manufacturer else None
    with db.connect() as conn:
        e = db.Erp(conn)
        keys = e.rows("select distinct trim(item_no) item_no, trim(manu_no) manu_no from item where trim(item_no) = %s"
                      + (" and trim(manu_no) = %s" if code else ""), (item_no.strip(), code) if code else (item_no.strip(),))
        out = []
        for k in keys:
            inv = erp.inventory(e, k["item_no"], k["manu_no"])
            lots = [x for x in erp.lots(e, k["item_no"], k["manu_no"]) if (x["bal_of_lot"] or 0) > 0]
            out.append({
                "item_no": k["item_no"], "manu_no": k["manu_no"],
                "itm_inv": {c: inv[c] for c in ("qty_on_hnd", "qty_commit", "qty_on_ord", "avg_lead_tm", "max_lead_tm")} if inv else None,
                "lots_with_balance": [{c: x[c] for c in ("lot_no", "bal_of_lot", "date_code", "cnty_of_origin", "vend_no", "received")} for x in lots],
                "open_supplier_po_lines": [{c: p[c] for c in ("po_no", "lin_no", "vend_no", "ord_qty", "qty_recvd", "unit_cost", "due_date", "src_cntry", "so_order_no", "so_lin_no")}
                                           for p in erp.open_po_lines_for_item(e, k["item_no"], k["manu_no"])],
            })
    return {"items": out, "note": "itm_inv and lot balances often disagree in this ERP; open PO lines with so_order_no set are committed to that SO."}


def tool_query_erp(sql: str) -> dict:
    from . import db, erp

    with db.connect() as conn:
        rows = erp.query(db.Erp(conn), sql)
    return {"rows": rows, "count": len(rows)}


def tool_search_mailbox(query: str) -> dict:
    hits = mail.search(query)
    return {"messages": [h for h in hits if not (h["subject"] or "").startswith(("[Order agent]", "[Order desk agent]"))][:15]}


def tool_read_order_documents(run_id: int) -> dict:
    """The documents and ERP data behind one verification, compactly, so the agent can read and quote them."""
    run = state_mod.get_run(int(run_id))
    if not run or not run.get("report_path"):
        return {"error": "run not found"}
    folder = Path(run["report_path"]).parent
    out = {"run_id": run_id}
    for name in ("po", "ack"):
        p = folder / f"{name}.json"
        if p.exists():
            x = Extracted.model_validate_json(p.read_text())
            h = x.doc.header
            out[name] = {
                "source": x.source, "type": h.doc_type,
                "header": {k: v.raw for k, v in h if hasattr(v, "raw") and v.raw},
                "lines": [{k: v.raw for k, v in l if hasattr(v, "raw") and v.raw} for l in x.doc.lines],
                "notes": [n.raw for n in x.doc.notes], "links": x.links,
            }
    result = json.loads((folder / "result.json").read_text())
    out["erp"] = result.get("erp")
    out["customer_terms"] = [{"clause": r["clause_type"], "text": r["quote"], "source": r["source"]} for r in result.get("requirements", [])]
    return out


def tool_resolve_review_item(run_id: int, index: int, verdict: str, reasoning: str, evidence: list) -> dict:
    from . import resolve
    return resolve.resolve(int(run_id), int(index), verdict, reasoning, evidence or [], _seen.get())


MAX_SUGGESTIONS = 6


def tool_add_suggestion(kind: str, priority: str, title: str, detail: str, action: str, evidence: list,
                        so_number: int | None = None, po_number: int | None = None) -> dict:
    mid = _current_message.get()
    if not evidence:
        return {"error": "a suggestion needs evidence: quote the values from your tool outputs that it rests on"}
    corpus = parse.squash(" ".join(_seen.get()))
    unseen = [e.get("quote", "") for e in evidence if parse.squash(e.get("quote", "")) not in corpus]
    if unseen:
        return {"error": f"these quotes do not appear in your tool outputs, so the suggestion was not recorded: {unseen}. Quote exactly what you saw."}
    if len(state_mod.suggestions(message_id=mid, status="open")) >= MAX_SUGGESTIONS:
        return {"error": f"already {MAX_SUGGESTIONS} suggestions for this email; keep the most useful ones"}
    # Attach it to this email's verification of the same order, so it shows with that order.
    run_id = None
    for rid in reversed(_runs.get()):
        run = state_mod.get_run(rid)
        if run and ((so_number and run.get("so_number") == so_number) or (po_number and run.get("po_number") == po_number)):
            run_id = rid
            break
    sid = state_mod.add_suggestion(mid, run_id, so_number, po_number, kind, priority, title, detail, action, evidence)
    return {"recorded": sid, "attached_to_run": run_id}


def tool_hold_order(so_number: int, reason: str) -> dict:
    from . import erp_write
    return erp_write.hold_order(int(so_number), reason, _current_message.get())


HANDLERS = {"get_email": tool_get_email, "verify_customer_order": tool_verify, "draft_reply": tool_draft_reply,
            "lookup_inventory": tool_lookup_inventory, "query_erp": tool_query_erp, "hold_order": tool_hold_order,
            "verify_supplier_order": tool_verify_supplier, "search_mailbox": tool_search_mailbox,
            "read_order_documents": tool_read_order_documents, "resolve_review_item": tool_resolve_review_item,
            "add_suggestion": tool_add_suggestion}


def run(message_id: str, max_steps: int = 40, folder: str = "inbox") -> dict:
    """Handle one email end to end. Returns the agent's final note and a transcript."""
    _current_message.set(message_id)
    _seen.set([])
    _runs.set([])
    state_mod.clear_open_suggestions(message_id)  # reprocessing replaces earlier open suggestions
    log = {"message_id": message_id, "folder": folder, "started": datetime.now().isoformat(timespec="seconds"), "steps": []}
    if message_id not in _LOCAL:
        email = mail.get_email(message_id, with_attachments=False)
        # The sender gate: in the Inbox, mail from the mailbox itself is our own or other agents' output.
        # Sent Items is all from the mailbox, so there only our agent's tagged replies are skipped.
        if email.from_agent or (folder == "inbox" and email.from_mailbox):
            log["final"] = f"Ignored: sent by {email.sender} (the order desk mailbox itself)."
            return _save(log)
    where = "sent by us (Sent Items)" if folder == "sentitems" else "received (Inbox)"
    resp = llm.client().responses.create(model=config.AGENT_MODEL, instructions=INSTRUCTIONS, tools=TOOLS,
                                         input=f"New email in the order desk mailbox, {where}. message_id: {message_id}")
    for _ in range(max_steps):
        calls = [o for o in resp.output if o.type == "function_call"]
        if not calls:
            break
        outputs = []
        for c in calls:
            args = json.loads(c.arguments or "{}")
            try:
                result = HANDLERS[c.name](**args)
            except Exception as e:  # tool errors go back to the model, they don't crash the loop
                result = {"error": repr(e)}
            log["steps"].append({"tool": c.name, "args": args, "result": result})
            output = json.dumps(result, default=str)
            if c.name not in ("resolve_review_item", "add_suggestion"):
                _seen.get().append(output)  # what the agent has seen, for checking the evidence it quotes
            outputs.append({"type": "function_call_output", "call_id": c.call_id, "output": output})
        resp = llm.client().responses.create(model=config.AGENT_MODEL, instructions=INSTRUCTIONS, tools=TOOLS,
                                             previous_response_id=resp.id, input=outputs)
    log["final"] = resp.output_text
    log["erp_records"] = record_outcomes(message_id, log)
    return _save(log)


def record_outcomes(message_id: str, log: dict) -> list[dict]:
    """Write each order's settled result to the ERP: the latest verification of each order in this
    email (a re-verification with more inputs supersedes the first), with the agent's resolutions,
    its summary, and whether it put the order on hold."""
    from . import erp_write

    held = {int(s["args"]["so_number"]) for s in log["steps"]
            if s["tool"] == "hold_order" and isinstance(s["result"], dict) and not ({"error", "skipped"} & s["result"].keys())}
    latest: dict[str, int] = {}
    for rid in _runs.get():
        run = state_mod.get_run(rid)
        if run and run.get("report_path"):
            latest[run.get("order_key") or str(rid)] = rid
    out = []
    for rid in latest.values():
        path = Path(state_mod.get_run(rid)["report_path"]).with_name("result.json")
        result = json.loads(path.read_text())
        so = result["order"].get("so_number")
        on_hold = None if result["order"].get("kind") == "supplier" else so in held
        out.append(erp_write.record_result(result, rid, message_id, agent_summary=log.get("final"), on_hold=on_hold))
    return out


def run_local(po_path: str, ack_path: str | None = None, subject: str = "Purchase order", sender: str = "customer@example.com") -> dict:
    """Run the agent on local files as if they had arrived by email (replay of the folder fixtures)."""
    mid = f"local:{Path(po_path).name}"
    _LOCAL[mid] = {"po_path": po_path, "ack_path": ack_path,
                   "view": {"from": sender, "subject": subject, "received": None, "body": "(see attachment)",
                            "attachments": [{"name": Path(po_path).name, "type": None, "size": Path(po_path).stat().st_size, "inline": False}]}}
    return run(mid)


def _save(log: dict) -> dict:
    out = config.STORE_DIR / "agent_runs"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(log, indent=1, default=str))
    log["log_path"] = str(path)
    return log
