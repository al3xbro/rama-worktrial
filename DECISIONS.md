# Decision log

Why the order desk agent is built the way it is. Newest decisions are at the bottom. The full plan and the data findings behind these decisions are in [PLAN.md](PLAN.md).

## 2026-10-06

**1. Validate, don't create sales orders.** Every trial PO already has its SO in the ERP, entered by a rep, and the demo order will too. The agent's job is the check that happens before release.

**2. Two layers: an order-desk agent with a verification tool.** Decided with a team member. The order-desk agent (an LLM tool loop) receives each email and decides what to do. `verify_customer_order` is a fixed pipeline it calls. The agent can act on the findings but cannot change which checks run or what they found. This leaves room for later actions (ERP lookups, adding lines to an outgoing supplier PO) without making the checks themselves non-deterministic.

**3. Code compares; the LLM reads.** The LLM extracts documents, interprets clauses it has not seen, and writes the reply. Code does every comparison and all arithmetic. The data is full of near misses that a model tends to explain away: freight account `000158830` vs `000158380`, part `9014177` vs `9014171`, price `19.382` vs `18.636`. Code just reports that they differ. It also means every check always runs, and the same input gives the same result.

**4. Extraction copies values verbatim, with locations.** There is one structured-output call per document, given the page text and page images together. Code parses numbers and dates afterwards. Two checks guard the call:
- Every value must appear in the source text (grounding).
- Line arithmetic must tie out.

A failure triggers one retry with feedback, and anything still wrong becomes a warning rather than a guess. Results are cached by file hash, model and prompt. This is what lets the German PO's `19.000` resolve to 19,000: the reading that makes qty × price = extended wins.

**5. The SO side is the ERP plus the ack PDF.** `ord_hedr` has only 17 columns. Ship option, ship via, shipping terms, freight account and addresses exist only on the ack, so the ack is ingested like the PO. Acks are not in the mailbox, so the tool looks for an ack attached to the email first, then one in the drop folder (`ACK_DIR`) by SO number.

**6. Orders are keyed on customer plus PO number.** 277 PO numbers in `ord_hedr` are reused across customers, never within one. Three trial POs collide, two of them between look-alike customers (Northstar and Norwood).

**7. Customer terms become a requirements ledger.** Customer master flags map to clauses in code. Free text goes through a dictionary of the clause sentences seen in this ERP and these POs: the 14 trial customers produce 75 clause rows and the dictionary recognises 74. Anything unrecognised becomes `unclassified` and is surfaced for human review, never dropped. Each clause type has a code verifier.

**8. Country of origin has an evidence hierarchy.** The supplier PO line comes first, then the lot, then the item default. Item defaults disagree with lot origin on 1,627 of 1,955 lots, so a default alone is reported as unverifiable, never as a pass.

**9. Statuses and policy.** Findings are pass, fail, needs_approval, needs_review, unverifiable, manual (process or carry-forward) or info.
- **Recommendation:** any medium or high fail means hold. Otherwise any approval means needs_approval. Otherwise any review item or high unverifiable means review. Otherwise release.
- **Approval exceptions:** a clause with an approval exception (for example "without prior written approval") gives needs_approval on a hit, not fail.

**10. Email triage.** Every email except those sent by the mailbox itself gets the "is this a customer PO or a revision" decision. Nothing is known about how the demo order will arrive. The mailbox is shared with other candidates, and simple rules fail on it: a PO-number rule misses 4 of the 14 real POs and flags 59 other emails. Replies go only to `test@tryrama.com`, carry an `X-Order-Agent` loop-guard header, and are dry-run by default.

**11. Revisions: detected in code, decided by the agent.** The tool keeps an index of every PO it has verified, by customer and PO number. A PO with the same key but different bytes is a revision. The agent can also pass a previous version explicitly. The tool returns a code diff (header, lines, notes) and which issues from the previous run were resolved, are new, or are still open. What to do about it is the agent's call.

