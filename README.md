# Order desk agent (Demo Distributor)

An agent for the order desk of Demo Distributor, an electronic component distributor. It watches the
order desk mailbox (`test@tryrama.com`) and handles two kinds of order:

- **Customer purchase orders.** Each is checked against our sales order in the ERP, the customer's terms and Demo's order
  checklist.
- **Our purchase orders to suppliers, and the suppliers' acknowledgements.** Each is checked against the ERP PO, the sales
  order it buys for and that customer's terms.

For every check it reports what was compared, where each value came from, and the result. That result is one of: passed,
failed, needs approval, needs a person to review, or couldn't be verified. The agent then:
- investigates what the checks couldn't settle;
- holds the order if it should be held;
- suggests next steps, such as who could supply a part we're short of;
- drafts a reply to the order desk.

A dashboard shows every email and how it was handled, and a chatbot answers questions from the ERP.

- [PLAN.md](PLAN.md): the plan, what we found in the data, open questions.
- [DECISIONS.md](DECISIONS.md): why it is built this way (34 decisions, with what testing showed).

## Contents

- [What it does](#what-it-does)
- [Results](#results)
- [Setup](#setup)
- [Run](#run)
- [How to use](#how-to-use)
- [Commands](#commands)
- [How it works](#how-it-works)
- [Code map](#code-map)
- [API](#api)
- [Configuration](#configuration)
- [What it writes](#what-it-writes)
- [Tests](#tests)
- [Safety](#safety)
- [Known limits](#known-limits)

## What it does

**Email intake**
- Watches the Inbox (customer POs, supplier acknowledgements) and Sent Items (our POs to suppliers).
- A model triages every email: customer PO, PO revision, our PO to a supplier, supplier acknowledgement, not a PO, or unsure.
  Only order emails go to the agent. Mail sent by the mailbox itself and listed senders are skipped.
- Reads PO documents in any of these forms:
  - PDFs, with text or scanned;
  - images, including screenshots pasted into the email body;
  - Excel, Word and attached `.eml` files;
  - POs typed in the email body.
- Handles several POs in one email, and several versions of the same PO (a revision).
- Runs up to 5 agents at once, one per email. Emails from the same customer or supplier keep their order, so a revision
  never overtakes its original.

**Customer PO verification (`verify_customer_order`, code-driven)**
- **Extraction:** the PO and our SO acknowledgement are read into a fixed schema. Values are copied verbatim, each with
  where it was read. Every value is checked against the document text, and quantity × price must equal the extended price
  (that check also detects decimal commas). On a problem, one retry.
- **The customer's terms:** customer-master flags and every clause in the PO become typed requirements (the requirements
  ledger):
  - rules first;
  - a model labels what the rules miss;
  - a model judge rules on what no check covers.
  
  The judge can clear a clause only if it is certain and quotes order data that proves it.
- **Checks:** 19 checks plus 43 clause verifiers. They cover:
  - header: PO number, addresses, terms, Incoterm, carrier and account;
  - lines: part, manufacturer, quantity, price, dates;
  - the approved quote;
  - credit;
  - the customer's requirements, for example country of origin (from our supplier PO line, then lots in stock, then the
    item default), date codes, CoC and export;
  - stock and ready dates;
  - whether our supplier POs carry the customer's requirements (flow-down).
- **Rollup:** findings roll up to Demo's 33-item order checklist. Items that can't be checked from data say why.
- **Recommendation:**
  - *hold*: any medium or high failure;
  - *needs approval*: no such failure, but something needs approval;
  - *review*: something needs a person, or a medium/high item couldn't be verified;
  - *release*: none of these.
- **Revisions:** a new version of a PO is diffed against the last one. The result lists what changed, which issues were
  resolved and which are new.

**Supplier side (`verify_supplier_order`)**
- **Our PO to a supplier:**
  - against the ERP PO;
  - against the sales order lines it buys for (part, quantity, date, cost, margin);
  - against the vendor's record;
  - against the flow-down of that customer's requirements.
- **The supplier's acknowledgement:** against our PO, including substituted parts, short quantities, price changes and
  later dates, and against the customer's dates, origin limits and date-code limits. The supplier checklist has items P1–P10.

**The order-desk agent** (an LLM tool loop, one per email)
1. **Verify:** calls the verification tool for each order in the email.
2. **Fill gaps, then verify again:**
   - the right sales order number, when the PO doesn't cite it;
   - the terms or quality clauses the PO only refers to: it fetches the link in the PO, or finds them in an earlier email.
3. **Investigate what's left open:** it uses SQL, stock lookups, the mailbox and the documents, then resolves review items
   with quoted evidence (the rules are under [Safety](#safety)).
4. **Hold:** puts orders recommended for hold on hold (dry-run).
5. **Suggest:** up to 5 next steps past the checks, such as a vendor that has sold us a part we're short of, unclaimed open
   supply, or cost against what we last paid.
6. **Reply:** drafts a reply to the order desk.
7. **Record:** writes the settled result to the ERP: the recommendation and every check with its evidence (dry-run).

**Chatbot:** answers questions from the ERP: customers, credit exposure, order status, open issues, stock, and any read-only SQL.

**UI:**
- **Dashboard:** every email and how it was handled, plus open suggestions.
- **Order view:** one order step by step, from the email to the checks.
- **Rerun:** a button on each email.
- **Manual check:** upload documents to check them by hand.
- **Chat:** a floating chat window.

## Results

| What | Result |
|---|---|
| Verification on the 14 trial customer orders, against a hand-built answer key | 58 of 58 expected findings, no unexpected issues (`batch` then `eval`) |
| Triage over the whole Inbox, when last measured | 14 of 14 customer POs caught, none missed. 2 other emails were passed to the agent: a HEIC photo and a handwritten note |
| End to end | 14 real emails in about 2 minutes with 5 agents; 3 real supplier PO emails from Sent Items |
| Tests | 83 pass. Without the ERP or the trial data, the tests that need them skip |

## Setup

You need:
- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12);
- Node 20.19+ or 22.12+, which Vite 8 needs, and [Yarn](https://yarnpkg.com/);
- an OpenAI API key;
- a Microsoft Graph app registration with application permissions `Mail.ReadWrite` and `Mail.Send` on the mailbox;
- the ERP's Postgres credentials;
- the trial documents in `data/order_docs/`. They're used for SO acknowledgements and supplier PO PDFs that an email
  doesn't carry, the batch and eval, and most tests.

```bash
cp .env.example .env          # fill in the OpenAI, Graph and Postgres values (see Configuration)
cd server && uv sync          # Python dependencies
cd ../client && yarn install  # UI dependencies
```

## Run

Two terminals:

```bash
# 1. Server, from server/: the API on http://127.0.0.1:8000 (interactive docs at /docs), watching the mailbox
uv run python -m agent serve --watch

# 2. UI, from client/: http://localhost:5173 (it calls the server through /api)
yarn dev
```

- **Watching is off without `--watch`.** You can turn it on and off later with `POST /watcher/start` and `/watcher/stop`.
- **Older mail isn't picked up.** On its first run the watcher starts from "now", so mail already in the mailbox is never
  processed by accident. To process earlier Inbox mail, replay a window:

  ```bash
  uv run python -m agent watch --once --since 2026-08-31T00:00:00Z   # from server/
  ```
- **Restart after code changes.** The server does not reload code.

## How to use

### 1. Get an order in

You can bring an order in three ways:
- **Email it** to `test@tryrama.com`: a PO attached in any supported format, or typed in the body. The watcher picks it up
  within `POLL_SECONDS` (30 s). **Check inbox now** on the dashboard polls straight away.
- **Upload it** under **Checked manually** on the dashboard. You can upload:
  - a customer PO, optionally with our SO acknowledgement;
  - our PO to a supplier and/or the supplier's acknowledgement.
  
  Uploads are checked by the verification tool alone, without the agent.
- **Replay** earlier mail with `watch --once --since …` (see [Run](#run)).

### 2. Read the dashboard

- **Stat cards:**
  - emails seen;
  - in progress, with up to 5 agents at once;
  - how many emails ended in each recommendation.
- **Filters:** All, Customer POs, Supplier POs, Needs attention, In progress, Ignored, Errors.
- **Columns:**
  - **In/out:** received, or sent by us;
  - **Triage:** what the email is;
  - **Processing:** queued, processing, done, ignored or error;
  - **Result:** the recommendation.
- **↻ on a row** runs that email again. It is re-read from the mailbox, so nobody has to resend it. Reruns join the same
  queue as new mail. Earlier results stay in the history.
- **Suggestions** lists the agent's open suggestions across orders. Mark each **Done** or **Dismiss** it.

### 3. Open an order

Click an email. If it holds several orders, there's a tab for each. **↻ Rerun** at the top runs it again; the panel refreshes
until the run is done.

A customer order reads in six steps:

1. **Purchase order email.** The email, how it was triaged, what the agent did (each tool call), and the reply it drafted.
2. **Customer's purchase order.** What the customer asked for, read verbatim. Hover a value to see where it was read.
3. **Sales order.** What we entered: the ERP sales order, and the acknowledgement we send the customer.
4. **Customer terms, as the agent read them.** Each clause with its source and the type it was read as.
5. **Checks and what went wrong.**
   - The recommendation and the reasons for it.
   - Each issue with its evidence; click **Evidence** for the values compared and where each came from.
   - Items the agent or the clause judge settled show who settled it, the original status and why.
   - The requirements to carry forward to sourcing and shipping.
   - Demo's checklist.
   - **ERP writes**: the rows the record and any hold would write, with the SQL behind **show SQL**.
6. **Suggestions.** The agent's ideas past the checks, each with what it is based on.

A supplier PO reads the same way. Its steps are:
1. the email;
2. our PO;
3. what it buys for: the customer's order lines and terms;
4. the supplier's acknowledgement;
5. checks;
6. suggestions.

### 4. Act on it

| Recommendation | Meaning | Usually needed |
|---|---|---|
| **Hold** | A check failed | A revised PO from the customer, the customer's written acceptance, or a fix to our sales order |
| **Needs approval** | Policy needs a person's approval (e.g. a Yield account, restricted origin, export classification) | A manager's approval, or the customer's written approval |
| **Review** | Something a person must read, or a medium/high item couldn't be verified | Someone reads the flagged items |
| **Release** | Every automated check passed or is informational | Nothing; carry-forward requirements still apply when sourcing and shipping |

When the customer sends a revised PO, the agent processes it like any email. The result shows what changed, and which earlier
issues are resolved or still open. After you fix something in the ERP, use **↻ Rerun** on the email.

### 5. Ask the chatbot

Open the chat at the bottom right. For example:
- "Which customers are over their credit limit?"
- "What's open for Quarry?"
- "Do we have stock of 14D22110201D?"
- "Which orders are on hold, and why?"

Answers come from the ERP and the verification results. Follow-up questions keep the conversation's context.

## Commands

From `server/`:

| Command | What it does |
|---|---|
| `uv run python -m agent serve [--watch] [--port 8000]` | The API, optionally watching the mailbox |
| `uv run python -m agent watch [--once] [--since ISO] [--interval N]` | Watch the mailbox without the API; `--once` polls once and waits for the agents |
| `uv run python -m agent process --message-id ID` | Triage and run the agent on one email |
| `uv run python -m agent agent --message-id ID` or `--po FILE [--ack FILE]` | Run the agent on an email, or on local files as if they had been emailed |
| `uv run python -m agent verify --po FILE [--ack FILE] [--so N] [--previous FILE]` | The verification tool alone, on documents |
| `uv run python -m agent batch [--only 01,05]` | Verify the 14 trial customer orders from `data/order_docs/` |
| `uv run python -m agent eval` | Score the latest batch run against `eval/answer_key.json` |
| `uv run python -m agent triage-eval [--since ISO]` | Triage the whole Inbox and compare with the known customer POs |
| `uv run python -m agent mail [--top 20]` | List recent mail with message ids |
| `uv run python tests/synthetic_acks.py OUT_DIR` | Write the synthetic supplier acknowledgements as PDFs, to upload or email |
| `uv run pytest` | The tests |

## How it works

```
Inbox + Sent Items ─► watcher (up to 5 agents at once; the same customer or supplier keeps its order)
      └► triage: customer PO · revision · our PO to a supplier · supplier acknowledgement · not a PO
           └► order-desk agent (LLM tool loop)
                ├ verify_customer_order ─ extract PO + ack (verbatim, grounded, arithmetic-checked)
                │                         └► bundle: SO, lines, customer, quote, credit, stock, lots, supplier POs (ERP, read-only)
                │                         └► requirements ledger: master flags + clauses (rules → labeller → judge)
                │                         └► checks + clause verifiers → findings with evidence
                │                         └► Demo's checklist rollup → recommendation, report, revision diff
                ├ verify_supplier_order ─ our PO / the supplier's ack vs the ERP PO, the SO lines, the customer's terms (P1–P10)
                ├ investigate: query_erp · lookup_inventory · search_mailbox · read_order_documents
                ├ re-verify with what was missing (so_number, referenced_documents)
                ├ resolve_review_item (evidence-checked) · hold_order (dry-run) · add_suggestion
                └ draft_reply (to the order desk, dry-run) ─► record the settled result in the ERP (dry-run)

chatbot (LLM tool loop): find_customers · customer_profile · credit_exposure · order_status · open_issues · lookup_inventory · query_erp
```

Where models are used, and how they are kept honest:
- **Code decides what runs.** Every check runs on every order; a model never chooses which checks to run.
- **Extraction** copies values verbatim. Code checks each value against the document text, and the arithmetic, before using it.
- **The labeller** only maps a clause to a known clause type. The verifier for that type is code.
- **The judge** rules only on clauses no check recognises. Code accepts its answer only if it quotes order data:
  - a pass needs certainty plus a quote;
  - a fail needs a grounded conflict;
  - anything else stays with a person.
- **The agent** can change only items the code left open (review, couldn't verify). Every quote it gives must appear in the
  documents, the verification result or its own tool outputs. "Not met" on missing evidence stays with a person. Accepting a
  term, such as governing law or card fees, is a person's call, not the agent's.

## Code map

| Path (`server/agent/`) | What it does |
|---|---|
| `config.py`, `llm.py` | Settings from `.env`; the one place that calls the model provider |
| `ingest.py`, `parse.py` | Documents to page text and images; extraction with grounding and retry; parsing of numbers, dates, terms, carriers, accounts |
| `db.py`, `erp.py` | ERP connection (read-only, capped) and lookups |
| `ledger.py` | Customer flags and clause text to typed requirements (rules, then the model labeller) |
| `judge.py` | The model judge for clauses no check recognises |
| `references.py` | Fetching documents a PO refers to (link or email), with the link guards |
| `bundle.py`, `checks/` | The order bundle, line matching, and every check (header, lines, quote, account, requirements, supply, flow-down) |
| `checklist.py`, `report.py` | Demo's checklist, the rollup and recommendation policy, the Markdown report |
| `revision.py` | Revision detection, diff, resolved and new issues |
| `verify.py` | The `verify_customer_order` tool |
| `supplier.py` | The `verify_supplier_order` tool and the supplier checklist (P1–P10) |
| `triage.py`, `watcher.py`, `mail.py` | Triage, mailbox watching and reruns, the Graph mailbox |
| `orchestrator.py` | The order-desk agent: instructions, tools, the loop, recording outcomes |
| `resolve.py` | The agent resolving review items, with its rules |
| `erp_write.py` | ERP writes (check record, holds); dry-run by default |
| `chat.py` | The chatbot |
| `state.py`, `api.py` | Processing state (SQLite) and the HTTP API |
| `evaluate.py`, `__main__.py` | The answer-key eval; the command line |

`client/src/`:
- **Components:** `Dashboard.tsx`, `OrderDetail.tsx` (customer order), `SupplierOrderDetail.tsx`, `detail.tsx` (the parts
  both share), `ChatWidget.tsx`.
- **Shared code:** `api.ts` (types and calls) and `ui.tsx` (badges and tables).

## API

Interactive docs are at `http://127.0.0.1:8000/docs`.

| Endpoint | What it does |
|---|---|
| `GET /health`, `GET /status`, `GET /config` | Liveness; watcher state, email counts and safety settings; models and mailbox |
| `POST /poll`, `POST /watcher/start`, `POST /watcher/stop` | Poll the mailbox now; turn watching on or off |
| `GET /emails`, `GET /emails/{id}` | Emails seen with triage, state and result. For one email: its runs, the agent's steps, the reply draft and suggestions |
| `POST /emails/{id}/process` | Run an email again (re-read from the mailbox, queued like new mail) |
| `GET /orders`, `GET /orders/{so}`, `GET /orders/{so}/report` | Latest verification of each order; the full result; the Markdown report |
| `GET /runs/{id}/detail` | One verification in reading order: email, PO, sales order, terms, findings, checklist, ERP writes, suggestions |
| `GET /issues?status=&severity=&so=` | Open issues across orders, most severe first |
| `GET /suggestions?status=&so=&po=`, `PATCH /suggestions/{id}` | The agent's suggestions (open by default); mark one done, dismissed or open |
| `GET /erp-actions?so=` | ERP writes made, or planned while `ERP_DRY_RUN=1`, with the rows they write |
| `POST /verify`, `GET /uploads` | Check uploaded documents; the latest upload of each order |
| `POST /chat`, `GET /chat/{session_id}` | Ask the chatbot (pass `session_id` to continue); a conversation's history |

## Configuration

Settings come from `.env` at the repository root; `.env.example` lists them all.

| Variable | Default | What it sets |
|---|---|---|
| `OPENAI_API_KEY` | | Model access |
| `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`, `GRAPH_MAILBOX` | mailbox `test@tryrama.com` | The mailbox (app-only Graph auth) |
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGOPTIONS` | | The ERP (`PGOPTIONS=-c search_path=worktrial`) |
| `EXTRACT_MODEL`, `AGENT_MODEL`, `JUDGE_MODEL`, `TRIAGE_MODEL` | gpt-5.5, gpt-5.5, the agent's, gpt-5.4-mini | Models for extraction, the agent and chatbot, the clause judge, triage and labelling |
| `REPLY_TO`, `DRY_RUN` | `test@tryrama.com`, `1` | Where replies go; `1` keeps them as drafts in the outbox |
| `ERP_DRY_RUN` | `1` | `1` plans ERP writes without executing them |
| `POLL_SECONDS`, `WATCH_WORKERS` | `30`, `5` | Polling interval; agents at once |
| `SUPPRESS_SENDERS` | | Exact sender addresses never processed (e.g. other testers in a shared mailbox) |
| `ACK_DIR` | `data/order_docs` | Where SO acknowledgements and our supplier PO PDFs are looked up when an email doesn't carry them (relative to the repository root) |

## What it writes

Everything goes under `server/store/`, which is git-ignored:

| Path | What |
|---|---|
| `runs/SO<n>/<time>/`, `runs/PO<n>/<time>/` | Each verification: `result.json`, `report.md`, and the extracted `po.json` / `ack.json` |
| `state.db` | Emails, runs, issues, ERP writes, suggestions, chat history |
| `agent_runs/` | Each agent run: every tool call and result |
| `outbox/` | Reply drafts (dry-run) |
| `cache/` | Model outputs (extraction, clause labels, judge), keyed by content, so repeats are free |
| `po_index.json` | Every customer PO version seen, for revision detection |

Two kinds of ERP write are planned, and executed only with `ERP_DRY_RUN=0`:
- `agent_check_result` and `agent_check_item`: one row per check, with its evidence;
- a hold: order status O/I to H, logged in `agent_order_hold`.

## Tests

```bash
cd server && uv run pytest
```

There are 83 tests:
- parsing and the terms ledger;
- every check on Meridian, end to end, plus mutation tests (change one value on the PO, and the right check must catch it);
- revisions, the checklist mapping and the email flow;
- the supplier side;
- agent resolution, the clause judge, referenced documents, suggestions and the ERP record;
- the watcher (the agent cap, sender order, reruns).

What they need:
- **The live ERP:** the tests that need it skip without the `PG*` variables.
- **The trial documents:** the tests that need them read from `data/order_docs/` through the extraction cache, and skip
  without them. No trial data is committed.
- **Supplier acknowledgements:** the trial data has none, so `tests/synthetic_acks.py` builds them from our real POs:
  - a clean one;
  - one late, with an old date code;
  - one with a substituted part, a short quantity and a higher price.

## Safety

- **ERP reads** use a read-only connection; the agent's SQL tool accepts one `SELECT` or `WITH` query, with a 200-row limit and a 20 s timeout.
- **ERP writes** are dry-run by default. They touch only our `agent_` tables and an order's status, O/I to H.
- **Replies** go only to `REPLY_TO` and stay in the outbox unless `DRY_RUN=0`. The mailbox is otherwise read-only: nothing
  is moved, deleted or marked.
- **Email content is untrusted:**
  - Extraction has no tools, and its output is schema-checked and grounded in the document.
  - Fetched documents only pass through clause classification.
  - A link is fetched only if it appears in the PO or an email the agent read, over http(s), to a public address, with
    size and time limits.
- **The agent's verdicts and suggestions** must quote what it actually saw. It can't clear failures or approvals.
- **Credentials** live in `.env`, which is git-ignored.

## Known limits

- **Late-delivery penalties** (e.g. 1% per week) are noted, but not yet tied to the lines the supply check predicts will
  ship late.
- **Pinecrest's referenced terms link returns 404.** That item stays with a person.
- **The agent can't preview a document.** It can pass a link into verification, but can't read a document's text first to
  confirm it's the right one.
- **Ready dates assume** lead time counts from the order date, so a late ready date is flagged for review, not as a failure.
- **A rerun goes through triage again**, so an email triage wrongly ignored stays ignored.
- **The ERP write statements have never run against Postgres** (dry-run only). The `worktrial` schema is shared, so give the
  `agent_` tables a prefix of our own before turning writes on.
- **Some customer-master flags are interpreted.** For example, `prt_coo` and `eccn_req` are read as "print on the shipping
  documents" and carried forward; PLAN.md lists the open questions.
