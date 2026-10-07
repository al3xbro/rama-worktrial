"""HTTP API over the order desk agent: email processing status, orders, and the issues that
need a person. Read-mostly; the only actions are polling, (re)processing one email, and
verifying uploaded documents.

  uv run python -m agent serve [--watch]      # then open http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import config, state, watcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("WATCH") == "1":
        watcher.start_background()
    yield
    watcher.stop_background()


app = FastAPI(title="Order desk agent", version="0.1.0", lifespan=lifespan,
              description="Status of email processing at Demo Distributor's order desk, the verification result for each order, the issues to act on, and a chatbot over the ERP.")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/status")
def status():
    """Watcher state, message counts by processing state, and safety settings."""
    return watcher.status()


@app.post("/poll")
def poll(background: BackgroundTasks):
    """Check the inbox once now (in the background)."""
    background.add_task(watcher.poll_once)
    return {"queued": True}


@app.post("/watcher/start")
def start_watcher(interval: int | None = None):
    watcher.start_background(interval)
    return watcher.status()


@app.post("/watcher/stop")
def stop_watcher():
    watcher.stop_background()
    return {"stopping": True}


# ---------------------------------------------------------------- emails


@app.get("/emails")
def emails(state_: str | None = Query(None, alias="state"), label: str | None = None, limit: int = 100):
    """Emails the watcher has seen, newest first. state: new, triaged, processing, done, ignored, error."""
    return state.list_messages(state_, label, limit)


@app.get("/emails/{message_id}")
def email(message_id: str):
    m = state.get_message(message_id)
    if not m:
        raise HTTPException(404, "email not seen")
    out = {**m, "runs": state.runs_for_message(message_id)}
    if m.get("agent_log") and Path(m["agent_log"]).exists():
        log = json.loads(Path(m["agent_log"]).read_text())
        out["agent"] = {"final": log.get("final"), "steps": [{"tool": s["tool"], "args": s["args"]} for s in log.get("steps", [])]}
    if m.get("reply") and Path(m["reply"]).exists():
        out["reply_draft"] = json.loads(Path(m["reply"]).read_text())
    out["suggestions"] = state.suggestions(message_id=message_id)
    return out


@app.post("/emails/{message_id}/process")
def process(message_id: str):
    """Run an email again: re-read from the mailbox by its id (the email is not re-sent), triaged and given to
    the agent, queued like new mail. Replies stay dry-run drafts while DRY_RUN=1."""
    try:
        return watcher.rerun(message_id)
    except KeyError:
        raise HTTPException(404, "email not seen")


# ---------------------------------------------------------------- orders and issues


@app.get("/orders")
def orders():
    """The latest verification of each order: recommendation and counts by status."""
    return state.latest_runs()


def _latest_result(so: int) -> dict:
    runs = state.runs_for_order(so)
    if not runs or not runs[0].get("report_path"):
        raise HTTPException(404, "order not verified")
    path = Path(runs[0]["report_path"]).with_name("result.json")
    return {"run": runs[0], "history": runs[1:], "result": json.loads(path.read_text()) if path.exists() else None}


@app.get("/orders/{so}")
def order(so: int):
    """Full result of the latest verification: every finding with its evidence, the checklist rollup, any revision diff."""
    return _latest_result(so)


@app.get("/orders/{so}/report", response_class=PlainTextResponse)
def order_report(so: int):
    runs = state.runs_for_order(so)
    if not runs or not runs[0].get("report_path") or not Path(runs[0]["report_path"]).exists():
        raise HTTPException(404, "no report")
    return Path(runs[0]["report_path"]).read_text()


@app.get("/issues")
def issues(status: str | None = None, severity: str | None = None, so: int | None = None):
    """Open issues across orders (latest run of each), most severe first: fail, needs approval, needs review, unverifiable."""
    return state.open_issues(status, severity, so)


@app.get("/suggestions")
def suggestions(status: str | None = "open", so: int | None = None, po: int | None = None, message_id: str | None = None):
    """The agent's suggestions past the checks (sourcing, allocation, pricing, ...), open ones first by priority.
    status: open (default), done, dismissed, or empty for all."""
    return state.suggestions(message_id=message_id, so=so, po=po, status=status or None)


class SuggestionUpdate(BaseModel):
    status: str


@app.patch("/suggestions/{sid}")
def update_suggestion(sid: int, body: SuggestionUpdate):
    """Mark a suggestion done or dismissed (or open again)."""
    try:
        s = state.set_suggestion_status(sid, body.status)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not s:
        raise HTTPException(404, "suggestion not found")
    return s


def _suggestions_for_run(run: dict) -> list[dict]:
    """Suggestions made while handling this run's email that concern this order (or no order in particular)."""
    if not run.get("message_id"):
        return []
    out = []
    for s in state.suggestions(message_id=run["message_id"]):
        mine = s["run_id"] == run["id"] or (s["so_number"] and s["so_number"] == run.get("so_number")) \
            or (s["po_number"] and s["po_number"] == run.get("po_number"))
        if mine or not (s["so_number"] or s["po_number"]):
            out.append(s)
    return out