**12. Models.** `gpt-5.5` for extraction and the agent, `gpt-5.4-mini` for triage. All three are set in `.env`. An extraction call takes about 35 seconds, so extractions are cached and the batch runs in parallel.

**14. Changes after the first full run.** The first batch over all 14 orders was compared with the hand-built answer key (PLAN.md, Appendix A). It caught the planted document and ERP issues and released Ironclad, the clean control, but it also showed four problems:
- **Customer item codes misplaced.** Cobalt's `05673-001-XTD` was extracted as a description, because on that line the Item column holds only a code. The extraction prompt now says a buyer's item code in the Item or Description column is `customer_pn`.
- **Master flags over-read.** I had read `prt_coo` and `eccn_req` as "the ack must state origin and ECCN", which failed four acks without evidence that this is what the flags mean. They now mean "print on the shipping documents" and are carry-forward requirements. Only an explicit PO clause (Quarry, Oakridge) fails the ack.
- **16 noisy "unrecognised clauses".** These were clause headings split off as sentences, email greetings and signatures, and ship-to instructions. The splitter and dictionary now handle them. Sentences still unrecognised are labelled into the known clause types by a small model (`gpt-5.4-mini`), cached per sentence. The model only classifies, and the code verifiers still decide. Sentences it calls "not a requirement" stay visible in the report.
- **Policy was too lenient.** Medium-severity unknowns, such as unreadable customer T&Cs or a required but missing quote, now send the order to `review` instead of `release`.

**15. Inventory goes in both places, split by purpose.**
- *The tool:* the checklist's stock and date items (4.1, 5.1) run inside the verification tool, in code, on every order.
- *The agent:* read-only `lookup_inventory` and `query_erp` for open-ended follow-up, such as other stock or an open supplier PO that could cover a short line.

Both use the same ERP functions, so they agree.

**16. How supply is judged.**
- **Two questions.** "Enough parts?" (4.1) and "by when?" (5.1) are separate checks.
- **Coverage order.** Each line is covered in need-date order from:
  1. supplier PO lines bought for it;
  2. stock;
  3. unallocated open supplier PO lines (an exact quantity and cost match first).

  Lines committed to another SO are never counted.
- **Two stock sources.** `itm_inv` and the lot balances disagree on most items (for example MMSZ10T1: 247,152 vs 0), so coverage is computed under both. A disagreement that changes the answer is `needs_review`, with both numbers.
- **Statuses for 4.1:**
  - Quoted from stock but not covered: fail.
  - Otherwise short, with nothing on order: manual ("needs sourcing"). That is normal in a quote-to-order business and does not block release.
- **Ready date for 5.1:** the checklist's 3 days for stock, the actual due date of the supplier PO lines used, or the quoted lead time counted from the order date. The last is an assumption (open question 1 in the plan), so it yields `needs_review`, not fail.

**17. Our checklist cannot be silently skipped.** All 31 items are encoded, plus 6 added from the brief. Every check is registered against an item, and every report shows the whole checklist. An item no check covers must carry a written `manual_reason` (for example "needs documents the ERP does not store"), and a test fails if any item has neither a check nor a reason.

**18. Flow-down to supplier POs.** For each supplier PO bought for the order, the customer's date-code, CoC and traceability requirements are compared with our conditions on that PO. Where we have the PO document it is the evidence, and a gap is a fail. Otherwise the vendor's standing PO notes are used, and a gap is `needs_review`.

**19. Inbox watching, triage, several POs per email, and the API.**
- **Watcher.** It polls the Inbox from a watermark that starts at the moment it first runs, so the mailbox's history is never processed by accident. Messages are processed one at a time, oldest first, and never twice unless forced.
- **Triage on every email.** Every email except the mailbox's own gets one triage call to a small model, which sees the body and a snippet of each attachment (text, or the image for scans and photos). `customer_po`, `po_revision` and `unsure` go on to the agent. Exact addresses of other candidates testing in the shared mailbox are suppressed after triage, and that is logged.
- **Several POs in one email.** POs with the same PO number are versions of one order: the latest is verified and diffed against the earlier one. The task says revised POs are often attached next to the original. Different PO numbers are verified as separate orders, and acks are paired by the customer PO number they print.
- **State.** Processing state lives in a local SQLite file (`store/state.db`), not in `worktrial`. The ERP database may be shared with other candidates, and this is not ERP data.
- **API.** A FastAPI app serves the status of email processing, orders, and issues ranked by severity, for the interface to come.

