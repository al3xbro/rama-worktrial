// Pieces shared by the customer-order and supplier-PO detail views.
import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api'
import type { DocLine, ErpAction, Extracted, Finding, Requirement, RunDetail, Suggestion, SuggestionStatus, Val } from '../api'
import { Card, FindingBadge, KeyValues, LabelBadge, RecommendationBadge, Table, fmtDate } from '../ui'

export function V({ v }: { v: Val | string | undefined | null }) {
  if (v === null || v === undefined) return <span className="text-slate-400">—</span>
  if (typeof v === 'string') return <>{v}</>
  if (!v.raw) return <span className="text-slate-400">—</span>
  const where = v.loc ? [v.loc.page ? `p${v.loc.page}` : '', v.loc.where ?? ''].filter(Boolean).join(' · ') : ''
  return (
    <span title={where ? `Read from ${where}` : undefined} className="whitespace-pre-line">
      {v.raw}
    </span>
  )
}

export function Step({ n, title, subtitle, children, tone = 'slate' }: { n: number; title: string; subtitle?: ReactNode; children: ReactNode; tone?: 'slate' | 'red' | 'emerald' }) {
  const dot = tone === 'red' ? 'bg-red-600' : tone === 'emerald' ? 'bg-emerald-600' : 'bg-slate-800'
  return (
    <li className="relative pl-12">
      <span className={`absolute left-0 top-0 flex h-8 w-8 items-center justify-center rounded-full text-sm font-semibold text-white ${dot}`}>{n}</span>
      <span className="absolute bottom-0 left-4 top-9 w-px bg-slate-200" aria-hidden />
      <div className="pb-8">
        <h2 className="text-base font-semibold text-slate-900">{title}</h2>
        {subtitle && <p className="mt-0.5 text-sm text-slate-500">{subtitle}</p>}
        <div className="mt-3 space-y-3">{children}</div>
      </div>
    </li>
  )
}

const HEADER_FIELDS: [string, string][] = [
  ['doc_number', 'Number'],
  ['doc_date', 'Date'],
  ['customer_po_ref', 'PO referenced'],
  ['customer_name', 'Customer'],
  ['customer_id', 'Customer id'],
  ['addressed_to', 'Addressed to'],
  ['buyer_name', 'Buyer'],
  ['buyer_email', 'Buyer email'],
  ['payment_terms', 'Payment terms'],
  ['ship_via', 'Ship via'],
  ['incoterm', 'Shipping terms'],
  ['carrier_account', 'Freight account'],
  ['freight_terms', 'Freight terms'],
  ['ship_option', 'Ship option'],
  ['currency', 'Currency'],
  ['ship_to', 'Ship to'],
  ['bill_to', 'Bill to'],
]

const DOC_TITLES: Record<string, string> = {
  po: "Customer's purchase order",
  ack: 'Our sales order acknowledgement',
  supplier_po: 'Our purchase order to the supplier',
  supplier_ack: "Supplier's acknowledgement",
}

export function DocumentView({ x, kind }: { x: Extracted; kind: 'po' | 'ack' | 'supplier_po' | 'supplier_ack' }) {
  const h = x.doc.header
  const rows = HEADER_FIELDS.filter(([k]) => {
    const v = h[k]
    return typeof v === 'object' && v?.raw
  }).map(([k, label]) => [label, <V v={h[k] as Val} />] as [string, ReactNode])
  const dated = kind === 'ack' || kind === 'supplier_ack'
  const origin = x.doc.lines.some((l) => l.country_of_origin?.raw || l.date_code?.raw)
  const lineRows = x.doc.lines.map((l: DocLine, i) => [
    <V v={l.line_no.raw ? l.line_no : String(i + 1)} />,
    <span className="font-mono text-xs"><V v={l.mpn} /></span>,
    <V v={l.manufacturer} />,
    <V v={l.customer_pn} />,
    <span className="tabular-nums"><V v={l.qty} /></span>,
    <span className="tabular-nums"><V v={l.unit_price} /></span>,
    <span className="tabular-nums"><V v={l.extended} /></span>,
    <V v={l.required_date} />,
    ...(dated ? [<V v={l.ship_date} />] : []),
    ...(origin ? [<V v={l.country_of_origin} />, <V v={l.date_code} />] : []),
  ])
  const head = ['Line', 'Part number', 'Manufacturer', 'Cust. part', 'Qty', 'Unit price', 'Extended', 'Required',
    ...(dated ? [kind === 'supplier_ack' ? 'Promised' : 'Est. ship'] : []), ...(origin ? ['Origin', 'Date code'] : [])]
  return (
    <Card
      title={DOC_TITLES[kind]}
      subtitle={<>{x.source} · read as {x.method}{x.links.length ? <> · links: {x.links.map((l) => <a key={l} href={l} target="_blank" rel="noreferrer" className="ml-1 text-blue-700 underline">{new URL(l).hostname}</a>)}</> : null}</>}
    >
      <KeyValues rows={rows} />
      <div className="mt-4"><Table head={head} rows={lineRows} /></div>
      {x.doc.notes.length > 0 && (
        <div className="mt-4">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Notes and conditions</h4>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-slate-700">{x.doc.notes.map((n, i) => <li key={i}><V v={n} /></li>)}</ul>
        </div>
      )}
      {x.warnings.length > 0 && <p className="mt-3 rounded bg-amber-50 px-3 py-2 text-xs text-amber-800">Extraction warnings: {x.warnings.join('; ')}</p>}
    </Card>
  )
}

