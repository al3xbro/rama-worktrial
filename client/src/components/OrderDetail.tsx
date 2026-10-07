import { useEffect, useState } from 'react'
import { api } from '../api'
import type { ErpRow, ErpSnapshot, RunDetail } from '../api'
import { Card, KeyValues, RecommendationBadge, Table, fmtDate, fmtValue } from '../ui'
import { ChecksSection, DocumentView, EmailCard, RequirementsTable, Step, SuggestionsSection, issueCount } from './detail'
import { SupplierOrderDetail } from './SupplierOrderDetail'

// One verification, read as steps. Customer orders: the email, the customer's PO, the sales order,
// the customer's terms as the agent read them, then the checks and everything that went wrong.
// Supplier POs have their own view (SupplierOrderDetail).

export function RunDetailView({ runId }: { runId: number }) {
  const [d, setD] = useState<RunDetail | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    setD(null)
    setErr(null)
    api.runDetail(runId).then(setD).catch((e: Error) => setErr(e.message))
  }, [runId])
  if (err) return <p className="p-6 text-sm text-red-700">Could not load this order: {err}</p>
  if (!d) return <p className="p-6 text-sm text-slate-500">Loading…</p>
  return d.run.kind === 'supplier' ? <SupplierOrderDetail d={d} /> : <CustomerOrderDetail d={d} />
}

function ErpLines({ lines }: { lines: ErpRow[] }) {
  return (
    <Table
      head={['Line', 'Part number', 'Mfr', 'Qty', 'Unit price', 'Our cost', 'Required', 'Vendor', 'Cust. part']}
      rows={lines.map((l) => [
        fmtValue(l.lin_no),
        <span className="font-mono text-xs">{fmtValue(l.item_no)}</span>,
        fmtValue(l.manu_no),
        <span className="tabular-nums">{fmtValue(l.ord_qty)}</span>,
        <span className="tabular-nums">{fmtValue(l.unit_price)}</span>,
        <span className="tabular-nums">{fmtValue(l.unit_cost)}</span>,
        fmtValue(l.req_date),
        fmtValue(l.vend_no),
        fmtValue(l.cust_itmno),
      ])}
    />
  )
}

export function CustomerOrderDetail({ d }: { d: RunDetail }) {
  const erp = d.erp as ErpSnapshot | null
  const so = erp?.so
  const cust = erp?.customer
  const carry = d.findings.filter((f) => f.status === 'manual').length
  const passed = d.findings.filter((f) => f.status === 'pass').length
  const exposure = (erp?.credit.open_orders ?? 0) + Math.max(erp?.credit.ar_balance ?? 0, 0)
  const n = issueCount(d)

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold text-slate-900">{d.run.customer ?? 'Unknown customer'}</h1>
        <RecommendationBadge value={d.recommendation} />
        <span className="text-sm text-slate-500">SO {d.run.so_number ?? '—'} · PO {d.run.cust_po ?? '—'} · verified {fmtDate(d.run.created)}</span>
      </div>
      <ol>
        <Step n={1} title="Purchase order email" subtitle={d.email ? `${d.email.sender} · ${fmtDate(d.email.received)}` : 'Verified from files, not from an email'}>
          <EmailCard d={d} emptyText="This order was checked from local documents (batch run), so there is no email record." />
        </Step>

        <Step n={2} title="Customer's purchase order" subtitle="What the customer asked for, read verbatim from their document. Hover a value to see where it was read.">
          {d.po ? <DocumentView x={d.po} kind="po" /> : <p className="text-sm text-slate-500">No PO document.</p>}
        </Step>

        <Step n={3} title="Sales order" subtitle="What we entered: the ERP sales order, and the acknowledgement we send the customer.">
          <Card title={`ERP sales order ${so ? fmtValue(so.order_no) : ''}`} subtitle="ord_hedr / ord_detl, as read when the order was checked">
            {so ? (
              <>
                <KeyValues rows={[
                  ['Customer', `${fmtValue(cust?.cust_name)} (${fmtValue(so.cust_no)})`],
                  ['Customer PO', fmtValue(so.cust_po)],
                  ['Order date', fmtValue(so.order_dt)],
                  ['Status', fmtValue(so.status)],
                  ['Payment terms', fmtValue(so.terms_code)],
                  ['Quote', fmtValue(so.quote_no)],
                ]} />
                <div className="mt-4"><ErpLines lines={erp?.lines ?? []} /></div>
              </>
            ) : <p className="text-sm text-slate-500">No sales order found in the ERP.</p>}
          </Card>
          {d.ack && <DocumentView x={d.ack} kind="ack" />}
          {cust && (
            <Card title="Customer account" subtitle="customer master and credit position">
              <KeyValues rows={[
                ['Master terms', fmtValue(cust.terms_code)],
                ['Credit limit', fmtValue(cust.crdt_limit)],
                ['Exposure', `${exposure.toLocaleString(undefined, { maximumFractionDigits: 2 })} (open orders ${fmtValue(erp?.credit.open_orders)} + AR ${fmtValue(erp?.credit.ar_balance)})`],
                ['Ship via / FOB', `${fmtValue(cust.ship_via)} / ${fmtValue(cust.fob_code)}`],
                ['Address', [cust.cust_addr1, cust.cust_city, cust.cust_state, cust.cust_zip, cust.cust_cntry].filter(Boolean).join(', ')],
              ]} />
              {erp?.supply && erp.supply.length > 0 && (
                <div className="mt-4">
                  <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Supplier PO lines bought for this order</h4>
                  <Table head={['PO', 'Line', 'For SO line', 'Vendor', 'Qty', 'Cost', 'Due', 'Origin']}
                    rows={erp.supply.map((p) => [fmtValue(p.po_no), fmtValue(p.lin_no), fmtValue(p.so_lin_no), fmtValue(p.vend_no), fmtValue(p.ord_qty), fmtValue(p.unit_cost), fmtValue(p.due_date), fmtValue(p.src_cntry)])} />
                </div>
              )}
            </Card>
          )}
        </Step>

        <Step n={4} title="Customer terms, as the agent read them" subtitle="Clauses from the customer master, the PO and its conditions. Each one is checked by a rule in step 5.">
          <Card title={`${d.requirements.length} clauses`}><RequirementsTable reqs={d.requirements} empty="No customer-specific terms." /></Card>
        </Step>

        <Step n={5} title="Checks and what went wrong" subtitle={`${n} issue${n === 1 ? '' : 's'} · ${passed} checks passed · ${carry} requirements to carry forward`} tone={d.recommendation === 'release' ? 'emerald' : 'red'}>
          <ChecksSection d={d} checklistName="our order checklist" />
        </Step>

        <Step n={6} title="Suggestions" subtitle="Ideas from the agent past the checks: sourcing, allocation, pricing. Advisory; they change no check.">
          <SuggestionsSection items={d.suggestions ?? []} />
        </Step>
      </ol>
    </div>
  )
}

// Kept for existing imports: the dashboard renders RunDetailView.
export { RunDetailView as OrderDetail }
