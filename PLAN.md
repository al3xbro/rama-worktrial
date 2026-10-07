# Order validation agent: plan

Status: draft v1. This is the working plan for the Rama work trial. It records what we found in the data, what we decided, and what is still open. Update it as decisions change, and keep reasoning in `DECISIONS.md` as we go (the task asks for our thought process).

No credentials belong in this file or anywhere else in the repo. They live in `.env` (see section 11).

## 1. Goal and scope

Build an agent on top of the ERP with four parts: ERP read/write, Outlook integration, sales order (SO) and purchase order (PO) validation, and an ERP chatbot.

**Priority order**

1. SO/PO validation on the 14 customer orders and 3 supplier POs. This is the main workflow and the first milestone.
2. Outlook: an order arrives by email, triage runs on every email and decides whether it is a customer PO or a revision (the pipeline never runs on anything else), the agent validates it, then replies on the same thread. Triage and read-only intake come early because they feed ingestion. Sending replies comes after validation is trusted.
3. Chatbot over the ERP, reusing the validator's ERP tools.
4. UI for progress (optional). Supplier ordering when stock is short is a stretch goal and out of scope for now.

**Scope decisions**

- For the trial orders the SO already exists in the ERP, so the agent validates and does not create SOs.
- The demo will send an unseen order. Its SO will already exist in the ERP (matched on customer and PO number), and an ack PDF comes with it, so there is no PO-only mode. Every check still declares the inputs it needs and returns `unverifiable` when one is missing.
- The LLM provider is OpenAI for now. The token comes later via `.env`. All LLM calls still go through one wrapper that reads provider and model from `.env`, and the model must support vision.
- "Flag someone" means replying on the originating email thread. Outbound mail only reaches `test@tryrama.com`.
- The verification pipeline is a tool, `verify_customer_order`, called by an overarching order-desk agent (decided 2026-10-06 with a team member). The order-desk agent is where incoming email goes. It calls the tool, reads the findings, and decides what to do next: reply, look at the ERP for other issues, or later add lines to an outgoing supplier PO. **Build now:** the verification tool for customer POs and SOs, and a minimal agent loop around it (tools: read the email, verify the order, draft a reply). **Not now:** supplier-PO actions, ERP writes, sending mail outside dry-run.

## 2. What the data told us

Verified by reading all 31 documents and cross-checking the ERP.

**The ERP** is Postgres, schema `worktrial`, 33 tables. It has no document storage (no blobs or attachments), so PDFs, emails and approvals exist only outside it. `shp_hedr` and `shp_detl` are empty.