export function EmailCard({ d, emptyText }: { d: RunDetail; emptyText: string }) {
  if (!d.email) return <p className="text-sm text-slate-500">{emptyText}</p>
  const e = d.email
  return (
    <Card title={e.subject ?? '(no subject)'} right={<LabelBadge value={e.label ?? null} />}>
      <KeyValues
        rows={[
          ['From', e.sender ?? '—'],
          [e.folder === 'sentitems' ? 'Sent' : 'Received', fmtDate(e.received)],
          ['Triage', <>{(e.label ?? '').replace(/_/g, ' ')} {e.triage_confidence != null && <span className="text-slate-400">({Math.round(e.triage_confidence * 100)}%)</span>}</>],
          ['Why', e.triage_reasons ?? '—'],
          ['Agent steps', e.agent?.steps.map((s) => s.tool).join(' → ') ?? '—'],
        ]}
      />
      {e.reply_draft && (
        <details className="mt-3 rounded-lg bg-slate-50 px-3 py-2">
          <summary className="cursor-pointer text-sm font-medium text-slate-700">
            Reply drafted to {e.reply_draft.to}{e.reply_draft.dry_run ? ' (dry run, not sent)' : ''}
          </summary>
          <p className="mt-2 text-sm font-medium">{e.reply_draft.subject}</p>
          <pre className="mt-1 whitespace-pre-wrap font-sans text-sm text-slate-700">{e.reply_draft.body}</pre>
        </details>
      )}
    </Card>
  )
}

