"""Processing state: which emails we have seen, how they were triaged, what each verification
found. A local SQLite file (store/state.db), shared by the inbox watcher and the API.

Kept out of the ERP database on purpose: that database may be shared with other candidates,
and nothing here is ERP data.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

_LOCK = threading.Lock()

SCHEMA = """
create table if not exists messages (
  id text primary key, internet_message_id text, received text, sender text, subject text,
  has_attachments integer, label text, reason_category text, triage_confidence real, triage_reasons text,
  state text, recommendation text, error text, agent_log text, reply text, created text, updated text
);
create table if not exists runs (
  id integer primary key autoincrement, message_id text, so_number integer, cust_no text, cust_po text,
  customer text, recommendation text, counts text, revision text, report_path text, created text
);
create table if not exists issues (
  run_id integer, so_number integer, check_id text, ref text, title text, status text,
  severity text, message text, line integer
);
create table if not exists meta (key text primary key, value text);
create table if not exists chat_messages (
  id integer primary key autoincrement, session_id text, role text, content text, tools text, created text
);
create table if not exists erp_actions (
  id integer primary key autoincrement, kind text, so_number integer, dry_run integer, detail text, message_id text, created text
);
create table if not exists suggestions (
  id integer primary key autoincrement, message_id text, run_id integer, so_number integer, po_number integer,
  kind text, priority text, title text, detail text, action text, evidence text, status text, created text, updated text
);
create index if not exists runs_so on runs (so_number, id);
create index if not exists issues_run on issues (run_id);
"""

# Message states: new -> triaged -> processing -> done | ignored | error
ISSUE_STATUSES = ("fail", "needs_approval", "needs_review", "unverifiable")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


MIGRATIONS = [
    "alter table runs add column kind text default 'customer'",  # customer order or supplier PO
    "alter table runs add column po_number integer",  # our PO number, for supplier runs
    "alter table runs add column order_key text",  # SO<n> or PO<n>: what 'latest run' groups by
    "alter table messages add column folder text default 'inbox'",  # inbox or sentitems
    "alter table runs add column source text",  # email, upload (POST /verify), or cli (batch, tests)
    "update runs set order_key = 'SO' || so_number where order_key is null and so_number is not null",
]


@contextmanager
def db():
    with _LOCK:
        config.STATE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.STATE_DB)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        for m in MIGRATIONS:
            try:
                conn.execute(m)
            except sqlite3.OperationalError:
                pass  # already applied
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _row(r) -> dict | None:
    return dict(r) if r else None


# ---------------------------------------------------------------- messages


def upsert_message(mid: str, **fields) -> None:
    fields["updated"] = now()
    with db() as c:
        exists = c.execute("select 1 from messages where id = ?", (mid,)).fetchone()
        if exists:
            sets = ", ".join(f"{k} = ?" for k in fields)
            c.execute(f"update messages set {sets} where id = ?", (*fields.values(), mid))
        else:
            fields.setdefault("created", fields["updated"])
            cols = ", ".join(["id", *fields])
            c.execute(f"insert into messages ({cols}) values ({', '.join('?' * (len(fields) + 1))})", (mid, *fields.values()))


def get_message(mid: str) -> dict | None:
    with db() as c:
        return _row(c.execute("select * from messages where id = ?", (mid,)).fetchone())


def seen(mid: str) -> bool:
    return get_message(mid) is not None


def list_messages(state: str | None = None, label: str | None = None, limit: int = 100) -> list[dict]:
    sql, args = "select * from messages where 1=1", []
    if state:
        sql += " and state = ?"
        args.append(state)
    if label:
        sql += " and label = ?"
        args.append(label)
    sql += " order by received desc limit ?"
    with db() as c:
        return [dict(r) for r in c.execute(sql, (*args, limit)).fetchall()]


def message_counts() -> dict:
    with db() as c:
        return {r["state"]: r["n"] for r in c.execute("select state, count(*) n from messages group by state")}


# ---------------------------------------------------------------- verification runs


def add_run(result, message_id: str | None = None) -> int:
    """Record a VerificationResult and its issues."""
    o = result.order
    kind = o.get("kind", "customer")
    key = f"PO{o.get('po_number')}" if kind == "supplier" else (f"SO{o['so_number']}" if o.get("so_number") else None)
    source = o.get("source") or ("email" if message_id and not message_id.startswith("local:") else "cli")
    with db() as c:
        cur = c.execute(
            "insert into runs (message_id, so_number, cust_no, cust_po, customer, recommendation, counts, revision, report_path, created, "
            "kind, po_number, order_key, source) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, o.get("so_number"), o.get("cust_no"), o.get("cust_po"), o.get("customer") if kind == "customer" else o.get("vendor"),
             result.recommendation, json.dumps(result.counts), json.dumps(result.revision) if result.revision else None, result.report_path, now(),
             kind, o.get("po_number"), key, source),
        )
        run_id = cur.lastrowid
        c.executemany(
            "insert into issues values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(run_id, o.get("so_number"), f.check_id, f.checklist_ref, f.title, f.status.value, f.severity, f.message, f.line)
             for f in result.findings if f.status.value in ISSUE_STATUSES],
        )
        return run_id


def update_run(run_id: int, recommendation: str, counts: dict, findings) -> None:
    """After the agent resolved items: new recommendation, counts and open issues for this run."""
    with db() as c:
        so = c.execute("select so_number from runs where id = ?", (run_id,)).fetchone()["so_number"]
        c.execute("update runs set recommendation = ?, counts = ? where id = ?", (recommendation, json.dumps(counts), run_id))
        c.execute("delete from issues where run_id = ?", (run_id,))
        c.executemany(
            "insert into issues values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(run_id, so, f.check_id, f.checklist_ref, f.title, f.status.value, f.severity, f.message, f.line)
             for f in findings if f.status.value in ISSUE_STATUSES],
        )


def runs_for_message(mid: str) -> list[dict]:
    with db() as c:
        return [_run(dict(r)) for r in c.execute("select * from runs where message_id = ? order by id", (mid,)).fetchall()]


def runs_for_order(so: int) -> list[dict]:
    """Customer-order runs for one SO (supplier runs that buy for it are not included)."""
    with db() as c:
        return [_run(dict(r)) for r in c.execute("select * from runs where so_number = ? and coalesce(kind, 'customer') = 'customer' order by id desc",
                                                 (so,)).fetchall()]


def latest_runs(source: str | None = None) -> list[dict]:
    """The latest run of each customer order (SO) and each supplier PO, optionally only from one source."""
    where = "order_key is not null" + (" and source = ?" if source else "")
    with db() as c:
        rows = c.execute(f"select r.* from runs r join (select order_key, max(id) id from runs where {where} group by order_key) l "
                         "on l.id = r.id order by r.id desc", (source,) if source else ()).fetchall()
        return [_run(dict(r)) for r in rows]


def runs_for_po(po: int) -> list[dict]:
    with db() as c:
        return [_run(dict(r)) for r in c.execute("select * from runs where kind = 'supplier' and po_number = ? order by id desc", (po,)).fetchall()]


def _run(r: dict) -> dict:
    r["counts"] = json.loads(r["counts"]) if r.get("counts") else {}
    r["revision"] = json.loads(r["revision"]) if r.get("revision") else None
    return r


SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}
STATUS_RANK = {"fail": 0, "needs_approval": 1, "needs_review": 2, "unverifiable": 3}


def open_issues(status: str | None = None, severity: str | None = None, so: int | None = None) -> list[dict]:
    """Issues from the latest run of each order, most severe first."""
    sql = ("select i.*, r.customer, r.cust_po, r.recommendation, r.created, r.kind, r.po_number from issues i join runs r on r.id = i.run_id "
           "join (select order_key, max(id) id from runs where order_key is not null group by order_key) l on l.id = r.id where 1=1")
    args = []
    for col, val in (("i.status", status), ("i.severity", severity), ("i.so_number", so)):
        if val is not None:
            sql += f" and {col} = ?"
            args.append(val)
    with db() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    return sorted(rows, key=lambda r: (STATUS_RANK.get(r["status"], 9), SEVERITY_RANK.get(r["severity"], 9), r["so_number"] or 0))


# ---------------------------------------------------------------- ERP actions (planned or applied writes)


def add_erp_action(kind: str, so_number, dry_run: bool, detail: dict, message_id: str | None = None) -> None:
    with db() as c:
        c.execute("insert into erp_actions (kind, so_number, dry_run, detail, message_id, created) values (?, ?, ?, ?, ?, ?)",
                  (kind, so_number, int(dry_run), json.dumps(detail, default=str), message_id, now()))


def erp_actions(so: int | None = None, limit: int = 200) -> list[dict]:
    sql, args = "select * from erp_actions", []
    if so is not None:
        sql += " where so_number = ?"
        args.append(so)
    with db() as c:
        rows = [dict(r) for r in c.execute(sql + " order by id desc limit ?", (*args, limit)).fetchall()]
    for r in rows:
        r["detail"] = json.loads(r["detail"])
        r["dry_run"] = bool(r["dry_run"])
    return rows


# ---------------------------------------------------------------- suggestions (the agent's ideas past the checks)

SUGGESTION_STATUSES = ("open", "done", "dismissed")


def add_suggestion(message_id: str | None, run_id: int | None, so_number: int | None, po_number: int | None, kind: str,
                   priority: str, title: str, detail: str, action: str, evidence: list[dict]) -> int:
    with db() as c:
        cur = c.execute("insert into suggestions (message_id, run_id, so_number, po_number, kind, priority, title, detail, action, "
                        "evidence, status, created, updated) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                        (message_id, run_id, so_number, po_number, kind, priority, title, detail, action, json.dumps(evidence), now(), now()))
        return cur.lastrowid


def clear_open_suggestions(message_id: str) -> None:
    """Reprocessing an email replaces its open suggestions; ones a person acted on are kept."""
    with db() as c:
        c.execute("delete from suggestions where message_id = ? and status = 'open'", (message_id,))


def suggestions(message_id: str | None = None, so: int | None = None, po: int | None = None, status: str | None = None) -> list[dict]:
    sql, args = "select * from suggestions where 1=1", []
    for col, val in (("message_id", message_id), ("so_number", so), ("po_number", po), ("status", status)):
        if val is not None:
            sql += f" and {col} = ?"
            args.append(val)
    with db() as c:
        rows = [dict(r) for r in c.execute(sql + " order by id desc", args).fetchall()]
    for r in rows:
        r["evidence"] = json.loads(r["evidence"] or "[]")
    rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(rows, key=lambda r: (r["status"] != "open", rank.get(r["priority"], 3), -r["id"]))


def set_suggestion_status(sid: int, status: str) -> dict | None:
    if status not in SUGGESTION_STATUSES:
        raise ValueError(f"status must be one of {SUGGESTION_STATUSES}")
    with db() as c:
        c.execute("update suggestions set status = ?, updated = ? where id = ?", (status, now(), sid))
        r = c.execute("select * from suggestions where id = ?", (sid,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- suggestions (the agent's ideas past the checks)

SUGGESTION_STATUSES = ("open", "done", "dismissed")


def add_suggestion(message_id: str | None, run_id: int | None, so_number: int | None, po_number: int | None, kind: str,
                   priority: str, title: str, detail: str, action: str, evidence: list[dict]) -> int:
    with db() as c:
        cur = c.execute("insert into suggestions (message_id, run_id, so_number, po_number, kind, priority, title, detail, action, "
                        "evidence, status, created, updated) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                        (message_id, run_id, so_number, po_number, kind, priority, title, detail, action, json.dumps(evidence), now(), now()))
        return cur.lastrowid


def clear_open_suggestions(message_id: str) -> None:
    """Reprocessing an email replaces its open suggestions; ones a person acted on are kept."""
    with db() as c:
        c.execute("delete from suggestions where message_id = ? and status = 'open'", (message_id,))


def suggestions(message_id: str | None = None, so: int | None = None, po: int | None = None, status: str | None = None) -> list[dict]:
    sql, args = "select * from suggestions where 1=1", []
    for col, val in (("message_id", message_id), ("so_number", so), ("po_number", po), ("status", status)):
        if val is not None:
            sql += f" and {col} = ?"
            args.append(val)
    with db() as c:
        rows = [dict(r) for r in c.execute(sql + " order by id desc", args).fetchall()]
    for r in rows:
        r["evidence"] = json.loads(r["evidence"] or "[]")
    rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(rows, key=lambda r: (r["status"] != "open", rank.get(r["priority"], 3), -r["id"]))


def set_suggestion_status(sid: int, status: str) -> dict | None:
    if status not in SUGGESTION_STATUSES:
        raise ValueError(f"status must be one of {SUGGESTION_STATUSES}")
    with db() as c:
        c.execute("update suggestions set status = ?, updated = ? where id = ?", (status, now(), sid))
        r = c.execute("select * from suggestions where id = ?", (sid,)).fetchone()
    if not r:
        return None
    out = dict(r)
    out["evidence"] = json.loads(out["evidence"] or "[]")
    return out


# ---------------------------------------------------------------- chat


def add_chat_message(session_id: str, role: str, content: str, tools: list | None = None) -> None:
    with db() as c:
        c.execute("insert into chat_messages (session_id, role, content, tools, created) values (?, ?, ?, ?, ?)",
                  (session_id, role, content, json.dumps(tools or []), now()))


def chat_messages(session_id: str) -> list[dict]:
    with db() as c:
        rows = [dict(r) for r in c.execute("select * from chat_messages where session_id = ? order by id", (session_id,)).fetchall()]
    for r in rows:
        r["tools"] = json.loads(r["tools"] or "[]")
    return rows


def get_run(run_id: int) -> dict | None:
    with db() as c:
        r = c.execute("select * from runs where id = ?", (run_id,)).fetchone()
        return _run(dict(r)) if r else None


# ---------------------------------------------------------------- meta


def get_meta(key: str) -> str | None:
    with db() as c:
        r = c.execute("select value from meta where key = ?", (key,)).fetchone()
        return r["value"] if r else None


def set_meta(key: str, value: str) -> None:
    with db() as c:
        c.execute("insert into meta (key, value) values (?, ?) on conflict(key) do update set value = excluded.value", (key, value))
