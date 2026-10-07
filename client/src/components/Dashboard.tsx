import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api'
import type { Email, EmailDetail, Run, Status, Suggestion } from '../api'
import { LabelBadge, RecommendationBadge, StateBadge, fmtDate } from '../ui'
import { OrderDetail } from './OrderDetail'
import { SuggestionCard } from './detail'

// Emails that came in, how each was triaged and processed, and what the agent recommended.
// Clicking an email opens the order detail for each order it contained.

type Filter = 'all' | 'orders' | 'supplier' | 'in_progress' | 'attention' | 'ignored' | 'error'

const FILTERS: { key: Filter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'orders', label: 'Customer POs' },
  { key: 'supplier', label: 'Supplier POs' },
  { key: 'attention', label: 'Needs attention' },
  { key: 'in_progress', label: 'In progress' },
  { key: 'ignored', label: 'Ignored' },
  { key: 'error', label: 'Errors' },
]

function matches(e: Email, f: Filter): boolean {
  switch (f) {
    case 'orders':
      return e.label === 'customer_po' || e.label === 'po_revision' || e.label === 'unsure'
    case 'supplier':
      return e.label === 'supplier_po' || e.label === 'supplier_ack'
    case 'in_progress':
      return ['queued', 'new', 'triaged', 'processing'].includes(e.state)
    case 'attention':
      return e.state === 'done' && !!e.recommendation && e.recommendation !== 'release'
    case 'ignored':
      return e.state === 'ignored'
    case 'error':
      return e.state === 'error'
    default:
      return true
  }
}

function StatCard({ label, value, hint, tone = 'slate' }: { label: string; value: number | string; hint?: string; tone?: string }) {
  const color: Record<string, string> = { slate: 'text-slate-900', red: 'text-red-700', amber: 'text-amber-700', emerald: 'text-emerald-700', indigo: 'text-indigo-700' }
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className={`mt-1 text-2xl font-semibold tabular-nums ${color[tone]}`}>{value}</p>
      {hint && <p className="mt-0.5 text-xs text-slate-500">{hint}</p>}
    </div>
  )
}

function Drawer({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <div className="absolute inset-0 bg-slate-900/30" onClick={onClose} />
      <div className="relative h-full w-full max-w-5xl overflow-y-auto bg-slate-50 shadow-2xl">
        <button onClick={onClose} className="sticky top-3 float-right mr-4 mt-3 rounded-full bg-white px-3 py-1 text-sm text-slate-600 shadow ring-1 ring-slate-200 hover:bg-slate-100">
          Close
        </button>
        <div className="p-6">{children}</div>
      </div>
    </div>
  )
}

const BUSY = ['queued', 'new', 'triaged', 'processing']
const RERUN_HELP = 'Run this email again: it is re-read from the mailbox (nothing is re-sent), triaged and given to the agent. Earlier results stay in the history; the reply is a dry-run draft.'

