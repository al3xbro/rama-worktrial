"""Inbox watching: poll the order desk Inbox, triage every new email, and hand customer POs and
revisions to the order-desk agent. Processing state goes to store/state.db for the API.

The watermark starts at the moment the watcher first runs, so the mailbox's history is never
processed by accident (pass `since` to replay a window). Each email gets its own agent call on
a pool of WATCH_WORKERS threads. Emails from the same sender are processed in arrival order,
so a revised PO never overtakes its original; different senders run in parallel. The ERP
connection cap is enforced in db.connect, and Graph and OpenAI calls retry when throttled.
"""
from __future__ import annotations

import threading
import traceback
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from datetime import datetime, timedelta, timezone

from . import config, mail, orchestrator, state, triage

_stop = threading.Event()
_thread: threading.Thread | None = None
_executor: ThreadPoolExecutor | None = None
_queues: dict[str, deque] = {}  # sender -> message ids waiting, in arrival order
_active: set[str] = set()  # senders with a worker draining their queue
_running: set[str] = set()  # message ids being processed right now
_qlock = threading.Lock()


def _pool() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=config.WATCH_WORKERS, thread_name_prefix="email")
    return _executor


def submit(mid: str, sender: str, folder: str = "inbox", force: bool = False) -> Future | None:
    """Queue one email. `sender` is the counterparty (the customer or supplier) whose emails must keep
    their order. Returns the future of a newly started per-counterparty worker, if one was started."""
    key = (sender or "").lower()
    with _qlock:
        _queues.setdefault(key, deque()).append((mid, folder, force))
        if key in _active:
            return None  # that sender's worker will pick it up, after the emails before it
        _active.add(key)
    return _pool().submit(_drain, key)


def _drain(key: str) -> None:
    while True:
        with _qlock:
            q = _queues.get(key)
            if not q:
                _active.discard(key)
                _queues.pop(key, None)
                return
            mid, folder, force = q.popleft()
            _running.add(mid)
        try:
            process_message(mid, force=force, folder=folder)
        finally:
            with _qlock:
                _running.discard(mid)


def rerun(mid: str) -> dict:
    """Process an email again: it is re-read from the mailbox by its id (nothing is re-sent), triaged
    and handed to the agent, queued like new mail so the agent cap and per-sender order hold. Its
    earlier runs stay in the history; open suggestions are replaced."""
    m = state.get_message(mid)
    if not m:
        raise KeyError(mid)
    with _qlock:
        busy = mid in _running or any(x[0] == mid for q in _queues.values() for x in q)
    if busy:
        return {"skipped": "already queued or being processed"}
    folder = m.get("folder") or "inbox"
    state.upsert_message(mid, state="queued", error=None)
    # Inbox mail keeps its sender's order (a revision never overtakes its original); our sent POs share one queue.
    submit(mid, m.get("sender") if folder == "inbox" else "sentitems", folder, force=True)
    return {"queued": mid}


def queued() -> int:
    with _qlock:
        return sum(len(q) for q in _queues.values()) + len(_active)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def process_message(mid: str, force: bool = False, folder: str | None = None) -> dict:
    """Triage one email and, if it holds a customer PO, a revision, our PO to a supplier or a supplier's
    acknowledgement, run the agent on it."""
    existing = state.get_message(mid)
    if existing and existing["state"] in ("done", "ignored") and not force:
        return existing
    folder = folder or (existing or {}).get("folder") or "inbox"
    try:
        email = mail.get_email(mid)
        state.upsert_message(mid, received=email.received, sender=email.sender, subject=email.subject,
                             has_attachments=int(bool(email.attachments)), state="new", error=None, folder=folder)
        t = triage.classify(email, folder)
        state.upsert_message(mid, label=t.label, reason_category=t.reason_category, triage_confidence=t.confidence,
                             triage_reasons=t.reasons, state="triaged")
        if not triage.should_process(t):
            state.upsert_message(mid, state="ignored")
            return state.get_message(mid)
        state.upsert_message(mid, state="processing")
        log = orchestrator.run(mid, folder=folder)
        runs = state.runs_for_message(mid)
        recs = [r["recommendation"] for r in runs]
        worst = next((x for x in ("hold", "needs_approval", "review", "release") if x in recs), None)
        reply = next((s["result"] for s in reversed(log.get("steps", [])) if s["tool"] == "draft_reply"), None)
        state.upsert_message(mid, state="done", recommendation=worst, agent_log=log.get("log_path"),
                             reply=reply.get("outbox") if isinstance(reply, dict) else None)
    except Exception as e:
        state.upsert_message(mid, state="error", error=f"{e!r}\n{traceback.format_exc()[-1500:]}")
    return state.get_message(mid)