**The SO is in the ERP, but only part of what the ack shows.** `ord_hedr` has just 17 columns: order number and date, customer, status, salesperson codes, PO number, terms code, quote number, and audit fields. `ord_detl` has the lines (item, manufacturer, quantity, price, cost, vendor, dates, customer part number). The ack also shows ship via, ship option (Ship Partials or Ship Complete), shipping terms (Ex Works, FOB), freight payment and customer freight account, bill-to and ship-to with contacts, and est ship. None of these is stored on the SO in this database. The only related columns are on `customer` and `quote`, and the ack does not simply copy them (Meridian's master says no partials but its ack says Ship Partials). So for those fields the ack PDF is the only SO-side source. Split the SO side as follows:
- *From the ERP:* customer, PO number, terms code, status, quote link, lines, salesperson, entered-by.
- *Only from the ack PDF:* ship option, ship via, shipping terms, freight terms and account, addresses and contacts, est ship, boilerplate.

This is where most of the header-level findings come from (Meridian's partials, Quarry's FCA vs Ex Works, Copperfield's freight account, Sandstone's ship-to). The ERP cannot hold the PDF itself (no document storage), so the ack is looked up in this order: an ack attached to the order email (triage role `so_ack`), then a drop folder keyed by SO number, and otherwise those checks return `unverifiable`.

**Documents.** 14 customer POs, 14 SO acknowledgements (acks), 3 supplier POs.

| Format | Count | Notes |
|---|---|---|
| Text PDF | 23 | Includes one German PO (`19.000` is 19,000, `0,45980` is 0.4598, dates are DD.MM.YYYY). |
| Scanned PDF | 4 | Folders 05 (PO), 09 (PO), 06 (ack), 12 (ack). Text extraction returns nothing. |
| jpg | 1 | A skewed photo of a printed PO (04). |
| xlsx | 1 | No shared-strings table. |
| docx | 1 | Folder 10. |
| eml | 1 | Quoted-printable body. |

T&C links live in PDF link annotations and are not in the visible text. Two POs (02, 13) have a blank "Authorized by" line.

**ERP traps we hit, which the typed ERP tools must hide**

- Text columns are space padded, so always `trim()`. `trim()` fails on integer columns (`order_no`, `po_no`, `quote.quote_no`, `ord_detl.gen_quote_no`).
- `quo_detl.del_date` is free text (`6-8 WKS`, `12-16WK`, `STOCK`). `customer.res_ship_addr` is boolean.
- Part numbers can have leading zeros (`064316`) or commas (`1N4749A,133`). Always treat them as strings.
- The ERP uses manufacturer codes (`COIL`, `SAMSNG`, `MXIC`); documents use names. There is no manufacturer table.
- `customer` has 214 columns, `item` 170, `vendor` 145. We have only looked at the ones the task names. A column survey is a to-do (section 9).
- Item-default country of origin (COO) differs from the actual lot COO on 1,627 of 1,955 lots, so it is weak evidence.
- `itm_inv.qty_on_hnd` and `lot_hedr.bal_of_lot` can disagree (MMSZ10T1: 247,152 on hand vs a lot balance of 0).
- `po_detl.so_order_no` links PO lines to SO lines for only 63 of 4,008 lines. Some SO lines are covered by an unlinked PO that matches on item, quantity and cost (Meridian lines 1 and 2 via PO 81764).
- Status codes, confirmed 2026-10-06:
  - Sales orders: C closed (shipped and invoiced), O open, I in process (open), H hold, V void.
  - Supplier POs: C closed (fully received), O open. A C line on an O PO means that line is received.
  - Quotes: C closed, O open, I in progress, V void.
  - Quote lines: W won, L lost, O open, I in progress, V void.
- Customer PO numbers are not unique. 277 PO numbers are reused in `ord_hedr`, always across different customers and never within one. Three trial POs collide: PO-NOR26-0031 (Northstar) also matches Norwood's closed SO 502967, PO-NOR26-0037 (Norwood) also matches Northstar's closed SO 502698, and PO-PIN26-0001 (Pinecrest) also matches Pinnacle Controls' SO 502669. Always key on `(cust_no, cust_po)`, never the PO number alone.
- Look-alike customers are the norm: 129 of 160 customers share a first word with another customer, and the siblings often have different terms.
- All 14 acks carry the same boilerplate (AS6081 certified vendor, 1 year warranty, NCNR) and set Est Ship equal to the requested date.

**The mailbox** (`test@tryrama.com`, read-only exploration on 2026-10-06; the app has `Mail.ReadWrite` and `Mail.Send`, so use GET only until we deliberately send).
- Inbox 405, Sent Items 191, Drafts 6, Deleted Items 54. `RFQ Responses` is empty.
- How the folder and the mailbox map (checked by hashing every attachment in all four folders against the 31 files in `data/order_docs`): the 14 customer POs match 1:1 with the 14 inbound customer emails, and the 3 supplier POs match 1:1 with 3 Sent Items emails. The 14 SO acknowledgements are not in the mailbox at all. The rep exports them from the ERP and they are not stored in the mailbox, so the folder copy is a fixture. So `data/order_docs` is a convenience copy of the mailbox POs plus the acks. In the real flow the PO comes from the email and the SO side comes from the ERP plus the ack PDF (the ERP lacks several ack fields, see above). Other candidates' resends mean one PO file can appear in many messages (Meridian's appears in 10), so dedupe by hash.
- There are exactly 14 original customer emails, all dated 2026-08-31, one per trial order. Every attachment is byte-identical (SHA-256) to the matching file in `data/order_docs`. Sandstone's order is body-only (no attachment). Its mailbox body matches the `.eml` except for an undecoded quoted-printable soft break inside a date (`need by 10/18=` newline `/2026`), so ingestion must repair these even when the text comes from Graph.
- The 3 supplier POs (81766, 81793, 81797) are in Sent Items on 2026-09-01, byte-identical to folders 15 to 17.
- No revised POs, no follow-ups from customer domains, and no PO without an SO. All 14 customer emails map to an existing SO.
- Subjects are unreliable for matching: Pinecrest's is just "PO", Crescent's is "order", Ironclad's is "Ironclad PO attached", and Hollowbrook's carries a quote number, not a PO number. Match on sender plus attachment or body content, then resolve the customer (section 5).
- The mailbox is shared with other candidates and is noisy. Besides Vanta, GoDaddy and old RFQ traffic, it holds other agents' output (`[Order agent] HOLD`, `NEEDS REVIEW`, `[B] HOLD`) from 2026-09-29 onward, and test emails from gmail accounts that reuse trial PO numbers (for example "New order PO-MER26-0037"). 151 of the 405 Inbox messages were sent by the mailbox itself. Only a small minority of the Inbox is real customer POs, so triage runs on every email except mail from the mailbox itself, and only customer POs and revisions reach the pipeline (section 5, `intake/`). Other candidates' mail is ignored through classification, dedupe and a short exact-address suppress list.
- Simple triage rules fail on this mailbox. Measured over the 405 Inbox messages, against the 14 true customer POs:

  | Rule | Flagged | True POs caught | False positives |
  |---|---|---|---|
  | Subject contains a PO number | 69 | 10 of 14 | 59 |
  | Has an attachment | 108 | 13 of 14 | 95 |
  | Sender is not tryrama.com or gmail.com | 104 | 14 of 14 | 90 |
  | Subject mentions "PO", "order" or "purchase" | 81 | 14 of 14 | 67 |
  | Metadata and content rules combined | 36 | 14 of 14 | 22 |

  The PO-number rule misses Pinecrest ("PO"), Crescent ("order"), Hollowbrook ("PO for quote 20261299") and Ironclad ("Ironclad PO attached"). Its false positives are other agents' replies, which carry PO numbers in the subject, and gmail tests. We use these rules as features for the classifier, never as filters in front of it.

Appendix A lists the issues we found per order. It seeds the eval answer key.

## 3. Architecture

Two layers. The outer layer is an LLM agent with tools. The verification tool inside it is a fixed pipeline.

```
email ─► ORDER-DESK AGENT (LLM tool loop; decides what to do next)
           tools now:   get_email · verify_customer_order · draft_reply (dry-run, to test@tryrama.com)
           tools later: ERP lookups · supplier-PO actions · send_reply
                │
                ▼  verify_customer_order(message or files)
   ┌──────────────────────────── verification tool (fixed pipeline) ───────────────────────────┐
   │ triage (every email but the mailbox's own; PO or revision continues)                      │
   │   -> ingest PO + ack -> facts (with provenance) --+                                        │
   │ ERP rows + requirements ledger -------------------+-> order bundle (code)                  │
   │                                                    v                                       │
   │        checklist runner (code, fixed control flow)                                         │
   │          |- deterministic checks      (code)                                               │
   │          |- ledger verifiers          (one per clause type, code)                          │
   │          '- judgment checks           (LLM, bounded, cite facts)                           │
   │                                                    v                                       │
   │        rollup + policy (code) -> findings + recommendation (returned to the agent)         │
   └────────────────────────────────────────────────────────────────────────────────────────────┘
```