function RerunButton({ id, state, onQueued, compact = false }: { id: string; state: string; onQueued?: () => void; compact?: boolean }) {
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const running = BUSY.includes(state)
  const click = async (ev: React.MouseEvent) => {
    ev.stopPropagation()
    setBusy(true)
    try {
      const r = await api.process(id)
      setNote(r.skipped ?? null)
      onQueued?.()
    } catch (err) {
      setNote((err as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <span className="inline-flex items-center gap-2">
      <button
        onClick={click}
        disabled={busy || running}
        title={running ? 'Already queued or processing' : RERUN_HELP}
        className={compact
          ? 'rounded px-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700 disabled:opacity-30'
          : 'rounded-full px-3 py-1 text-sm text-slate-700 ring-1 ring-slate-200 hover:bg-slate-50 disabled:opacity-40'}
      >
        {compact ? '↻' : running ? 'Running…' : '↻ Rerun'}
      </button>
      {note && !compact && <span className="text-xs text-slate-500">{note}</span>}
    </span>
  )
}

function EmailPanel({ id }: { id: string }) {
  const [e, setE] = useState<EmailDetail | null>(null)
  const [runIdx, setRunIdx] = useState(0)
  const load = useCallback(() => api.email(id).then(setE), [id])
  useEffect(() => {
    setRunIdx(0)
    load()
  }, [load])
  // While a rerun is queued or processing, refresh until it is done.
  useEffect(() => {
    if (!e || !BUSY.includes(e.state)) return
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [e, load])
  if (!e) return <p className="text-sm text-slate-500">Loading…</p>
  const toolbar = (
    <div className="flex items-center justify-between gap-3 rounded-lg bg-white px-3 py-2 ring-1 ring-slate-200">
      <span className="flex items-center gap-2 text-sm text-slate-600"><StateBadge value={e.state} /> <span className="truncate">{e.subject || '(no subject)'}</span></span>
      <RerunButton id={e.id} state={e.state} onQueued={load} />
    </div>
  )
  // One email can carry several orders; show the latest run of each.
  const latest = Object.values(
    e.runs.reduce<Record<string, Run>>((acc, r) => {
      const key = String(r.so_number ?? r.cust_po ?? r.id)
      if (!acc[key] || acc[key].id < r.id) acc[key] = r
      return acc
    }, {}),
  )
  if (!latest.length) {
    return (
      <div className="space-y-3">
        {toolbar}
        <h1 className="text-xl font-semibold">{e.subject || '(no subject)'}</h1>
        <p className="text-sm text-slate-600">From {e.sender} · {fmtDate(e.received)}</p>
        <div className="flex gap-2"><StateBadge value={e.state} /><LabelBadge value={e.label} /></div>
        <p className="rounded-lg bg-white p-4 text-sm text-slate-700 ring-1 ring-slate-200">
          {e.state === 'ignored' ? 'Not processed: ' : e.state === 'error' ? 'Processing failed: ' : 'No order verified yet. '}
          {e.state === 'error' ? e.error : e.triage_reasons}
        </p>
      </div>
    )
  }
  return (
    <div className="space-y-4">
      {toolbar}
      {latest.length > 1 && (
        <div className="flex gap-2">
          {latest.map((r, i) => (
            <button key={r.id} onClick={() => setRunIdx(i)} className={`rounded-full px-3 py-1 text-sm ring-1 ${i === runIdx ? 'bg-slate-900 text-white ring-slate-900' : 'bg-white text-slate-700 ring-slate-200'}`}>
              {r.kind === 'supplier' ? `PO ${r.po_number}` : r.cust_po ?? `SO ${r.so_number}`}
            </button>
          ))}
        </div>
      )}
      <OrderDetail runId={latest[Math.min(runIdx, latest.length - 1)].id} />
    </div>
  )
}

function UploadForm({ onDone }: { onDone: (runId: number | null) => void }) {
  const [doc, setDoc] = useState<File | null>(null)
  const [second, setSecond] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault()
        if (!doc) return
        setBusy(true)
        setErr(null)
        try {
          const r = await api.upload(doc, second)
          onDone(r.run_id)
        } catch (x) {
          setErr((x as Error).message)
        } finally {
          setBusy(false)
        }
      }}
      className="flex flex-wrap items-end gap-3 px-4 py-3 text-sm"
    >
      <label className="flex flex-col gap-1">
        <span className="text-xs text-slate-500">Purchase order (customer's, or ours to a supplier)</span>
        <input type="file" onChange={(e) => setDoc(e.target.files?.[0] ?? null)} className="text-xs" />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-xs text-slate-500">Acknowledgement (optional)</span>
        <input type="file" onChange={(e) => setSecond(e.target.files?.[0] ?? null)} className="text-xs" />
      </label>
      <button disabled={!doc || busy} className="rounded-lg bg-slate-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40">
        {busy ? 'Checking… (about a minute)' : 'Check documents'}
      </button>
      {err && <span className="text-xs text-red-700">{err}</span>}
    </form>
  )
}

export function Dashboard() {
  const [emails, setEmails] = useState<Email[]>([])
  const [status, setStatus] = useState<Status | null>(null)
  const [uploads, setUploads] = useState<Run[]>([])
  const [suggestions, setSuggestions] = useState<Suggestion[]>([])
  const [filter, setFilter] = useState<Filter>('orders')
  const [selected, setSelected] = useState<{ email?: string; run?: number } | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(() => {
    Promise.all([api.emails(), api.status(), api.uploads(), api.suggestions('open')])
      .then(([e, s, u, g]) => {
        setEmails(e)
        setStatus(s)
        setUploads(u)
        setSuggestions(g)
        setError(null)
      })
      .catch((err: Error) => setError(err.message))
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [refresh])

  const shown = useMemo(() => emails.filter((e) => matches(e, filter)), [emails, filter])
  const counts = status?.messages ?? {}
  const inProgress = ['queued', 'new', 'triaged', 'processing'].reduce((n, k) => n + (counts[k] ?? 0), 0)
  const byRec = (r: string) => emails.filter((e) => e.recommendation === r).length

  return (
    <div className="mx-auto max-w-7xl space-y-6 px-6 py-6">
      {error && <p className="rounded-lg bg-red-50 px-4 py-2 text-sm text-red-800">Can't reach the server ({error}). Is `uv run python -m agent serve` running?</p>}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
        <StatCard label="Emails seen" value={emails.length} hint={status ? `watching: ${status.running ? 'on' : 'off'}` : undefined} />
        <StatCard label="In progress" value={inProgress} tone="amber" hint={status ? `up to ${status.workers} agents` : undefined} />
        <StatCard label="Hold" value={byRec('hold')} tone="red" />
        <StatCard label="Needs approval" value={byRec('needs_approval')} tone="amber" />
        <StatCard label="Review" value={byRec('review')} tone="indigo" />
        <StatCard label="Release" value={byRec('release')} tone="emerald" />
      </div>

      <section className="rounded-xl border border-slate-200 bg-white shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
          <div className="flex flex-wrap gap-1">
            {FILTERS.map((f) => (
              <button key={f.key} onClick={() => setFilter(f.key)} className={`rounded-full px-3 py-1 text-sm ${filter === f.key ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'}`}>
                {f.label} <span className="opacity-60">{emails.filter((e) => matches(e, f.key)).length}</span>
              </button>
            ))}
          </div>
          <div className="flex items-center gap-3 text-xs text-slate-500">
            {status?.last_poll && <span>last check {fmtDate(status.last_poll)}</span>}
            {status?.last_error && <span className="text-red-600" title={status.last_error}>last poll failed</span>}
            <button onClick={() => api.poll().then(refresh)} className="rounded-full px-3 py-1 text-sm text-slate-700 ring-1 ring-slate-200 hover:bg-slate-50">
              Check inbox now
            </button>
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
                <th className="px-4 py-2 font-medium">Date</th>
                <th className="px-4 py-2 font-medium" title="Received (in) or sent by us (out)">In/out</th>
                <th className="px-4 py-2 font-medium">From</th>
                <th className="px-4 py-2 font-medium">Subject</th>
                <th className="px-4 py-2 font-medium">Triage</th>
                <th className="px-4 py-2 font-medium">Processing</th>
                <th className="px-4 py-2 font-medium">Result</th>
                <th className="px-2 py-2" aria-label="Rerun" />
              </tr>
            </thead>
            <tbody>
              {shown.map((e) => (
                <tr key={e.id} onClick={() => setSelected({ email: e.id })} className="cursor-pointer border-b border-slate-100 hover:bg-slate-50">
                  <td className="whitespace-nowrap px-4 py-2 text-slate-500">{fmtDate(e.received)}</td>
                  <td className="px-4 py-2 text-xs text-slate-500">{e.folder === 'sentitems' ? '↑ sent' : '↓ in'}</td>
                  <td className="max-w-[14rem] truncate px-4 py-2">{e.sender}</td>
                  <td className="max-w-[22rem] truncate px-4 py-2 font-medium text-slate-900">{e.subject || '(no subject)'}</td>
                  <td className="px-4 py-2" title={e.triage_reasons ?? undefined}><LabelBadge value={e.label} /></td>
                  <td className="px-4 py-2"><StateBadge value={e.state} /></td>
                  <td className="px-4 py-2"><RecommendationBadge value={e.recommendation} /></td>
                  <td className="px-2 py-2 text-right"><RerunButton id={e.id} state={e.state} onQueued={refresh} compact /></td>
                </tr>
              ))}
              {!shown.length && (
                <tr>
                  <td colSpan={8} className="px-4 py-8 text-center text-sm text-slate-500">No emails here yet.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {suggestions.length > 0 && (
        <section className="rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="border-b border-slate-100 px-4 py-3">
            <h2 className="text-sm font-semibold">Suggestions <span className="font-normal text-slate-500">{suggestions.length} open</span></h2>
            <p className="text-xs text-slate-500">What the agent thinks we should do past the checks. Advisory: they change no check or recommendation.</p>
          </div>
          <ul className="space-y-2 bg-slate-50/60 p-3">
            {suggestions.map((s) => (
              <div key={s.id}>
                <SuggestionCard s={s} onChange={refresh} />
                {(s.run_id || s.message_id) && (
                  <button onClick={() => setSelected(s.run_id ? { run: s.run_id } : { email: s.message_id! })} className="ml-4 mt-1 text-xs text-slate-500 underline">
                    view {s.run_id ? 'order' : 'email'}
                  </button>
                )}
              </div>
            ))}
          </ul>
        </section>
      )}

      <section className="rounded-xl border border-slate-200 bg-white shadow-sm">
        <div className="border-b border-slate-100 px-4 py-3">
          <h2 className="text-sm font-semibold">Checked manually</h2>
          <p className="text-xs text-slate-500">Documents uploaded here instead of arriving by email.</p>
        </div>
        <UploadForm
          onDone={(runId) => {
            refresh()
            if (runId) setSelected({ run: runId })
          }}
        />
        {uploads.length > 0 && (
          <ul className="divide-y divide-slate-100 border-t border-slate-100">
            {uploads.map((o) => (
              <li key={o.id} onClick={() => setSelected({ run: o.id })} className="flex cursor-pointer items-center justify-between px-4 py-2 text-sm hover:bg-slate-50">
                {o.kind === 'supplier' ? (
                  <span><span className="font-medium">Supplier PO {o.po_number}</span> <span className="text-slate-500">{o.customer} · for SO {o.so_number}</span></span>
                ) : (
                  <span><span className="font-medium">{o.customer}</span> <span className="text-slate-500">SO {o.so_number} · PO {o.cust_po}</span></span>
                )}
                <RecommendationBadge value={o.recommendation} />
              </li>
            ))}
          </ul>
        )}
      </section>

      {selected && (
        <Drawer onClose={() => setSelected(null)}>
          {selected.email ? <EmailPanel id={selected.email} /> : selected.run ? <OrderDetail runId={selected.run} /> : null}
        </Drawer>
      )}
    </div>
  )
}