FOLDERS = {"inbox": "watermark", "sentitems": "watermark_sentitems"}


def poll_once(wait: bool = False) -> list[str]:
    """Queue every message since each folder's watermark that we have not seen: the Inbox (customer POs,
    supplier acknowledgements) and Sent Items (our POs to suppliers). With wait=True, return only when
    they have all been processed."""
    state.set_meta("last_poll", state.now())
    queued_ids, futures = [], []
    for folder, key in FOLDERS.items():
        since = state.get_meta(key)
        if since is None:
            since = _iso(datetime.now(timezone.utc))
            state.set_meta(key, since)
        newest = since
        for m in mail.list_since(since, folder=folder):
            newest = max(newest, m["receivedDateTime"])
            if state.seen(m["id"]):
                continue
            sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "")
            to = [r["emailAddress"]["address"] for r in m.get("toRecipients") or []]
            counterparty = (to[0] if to else sender) if folder == "sentitems" else sender
            state.upsert_message(m["id"], internet_message_id=m.get("internetMessageId"), received=m["receivedDateTime"], sender=sender,
                                 subject=m.get("subject") or "", has_attachments=int(bool(m.get("hasAttachments"))), state="queued", folder=folder)
            f = submit(m["id"], counterparty, folder)
            if f:
                futures.append(f)
            queued_ids.append(m["id"])
        # Keep a small overlap so a message arriving in the same second is not skipped; `seen` dedupes.
        overlap = datetime.fromisoformat(newest.replace("Z", "+00:00")) - timedelta(seconds=5)
        state.set_meta(key, max(since, _iso(overlap)))
    if wait:
        wait_futures(futures)
    return queued_ids


def run_forever(interval: int | None = None) -> None:
    interval = interval or config.POLL_SECONDS
    state.set_meta("watcher_running", "1")
    state.set_meta("poll_seconds", str(interval))
    try:
        while not _stop.is_set():
            try:
                poll_once()
                state.set_meta("last_error", "")
            except Exception as e:  # a failed poll (network, token) must not kill the watcher
                state.set_meta("last_error", f"{state.now()} {e!r}")
            _stop.wait(interval)
    finally:
        state.set_meta("watcher_running", "0")


def start_background(interval: int | None = None) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=run_forever, args=(interval,), name="inbox-watcher", daemon=True)
    _thread.start()


def stop_background() -> None:
    _stop.set()


def status() -> dict:
    return {
        "running": bool(_thread and _thread.is_alive()) or state.get_meta("watcher_running") == "1",
        "poll_seconds": int(state.get_meta("poll_seconds") or config.POLL_SECONDS),
        "watermark": state.get_meta("watermark"),
        "watermark_sentitems": state.get_meta("watermark_sentitems"),
        "last_poll": state.get_meta("last_poll"),
        "last_error": state.get_meta("last_error") or None,
        "messages": state.message_counts(),
        "workers": config.WATCH_WORKERS,
        "in_progress_or_queued": queued(),
        "dry_run": config.DRY_RUN,
        "erp_dry_run": config.ERP_DRY_RUN,
        "reply_to": config.REPLY_TO,
    }
