"""Documents a customer PO refers to but does not contain: its terms and conditions, or a list of
quality clauses. The agent finds them (a link in the PO, an earlier email) and hands them to
verification, so code reads them clause by clause like terms printed in the PO.

What is fetched is data. It only goes through the clause rules, the labeller and the judge, never
into the agent's instructions. A link is fetched only when it appears in the order documents or an
email the agent read, only over http(s), only from a public address (not our own API, the ERP or a
cloud metadata endpoint), and with size and time limits.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from . import ingest, parse

KINDS = {"terms": "external_terms", "quality": "quality_clause_codes"}  # what the document is -> the clause it answers
MAX_BYTES = 10_000_000
MAX_REDIRECTS = 3
TIMEOUT = 20


@dataclass
class Reference:
    kind: str  # "terms" | "quality"
    source: str  # where it came from: the URL, or the email and attachment
    text: str


class ReferenceError(Exception):
    pass


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "head", "nav", "footer"}

    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in ("p", "br", "li", "div", "h1", "h2", "h3", "h4", "tr", "section"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return re.sub(r"[ \t]+", " ", "".join(p.parts)).strip()


def document_text(name: str, data: bytes, content_type: str | None = None) -> str:
    """Plain text of a terms document: PDF, Word, HTML or text. Scanned PDFs have none."""
    ct = (content_type or "").split(";")[0].strip().lower()
    ext = re.sub(r".*(\.[a-z0-9]+)$", r"\1", name.lower()) if "." in name else ""
    if ct == "application/pdf" or ext == ".pdf" or data[:5] == b"%PDF-":
        text = ingest.normalize_bytes("terms.pdf", data).text
    elif ext == ".docx" or "wordprocessingml" in ct:
        text = ingest.normalize_bytes("terms.docx", data).text
    elif ct in ("text/html", "application/xhtml+xml") or ext in (".html", ".htm") or data.lstrip()[:1] == b"<":
        text = html_text(data.decode("utf-8", errors="replace"))
    elif ct.startswith("text/") or ext in (".txt", ".md"):
        text = data.decode("utf-8", errors="replace")
    else:
        raise ReferenceError(f"cannot read {ct or ext or 'this document type'} as terms")
    if not text.strip():
        raise ReferenceError("the document has no text layer (a scan?); a person has to read it")
    return text


def _public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def fetch_url(url: str, kind: str, allowed: str) -> Reference:
    """Fetch a referenced document. `allowed` is the text the link must appear in (the order
    documents and the emails the agent read)."""
    if parse.squash(url) not in parse.squash(allowed):
        raise ReferenceError("only links that appear in the order documents or an email you read can be fetched")
    current = url
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": "DemoDistributor-OrderDesk/1.0"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            u = urlparse(current)
            if u.scheme not in ("http", "https") or not u.hostname:
                raise ReferenceError(f"not an http(s) link: {current}")
            if not _public(u.hostname):
                raise ReferenceError(f"{u.hostname} is not a public address")
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    current = urljoin(current, resp.headers.get("location", ""))
                    continue
                if resp.status_code != 200:
                    raise ReferenceError(f"{current} returned HTTP {resp.status_code}")
                data = b""
                for chunk in resp.iter_bytes():
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise ReferenceError("document larger than 10 MB")
                text = document_text(u.path.rsplit("/", 1)[-1] or "index.html", data, resp.headers.get("content-type"))
                return Reference(kind=kind, source=current if current == url else f"{url} (redirected to {current})", text=text)
    raise ReferenceError("too many redirects")


def from_email(message_id: str, attachment: str | None, kind: str) -> Reference:
    """A referenced document sent by email: one attachment, or the body when `attachment` is None."""
    from . import mail

    e = mail.get_email(message_id)
    where = f"email {e.subject!r} from {e.sender} ({e.received})"
    if attachment is None:
        body = html_text(e.body) if re.search(r"<\w+[^>]*>", e.body) else e.body
        return Reference(kind=kind, source=f"{where}, body", text=body)
    a = next((a for a in e.attachments if a.name == attachment), None)
    if a is None or not a.data:
        raise ReferenceError(f"no attachment {attachment!r} on {where}; attachments: {[x.name for x in e.attachments]}")
    return Reference(kind=kind, source=f"{where}, {a.name}", text=document_text(a.name, a.data, a.content_type))