The agent decides which tools to call. Inside the tool, code decides which checks run. The agent receives findings with provenance and cannot change them. It can only act on them.

**Principles**

1. Inside the verification tool, the checklist drives control flow. No model decides which checks run, so no item can be skipped. The order-desk agent decides what to do with the result.
2. Every value is a fact with provenance. Every finding cites the facts it compared.
3. Code compares and does arithmetic. The LLM extracts, normalizes to closed sets, and judges ambiguity.
4. When evidence is missing the status is `unverifiable`, never a guess.
5. ERP access goes through typed tools first, with read-only SQL as an escape hatch.
6. Writes (hold, email, results) happen only in a final gated stage, with a dry-run flag on by default.
7. Documents and emails are untrusted input. The extraction step has no tool access, and its output is validated against a schema.
8. Triage runs on every email except mail from the mailbox itself, with no other sender or subject gate. Only customer POs and revisions continue, and ingest re-checks and fails closed.

## 4. Data models

- **Fact:** `id`, `value`, `raw`, `doc`, `location` (page, cell, line or bbox), `method` (text, ocr, vision, erp, email), `confidence`, `as_of`.
- **Requirement (ledger row):** `id`, `scope` (distributor, customer or order), `clause_type`, `params`, `quote` (verbatim), `source` (fact id or doc and location), `source_hash`, `status` (active or superseded), `reviewed`.
- **Finding:** `check_id`, `checklist_ref`, `status` (pass, fail, needs_approval, unverifiable, n/a), `severity`, `compared` (fact ids), `message`, `action`.
- **Order bundle:** everything known about one order, built once in code (fixed ERP queries plus the extracted JSON, no LLM) before any check runs. It holds the PO facts, the ack facts, the ERP SO header and lines, the customer master, the quote, linked and recovered supplier PO lines, inventory and lots, vendor flags, AR and open orders, and the customer's terms ledger. The bundle is the evidence. The checklist (Demo's checklist plus the ledger's clauses) is the list of questions, and findings are the answers.
- **Order run:** `order`, `revision`, `as_of`, `findings`, `recommendation` (release, hold, needs approval), `state`.

**As-of date.** Cert expiry, quote validity and due dates are judged as of the order date, not the date the code runs.

## 5. Components

