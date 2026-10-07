// Small shared pieces: status badges, formatting, section cards.
import type { ReactNode } from 'react'

const RECOMMENDATION: Record<string, { label: string; cls: string }> = {
  hold: { label: 'Hold', cls: 'bg-red-100 text-red-800 ring-red-200' },
  needs_approval: { label: 'Needs approval', cls: 'bg-amber-100 text-amber-800 ring-amber-200' },
  review: { label: 'Review', cls: 'bg-indigo-100 text-indigo-800 ring-indigo-200' },
  release: { label: 'Release', cls: 'bg-emerald-100 text-emerald-800 ring-emerald-200' },
}

const FINDING: Record<string, { label: string; cls: string }> = {
  fail: { label: 'Fail', cls: 'bg-red-100 text-red-800 ring-red-200' },
  needs_approval: { label: 'Needs approval', cls: 'bg-amber-100 text-amber-800 ring-amber-200' },
  needs_review: { label: 'Needs review', cls: 'bg-violet-100 text-violet-800 ring-violet-200' },
  unverifiable: { label: 'Unverifiable', cls: 'bg-slate-200 text-slate-700 ring-slate-300' },
  manual: { label: 'Manual', cls: 'bg-stone-100 text-stone-700 ring-stone-200' },
  info: { label: 'Info', cls: 'bg-sky-100 text-sky-800 ring-sky-200' },
  pass: { label: 'Pass', cls: 'bg-emerald-100 text-emerald-800 ring-emerald-200' },
}

const EMAIL_STATE: Record<string, { label: string; cls: string }> = {
  queued: { label: 'Queued', cls: 'bg-slate-100 text-slate-700 ring-slate-200' },
  new: { label: 'New', cls: 'bg-slate-100 text-slate-700 ring-slate-200' },
  triaged: { label: 'Triaged', cls: 'bg-sky-100 text-sky-800 ring-sky-200' },
  processing: { label: 'Processing', cls: 'bg-amber-100 text-amber-800 ring-amber-200 animate-pulse' },
  done: { label: 'Done', cls: 'bg-emerald-100 text-emerald-800 ring-emerald-200' },
  ignored: { label: 'Ignored', cls: 'bg-stone-100 text-stone-600 ring-stone-200' },
  error: { label: 'Error', cls: 'bg-red-100 text-red-800 ring-red-200' },
}

function Badge({ label, cls }: { label: string; cls: string }) {
  return <span className={`inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${cls}`}>{label}</span>
}

export function RecommendationBadge({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="text-xs text-slate-400">—</span>
  const s = RECOMMENDATION[value] ?? { label: value, cls: 'bg-slate-100 text-slate-700 ring-slate-200' }
  return <Badge {...s} />
}

export function FindingBadge({ value }: { value: string }) {
  return <Badge {...(FINDING[value] ?? { label: value, cls: 'bg-slate-100 text-slate-700 ring-slate-200' })} />
}

export function StateBadge({ value }: { value: string }) {
  return <Badge {...(EMAIL_STATE[value] ?? { label: value, cls: 'bg-slate-100 text-slate-700 ring-slate-200' })} />
}

export function LabelBadge({ value }: { value: string | null }) {
  if (!value) return <span className="text-xs text-slate-400">—</span>
  const isPo = value === 'customer_po' || value === 'po_revision'
  const isSupplier = value === 'supplier_po' || value === 'supplier_ack'
  const text = value.replace(/_/g, ' ')
  const cls = isPo ? 'bg-blue-50 text-blue-800 ring-blue-200' : isSupplier ? 'bg-orange-50 text-orange-800 ring-orange-200' : 'bg-white text-slate-500 ring-slate-200'
  return <Badge label={text} cls={cls} />
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export function fmtValue(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'boolean') return v ? 'yes' : 'no'
  if (typeof v === 'number') return v.toLocaleString(undefined, { maximumFractionDigits: 5 })
  const s = String(v)
  return /^\d{4}-\d{2}-\d{2}T/.test(s) ? s.slice(0, 10) : s
}

export function Card({ title, subtitle, children, right }: { title: string; subtitle?: ReactNode; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white shadow-sm">
      <header className="flex items-start justify-between gap-4 border-b border-slate-100 px-4 py-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
          {subtitle && <div className="mt-0.5 text-xs text-slate-500">{subtitle}</div>}
        </div>
        {right}
      </header>
      <div className="px-4 py-3">{children}</div>
    </section>
  )
}

export function KeyValues({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-slate-500">{k}</dt>
          <dd className="min-w-0 break-words text-slate-900">{v}</dd>
        </div>
      ))}
    </dl>
  )
}

export function Table({ head, rows, empty = 'Nothing to show.' }: { head: string[]; rows: ReactNode[][]; empty?: string }) {
  if (!rows.length) return <p className="text-sm text-slate-500">{empty}</p>
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
            {head.map((h) => (
              <th key={h} className="whitespace-nowrap px-2 py-1.5 font-medium">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-slate-100 last:border-0">
              {r.map((c, j) => (
                <td key={j} className="px-2 py-1.5 align-top">{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
