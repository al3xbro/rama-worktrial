// Types mirror the FastAPI server's JSON (server/agent/api.py). All calls go through the Vite /api proxy.

export type Val = { raw: string | null; loc: { page: number | null; where: string | null } | null }

export type DocLine = {
  line_no: Val
  customer_pn: Val
  mpn: Val
  manufacturer: Val
  description: Val
  qty: Val
  uom: Val
  unit_price: Val
  extended: Val
  required_date: Val
  ship_date: Val
  country_of_origin?: Val
  date_code?: Val
  line_notes: Val[]
}

export type DocHeader = { doc_type: string } & Record<string, Val | string>

export type OrderDoc = {
  header: DocHeader
  lines: DocLine[]
  notes: Val[]
  references: Val[]
  totals: { subtotal: Val; tax: Val; total: Val }
}

export type Extracted = { doc: OrderDoc; source: string; method: string; links: string[]; warnings: string[] }

export type Evidence = { label: string; value: string | null; source: string }

export type FindingStatus = 'pass' | 'fail' | 'needs_approval' | 'needs_review' | 'unverifiable' | 'manual' | 'info'

export type Finding = {
  check_id: string
  checklist_ref: string
  title: string
  status: FindingStatus
  severity: 'high' | 'medium' | 'low' | 'info'
  message: string
  evidence: Evidence[]
  line: number | null
  // Set when the agent (resolve_review_item) or the clause judge settled an item the code left open.
  original_status?: FindingStatus | null
  resolution?: { by: 'agent' | 'llm_judge'; verdict: string; reasoning: string; evidence: { quote: string; source: string }[] } | null
}

export type Requirement = { clause_type: string; params: Record<string, unknown>; quote: string; source: string; scope: string }

export type ChecklistRow = {
  ref: string
  section: string
  text: string
  added: boolean
  status: FindingStatus
  findings: number
  automated: boolean
  manual_reason: string | null
}

export type Run = {
  id: number
  message_id: string | null
  kind?: 'customer' | 'supplier' | null
  po_number?: number | null
  so_number: number | null
  cust_no: string | null
  cust_po: string | null
  customer: string | null
  recommendation: string
  counts: Record<string, number>
  revision: Revision | null
  report_path: string | null
  created: string
}

export type Revision = {
  detected: boolean
  how: string
  previous_source?: string
  changes: { where: string; field: string; old: string | null; new: string | null; change?: string }[] | null
  issues?: { resolved: string[]; new: string[]; still_open: string[] }
}

export type Email = {
  id: string
  received: string | null
  sender: string | null
  subject: string | null
  has_attachments: number | null
  label: string | null
  reason_category: string | null
  triage_confidence: number | null
  triage_reasons: string | null
  state: string
  recommendation: string | null
  error: string | null
  updated: string | null
  folder?: 'inbox' | 'sentitems' | null
}

export type EmailDetail = Email & {
  runs: Run[]
  agent?: { final: string | null; steps: { tool: string; args: Record<string, unknown> }[] }
  reply_draft?: { to: string; subject: string; body: string; dry_run: boolean }
}

export type ErpRow = Record<string, string | number | boolean | null>

export type ErpSnapshot = {
  so: ErpRow | null
  lines: ErpRow[]
  customer: ErpRow | null
  credit: { open_orders: number | null; ar_balance: number | null }
  quote: ErpRow | null
  supply: ErpRow[]
}

// The supplier side's ERP snapshot: our PO, its lines, the vendor, and the sales orders it buys for.
export type SupplierErp = {
  po: ErpRow | null
  po_lines: ErpRow[]
  vendor: ErpRow | null
  sales_orders: { so: ErpRow; customer: ErpRow; lines: ErpRow[] }[]
}

export type ErpStatement = { table?: string; row?: Record<string, unknown>; sql: string; params?: unknown[] }
export type ErpAction = {
  id: number
  kind: string
  so_number: number | null
  dry_run: boolean
  message_id: string | null
  detail: { statements: ErpStatement[]; note?: string; run_id?: number; po_number?: number; result?: Record<string, unknown> }
  created: string
}

export type RunDetail = {
  run: Run
  email: (Partial<EmailDetail> & { agent?: EmailDetail['agent']; reply_draft?: EmailDetail['reply_draft'] }) | null
  po: Extracted | null
  ack: Extracted | null
  erp: ErpSnapshot | SupplierErp | null
  requirements: Requirement[]
  findings: Finding[]
  checklist: ChecklistRow[]
  recommendation: string
  reasons: string[]
  revision: Revision | null
  problems: string[]
  erp_actions: ErpAction[]
  suggestions: Suggestion[]
}

export type Status = {
  running: boolean
  poll_seconds: number
  watermark: string | null
  last_poll: string | null
  last_error: string | null
  messages: Record<string, number>
  workers: number
  in_progress_or_queued: number
  dry_run: boolean
  erp_dry_run: boolean
  reply_to: string
}

export type SuggestionStatus = 'open' | 'done' | 'dismissed'
export type Suggestion = {
  id: number
  message_id: string | null
  run_id: number | null
  so_number: number | null
  po_number: number | null
  kind: string
  priority: 'high' | 'medium' | 'low'
  title: string
  detail: string
  action: string
  evidence: { quote: string; source: string }[]
  status: SuggestionStatus
  created: string
}

export type ChatReply ={ session_id: string; answer: string; tools: { tool: string; args: Record<string, unknown> }[] }

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`/api${path}`)
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
  return r.json() as Promise<T>
}

async function post<T>(path: string, body?: unknown, method = 'POST'): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
  return r.json() as Promise<T>
}

export const api = {
  status: () => get<Status>('/status'),
  emails: (limit = 300) => get<Email[]>(`/emails?limit=${limit}`),
  email: (id: string) => get<EmailDetail>(`/emails/${encodeURIComponent(id)}`),
  orders: () => get<Run[]>('/orders'),
  runDetail: (runId: number) => get<RunDetail>(`/runs/${runId}/detail`),
  process: (id: string) => post<{ queued?: string; skipped?: string }>(`/emails/${encodeURIComponent(id)}/process`),
  poll: () => post<{ queued: boolean }>('/poll'),
  chat: (message: string, sessionId: string | null) => post<ChatReply>('/chat', { message, session_id: sessionId }),
  uploads: () => get<Run[]>('/uploads'),
  suggestions: (status: SuggestionStatus | '' = 'open') => get<Suggestion[]>(`/suggestions?status=${status}`),
  setSuggestion: (id: number, status: SuggestionStatus) => post<Suggestion>(`/suggestions/${id}`, { status }, 'PATCH'),
  upload: async (doc: File, second: File | null): Promise<{ run_id: number | null; recommendation: string }> => {
    const form = new FormData()
    form.append('po', doc)
    if (second) form.append('ack', second)
    const r = await fetch('/api/verify', { method: 'POST', body: form })
    if (!r.ok) throw new Error(((await r.json().catch(() => null)) as { detail?: string } | null)?.detail ?? `${r.status}`)
    return r.json()
  },
}