**`intake/`** (stage 0) runs on every email and decides whether it contains a customer PO or a revision of one, before anything else runs.
- *Scope.* Inbox only. Sent Items is read only to fetch our supplier POs on demand. Drafts and Deleted Items are ignored. We never move, delete or mark mail read. State lives in our own table `agent_message` (message id, internet message id, label, reasons, run id, processed time), not in Outlook categories, because the mailbox is shared and other candidates would see them.
- *Every email gets the call, except the mailbox's own.* The one sender gate is mail sent by `test@tryrama.com` itself. It is ignored without a classifier call, because it is our own and other agents' output plus self-sent tests (151 of the 405 Inbox messages). No other sender, domain or subject gate decides whether the classifier runs, because we know nothing about how the demo order will arrive. Idempotency also skips a call (a message id we have already classified). Known limitation: a demo order sent from `test@tryrama.com` itself would be ignored.
- *Where the PO can be.* The classifier looks at the email body (plain text or HTML, including a PO typed or pasted into the body, as with Sandstone) and at every attachment: PDF (text or scanned), image (jpg, png, a photo like Pinecrest's), xlsx and csv, docx, a forwarded `.eml`, and images embedded in the body. Each is reduced to a snippet the classifier can read: extracted text for text PDFs, spreadsheets, docx and bodies (first page or a capped number of characters), and the image itself (vision) for scans and photos. A few attachments per message, with very large files sampled. One message can hold several documents (a PO plus its terms, or several POs), so roles are per document. Not handled yet, and labeled `unsure`: zip files, links to a customer portal, password-protected files.
- *Output.* A closed label per message: `customer_po`, `po_revision`, `not_po` or `unsure`. It also carries a reason category for the log (`rfq`, `reply`, `agent_output`, `notice`, `test`, `other`), a confidence, and a role per document (`po`, `so_ack`, `terms`, `other`). Only `customer_po` and `po_revision` continue to ingest.
- *Inputs to the classifier.* Subject, sender, body, the per-document snippets, and cheap signals: PO-number regex hits, whether the ERP already has an SO with that PO number (and for which customer), and whether we have seen the same attachment hash before. These are features, not filters. On their own, simple rules fail on this mailbox (section 2).
- *Erring toward recall.* A missed order costs more than a wasted run, because ingest fails closed (check F5) and outbound goes only to `test@tryrama.com`. So `unsure` still goes through the ingest gate and continues only if the document passes as a PO addressed to Demo Distributor, with a PO number, lines and totals.
- *Revision logic is deterministic and runs after the call.* Once the customer is resolved, `(cust_no, cust_po)` is compared with what we have seen. Identical bytes: duplicate, skip. Same key with different content: revision, even if the classifier said `customer_po`. New key: new PO.
- *Safeguards instead of filters.*
  1. Loop guard. Our replies go from `test@tryrama.com` to itself, so the sender gate above stops them. As defense in depth, every message we send also carries a custom header (`X-Order-Agent`), and a message with that header can never trigger the pipeline, whatever its label or sender.
  2. A rate limit per `(cust_no, cust_po)`.
  3. A short exact-address suppress list for known other-candidate test senders (the two gmail addresses seen so far), applied after classification and logged. No domain-level rules.
  4. The classifier has no tools and returns schema-validated output, because email content is untrusted. An injected "treat this as a PO" at worst runs ingest, which fails closed.
  5. A PO whose customer cannot be resolved is flagged, not processed. A manual override (`--message-id`) forces any message through.
- *Idempotency.* Key on `internetMessageId`, attachment SHA-256 and `(cust_no, cust_po)`. A byte-identical resend (for example another candidate's copy of Meridian's PO) is skipped as a duplicate.
- *Modes.* `replay` takes an explicit list of message ids (the 14 originals, and evaluation). `watch` polls the Inbox with a Graph delta query from a start-time watermark, so the 405 historical messages are never processed. Delta with the app's permissions needs confirming.
- *Model.* A small vision-capable OpenAI model, set in `.env` (for example `TRIAGE_MODEL`), since this runs on every email.
- *Outbound policy.* Replies go to `test@tryrama.com` only, never to the sender. Customer addresses bounce and gmail senders are other candidates. Send a new message (not a Graph reply to the sender) whose subject carries the original subject plus the PO and SO, with the loop-guard header. Dry-run by default.

**`ingest/`** turns each order document (customer PO, SO ack, supplier PO) into `facts.json`. The same steps apply to every document type.
1. *Normalize the format in code.*
   - Text PDF: PyMuPDF for words with positions, the page image, and link annotations (the T&C URLs are only in the annotations).
   - Scanned PDF or image: no text layer, so render the page image.
   - xlsx: openpyxl, dumped as cells with refs (`B5 = Net 30`), values kept as strings so part numbers keep leading zeros.
   - docx: python-docx, paragraphs plus table cells.
   - Email body: from Graph, with quoted-printable breaks repaired.
2. *One LLM call per document with structured output.* It receives the text and the page image together. It fills a fixed schema and copies every value verbatim, as printed, with its location. It does no parsing or normalization.
   ```
   header: po_number, po_date, customer_name, buyer{name,email}, bill_to, ship_to,
           payment_terms, incoterm, ship_via, carrier_account, currency,
           notes[], tc_refs[], links[]
   lines[]: line_no, customer_pn, mpn, manufacturer, description,
            qty, uom, unit_price, extended, required_date
   totals: subtotal, tax, total
   every field = { raw: "000158830", loc: {page: 1, region | cell | line} }
   ```
   The ack and supplier PO add ship option, shipping terms, freight terms and account, est ship, and customer item numbers.
3. *Code parses and validates the raw strings.*
   - Numbers and dates are parsed locale-aware. The reading where qty x price = extended and the lines sum to the total wins (this resolves Oakridge's `19.000`).
   - Part numbers stay exact strings.
   - Manufacturer names map to ERP codes through an alias table.
   - The is-a-PO gate (F5) runs here.
4. *Grounding checks.* For text sources, every `raw` value must appear in the source text, which catches invented or "corrected" values. For scans and images, a second independent read (Tesseract OCR) covers part numbers, quantities, prices and account numbers. A disagreement is flagged with the image crop.
5. *One retry with feedback* (for example "line 2 does not tie out, re-read"). If it still fails, the affected fields are marked uncertain, never guessed.

Long conditions pages (for example Quarry's page 2) go to the ledger extraction, not this call. Multi-page line tables are extracted per page and merged, with a line-number continuity check.

**`erp/`** has typed read functions that return facts and encapsulate the traps in section 2. They cover the order and lines, the customer master, the quote, linked and recovered PO lines, inventory and lots, vendors, AR and credit. Batch to one query per table per order. The same functions serve the chatbot.

**`ledger/`** has the closed clause taxonomy, the extractor and the verifiers.
- *Extraction.* Each source is split into units (by headings and numbering). Each unit gets exactly one label: a clause type with params and a verbatim quote, informational, or unclassified. A coverage check requires every unit to be labeled. Long T&Cs are chunked, with the heading path as context and a document-wide pre-pass for definitions. Results are cached per customer and `source_hash`. Run extraction twice with different chunk boundaries and send disagreements to unclassified.
- *Sources of requirements.* Standing: ERP flags (code, no LLM), `ord_notes` (dictionary first, LLM fallback), customer T&Cs. Per order: the PO's own notes and emails. Distributor: our checklist and own rules, such as CoC for unmarked SMT parts and prepay fees.
- *Verifiers.* One code function per clause type, looked up from the requirement row's `clause_type` and driven by its `params`. Outcome rules:
  - A clause with an exception (for example "without prior written approval") returns `needs_approval` on a hit, not `fail`.
  - Weak evidence never passes. For origin, an item default alone gives `unverifiable`, with the default noted and severity raised when it points at a prohibited country (item defaults disagree with lot origin on 83% of lots).
  - Clauses that can only be satisfied later (packaging, date code at shipment, CoC) are verified by flow-down: does our supplier PO carry the requirement? Otherwise they are listed as carry-forward requirements.
  - A clause type with no verifier gets an LLM assessment with cited facts attached, and its status is always `needs_review`. Recurring unknown types are candidates for a new verifier.

**`checks/`** runs the checklist over the order bundle.
1. *Build the bundle* (section 4) from `facts.json` for the PO and ack plus the ERP lookups.
2. *Match lines in code.* Each line is paired across PO, SO, ack, quote and supply on MPN plus manufacturer. Duplicate-MPN lines (Norwood, Granite Peak, Copperfield) are paired on qty, price and date. Unmatched lines are findings in their own right (missing or unrequested).
3. *A registry of checks.* Each check declares an id, the checklist item it serves, the inputs it needs, and a function that returns findings. Every check runs on every order in a fixed order. A missing input returns `unverifiable`. Four kinds:
   - *Code comparisons:* families A to D in section 6.
   - *Ledger verifiers:* one per clause type, run for each requirement row in the customer's ledger.
   - *LLM judgment checks:* T&C conflicts, unclassified clauses and the summary. They must cite facts and cannot overturn a code result.
   - *Human-only items:* marked as manual.
4. *Roll up to Demo's checklist.* Each item in `data/Order_Processing_Checklist.docx` maps to check ids. The worst status wins (fail, then needs approval, then unverifiable, then pass). A policy table turns the rollup into release, hold or needs approval.

Example output for Meridian (503097):
```
§1 Payment terms      FAIL          PO "Net 30" (PO p1, Terms) · SO N45 (ord_hedr.terms_code) · master N30 (customer C1002)
§2 Partial shipments  FAIL          PO "Ship complete. No partial shipments…" (PO p1, notes) · ack "Ship Partials" (ack p1) · master no_partials=1
§2 Line 3 quantity    PASS          9,300 (PO p1, line 3) = 9300 (ord_detl 503097/3)
§5 Ready date         UNVERIFIABLE  Lead Time Matrix not provided
```

**`judge/`** has narrow LLM checks, each with its own prompt. Tools: `get_facts`, `read_source`, `get_requirements`, the ERP lookups, read-only SQL (SELECT only, read-only transaction, row limit), and `record_finding`, which rejects unknown fact ids.

**`report/`** does the rollup and policy, writes JSON and markdown per order, and writes a results table in `worktrial`. `set_order_hold` and `send_email` live here, behind the dry-run flag.

**`revision.py`** handles PO revisions. Detection is code, and what to do about a revision is the order-desk agent's decision.
- *Detection.* Every verified PO is recorded in an index keyed by `(cust_no, cust_po)`, with its file hash and run. A PO with the same key and different bytes is a revision. The agent can also pass a previous version explicitly, for example when the email says "revised PO" but the number changed.
- *What the tool returns.* A code diff of header fields, lines (added, removed, changed quantity, price, date, part) and notes. It also returns which issues from the previous run are resolved, new or still open. All checks still run against the newest version.
- *What the agent does with it.* It decides. For example: a revision that fixes the open issues can be released, and one that changes quantities needs the SO updated.

**Customer identity.** Resolve the customer first. The ledger and every check hang off it, so a wrong match means wrong terms, credit and requirements. Examples: Sandstone Lighting (C1143, N45) vs Sandstone Electronics Manufacturing (C1061, N30, Yield flag), and Oakridge Imaging (CIA) vs Oakridge Sourcing (N30). In three of the four trial SOs whose terms disagree with the master (Oakridge, Quarry, Crescent), the SO terms equal the look-alike sibling's terms. N30 is also the most common code, so this may be a coincidence, but it is worth testing whether those SOs were keyed from the wrong customer.
- Signals, each stored as a fact: normalized legal name (strip GmbH, K.K., Inc., LLC), bill-to and ship-to street and postcode vs the master, buyer email domain and name vs `contact.b_email` (the two Sandstones use different domains), customer part numbers vs `cust_itm` (to confirm), and PO-number prefix (weak, because siblings share the three-letter prefix).
- Require at least two independent signals to agree, one of them name or address. Otherwise the result is `unverifiable: ambiguous customer` with the candidates listed. Never pick silently.
- Key every lookup on `(cust_no, cust_po)`.

**Storage.** The ERP has no document storage, so we keep four kinds of artifact ourselves:
- *Source documents as received* (pdf, xlsx, jpg, eml, docx), immutable under `store/sources/<sha256>.<ext>`. Provenance locations (page, cell, bbox) only mean something against the exact bytes, so we hash them.
- *Facts*: `store/facts/<sha256>.json`, one per source document.
- *Customer terms (the ledger)*: structured rows in `agent_requirement`, plus the raw source chunks in `agent_source_chunk`, keyed by customer and source hash. A readable per-customer terms document is rendered from the rows for human review. It is a view, not the source of truth.
- *Runs and findings*: `agent_run`, `agent_finding`.

Our tables use the `agent_` prefix in the `worktrial` schema. We do not know whether the database is private to us, so we never touch ERP tables except the gated hold or status write. Approvals and email attachments are stored with the email work.

## 6. Check catalogue

Checklist references are to `data/Order_Processing_Checklist.docx`. All checks are code unless marked J (judgment).

**A. Document vs ERP and PO**

| Id | Check |
|---|---|
| A1 | Header: customer name (strip legal suffixes), PO number, dates, bill-to and ship-to, contacts (§2). |
| A2 | Commercial terms: payment terms, currency, Incoterm, ship via, carrier, account number, partial-shipment policy (§1, §2). |
| A3 | Lines: match on MPN + manufacturer, then compare qty, price, UoM, due date, customer P/N, description. MPN is exact (§2). Duplicate-MPN lines are assigned by qty, price and date. |
| A4 | Arithmetic: line extended, subtotal, total, tax. |
| A5 | Additions and omissions: ack content not requested (allowlist the standard boilerplate), PO content missing from the ack (notes, clauses). J for conflicts with customer clauses. |
| A6 | Ack PDF vs ERP rows, for the fields that exist in both (terms, lines, prices, quantities, dates). Catches a stale or mis-exported ack. Fields only on the ack (section 2) are checked against the PO and master directly. |

**B. Customer master and quote**

| Id | Check |
|---|---|
| B1 | SO terms vs master vs quote vs PO (§1). |
| B2 | Price and cost vs quote (apply the customer's tolerance, for example 2%), quote validity (`valid_days`), quote link recovery (§2). |
| B3 | Credit: limit vs open orders plus AR, for net terms only. Prepay fees (card 3.5%, wire $25) (§1). |
| B4 | Yield flag and required approvals (§3). |
| B5 | Ship account vs `shipinst`, partial instructions vs `no_partials` (§2). |
| B6 | Customer identity resolution (section 5, "Customer identity"), and the SO's `cust_no` agrees with the resolved customer. |

**C. Supply**

| Id | Check |
|---|---|
| C1 | Stock: on hand minus committed, aggregated by item across lines and open orders. Reconcile `itm_inv` against lots and say which we trust (§4). |
| C2 | Inbound: linked and recovered PO lines, due date vs required date. |
| C3 | Lead time: Est Ship vs quoted lead time (regex on `N-M WKS`), and "quoted STOCK but not in stock" (§5). |
| C4 | Unrequested supply: PO lines for parts not on the SO (PO 81797 line 2, PO 81766 line 2). |
| C5 | Cost: SO unit cost vs PO unit cost vs quote cost, and margin. |

**D. Compliance**

| Id | Check |
|---|---|
| D1 | Country of origin with the hierarchy: PO line `src_cntry`, then allocated lot, then item default (weak), then unknown. Customer prohibitions need approval. The ack must state COO, ECCN and HTS per line when the customer requires it. Quantify China-origin value per order. |
| D2 | Export: destination other than US, or ECCN other than EAR99, requires export review (§6). Flag only. |
| D3 | Date code, shelf life, AS5553/OCM, CoC and similar customer requirements vs our supplier PO conditions and vendor `po_notes`. |
| D4 | Vendor risk: `pay_hold`, ISO expiry as of order date, `po_notes`. |
| D5 | NCNR and T&C conflicts between the ack and the customer's terms. J. |

**E. Ledger verifiers.** The generic mechanism for any clause type (partials, date code, prohibited origin, price tolerance, late-delivery penalty, DFARS/ITAR flow-down, and so on).

**F. Process and document quality**

| Id | Check |
|---|---|
| F1 | Unsigned PO, scan reads that disagree, locale mismatches. |
| F2 | SLA clock, such as "acknowledge within 48 hours". |
| F3 | Referenced but unread terms (links, clause codes like Q2/Q5/Q11, document ids). Always `unverifiable`. |
| F4 | Human-only checklist items (uploads, emailing inspection, test-report approval) are marked as such. |
| F5 | Ingest re-confirms the document is a purchase order addressed to Demo Distributor (PO number, lines, totals). If not, stop and log. Never validate a non-PO. |

## 7. LLM policy

Use an LLM only where the problem is "I cannot turn this into canonical form". Never use it to decide whether two identifiers or numbers agree, because disagreement is the finding.

| Step | LLM? |
|---|---|
| Document and clause extraction, email understanding | Primary. |
| Arithmetic, number/date parsing, comparisons, inventory, credit, quote price, origin hierarchy | Never. |
| Manufacturer name to code | Proposal only. Seed from the 14 acks, queue unknowns for review. |
| Payment-terms phrasing | Yes, closed set of 6 term codes, audited. Scope to the terms field. |
| Line matching | Only on ties or lines with no MPN. A near miss is reported as a mismatch with the closest candidate. |
| Incoterms, ship via, account numbers, address, customer name, lead-time text | Code only (regex, alias tables, fuzzy match). |
| Quote or PO link recovery | Code only. A guessed quote would "verify" against the wrong price. |

Fallback tiers: exact, normalized, fuzzy, LLM proposal, unverifiable. LLM output must be a closed set or a cited span. An LLM-resolved value is marked in provenance and cannot produce a pass while the check's other inputs disagree. Approved mappings are cached.

## 8. Build order

Start with one vertical slice on Meridian (order 01), then widen to all 14.

**Status (2026-10-06):**
- **Done:** M0, and M1 including the agent loop on Meridian's real email.
- **Built:** revision support.
- **In progress:** M2 to M6 across all 14 orders.
- **Also built:**
  - Stock, supply and ready-date checks (4.1, 5.1);
  - Supplier-PO cost and unrequested lines, vendor standing, and flow-down (4.4, 4.5);
  - Lot-level origin;
  - Checklist coverage enforced by a test;
  - Read-only `lookup_inventory` and `query_erp` tools for the agent.
- **Also built:** inbox watching with triage on every email, several POs per email (versions treated as a revision), a local state store, and the FastAPI app.
- **Also built:**
  - Parallel processing (at most 5 agents);
  - Inline-image POs;
  - ERP writes (dry-run);
  - The chatbot v1 (API and floating chat);
  - The React client: email dashboard and order detail in steps.
- **Code layout:** `server/` and `client/`.
- **Also built:** the supplier side.
  - Our POs to suppliers (from Sent Items) and supplier acknowledgements (Inbox) are verified with checklist P1–P10.
  - It is tested on the 3 real supplier POs and 3 synthetic acknowledgements.
  - The UI has its own supplier PO view.
- **Not built:**
  - An OCR second read on scans;
  - Unsigned-PO detection;
  - A code-generated findings block in replies.
- **Eval:** 54 of 54 expected findings caught across the 14 orders (`uv run python -m agent eval`), with 0 issue-level findings outside the answer key.

| Step | Work | Done when |
|---|---|---|
| M0 | Repo setup, `.env`, `.gitignore`, `DECISIONS.md`, LLM wrapper | A README stub and a clean first commit with no secrets. |
| M1 | Meridian slice: ingest PO and ack, ERP bundle, ledger from the master, checks A2 and A3, report, and the minimal order-desk agent loop calling `verify_customer_order` | Given Meridian's email, the agent calls the tool, gets the terms and partials mismatches with provenance, and drafts a reply (dry-run). |
| M2 | Ingest all 14 POs and acks | All 14 produce `facts.json` and totals tie out. Scans, xlsx, docx, eml and the German PO all parse. |
| M3 | Families A and B | The eval shows recall on the doc-diff and master/quote/credit issues. |
| M4 | Family C | Supply, lead-time and link-recovery issues are caught. |
| M5 | Family D and the ledger verifiers | The origin, export and vendor findings match Appendix A. |
| M6 | Judgment checks and rollup | Each order has a recommendation and a readable summary. |
| M7 | Eval and mutation tests (section 9) | A scored report exists. |
| M2b | Triage and intake, read-only: classify the Inbox, fetch attachments, link each PO to an order by `(cust_no, cust_po)` | On the 405-message Inbox, triage selects exactly the 14 customer POs and runs nothing else. All the formats are recognized (text PDF, scan, jpg, xlsx, docx, body-only). The attachments match the folder files. |
| M8 | Watch mode: Graph delta polling from a start-time watermark, triage, run, reply on the thread (to `test@tryrama.com`) | A dropped-in order is processed end to end, and everything else is ignored. |
| M9 | Chatbot reusing the ERP tools | The two example questions in the task answer correctly. |

**If time is short:** cut the UI and supplier ordering first, then the automatic trigger and reply (drop files in by hand for the first demo). Keep M2b read-only intake, because it answers questions the folders cannot. Do not cut the eval.

## 9. Evaluation and testing

- **Answer key:** Appendix A, hand-verified. Metrics: recall on seeded issues, false positives per order, and the unverifiable rate.
- **Mutation tests:** only 14 examples exist and the demo order is unseen, so avoid overfitting. Perturb the extracted facts (change a quantity, swap two digits in an account number, swap terms, remove a line, change a price by 4%) and require detection. Degrade scan quality and check that disagreements are flagged.
- **Negative control:** Ironclad (14) is essentially clean. It should produce no failures.
- **ERP survey:** list the unexplored `customer`, `item` and `vendor` columns and the tables we have not inspected (`cust_itm`, `slsman`, `req_*`, `rfq_*`, `comm_due*`) for anything the checklist needs (tax exemption, credit application, MOQ, testing flags).
- **Triage eval:** the 405 Inbox messages are labeled (14 customer POs, the rest not). Report precision and recall, with the adversarial negatives called out: agent replies that carry PO numbers in the subject, gmail tests that reuse trial PO numbers, and RFQ look-alikes. Also check that a byte-identical resend is skipped as a duplicate.
- **Consistency:** the validator and the chatbot share one definition of "open order" and "credit exposure".

## 10. Other problems to solve

Not yet in the catalogue. Items 1, 2, 4 and 5 depend on the mailbox. Read-only intake (M2b) comes first. Drafting and sending come after validation is trusted.

1. **Revised POs.** The task says revisions are attached next to the original. Pick the latest, diff against the previous revision, record which revision was checked, and re-run on change. None of our sample folders has one, so check the mailbox.
2. **Email handling beyond triage.** Thread messages to orders, handle multiple attachments per message, and keep approvals that arrive only by email. Triage itself (is this a customer PO?) is in scope now (section 5, `intake/`). The mailbox copies of the POs were confirmed identical to the folder files.
3. **PO-only mode** is not needed for the demo, because the SO will exist. Missing inputs still degrade to `unverifiable`.
4. **Approval workflow.** States: requested, granted or denied, attached, re-run. Know who approves what (Yield, price variance, restricted origin) and how the agent recognizes a grant.
5. **Customer and manager emails.** Draft the discrepancy email asking for a revised PO or written acceptance, and the approval request. Track the waiting state and the 24 to 48 hour clock.
6. **State machine and idempotency.** received, extracted, checked, awaiting customer, approved, released. Re-running must not duplicate holds or emails.
7. **Attachment storage.** The ERP has no document storage, so approvals and uploads need our own store and tables. This is also where a check result is "recorded".
8. **Units and order multiples.** UoM, minimum order quantity and multiples. A PO description includes "Min Qty:250".
9. **Tariff exposure and margin.** The task's $25k tariff story is a thin-margin problem. Report China-origin value and per-line margin, and flag margin shifts versus the quote. We do not have tariff rates, so this is exposure, not cost.
10. **Substitutions.** Vendor notes forbid substitutes without written approval. Compare SO item against what the supplier PO actually bought.
11. **Security.** Prompt injection from document text, fetching T&C links (they point to an external host), secrets in transcripts.
12. **Demo latency.** An unseen order is processed live. Parallelize calls, pre-warm the customer ledger cache, and keep a fast path.
13. **Generalization.** Avoid hard-coding to the 14 orders (the mutation tests help).
14. **Multi-tenant.** Put the ERP behind an adapter before a second distributor.
15. **Review fatigue.** Distinguish "needs sourcing" (normal) from "quoted STOCK but not in stock" (exception), and rank by severity.

## 11. Repo hygiene and deliverables

- `.env` holds DB, Graph and model credentials. Add `.env` and `task.md` (it contains credentials) to `.gitignore`. Commit `.env.example` with variable names only.
- AI transcripts are required in full and unedited, and they will contain credentials. Rotate or scrub before sharing.
- Keep `DECISIONS.md`, a README on how to run, and a `KNOWN_LIMITS.md` for the demo conversation (what falls short, what we would do next).
- Hand in: the code, the transcripts, and the agent's results per order.

## Appendix A: issues found per order (seed answer key)

Hand-verified. The statuses "open" (O, I, H) are assumed. "As read" marks values read from a scan.

| # | Order | Issues |
|---|---|---|
| 01 | Meridian 503097 | SO Net 45 vs PO and master Net 30. SO "Ship Partials" vs PO ship complete and master no partials. Master date code at most 12 months vs supplier PO 81797 "within 36 months of PO date". PO 81797 line 2 (Bourns 1.5SMC16AT3, 2.4M, $197,688) is not on the SO. Lines 1 and 2 are covered by unlinked PO 81764 (line 1 due 9/20 vs need 9/14). Line 2 has no `itm_inv` row. T&C link hidden in the PDF. |
| 02 | Northstar 503105 | PO and SO match. PO unsigned. Clauses Q2/Q5/Q11 and NSA-SCM-001 unreadable. Master requires DFARS/ITAR, AS5553/OCM, date code at most 24 months. Line 1 has no inventory row and no supply. Line 2 needs 720k with 4.2k on hand and none inbound. No quote. |
| 03 | Quarry 503153 | SO Net 30 vs PO credit card and master CC. PO FCA Austin vs SO Ex Works. PO clause 3 requires COO, ECCN and HTS per line on the ack, and the ack has none. Clause 5 prohibits PRC origin: line 5 is CN per PO 81787 (firm), line 1 defaults to CN (and is ECCN 5A991.b, so export review), line 3 conflicts (item CN, lot MX), lines 2 and 4 unverifiable. Line 3 needs 940k with about 173k available and none inbound. Ack NCNR conflicts with PO clauses 1 and 9. Page 2 also has date code at most 36 months and a late-delivery penalty. No quote. |
| 04 | Pinecrest 503156 (jpg) | PO and SO match. "Acknowledge within 48 hours". T&C is an external URL. Quote lead times (6-8 and 12-16 weeks from 8/31) are later than SO due dates on lines 1 and 2. Line 3 quoted STOCK, 5 on hand with 2 committed vs 390. |
| 05 | Copperfield 503085 (scan PO) | Line 1 qty PO 45,000 vs SO 49,000 (a $2,614.80 difference). Freight account PO 000158830 vs SO and master 000158380, as read. Supplier PO 81766 lines 2 and 3 of the SO due 11/26 vs need 9/7 and 9/10. SO line 3 cost 10.112 and vendor V1212 vs PO 81766 10.333 and V1029. PO 81766 line 2 (Murata, 230k, about $105k, CN) is not tied to the SO. PO 81766 omits the vendor's no-substitutions note and the customer's AS5553, DFARS/ITAR and date-code flow-downs. V1212 ISO cert expires 9/26. |
| 06 | Cobalt 503102 (scan ack) | Line 3 price SO 19.382 vs PO and quote 18.636 (+4.0%, over the 2% tolerance, needs buyer approval). Customer P/N 05673-001-XTD missing from the SO. Export to Mexico, all EAR99. MPN contains a comma. |
| 07 | Crescent 503170 (xlsx) | PO and SO Net 30 vs master CC (no 3.5% card fee, SO total equals the line sum). Open orders $44.7k vs $25k limit. "Our P/N" blank on lines 1 and 2. No quote. |
| 08 | Sandstone 503178 (eml) | Email gives a new ship-to (2200 Harbor Point Dr, Bldg C, Norfolk VA 23510). SO and master have 6013 Research Blvd, Richmond VA. Email gives no freight account (SO has 000285084). Open orders $104k vs $75k limit. Vendor V1007 ISO cert expires 9/3. No quote. |
| 09 | Ospreyfield 503137 (scan PO) | Line 2 PO 9014177 / Molex vs SO 9014171 / Murata, as read. Yield flag requires manager approval. Open orders $67.9k vs $50k limit. Vendor V1074 on pay hold, V1118 ISO expired 8/12. Export to the Netherlands. No quote. |
| 10 | Norwood 503180 (docx) | PO "partial shipments acceptable" vs master requiring written approval. Same MPN on two lines (different qty, price, date). Line 1 (6.9M) has no supply and vendor V1246 is on pay hold. Line 2 covered by PO 81774. No quote. |
| 11 | Oakridge 503144 (German PO) | SO Net 30 vs PO (Vorkasse), quote and master (prepay). PO requires COO and ECCN per line on the ack, and the ack has neither. Quote lead time (8-12 weeks from 8/18) is later than SO due 10/6. Locale parsing. |
| 12 | Granite Peak 503107 (scan ack) | PO and SO match. 660k demand on 0A37039 vs 14,000 on hand, 5,854 committed, 28,000 inbound (unlinked PO 81653). No quote although the master requires price to match the quote. Same MPN on two lines. |
| 13 | Hollowbrook 503184 | PO, SO and quote match. PO unsigned. "Acknowledge within 48 hours". Line 2 supply (PO 81798) due 10/10 vs need 9/22. |
| 14 | Ironclad 503095 | PO, SO and quote match. Description text truncated ("Min Qty:250Lead..."). Stock shortfall with no inbound, which is normal sourcing. Negative control. |

All 14 acks: standard boilerplate, Est Ship equals requested date.

## Appendix B: open questions (to ask later)

1. Are lead-time weeks counted from the quote date or the order date?
2. Resolved: status codes confirmed (section 2). Check results are recorded in `agent_check_result` and `agent_check_issue`. A hold sets the order's status to H, logged in `agent_order_hold`. Both are dry-run until ERP writes are switched on (`ERP_DRY_RUN=0`).
3. Resolved: the demo order's SO will exist in the ERP, and an ack PDF comes with it. Still to confirm: how the PDF reaches us (attached to the email, or separately). The plan handles both.
4. What counts as "written approval" (for example Quarry's no-China rule), and who approves what?
5. Resolved for now: flagging means replying on the originating thread. Outbound email goes only to `test@tryrama.com`.
6. Resolved for now: OpenAI. Confirm which model, and whether the key in the task doc is the one promised "on the day".
7. Resolved: the mailbox attachments are byte-identical to the folder files, and the mailbox is shared with other candidates. Ignore their mail.
8. Where do "testing required" and tariff data live? The task mentions both on the supplier side.
9. Are the T&C links (on an external host) in scope to fetch?
10. The Graph tenant ID, client ID and secret were "sent separately". Where are they?
11. The Lead Time Matrix, Export Review Form and inspection mailbox are named in the checklist but not provided.
12. Shipments are empty in the ERP. Is that intended?
13. Customer PO numbers are reused across customers (277 times). Is `(cust_no, cust_po)` the intended unique key?
14. Resolved for now: we assume nothing about how the demo order arrives (sender, format, body or attachment). Triage runs on every email except mail from `test@tryrama.com` itself.
