"""Writes to the ERP: record the outcome of an order check, put an order on hold.

Dry-run by default (ERP_DRY_RUN=1): each action is planned, with its exact SQL and the rows it
would write, and logged to the state store (visible at GET /erp-actions) but not executed. With
ERP_DRY_RUN=0 the same statements run in one transaction. Our own tables carry an `agent_` prefix;
the only change to an ERP table is the order status, and only from open (O, I) to hold (H).

The check record is written once an order is settled: at the end of the agent's handling of an
email (after it resolved what it could and decided on a hold), or when a person checks uploaded
documents. Not from batch or test runs, and not for each intermediate verification.
"""
from __future__ import annotations

import json

from . import config, db, state

SCHEMA = [
    "create table if not exists worktrial.agent_check_result (id bigserial primary key, kind text, so_number integer, "
    "po_number integer, cust_no text, cust_po text, customer text, vendor text, recommendation text, reasons jsonb, counts jsonb, "
    "requirements jsonb, agent_summary text, on_hold boolean, source text, message_id text, run_id integer, "
    "checked_at timestamptz default now())",
    # One row per check: what was compared, where each value came from (evidence), and the result.
    "create table if not exists worktrial.agent_check_item (result_id bigint references worktrial.agent_check_result(id), "
    "seq integer, check_id text, checklist_ref text, title text, line integer, status text, severity text, message text, "
    "evidence jsonb, original_status text, resolved_by text, resolution jsonb)",
    "create table if not exists worktrial.agent_order_hold (id bigserial primary key, so_number integer, previous_status text, "
    "reason text, message_id text, created_at timestamptz default now())",
]


def _insert(table: str, row: dict, returning: str | None = None) -> dict:
    """An insert statement plus the row it writes, so a dry run shows the values, not placeholders."""
    cols = list(row)
    marks = ", ".join("%s::jsonb" if isinstance(row[c], (dict, list)) else "%s" for c in cols)
    sql = f"insert into worktrial.{table} ({', '.join(cols)}) values ({marks})" + (f" returning {returning}" if returning else "")
    params = [json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in row.values()]
    return {"table": table, "row": row, "sql": sql, "params": params}


def record_result(result: dict, run_id: int | None = None, message_id: str | None = None,
                  agent_summary: str | None = None, on_hold: bool | None = None) -> dict:
    """Record a settled check: the order, the recommendation and why, the customer's terms as read,
    and every check with its evidence. `result` is the stored result (result.json), including any
    items the agent resolved."""
    o = result.get("order") or {}
    kind = o.get("kind") or "customer"
    key = o.get("po_number") if kind == "supplier" else o.get("so_number")
    if not key:
        return {"skipped": "no sales order" if kind == "customer" else "no supplier PO number"}
    head = _insert("agent_check_result", {
        "kind": kind, "so_number": o.get("so_number"), "po_number": o.get("po_number"), "cust_no": o.get("cust_no"),
        "cust_po": o.get("cust_po"), "customer": o.get("customer"), "vendor": o.get("vendor"),
        "recommendation": result.get("recommendation"), "reasons": result.get("reasons") or [], "counts": result.get("counts") or {},
        "requirements": [{"clause_type": r["clause_type"], "quote": r["quote"], "source": r["source"], "params": r.get("params") or {}}
                         for r in result.get("requirements") or []],
        "agent_summary": agent_summary, "on_hold": on_hold, "source": o.get("source") or ("email" if message_id else None),
        "message_id": message_id, "run_id": run_id,
    }, returning="id")
    items = []
    for i, f in enumerate(result.get("findings") or []):
        res = f.get("resolution") or None
        items.append(_insert("agent_check_item", {
            "result_id": None, "seq": i, "check_id": f["check_id"], "checklist_ref": f.get("checklist_ref"), "title": f["title"],
            "line": f.get("line"), "status": f["status"], "severity": f.get("severity"), "message": f.get("message"),
            "evidence": f.get("evidence") or [], "original_status": f.get("original_status"),
            "resolved_by": res.get("by") if res else None, "resolution": res,
        }))

    def run(cur):
        cur.execute(head["sql"], head["params"])
        rid = cur.fetchone()["id"]
        for s in items:
            cur.execute(s["sql"], [rid, *s["params"][1:]])
        return {"result_id": rid, "items": len(items)}

    summary = f"{kind} order {key}: {result.get('recommendation')}, {len(items)} checks recorded"
    return _apply("record_result", o.get("so_number"), [head] + items, run, message_id, summary, run_id=run_id, po_number=o.get("po_number"))


def hold_order(so_number: int, reason: str, message_id: str | None = None) -> dict:
    """Put an open order on hold (status O or I -> H) and keep the reason."""
    with db.connect() as conn:
        row = db.Erp(conn).row("select trim(status) as status from ord_hedr where order_no = %s", (so_number,))
    if not row:
        return {"skipped": f"SO {so_number} not found"}
    if row["status"] not in ("O", "I"):
        return {"skipped": f"SO {so_number} has status {row['status']}; only open orders (O, I) are put on hold"}
    update = {"table": "ord_hedr", "row": {"order_no": so_number, "status": f"{row['status']} -> H", "updt_id": "AGENT"},
              "sql": "update worktrial.ord_hedr set status = 'H', updt_dt = now(), updt_id = 'AGENT' where order_no = %s and trim(status) in ('O', 'I')",
              "params": [so_number]}
    log = _insert("agent_order_hold", {"so_number": so_number, "previous_status": row["status"], "reason": reason, "message_id": message_id})

    def run(cur):
        cur.execute(update["sql"], update["params"])
        cur.execute(log["sql"], log["params"])
        return {"previous_status": row["status"]}

    return _apply("hold_order", so_number, [update, log], run, message_id, reason)


def _apply(kind: str, so_number, statements: list[dict], run, message_id=None, note=None, **extra) -> dict:
    planned = [{"table": s["table"], "row": s["row"], "sql": s["sql"], "params": s["params"]} for s in statements]
    detail = {"statements": planned, "note": note, **extra}
    if config.ERP_DRY_RUN:
        state.add_erp_action(kind, so_number, True, detail, message_id)
        return {"dry_run": True, "kind": kind, "so_number": so_number, "statements": len(planned)}
    with db.connect(read_only=False) as conn:
        with conn.cursor() as cur:
            for ddl in SCHEMA:
                cur.execute(ddl)
            out = run(cur)
        conn.commit()
    state.add_erp_action(kind, so_number, False, {**detail, "result": out}, message_id)
    return {"dry_run": False, "kind": kind, "so_number": so_number, **out}
