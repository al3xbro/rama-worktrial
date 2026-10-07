"""Command line entry points.

  uv run python -m agent verify --po PATH [--ack PATH] [--so N] [--previous PATH]
  uv run python -m agent batch [--only 01,05] [--workers 3]
  uv run python -m agent agent --message-id ID
  uv run python -m agent agent --po PATH [--ack PATH]
  uv run python -m agent mail [--top 20]
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config


def cmd_verify(a):
    from . import ingest, verify
    prev = ingest.extract(ingest.normalize_file(Path(a.previous))) if a.previous else None
    po = ingest.extract(ingest.normalize_file(Path(a.po))) if a.po else None
    ack = ingest.extract(ingest.normalize_file(Path(a.ack))) if a.ack else None
    r = verify.verify(po, ack, a.so, previous_po=prev)
    print(json.dumps(r.for_agent(), indent=1, default=str))


def customer_folders(only: list[str] | None = None) -> list[Path]:
    folders = sorted(p for p in (config.DATA_DIR / "order_docs").iterdir() if p.is_dir() and "supplier-po" not in p.name)
    if only:
        folders = [f for f in folders if f.name.split("-")[0] in only]
    return folders


def _po_file(folder: Path) -> Path:
    return next(p for p in folder.iterdir() if p.name.startswith("PO "))


def cmd_batch(a):
    from . import verify
    folders = customer_folders(a.only.split(",") if a.only else None)

    def one(folder: Path):
        try:
            r = verify.verify_files(_po_file(folder))
            return folder.name, r.recommendation, r.counts, r.report_path, None
        except Exception as e:
            return folder.name, "error", {}, None, repr(e)

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        rows = list(pool.map(one, folders))
    for name, rec, counts, path, err in rows:
        c = " ".join(f"{k}={v}" for k, v in sorted(counts.items()) if k != "pass")
        print(f"{name:<34} {rec:<15} {c}  {err or ''}")


def cmd_agent(a):
    from . import orchestrator
    log = orchestrator.run(a.message_id) if a.message_id else orchestrator.run_local(a.po, a.ack)
    for s in log["steps"]:
        print(f"-> {s['tool']}({', '.join(f'{k}={v!r}' for k, v in s['args'].items() if v)})")
    print("\n" + (log.get("final") or ""))
    print(f"\nlog: {log['log_path']}")


def cmd_serve(a):
    import os

    import uvicorn
    if a.watch:
        os.environ["WATCH"] = "1"
    uvicorn.run("agent.api:app", host=a.host, port=a.port, reload=False)


def cmd_watch(a):
    from . import state, watcher
    if a.since:
        state.set_meta("watermark", a.since)
    if a.once:
        print(watcher.poll_once(wait=True))
    else:
        print(f"watching {config.MAILBOX} every {a.interval}s (Ctrl-C to stop)")
        watcher.run_forever(a.interval)


def cmd_process(a):
    from . import watcher
    m = watcher.process_message(a.message_id, force=True)
    print(json.dumps({k: m.get(k) for k in ("subject", "sender", "label", "reason_category", "triage_reasons", "state", "recommendation", "error")}, indent=1))


def cmd_triage_eval(a):
    """Triage every Inbox message received since --since and compare with the known customer POs."""
    from . import mail, triage
    msgs = [m for m in mail.list_since(a.since) if True]
    truth = lambda m: (m.get("from") or {}).get("emailAddress", {}).get("address", "").endswith(".example") and m["receivedDateTime"].startswith("2026-08-31")

    def one(m):
        try:
            return m, triage.classify(mail.get_email(m["id"]))
        except Exception as e:
            return m, e

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        results = list(pool.map(one, msgs))
    tp = fp = fn = 0
    for m, t in results:
        if isinstance(t, Exception):
            print("ERROR", m.get("subject"), t)
            continue
        is_po, said_po = truth(m), triage.should_process(t)
        tp += is_po and said_po
        fp += (not is_po) and said_po
        fn += is_po and not said_po
        if is_po != said_po:
            frm = (m.get("from") or {}).get("emailAddress", {}).get("address", "")
            print(f"{'MISSED' if is_po else 'EXTRA '} {t.label:<12} {frm[:32]:<32} | {(m.get('subject') or '')[:50]} | {t.reasons[:90]}")
    print(f"\n{len(results)} messages: {tp} customer POs caught, {fn} missed, {fp} other emails sent on to the agent.")


def cmd_eval(a):
    from . import evaluate
    evaluate.print_report(evaluate.evaluate())


def cmd_mail(a):
    from . import mail
    for m in mail.recent(a.top):
        frm = (m.get("from") or {}).get("emailAddress", {}).get("address", "?")
        print(f"{m['receivedDateTime'][:16]} att={int(m['hasAttachments'])} {frm[:34]:<34} | {(m['subject'] or '')[:60]} | {m['id']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--po")
    v.add_argument("--ack")
    v.add_argument("--so", type=int)
    v.add_argument("--previous", help="an earlier version of the same PO, to diff against")
    b = sub.add_parser("batch")
    b.add_argument("--only")
    b.add_argument("--workers", type=int, default=3)
    g = sub.add_parser("agent")
    g.add_argument("--message-id")
    g.add_argument("--po")
    g.add_argument("--ack")
    m = sub.add_parser("mail")
    m.add_argument("--top", type=int, default=20)
    sub.add_parser("eval")
    s = sub.add_parser("serve")
    s.add_argument("--watch", action="store_true", help="also watch the inbox in the background")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    w = sub.add_parser("watch")
    w.add_argument("--interval", type=int, default=config.POLL_SECONDS)
    w.add_argument("--since", help="ISO time to start from (default: now, on first run)")
    w.add_argument("--once", action="store_true")
    pr = sub.add_parser("process")
    pr.add_argument("--message-id", required=True)
    te = sub.add_parser("triage-eval")
    te.add_argument("--since", default="2026-01-01T00:00:00Z")
    te.add_argument("--workers", type=int, default=8)
    a = p.parse_args(argv)
    {"verify": cmd_verify, "batch": cmd_batch, "agent": cmd_agent, "mail": cmd_mail, "eval": cmd_eval, "serve": cmd_serve,
     "watch": cmd_watch, "process": cmd_process, "triage-eval": cmd_triage_eval}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
