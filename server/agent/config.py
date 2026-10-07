"""Settings, loaded from the repo .env. Nothing here should hold a secret value."""
import os
from pathlib import Path

from dotenv import load_dotenv

SERVER_DIR = Path(__file__).resolve().parent.parent  # server/
REPO_ROOT = SERVER_DIR.parent
load_dotenv(REPO_ROOT / ".env", override=True)

DATA_DIR = REPO_ROOT / "data"
STORE_DIR = SERVER_DIR / "store"
RUNS_DIR = STORE_DIR / "runs"
OUTBOX_DIR = STORE_DIR / "outbox"

# Where our SO acknowledgements and our PO PDFs to suppliers are looked up when an email doesn't carry
# them. A relative path is relative to the repository root, wherever the server is started from.
ACK_DIR = REPO_ROOT / os.getenv("ACK_DIR", "data/order_docs")

EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "gpt-5.5")
AGENT_MODEL = os.getenv("AGENT_MODEL", "gpt-5.5")
TRIAGE_MODEL = os.getenv("TRIAGE_MODEL", "gpt-5.4-mini")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", AGENT_MODEL)  # judges clauses no check recognises

MAILBOX = os.getenv("GRAPH_MAILBOX", "test@tryrama.com")
# Replies only ever go here, never to the original sender (customer addresses bounce).
REPLY_TO = os.getenv("REPLY_TO", "test@tryrama.com")
# Nothing is sent or written to the ERP unless DRY_RUN=0.
DRY_RUN = os.getenv("DRY_RUN", "1") != "0"

# ERP writes (put an order on hold, record check results). Planned and logged only, unless ERP_DRY_RUN=0.
ERP_DRY_RUN = os.getenv("ERP_DRY_RUN", "1") != "0"
# The ERP allows 5 connections; stay under it across parallel email processing.
ERP_MAX_CONNECTIONS = int(os.getenv("ERP_MAX_CONNECTIONS", "4"))

# Inbox watching.
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
# At most this many agents run at once (one per email). Emails from the same sender keep arrival order.
WATCH_WORKERS = int(os.getenv("WATCH_WORKERS", "5"))
# Inline images at least this large are treated as possible documents (a pasted PO), smaller ones as signatures.
INLINE_DOC_MIN_BYTES = int(os.getenv("INLINE_DOC_MIN_BYTES", "40000"))
STATE_DB = Path(os.getenv("STATE_DB", STORE_DIR / "state.db"))
# Exact addresses whose mail is never processed (other candidates testing in the shared mailbox).
# Applied after triage and logged. Kept in .env, not in the repo.
SUPPRESS_SENDERS = {s.strip().lower() for s in os.getenv("SUPPRESS_SENDERS", "").split(",") if s.strip()}
