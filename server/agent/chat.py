"""The order desk chatbot: plain-English questions answered from the ERP.

An LLM tool loop with read-only tools. Definitions are shared with the verification tool so the
two never disagree: open orders are statuses O, I and H; credit exposure is open orders plus a
positive AR balance, against the credit limit, for net-terms customers. Conversations are kept
per session (the model provider stores the turns; we keep a copy for the UI).
"""
from __future__ import annotations

import json
import uuid

from . import config, db, erp, ledger, llm, state
from .orchestrator import tool_lookup_inventory, tool_query_erp

INSTRUCTIONS = """You answer questions from Demo Distributor's order desk (an electronic component distributor) using
its ERP and the order agent's verification results. Use the tools; do not answer from memory.

- Customers: resolve names with find_customers. Many customers have look-alike names (e.g. Sandstone Lighting vs
  Sandstone Electronics Manufacturing); if more than one could match, say which you used or ask.
- Open sales orders are statuses O (open), I (in process) and H (hold). C is closed (shipped and invoiced), V is void.
- Credit: use credit_exposure. Exposure is open orders plus a positive AR balance, compared with the credit limit, and
  matters for net terms (N30, N45, N60); prepay customers (CIA, CC, COD) don't use a credit line.
- For a customer's terms use customer_profile: it returns the ERP terms and the clauses the agent reads from them.
- For the agent's findings on an order use order_status or open_issues.
- query_erp runs one read-only SELECT (schema worktrial; text columns are space padded, use trim(); customer PO
  numbers repeat across customers, so filter on cust_no too).
Answer briefly and plainly. Give the numbers and say where they came from (table or tool). Use a short table when
listing several rows. If the data can't answer the question, say so."""


def _f(x):
    return float(x) if x is not None else None


def tool_find_customers(name: str) -> dict:
    with db.connect() as conn:
        rows = db.Erp(conn).rows(
            "select trim(cust_no) cust_no, trim(cust_name) cust_name, trim(terms_code) terms_code, crdt_limit "
            "from customer where cust_name ilike %s order by cust_name limit 15", (f"%{name.strip()}%",))
    return {"matches": [{**r, "crdt_limit": _f(r["crdt_limit"])} for r in rows]}


def tool_customer_profile(cust_no: str) -> dict:
    with db.connect() as conn:
        e = db.Erp(conn)
        c = erp.customer(e, cust_no)
        if not c:
            return {"error": f"no customer {cust_no}"}
        terms = erp.term_codes(e)
        open_orders, ar = erp.open_order_value(e, cust_no), erp.ar_balance(e, cust_no)
    reqs = ledger.from_customer(c)
    keep = ("cust_no", "cust_name", "terms_code", "crdt_limit", "ship_via", "fob_code", "cust_addr1", "cust_city", "cust_state",
            "cust_zip", "cust_cntry", "comments", "ord_notes", "shipinst")
    return {
        "customer": {k: (_f(c[k]) if k == "crdt_limit" else c.get(k)) for k in keep},
        "payment_terms": {"code": c["terms_code"], "meaning": terms.get(c["terms_code"])},
        "credit": {"limit": _f(c["crdt_limit"]), "open_orders": open_orders, "ar_balance": ar,
                   "exposure": open_orders + max(ar, 0), "over_limit": open_orders + max(ar, 0) > float(c["crdt_limit"] or 0)},
        "terms_as_read_by_the_agent": [{"clause": r.clause_type, "params": r.params, "text": r.quote, "source": r.source} for r in reqs],
    }


def tool_credit_exposure(cust_no: str | None = None, only_over_limit: bool = False) -> dict:
    sql = """
    with o as (select trim(h.cust_no) cust_no, count(distinct h.order_no) open_orders, sum(d.ord_qty * d.unit_price) open_value,
                      string_agg(distinct h.order_no::text, ', ') order_nos,
                      bool_or(trim(h.terms_code) in ('N30', 'N45', 'N60')) any_net_order
               from ord_hedr h join ord_detl d on d.order_no = h.order_no where trim(h.status) in ('O', 'I', 'H') group by 1),
         a as (select trim(cust_no) cust_no, sum(amount) ar from arhist group by 1)
    select c.cust_no, trim(c.cust_name) cust_name, trim(c.terms_code) terms_code, c.crdt_limit, o.open_orders, o.order_nos,
           o.any_net_order, coalesce(o.open_value, 0) open_value, coalesce(a.ar, 0) ar_balance
    from o join customer c on trim(c.cust_no) = o.cust_no left join a on a.cust_no = o.cust_no
    """ + (" where trim(c.cust_no) = %s" if cust_no else "") + " order by c.cust_no"
    with db.connect() as conn:
        rows = db.Erp(conn).rows(sql, (cust_no.strip(),) if cust_no else None)
    out = []
    for r in rows:
        exposure = float(r["open_value"]) + max(float(r["ar_balance"]), 0)
        limit = float(r["crdt_limit"] or 0)
        # Same rule as the verifier: an order on net terms uses the credit line, whatever the master says
        # (Crescent's master says credit card, but its open order is on Net 30).
        net = r["terms_code"] in ("N30", "N45", "N60") or bool(r["any_net_order"])
        item = {"cust_no": r["cust_no"], "customer": r["cust_name"], "terms": r["terms_code"], "credit_limit": limit,
                "open_orders": r["open_orders"], "order_numbers": r["order_nos"], "open_order_value": round(float(r["open_value"]), 2),
                "ar_balance": round(float(r["ar_balance"]), 2), "exposure": round(exposure, 2),
                "over_limit": net and exposure > limit, "uses_credit_line": net}
        if not only_over_limit or item["over_limit"]:
            out.append(item)
    return {"definition": "exposure = open orders (O, I, H) + positive AR balance; a customer uses a credit line if its master "
                          "terms or any open order's terms are net (N30, N45, N60)",
            "customers": out}