**20. Robustness fixes from the first triage eval over the whole Inbox (405 messages).** Triage itself caught every customer PO it read and missed none, but the run found two failures:
- **Graph throttling.** About five messages failed with `429 Too Many Requests` under 8 parallel workers, one of them Ironclad's real PO. Graph reads now retry, honouring `Retry-After` and otherwise backing off exponentially.
- **Unreadable spreadsheets.** openpyxl rejects some real-world xlsx files (6 RFQ spreadsheets with unusual styles), which crashed triage. There is now a fallback that reads cells straight from the sheet XML, and one unreadable attachment no longer stops triage of the email.

**21. Status codes confirmed.**
- Sales orders: C closed, O open, I in process, H hold, V void.
- Supplier POs: C closed, O open.
- Quote lines: W won, L lost.

Credit exposure counts O, I and H.

**22. ERP writes are built but dry-run.**
- **What is written:** every verification records its result and issues in `agent_check_result` and `agent_check_issue`. The agent puts an order on hold with `hold_order` when the recommendation is hold or needs_approval. That sets the order's status to H, only from O or I, with the reason in `agent_order_hold`.
- **Dry-run behaviour:** while `ERP_DRY_RUN=1` each action is planned with its exact SQL, logged to the state store and shown at `GET /erp-actions`, but not executed.
- **Switching it on** later is setting `ERP_DRY_RUN=0`.

**23. Emails are processed in parallel, at most 5 agents at once.** Each email gets its own agent call. Emails from the same sender keep their arrival order, so a revised PO never overtakes its original, while different senders run concurrently.
- The ERP's 5-connection limit is enforced with a semaphore (4 connections).
- Verification reads everything from the ERP first and releases the connection before any model call.
- Graph and OpenAI calls retry when throttled.

**24. Inline images can be POs.** An inline image of at least 40 KB is treated as a possible document, such as a pasted screenshot of a PO. Smaller ones are signatures and logos.

**25. Server and client.** The Python code lives in `server/` (FastAPI) and the UI in `client/` (Vite, React, Tailwind). The client reaches the server through a `/api` proxy. The UI has three parts:
- **A dashboard** of emails: triage, processing state, result.
- **An order detail view in five steps:** the email, the customer's PO, the sales order (ERP and ack), the customer's terms as the agent read them, then the checks and what went wrong.
- **A floating chat.**

To make the detail view possible, each verification now stores the requirements ledger and a snapshot of the ERP data it compared against, so a reviewer sees exactly what the checks saw.

**26. Chatbot v1.** It is an LLM tool loop with read-only tools. Its definitions are shared with the verifier: open orders are O, I and H, and credit exposure is open orders plus a positive AR balance. A customer uses a credit line if its master terms or any open order is on net terms. Testing showed this matters: with master terms alone, Crescent (master says credit card, open order on Net 30) dropped out of the "over credit limit" answer while the verifier flagged it. A test now keeps the two in agreement. Detailed design comes later.