export function RequirementsTable({ reqs, empty }: { reqs: Requirement[]; empty: string }) {
  return (
    <Table
      head={['Clause', 'Details', 'Text', 'Source']}
      rows={reqs.map((r) => [
        <span className="font-medium">{r.clause_type.replace(/_/g, ' ')}</span>,
        <span className="text-xs text-slate-600">
          {Object.entries(r.params).filter(([, v]) => v !== null && v !== undefined && v !== '').map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : String(v)}`).join(' · ') || '—'}
        </span>,
        <span className="text-slate-700">{r.quote}</span>,
        <span className="text-xs text-slate-500">{r.source} · {r.scope}</span>,
      ])}
      empty={empty}
    />
  )
}

function FindingItem({ f }: { f: Finding }) {
  const [open, setOpen] = useState(false)
  return (
    <li className="rounded-lg border border-slate-200 bg-white">
      <button onClick={() => setOpen(!open)} className="flex w-full items-start gap-3 px-3 py-2 text-left">
        <FindingBadge value={f.status} />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-slate-900">
            {f.title} <span className="font-normal text-slate-400">· checklist {f.checklist_ref}{f.severity !== 'info' ? ` · ${f.severity}` : ''}</span>
          </p>
          <p className="text-sm text-slate-600">{f.message}</p>
          {f.resolution && (
            <p className="mt-1 text-xs text-indigo-700">
              {f.resolution.by === 'agent' ? 'Resolved by the agent' : 'Judged by the clause judge'}
              {f.original_status ? ` (was ${f.original_status.replace('_', ' ')})` : ''}: {f.resolution.verdict.replace('_', ' ')}
              {f.resolution.by === 'agent' && f.resolution.reasoning ? ` — ${f.resolution.reasoning}` : ''}
            </p>
          )}
        </div>
        <span className="text-xs text-slate-400">{open ? 'Hide' : 'Evidence'}</span>
      </button>
      {open && (f.evidence.length > 0 || (f.resolution?.evidence.length ?? 0) > 0) && (
        <div className="border-t border-slate-100 px-3 py-2">
          <Table
            head={['', 'Value', 'Where it came from']}
            rows={[
              ...f.evidence.map((e) => [<span className="font-medium text-slate-700">{e.label}</span>, <span className="whitespace-pre-line">{e.value ?? '—'}</span>, <span className="text-xs text-slate-500">{e.source}</span>]),
              ...(f.resolution?.evidence ?? []).map((e) => [<span className="font-medium text-indigo-700">{f.resolution!.by === 'agent' ? 'Agent' : 'Judge'}</span>, <span className="whitespace-pre-line">{e.quote}</span>, <span className="text-xs text-slate-500">{e.source}</span>]),
            ]}
          />
        </div>
      )}
    </li>
  )
}

const ISSUE_ORDER: Finding['status'][] = ['fail', 'needs_approval', 'needs_review', 'unverifiable']
const ISSUE_TITLES: Record<string, string> = {
  fail: 'Failed — must be fixed',
  needs_approval: 'Needs approval',
  needs_review: 'Needs a person to review',
  unverifiable: "Couldn't be verified",
}

export function useIssues(d: RunDetail | null) {
  return useMemo(() => {
    const out: Record<string, Finding[]> = {}
    for (const f of d?.findings ?? []) {
      const relevant = f.status !== 'unverifiable' || f.severity === 'high' || f.severity === 'medium'
      if (ISSUE_ORDER.includes(f.status) && relevant) (out[f.status] ??= []).push(f)
    }
    return out
  }, [d])
}

export function ChecksSection({ d, checklistName }: { d: RunDetail; checklistName: string }) {
  const issues = useIssues(d)
  const [showChecklist, setShowChecklist] = useState(false)
  const carry = d.findings.filter((f) => f.status === 'manual')
  return (
    <>
      <div className="rounded-xl border border-slate-200 bg-white px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">Recommendation</span>
          <RecommendationBadge value={d.recommendation} />
        </div>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-700">{d.reasons.slice(0, 8).map((r, i) => <li key={i}>{r}</li>)}</ul>
        {d.problems.length > 0 && <p className="mt-2 text-xs text-amber-700">Notes: {d.problems.join('; ')}</p>}
      </div>

      {d.revision && (
        <Card title="Revision of an earlier PO" subtitle={d.revision.how}>
          <Table head={['Where', 'Field', 'Was', 'Now']} rows={(d.revision.changes ?? []).map((c) => [c.where, c.field, c.old ?? '—', c.new ?? '—'])} empty="No differences." />
          {d.revision.issues && (
            <div className="mt-3 grid gap-2 text-sm sm:grid-cols-3">
              {(['resolved', 'new', 'still_open'] as const).map((k) => (
                <div key={k}>
                  <p className="font-medium capitalize">{k.replace('_', ' ')}</p>
                  <ul className="list-disc pl-5 text-slate-600">{(d.revision?.issues?.[k] ?? []).map((x) => <li key={x}>{x.split(':')[0]}</li>)}</ul>
                </div>
              ))}
            </div>
          )}
        </Card>
      )}

      {ISSUE_ORDER.filter((s) => issues[s]?.length).map((s) => (
        <div key={s}>
          <h3 className="mb-2 text-sm font-semibold text-slate-800">{ISSUE_TITLES[s]} ({issues[s].length})</h3>
          <ul className="space-y-2">{issues[s].map((f, i) => <FindingItem key={`${f.check_id}-${f.line}-${i}`} f={f} />)}</ul>
        </div>
      ))}

      {carry.length > 0 && (
        <details className="rounded-xl border border-slate-200 bg-white px-4 py-3">
          <summary className="cursor-pointer text-sm font-semibold">Carry forward when sourcing and shipping ({carry.length})</summary>
          <ul className="mt-2 space-y-1 text-sm text-slate-700">{carry.map((f, i) => <li key={i}><span className="font-medium">{f.title}</span> — {f.message}</li>)}</ul>
        </details>
      )}

      <div className="rounded-xl border border-slate-200 bg-white px-4 py-3">
        <button onClick={() => setShowChecklist(!showChecklist)} className="text-sm font-semibold text-slate-800">
          {showChecklist ? 'Hide' : 'Show'} {checklistName} ({d.checklist.length} items)
        </button>
        {showChecklist && (
          <div className="mt-3">
            <Table
              head={['Ref', 'Item', 'Status', 'Checks']}
              rows={d.checklist.map((c) => [c.ref, <span>{c.text}{c.added && <span className="text-slate-400"> (added)</span>}</span>, <FindingBadge value={c.status} />,
                c.automated ? String(c.findings) : <span className="text-xs text-slate-500">not automated: {c.manual_reason ?? 'no check applies'}</span>])}
            />
          </div>
        )}
      </div>

      {d.erp_actions.length > 0 && (
        <details className="rounded-xl border border-slate-200 bg-white px-4 py-3">
          <summary className="cursor-pointer text-sm font-semibold">
            ERP writes ({d.erp_actions.length}{d.erp_actions.some((a) => a.dry_run) ? ', dry run: planned, not executed' : ''})
          </summary>
          <ul className="mt-3 space-y-4">
            {d.erp_actions.map((a) => <ErpActionView key={a.id} a={a} />)}
          </ul>
        </details>
      )}
    </>
  )
}

export function issueCount(d: RunDetail): number {
  return d.findings.filter((f) => ['fail', 'needs_approval', 'needs_review'].includes(f.status) || (f.status === 'unverifiable' && f.severity !== 'low' && f.severity !== 'info')).length
}

// ---------------------------------------------------------------- ERP writes

function cell(v: unknown): ReactNode {
  if (v === null || v === undefined || v === '') return <span className="text-slate-400">—</span>
  if (typeof v === 'boolean') return v ? 'yes' : 'no'
  if (typeof v === 'object') return <span className="font-mono text-[11px]">{JSON.stringify(v)}</span>
  return String(v)
}

function evidenceText(ev: unknown): ReactNode {
  if (!Array.isArray(ev) || !ev.length) return <span className="text-slate-400">—</span>
  return (
    <ul className="space-y-0.5">
      {(ev as { label: string; value: string | null; source: string }[]).map((e, i) => (
        <li key={i}><span className="text-slate-500">{e.label}:</span> {e.value ?? '—'} <span className="text-slate-400">({e.source})</span></li>
      ))}
    </ul>
  )
}

function ErpActionView({ a }: { a: ErpAction }) {
  const [sql, setSql] = useState(false)
  const head = a.detail.statements[0]
  const items = a.detail.statements.filter((s) => s.table === 'agent_check_item')
  const rest = a.detail.statements.filter((s) => s.table !== 'agent_check_item')
  return (
    <li className="text-xs">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-sm font-medium">{a.kind === 'record_result' ? 'Record check result' : a.kind === 'hold_order' ? 'Put order on hold' : a.kind}</span>
        <span className={a.dry_run ? 'text-amber-700' : 'text-emerald-700'}>{a.dry_run ? 'planned, not executed' : 'applied'}</span>
        <span className="text-slate-400">{fmtDate(a.created)}</span>
        <button className="ml-auto text-slate-500 underline" onClick={() => setSql(!sql)}>{sql ? 'hide SQL' : 'show SQL'}</button>
      </div>
      {a.detail.note && <p className="mt-0.5 text-slate-500">{a.detail.note}</p>}
      {head?.row === undefined ? (
        <pre className="mt-1 overflow-x-auto rounded bg-slate-50 p-2 text-[11px] text-slate-600">{head?.sql}</pre>
      ) : (
        <div className="mt-2 space-y-3">
          {rest.map((s, i) => (
            <div key={i}>
              <p className="mb-1 font-mono text-[11px] text-slate-500">worktrial.{s.table}</p>
              <KeyValues rows={Object.entries(s.row ?? {}).map(([k, v]) => [k, k === 'agent_summary' ? <span className="whitespace-pre-line">{String(v ?? '—')}</span> : cell(v)])} />
            </div>
          ))}
          {items.length > 0 && (
            <div>
              <p className="mb-1 font-mono text-[11px] text-slate-500">worktrial.agent_check_item ({items.length} rows, one per check)</p>
              <Table
                head={['Ref', 'Check', 'Line', 'Status', 'Compared (value and source)', 'Resolved by']}
                rows={items.map((s) => {
                  const r = s.row ?? {}
                  return [cell(r.checklist_ref), <span><span className="font-mono text-slate-500">{String(r.check_id)}</span> {String(r.title)}</span>, cell(r.line),
                    <span><FindingBadge value={String(r.status)} />{r.original_status ? <span className="text-slate-400"> was {String(r.original_status)}</span> : null}</span>,
                    evidenceText(r.evidence), cell(r.resolved_by)]
                })}
              />
            </div>
          )}
        </div>
      )}
      {sql && (
        <pre className="mt-2 max-h-64 overflow-auto rounded bg-slate-50 p-2 text-[11px] text-slate-600">
          {a.detail.statements.map((s) => `${s.sql};\n  -- ${JSON.stringify(s.params ?? [])}`).join('\n')}
        </pre>
      )}
    </li>
  )
}

// ---------------------------------------------------------------- suggestions

const KIND_LABEL: Record<string, string> = {
  sourcing: 'Sourcing', allocation: 'Allocation', pricing: 'Pricing', customer: 'Customer', supplier: 'Supplier', data_fix: 'Data fix', other: 'Other',
}
const PRIORITY_CLS: Record<string, string> = {
  high: 'bg-red-50 text-red-700 ring-red-200', medium: 'bg-amber-50 text-amber-800 ring-amber-200', low: 'bg-slate-100 text-slate-600 ring-slate-200',
}

export function SuggestionCard({ s, onChange }: { s: Suggestion; onChange: (s: Suggestion) => void }) {
  const [busy, setBusy] = useState(false)
  const set = async (status: SuggestionStatus) => {
    setBusy(true)
    try {
      onChange(await api.setSuggestion(s.id, status))
    } finally {
      setBusy(false)
    }
  }
  const closed = s.status !== 'open'
  return (
    <li className={`rounded-xl border border-slate-200 bg-white px-4 py-3 ${closed ? 'opacity-60' : ''}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${PRIORITY_CLS[s.priority] ?? PRIORITY_CLS.low}`}>{s.priority}</span>
        <span className="text-[11px] uppercase tracking-wide text-slate-500">{KIND_LABEL[s.kind] ?? s.kind}</span>
        {s.so_number && <span className="text-[11px] text-slate-400">SO {s.so_number}</span>}
        {s.po_number && <span className="text-[11px] text-slate-400">PO {s.po_number}</span>}
        <span className="ml-auto flex gap-2 text-xs">
          {closed ? (
            <>
              <span className="text-slate-500">{s.status}</span>
              <button disabled={busy} className="text-slate-500 underline" onClick={() => set('open')}>reopen</button>
            </>
          ) : (
            <>
              <button disabled={busy} className="rounded-md bg-slate-900 px-2 py-0.5 text-white" onClick={() => set('done')}>Done</button>
              <button disabled={busy} className="rounded-md px-2 py-0.5 text-slate-600 ring-1 ring-slate-200" onClick={() => set('dismissed')}>Dismiss</button>
            </>
          )}
        </span>
      </div>
      <p className="mt-1.5 text-sm font-medium text-slate-900">{s.title}</p>
      <p className="mt-1 text-sm text-slate-700">{s.detail}</p>
      <p className="mt-1.5 text-sm"><span className="font-medium text-slate-900">Next step:</span> <span className="text-slate-700">{s.action}</span></p>
      {s.evidence.length > 0 && (
        <details className="mt-1.5 text-xs">
          <summary className="cursor-pointer text-slate-500">Based on ({s.evidence.length})</summary>
          <ul className="mt-1 space-y-0.5">
            {s.evidence.map((e, i) => <li key={i}><span className="font-mono text-[11px]">{e.quote}</span> <span className="text-slate-400">({e.source})</span></li>)}
          </ul>
        </details>
      )}
    </li>
  )
}

export function SuggestionsSection({ items }: { items: Suggestion[] }) {
  const [list, setList] = useState(items)
  if (!list.length) return <p className="text-sm text-slate-500">The agent had no suggestions for this order.</p>
  return (
    <ul className="space-y-2">
      {list.map((s) => <SuggestionCard key={s.id} s={s} onChange={(n) => setList(list.map((x) => (x.id === n.id ? { ...x, status: n.status } : x)))} />)}
    </ul>
  )
}
