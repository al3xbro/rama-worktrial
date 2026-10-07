import type { RunDetail, SupplierErp } from '../api'
import { Card, KeyValues, RecommendationBadge, Table, fmtDate, fmtValue } from '../ui'
import { ChecksSection, DocumentView, EmailCard, RequirementsTable, Step, SuggestionsSection, issueCount } from './detail'

// Our purchase order to a supplier, read as steps: the email (our PO going out, or the supplier's
// acknowledgement coming in), our PO, what it buys for (the customer's sales order and terms), the
// supplier's acknowledgement, then the checks and everything that went wrong.

export function SupplierOrderDetail({ d }: { d: RunDetail }) {
  const erp = d.erp as SupplierErp | null
  const po = erp?.po
  const vendor = erp?.vendor
  const customerTerms = d.requirements.filter((r) => r.scope !== 'distributor')
  const ourConditions = d.requirements.filter((r) => r.scope === 'distributor')
  const passed = d.findings.filter((f) => f.status === 'pass').length
  const n = issueCount(d)
  const sentByUs = d.email?.folder === 'sentitems'

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold text-slate-900">PO {d.run.po_number ?? '—'} to {fmtValue(vendor?.po_name) || 'supplier'}</h1>
        <RecommendationBadge value={d.recommendation} />
        <span className="text-sm text-slate-500">
          for {erp?.sales_orders.map((s) => `SO ${fmtValue(s.so.order_no)} (${fmtValue(s.customer.cust_name)})`).join(', ') || 'no sales order'} · verified {fmtDate(d.run.created)}
        </span>
      </div>
      <ol>
        <Step n={1} title={sentByUs ? 'Our purchase order email' : "Supplier's email"} subtitle={d.email ? `${d.email.sender} · ${fmtDate(d.email.received)}` : 'Verified from files, not from an email'}>
          <EmailCard d={d} emptyText="Checked from local documents, so there is no email record." />
        </Step>

        <Step n={2} title="Our purchase order" subtitle="What we asked the supplier for: the PO document we sent, and the PO in the ERP.">
          {d.po ? <DocumentView x={d.po} kind="supplier_po" /> : <p className="text-sm text-slate-500">No PO document; checked against the ERP PO.</p>}
          <Card title={`ERP purchase order ${fmtValue(po?.po_no)}`} subtitle="po_hedr / po_detl, as read when the PO was checked">
            {po ? (
              <>
                <KeyValues rows={[
                  ['Vendor', `${fmtValue(vendor?.po_name)} (${fmtValue(po.vend_no)})`],
                  ['PO date', fmtValue(po.po_dt)],
                  ['Terms', `${fmtValue(po.terms_code)} (vendor's terms: ${fmtValue(vendor?.terms_code)})`],
                  ['Vendor standing', `${vendor?.pay_hold ? 'on payment hold' : 'no payment hold'} · ${fmtValue(vendor?.iso_cert)} expires ${fmtValue(vendor?.iso_cert_exp)}`],
                ]} />
                <div className="mt-4">
                  <Table
                    head={['Line', 'Part number', 'Mfr', 'Qty', 'Cost', 'Due', 'Origin', 'Condition', 'For SO line']}
                    rows={(erp?.po_lines ?? []).map((l) => [fmtValue(l.lin_no), <span className="font-mono text-xs">{fmtValue(l.item_no)}</span>, fmtValue(l.manu_no),
                      fmtValue(l.ord_qty), fmtValue(l.unit_cost), fmtValue(l.due_date), fmtValue(l.src_cntry), fmtValue(l.condition),
                      l.so_order_no ? `${l.so_order_no} / ${l.so_lin_no}` : <span className="text-amber-700">none</span>])}
                  />
                </div>
              </>
            ) : <p className="text-sm text-slate-500">Not found in the ERP.</p>}
          </Card>
        </Step>

        <Step n={3} title="What it buys for" subtitle="The customer's sales order lines this PO fills, and the customer's terms it must carry.">
          {(erp?.sales_orders ?? []).map((s) => (
            <Card key={String(s.so.order_no)} title={`SO ${fmtValue(s.so.order_no)} · ${fmtValue(s.customer.cust_name)}`} subtitle={`customer PO ${fmtValue(s.so.cust_po)} · terms ${fmtValue(s.so.terms_code)}`}>
              <Table
                head={['Line', 'Part number', 'Mfr', 'Qty', 'Price', 'Our cost', 'Required', 'Vendor']}
                rows={s.lines.map((l) => [fmtValue(l.lin_no), <span className="font-mono text-xs">{fmtValue(l.item_no)}</span>, fmtValue(l.manu_no), fmtValue(l.ord_qty),
                  fmtValue(l.unit_price), fmtValue(l.unit_cost), fmtValue(l.req_date), fmtValue(l.vend_no)])}
              />
            </Card>
          ))}
          {!erp?.sales_orders.length && <p className="text-sm text-slate-500">No sales order is linked to this PO.</p>}
          <Card title={`Customer terms (${customerTerms.length})`} subtitle="what the supplier PO must flow down">
            <RequirementsTable reqs={customerTerms} empty="No customer-specific terms." />
          </Card>
          <Card title={`Conditions on our PO (${ourConditions.length})`} subtitle="as printed on the PO we sent">
            <RequirementsTable reqs={ourConditions} empty="No conditions printed on the PO." />
          </Card>
        </Step>

        <Step n={4} title="Supplier's acknowledgement" subtitle="What the supplier confirmed: part, quantity, price, promised date, origin, date code.">
          {d.ack ? <DocumentView x={d.ack} kind="supplier_ack" /> : <p className="text-sm text-slate-500">No acknowledgement from the supplier yet.</p>}
        </Step>

        <Step n={5} title="Checks and what went wrong" subtitle={`${n} issue${n === 1 ? '' : 's'} · ${passed} checks passed`} tone={d.recommendation === 'release' ? 'emerald' : 'red'}>
          <ChecksSection d={d} checklistName="the supplier PO checklist" />
        </Step>

        <Step n={6} title="Suggestions" subtitle="Ideas from the agent past the checks: sourcing, allocation, pricing. Advisory; they change no check.">
          <SuggestionsSection items={d.suggestions ?? []} />
        </Step>
      </ol>
    </div>
  )
}