**27. The supplier side runs the same pipeline.** Our POs to suppliers (Sent Items) and suppliers' acknowledgements (Inbox) are triaged, verified and replied to like customer POs, through `verify_supplier_order` and its own checklist:
- **P1:** our PO document against the ERP PO.
- **P2–P4:** each line against the sales order line it buys for (part, quantity, due date against the customer's date, cost against the SO cost, margin).
- **P5:** the vendor's standing and terms, and new condition.
- **P6:** flow-down of the customer's date-code, CoC, traceability and DFARS/ITAR requirements.
- **P7:** the vendor's standing PO notes printed on the PO.
- **P8:** country of origin against the customer's restrictions.
- **P9–P10:** the supplier's acknowledgement against our PO, and its dates, origin and date codes against the customer's order.

Supporting choices:
- **Acknowledgement lines** are paired one-to-one: by line number when the part is the same or similar (a similar part is a substitution), then by part number.
- **Watching:** the watcher polls Sent Items as well as the Inbox. Sent mail keeps order per supplier.
- **Supplier quotes:** the `offers` table holds suppliers' quotes to us, mostly from 2024, so it is too old to judge a PO price and is not used. Supplier acknowledgements are not stored in the ERP at all; they exist only as email.
- **Testing:** the trial data has no supplier acknowledgements, so three synthetic ones are built from our real POs (`server/tests/synthetic_acks.py`): a clean one, one late with an old date code, and one with a substitution, a short quantity and a price change. They are described as changes to the PO, so no trial values are in the repository; the script can also write them as PDFs.

**13. Other agents' output in the shared mailbox was not read.** It contains other agents' conclusions about the same trial orders. Reading them could contaminate our evaluation.

**28. The agent may resolve review items, with evidence it saw.** The order-desk agent investigates `needs_review` and `unverifiable` items: it queries the ERP, searches the mailbox and reads the order documents, then gives a verdict through `resolve_review_item`. The rules, enforced in code (`server/agent/resolve.py`):
- **Which items:** only items currently `needs_review` or `unverifiable`. Failures, approvals and items already settled are out of reach.
- **Evidence:** every quote must appear verbatim in the verification result, the order documents, or the output of a tool the agent called while handling the email. A paraphrase is rejected and the agent has to quote again (this happened three times in the first live run).
- **Missing evidence is not a failure.** An `unverifiable` item the agent judges "not met" becomes `needs_review`, not `fail`. In the first live run the agent turned "no quote found", "origin only from the item default" and "our supplier PO is not in the mailbox" into failures. Each was really a missing record.
- The recommendation is recalculated, and the item keeps its original status and the agent's reasoning and evidence.

**29. A model judges clauses no check recognises, and code decides what its answer is worth.** After the code checks, each clause that the rules and the labeller could not map is read by a model against the customer PO, our ack and the ERP data (`server/agent/judge.py`, `JUDGE_MODEL`). Its verdict counts only on these terms:
- **Pass:** it says *met*, says it is *certain*, and every quote it gives appears verbatim in the order data, with at least one quote that is not the clause itself.
- **Fail:** it reports a *conflict* and grounds it the same way.
- **Anything else stays `needs_review`,** with the model's reading attached for the person. This covers clauses only met later (sourcing, shipping), legal terms a person must accept, and anything the data does not show.
The fallback matters most for uploads through `/verify`, which never reach the agent. None of the 14 trial orders has a clause left for the judge. In a test with planted clauses:
- "unit prices below USD 5.00" passed, quoting the three prices;
- "no line over 1,000 pieces" failed, quoting the quantities;
- "ISO 9001 registration" stayed with a person.

**30. Quarry's conditions of purchase, clause by clause.** Reviewing the 10 conditions showed four that were only noted. Each now has a check:
- **Card fees borne by the seller** conflict with our 3.5% card fee policy (1.2), so the item goes to review.
- **Foreign governing law or jurisdiction** (Japan, Tokyo courts) goes to review for a person in legal. US law is noted.
- **Packaging** (sealed original packaging, dry pack) is now part of the flow-down check against our supplier PO, like date code and CoC.
- **Export authorisation confirmed in writing** is listed as a carry-forward. The export review itself was already an approval.
"Governed by the Conditions of Purchase on page 2" is no longer flagged as unavailable external terms: when the reference points inside the document, those clauses were read one by one.

Still open: the late-delivery penalty (1% per week, capped at 10%) is noted, but not yet tied to the lines the supply check predicts will ship late.

**31. The agent gives the tool what it was missing; the code checks again.** The agent's per-item verdicts (28) are for genuine judgement calls. When the agent finds a missing input, it passes that input back to `verify_customer_order`, which runs the checks again. Two inputs so far:
- **`so_number`:** for when the PO does not cite its sales order, or the tool matched the wrong one.
- **`referenced_documents`:** the terms or quality clauses a PO refers to but does not contain. They come from a link in the PO, or from an email (attachment or body). They are read by the same rules, labeller and judge as terms printed in the PO, and each clause is checked under its own item. `R.external_terms` then records where the document came from.

Fetching is guarded (`server/agent/references.py`):
- The link must appear in the order documents or an email the agent read.
- http(s) only, to public addresses only, re-checked on every redirect.
- At most 10 MB, with a 20 s timeout.
- The content is data. It goes only through clause classification, never into instructions.

In the trial data:
- **Meridian and Northstar:** the links open. Northstar's 42 clauses were read, and its quality clauses Q2, Q5 and Q11 are in the same document.
- **Pinecrest:** the link returns 404 (the PO image shows the same URL). The item stays with a person, and the reason is now known.

**32. Accepting a term is a person's decision.** Governing law and card fees ask someone (legal, sales) to accept a term, not to establish a fact. The agent may add notes (`cannot_tell`) but cannot pass or fail them. In a live run the agent failed a governing-law item because "legal acceptance is needed". That item was itself a false flag: the check read "the laws of the State of Colorado govern this order" as a place called "Colorado Govern This Order". Both are fixed.

**33. The ERP check record carries the result itself, written once the order is settled.** It used to be planned inside every verification, which caused three problems:
- **Too early:** before the agent resolved items or decided on a hold, and once more for every batch run and re-verification.
- **Too thin:** a summary row with counts and a local file path, plus issues without evidence. Passes were left out.
- **Unreadable in the dry-run view:** it showed only the SQL template, with `%s` placeholders.

Now:
- **When:** `record_result` runs at the end of the agent's handling of an email, once per order. A re-verification with more inputs supersedes the first. It also runs when a person checks an upload. Batch and test runs do not write.
- **`agent_check_result`:**
  - the order and the recommendation, with its reasons;
  - the customer's terms as read;
  - the agent's summary;
  - whether the agent put the order on hold.
- **`agent_check_item`:** one row per check, passes included. Each row has the status, the evidence (what was compared, and where each value came from) and, for an item the agent or the clause judge resolved, its original status and the resolution.
- **Supplier POs** are recorded too, by PO number.
- **The dry-run log** keeps each statement's row, and the UI shows those values instead of SQL.

**34. The agent suggests what to do past the checks.** At the end of each email, the agent looks beyond the checklist using its read-only tools and records up to 5 suggestions with `add_suggestion`. Examples:
- **Sourcing:** a line we cannot cover, and who has sold us that part before (or that manufacturer's parts), with the latest price and origin, whether the vendor is active and not on pay hold, and whether we already have an open PO to them.
- **Allocation:** an open supplier PO line no order has claimed yet, or stock in another warehouse.
- **Pricing:** the SO's cost against what we last paid.
- **Also:** customer follow-ups, and master data that looks wrong.

Suggestions are advisory: they change no check, recommendation or ERP record. The rules:
- Each needs evidence, and every quote must appear in a tool output the agent saw.
- At most 6 per email.
- Each attaches to the order it concerns.
- Reprocessing an email replaces its open suggestions and keeps the ones a person marked done or dismissed.

They show on the dashboard and as step 6 of the order view, with Done and Dismiss buttons.

First live run:
- **Northstar:** for line 2 (720,000 pcs, 4,200 on hand, nothing on order) the agent suggested Nova Microelectronics (V1135), which sold us that exact part. For line 1, never bought, it suggested Tessera, an active Diodes supplier with an open PO.
- **Quarry:** for each short line it found non-China sources with recent prices below the SO cost.