def tool_order_status(so_number: int | None = None, cust_po: str | None = None) -> dict:
    with db.connect() as conn:
        e = db.Erp(conn)
        if so_number is None and cust_po:
            hits = erp.sales_orders_for_po(e, cust_po)
            if len(hits) != 1:
                return {"matches": [{"so_number": h["order_no"], "cust_no": h["cust_no"], "customer": h["cust_name"], "status": h["status"]} for h in hits],
                        "note": "PO numbers repeat across customers; pick one SO"}
            so_number = hits[0]["order_no"]
        so = erp.sales_order(e, so_number) if so_number else None
        if not so:
            return {"error": "order not found"}
        lines = erp.so_lines(e, so_number)
    runs = state.runs_for_order(so_number)
    issues = state.open_issues(so=so_number)
    return {
        "erp": {"so_number": so["order_no"], "cust_no": so["cust_no"], "cust_po": so["cust_po"], "status": so["status"],
                "terms": so["terms_code"], "order_date": str(so["order_dt"])[:10],
                "lines": [{"line": l["lin_no"], "item": l["item_no"], "mfr": l["manu_no"], "qty": _f(l["ord_qty"]), "price": _f(l["unit_price"])} for l in lines]},
        "agent": {"recommendation": runs[0]["recommendation"] if runs else None, "verified_at": runs[0]["created"] if runs else None,
                  "issues": [{"status": i["status"], "severity": i["severity"], "title": i["title"], "message": i["message"]} for i in issues[:15]]}
        if runs else "not verified by the agent yet",
    }


def tool_open_issues(status: str | None = None) -> dict:
    rows = state.open_issues(status=status)
    return {"issues": [{"so_number": r["so_number"], "customer": r["customer"], "status": r["status"], "severity": r["severity"],
                        "title": r["title"], "message": r["message"]} for r in rows[:60]], "total": len(rows)}


TOOLS = [
    {"type": "function", "name": "find_customers", "description": "Find customers by (part of) name.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False}},
    {"type": "function", "name": "customer_profile", "description": "A customer's ERP terms, credit position, notes, and the clauses the agent reads from them.",
     "parameters": {"type": "object", "properties": {"cust_no": {"type": "string"}}, "required": ["cust_no"], "additionalProperties": False}},
    {"type": "function", "name": "credit_exposure",
     "description": "Credit exposure per customer with open orders (or one customer). only_over_limit=true lists only customers over their limit, with their open order numbers.",
     "parameters": {"type": "object", "properties": {"cust_no": {"type": ["string", "null"]}, "only_over_limit": {"type": "boolean"}},
                    "required": ["cust_no", "only_over_limit"], "additionalProperties": False}},
    {"type": "function", "name": "order_status", "description": "A sales order from the ERP plus the agent's latest verification of it. Give so_number, or cust_po.",
     "parameters": {"type": "object", "properties": {"so_number": {"type": ["integer", "null"]}, "cust_po": {"type": ["string", "null"]}},
                    "required": ["so_number", "cust_po"], "additionalProperties": False}},
    {"type": "function", "name": "open_issues", "description": "Open issues across verified orders, most severe first. status: fail, needs_approval, needs_review, unverifiable, or null for all.",
     "parameters": {"type": "object", "properties": {"status": {"type": ["string", "null"]}}, "required": ["status"], "additionalProperties": False}},
    {"type": "function", "name": "lookup_inventory", "description": "Stock, lots and open supplier PO lines for a part.",
     "parameters": {"type": "object", "properties": {"item_no": {"type": "string"}, "manufacturer": {"type": ["string", "null"]}},
                    "required": ["item_no", "manufacturer"], "additionalProperties": False}},
    {"type": "function", "name": "query_erp", "description": "One read-only SELECT against the ERP (max 200 rows).",
     "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"], "additionalProperties": False}},
]

HANDLERS = {"find_customers": tool_find_customers, "customer_profile": tool_customer_profile, "credit_exposure": tool_credit_exposure,
            "order_status": tool_order_status, "open_issues": tool_open_issues, "lookup_inventory": tool_lookup_inventory,
            "query_erp": tool_query_erp}


def ask(message: str, session_id: str | None = None, max_steps: int = 8) -> dict:
    """One user turn. Returns the answer, the tools used, and the session id to continue with."""
    session_id = session_id or uuid.uuid4().hex[:12]
    previous = state.get_meta(f"chat:{session_id}")
    state.add_chat_message(session_id, "user", message)
    kwargs = {"previous_response_id": previous} if previous else {}
    resp = llm.client().responses.create(model=config.AGENT_MODEL, instructions=INSTRUCTIONS, tools=TOOLS, input=message, **kwargs)
    used = []
    for _ in range(max_steps):
        calls = [o for o in resp.output if o.type == "function_call"]
        if not calls:
            break
        outputs = []
        for c in calls:
            args = json.loads(c.arguments or "{}")
            try:
                result = HANDLERS[c.name](**args)
            except Exception as e:
                result = {"error": repr(e)}
            used.append({"tool": c.name, "args": args})
            outputs.append({"type": "function_call_output", "call_id": c.call_id, "output": json.dumps(result, default=str)})
        resp = llm.client().responses.create(model=config.AGENT_MODEL, instructions=INSTRUCTIONS, tools=TOOLS,
                                             previous_response_id=resp.id, input=outputs)
    answer = resp.output_text
    state.set_meta(f"chat:{session_id}", resp.id)
    state.add_chat_message(session_id, "assistant", answer, used)
    return {"session_id": session_id, "answer": answer, "tools": used}