def _read_json(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


@app.get("/runs/{run_id}/detail")
def run_detail(run_id: int):
    """Everything about one verification, in the order a reviewer reads it: the email, the customer's PO,
    the sales order (ERP and ack), the customer's terms as the agent read them, then the findings."""
    run = state.get_run(run_id)
    if not run or not run.get("report_path"):
        raise HTTPException(404, "run not found")
    folder = Path(run["report_path"]).parent
    result = _read_json(folder / "result.json") or {}
    email_info = None
    if run.get("message_id"):
        m = state.get_message(run["message_id"])
        if m:
            email_info = {k: m.get(k) for k in ("id", "received", "sender", "subject", "label", "reason_category", "triage_confidence",
                                                 "triage_reasons", "state", "recommendation", "folder")}
            if m.get("agent_log") and Path(m["agent_log"]).exists():
                log = json.loads(Path(m["agent_log"]).read_text())
                email_info["agent"] = {"final": log.get("final"), "steps": [{"tool": s["tool"], "args": s["args"]} for s in log.get("steps", [])]}
            if m.get("reply") and Path(m["reply"]).exists():
                email_info["reply_draft"] = json.loads(Path(m["reply"]).read_text())
    return {
        "run": run,
        "email": email_info,
        "po": _read_json(folder / "po.json"),
        "ack": _read_json(folder / "ack.json"),
        "erp": result.get("erp"),
        "requirements": result.get("requirements", []),
        "findings": result.get("findings", []),
        "checklist": result.get("checklist", []),
        "recommendation": result.get("recommendation"),
        "reasons": result.get("reasons", []),
        "revision": result.get("revision"),
        "problems": result.get("problems", []),
        "suggestions": _suggestions_for_run(run),
        # This run's check record, and holds the agent placed on its SO while handling the same email.
        "erp_actions": [a for a in state.erp_actions(run.get("so_number"), limit=200)
                        if a["detail"].get("run_id") == run_id
                        or (a["kind"] == "hold_order" and run.get("so_number") and a["so_number"] == run["so_number"]
                            and a.get("message_id") == run.get("message_id"))],
    }


# ---------------------------------------------------------------- chatbot


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None


@app.post("/chat")
def chat(body: ChatIn):
    """Ask the order desk chatbot a question; answers come from the ERP. Pass session_id to continue a conversation."""
    from . import chat as chatbot
    return chatbot.ask(body.message, body.session_id)


@app.get("/chat/{session_id}")
def chat_history(session_id: str):
    return state.chat_messages(session_id)


@app.get("/erp-actions")
def erp_actions(so: int | None = None, limit: int = 200):
    """Writes to the ERP the agent has made, or would make (dry_run=true): check results and holds, with the exact SQL."""
    return state.erp_actions(so, limit)


@app.post("/verify")
async def verify_upload(po: UploadFile = File(...), ack: UploadFile | None = File(None)):
    """Check uploaded documents by hand: a customer PO (optionally with our SO acknowledgement), or our PO
    to a supplier and/or the supplier's acknowledgement. Routed by what the documents turn out to be."""
    from . import ingest, supplier, verify

    docs = []
    with tempfile.TemporaryDirectory() as d:
        for f in (po, ack):
            if f is None:
                continue
            path = Path(d) / Path(f.filename or "document.pdf").name
            path.write_bytes(await f.read())
            try:
                docs.append(ingest.extract(ingest.normalize_file(path)))
            except ValueError as e:
                raise HTTPException(400, str(e))
    of = lambda t: next((x for x in docs if x.doc.header.doc_type == t), None)
    if of("customer_po"):
        r = verify.verify(of("customer_po"), of("so_ack"), source="upload")
    elif of("supplier_po") or of("supplier_ack"):
        r = supplier.verify_supplier_order(of("supplier_po"), of("supplier_ack"), source="upload")
    else:
        raise HTTPException(400, "No customer PO, supplier PO or supplier acknowledgement found in the upload: "
                                 + ", ".join(f"{x.source} looks like {x.doc.header.doc_type}" for x in docs))
    from . import erp_write
    erp_write.record_result(r.model_dump(mode="json"), r.run_id)  # a person checked these by hand: the result is settled
    return r.for_agent()


@app.get("/uploads")
def uploads():
    """Orders and supplier POs checked by hand (uploaded documents): the latest upload of each."""
    return state.latest_runs(source="upload")


@app.get("/config")
def config_view():
    return {"extract_model": config.EXTRACT_MODEL, "agent_model": config.AGENT_MODEL, "triage_model": config.TRIAGE_MODEL,
            "mailbox": config.MAILBOX, "reply_to": config.REPLY_TO, "dry_run": config.DRY_RUN, "poll_seconds": config.POLL_SECONDS}
