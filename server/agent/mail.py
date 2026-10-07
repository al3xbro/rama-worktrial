"""The order desk mailbox via Microsoft Graph (app-only auth).

Reads are plain GETs. The app also holds Mail.ReadWrite and Mail.Send, so the only write is
`send`, which refuses any recipient but REPLY_TO and does nothing while DRY_RUN is on.
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from urllib.parse import quote

import httpx

from . import config

GRAPH = "https://graph.microsoft.com/v1.0"
AGENT_HEADER = "X-Order-Agent"


@lru_cache(maxsize=1)
def _token_cached(_bucket: int) -> str:
    r = httpx.post(
        f"https://login.microsoftonline.com/{os.environ['GRAPH_TENANT_ID']}/oauth2/v2.0/token",
        data={"client_id": os.environ["GRAPH_CLIENT_ID"], "client_secret": os.environ["GRAPH_CLIENT_SECRET"],
              "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _token() -> str:
    return _token_cached(int(time.time() // 3000))  # tokens last an hour; refresh every 50 minutes


def _request(url: str, params: dict | None = None, attempts: int = 6) -> dict:
    """GET with retries: Graph throttles with 429 (and sometimes 503/504). Honour Retry-After, else back off."""
    for i in range(attempts):
        r = httpx.get(url, params=params, headers={"Authorization": f"Bearer {_token()}",
                      "Prefer": 'outlook.body-content-type="text"'}, timeout=60)
        if r.status_code in (429, 503, 504) and i < attempts - 1:
            wait = float(r.headers.get("Retry-After") or 2 ** i)
            time.sleep(min(wait, 60))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


def _get(path: str, params: dict | None = None) -> dict:
    return _request(f"{GRAPH}{path}", params)


def _mbox() -> str:
    return f"/users/{quote(config.MAILBOX)}"


@dataclass
class Attachment:
    name: str
    content_type: str | None
    size: int
    inline: bool
    data: bytes | None

    @property
    def is_document(self) -> bool:
        """Attachments, plus inline images large enough to be a pasted PO (signatures and logos are small)."""
        if not self.inline:
            return True
        is_image = (self.content_type or "").startswith("image/") or self.name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        return is_image and self.size >= config.INLINE_DOC_MIN_BYTES


@dataclass
class Email:
    id: str
    subject: str
    sender: str
    received: str
    body: str
    attachments: list[Attachment] = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    to: list[str] = field(default_factory=list)

    @property
    def from_mailbox(self) -> bool:
        return self.sender.lower() == config.MAILBOX.lower()

    @property
    def from_agent(self) -> bool:
        return AGENT_HEADER.lower() in {k.lower() for k in self.headers}


def get_email(message_id: str, with_attachments: bool = True) -> Email:
    m = _get(f"{_mbox()}/messages/{message_id}", {"$select": "subject,from,toRecipients,receivedDateTime,body,hasAttachments,internetMessageHeaders"})
    headers = {h["name"]: h["value"] for h in m.get("internetMessageHeaders") or []}
    mail = Email(id=message_id, subject=m.get("subject") or "", sender=(m.get("from") or {}).get("emailAddress", {}).get("address", ""),
                 received=m.get("receivedDateTime") or "", body=(m.get("body") or {}).get("content", ""), headers=headers,
                 to=[r["emailAddress"]["address"] for r in m.get("toRecipients") or []])
    if with_attachments and m.get("hasAttachments"):
        for a in _get(f"{_mbox()}/messages/{message_id}/attachments").get("value", []):
            data = base64.b64decode(a["contentBytes"]) if a.get("contentBytes") else None
            mail.attachments.append(Attachment(a.get("name") or "attachment", a.get("contentType"), a.get("size") or 0, bool(a.get("isInline")), data))
    return mail


def list_since(since: str, top: int = 50, folder: str = "inbox") -> list[dict]:
    """Messages in a folder ('inbox' or 'sentitems') received at or after `since` (ISO 8601, UTC), oldest first, all pages."""
    params = {"$top": top, "$orderby": "receivedDateTime asc", "$filter": f"receivedDateTime ge {since}",
              "$select": "id,internetMessageId,subject,from,toRecipients,receivedDateTime,hasAttachments"}
    data = _get(f"{_mbox()}/mailFolders/{folder}/messages", params)
    out = list(data.get("value", []))
    while data.get("@odata.nextLink"):
        data = _request(data["@odata.nextLink"])
        out += data.get("value", [])
    return out


def search(query: str, top: int = 10) -> list[dict]:
    """Search the Inbox and Sent Items (for example for a PO number). Read-only."""
    out = []
    q = '"' + query.replace('"', "") + '"'
    for folder in ("inbox", "sentitems"):
        for m in _get(f"{_mbox()}/mailFolders/{folder}/messages",
                      {"$search": q, "$top": top, "$select": "id,subject,from,toRecipients,receivedDateTime,hasAttachments"}).get("value", []):
            out.append({"message_id": m["id"], "folder": folder, "date": m.get("receivedDateTime"), "subject": m.get("subject"),
                        "from": (m.get("from") or {}).get("emailAddress", {}).get("address"),
                        "to": [r["emailAddress"]["address"] for r in m.get("toRecipients") or []], "has_attachments": m.get("hasAttachments")})
    return out


def recent(top: int = 25, since: str | None = None) -> list[dict]:
    params = {"$top": top, "$orderby": "receivedDateTime desc", "$select": "id,subject,from,receivedDateTime,hasAttachments"}
    if since:
        params["$filter"] = f"receivedDateTime ge {since}"
    return _get(f"{_mbox()}/mailFolders/inbox/messages", params).get("value", [])


def send(subject: str, body: str, in_reply_to: str | None = None) -> dict:
    """Send to REPLY_TO only, tagged with the loop-guard header. Writes to the outbox when dry-run."""
    msg = {"subject": subject, "body": {"contentType": "Text", "content": body},
           "toRecipients": [{"emailAddress": {"address": config.REPLY_TO}}],
           "internetMessageHeaders": [{"name": AGENT_HEADER, "value": in_reply_to or "1"}]}
    config.OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = config.OUTBOX_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"to": config.REPLY_TO, "subject": subject, "body": body, "in_reply_to": in_reply_to, "dry_run": config.DRY_RUN}, indent=1))
    if config.DRY_RUN:
        return {"sent": False, "dry_run": True, "outbox": str(path), "to": config.REPLY_TO}
    r = httpx.post(f"{GRAPH}{_mbox()}/sendMail", json={"message": msg, "saveToSentItems": True},
                   headers={"Authorization": f"Bearer {_token()}"}, timeout=60)
    r.raise_for_status()
    return {"sent": True, "outbox": str(path), "to": config.REPLY_TO}
