"""Demo Distributor's order processing checklist (data/Order_Processing_Checklist.docx).

Each item has a ref like "2.5" (section 2, item 5). Items marked `added` are not on the
paper checklist but come from the task brief (line quantities, prices and dates, arithmetic,
and the customer's own terms). `default` is the status shown when no check covers the item.
"""
from dataclasses import dataclass

from .models import Status


@dataclass(frozen=True)
class Item:
    ref: str
    section: str
    text: str
    default: Status = Status.MANUAL
    added: bool = False
    # Why no check covers this item. Every item needs either a registered check or a reason
    # (tests/test_checklist.py enforces it), so nothing on the checklist is silently skipped.
    manual_reason: str | None = None


NO_DOCS = "needs documents the ERP does not store (no document storage); a person confirms"

CHECKLIST = [
    Item("1.1", "Payment & credit", "Payment method confirmed: prepay or approved net terms."),
    Item("1.2", "Payment & credit", "Prepay fees applied correctly: credit card 3.5%; wire $25; ACH domestic banks only, no fee.",
         manual_reason="only checkable for card terms; wire vs ACH is not recorded"),
    Item("1.3", "Payment & credit", "For net terms: credit application returned, trade references provided, and US tax-exempt form received if applicable.",
         manual_reason=NO_DOCS),
    Item("1.4", "Payment & credit", "AR has assigned terms and an adequate credit line before order release."),
    Item("2.1", "Customer & PO", "Customer name matches the purchase order."),
    Item("2.2", "Customer & PO", "Billing and shipping addresses match the purchase order."),
    Item("2.3", "Customer & PO", "Customer PO number entered in the ERP."),
    Item("2.4", "Customer & PO", "Customer internal part number entered when provided."),
    Item("2.5", "Customer & PO", "Manufacturer part number entered EXACTLY, including spaces and special characters."),
    Item("2.6", "Customer & PO", "Customer price and our cost match the approved quote."),
    Item("2.7", "Customer & PO", "Shipping speed is correct."),
    Item("2.8", "Customer & PO", "Shipping account is authorized."),
    Item("2.9", "Customer & PO", "Ship-complete or partial-shipment instructions are noted on the sale."),
    Item("2.L", "Customer & PO", "Every PO line is on the SO and vice versa, with matching manufacturer, quantity, price and date.", added=True),
    Item("2.T", "Customer & PO", "PO and ack arithmetic ties out (line extensions and totals).", added=True),
    Item("3.1", "Yield / account risk", "Checked the account for the Yield flag."),
    Item("3.2", "Yield / account risk", "Reviewed all special requirements carefully (customer terms and PO clauses)."),
    Item("3.3", "Yield / account risk", "Obtained and uploaded required approvals before proceeding.",
         manual_reason="the agent lists every approval needed (needs_approval findings); obtaining and filing them is a person's step"),
    Item("4.1", "Vendor & inventory", "Stock check requested and availability confirmed."),
    Item("4.2", "Vendor & inventory", "Completed stock check is recorded/uploaded before the order is submitted.",
         manual_reason="the agent's report is the stock check record; uploading it to the sale is a person's step"),
    Item("4.3", "Vendor & inventory", "Inventory allocated to the correct lot.",
         manual_reason="lot allocation is not visible in the ERP extract (lot commitments are all zero)"),
    Item("4.4", "Vendor & inventory", "Vendors on the order are in good standing (no payment hold, certification current).", added=True),
    Item("4.5", "Vendor & inventory", "Supplier POs buy only what the order needs, at the SO cost, and flow down the customer's requirements.", added=True),
    Item("5.1", "Dates & lead time", "Ready date calculated using the Lead Time Matrix (3 days for stock sales; longer when testing is required).", default=Status.UNVERIFIABLE),
    Item("6.1", "Export", "Confirmed whether we are exporting."),
    Item("6.2", "Export", "If exporting and the ECCN is not EAR99, the Export Review Form was sent and approval retained."),
    Item("7.1", "Test report approval", "When customer approval is required before shipment, checked 'Hold from Shipping' on the line.",
         manual_reason="test-report approvals are an ERP and inspection workflow, not in the data"),
    Item("7.2", "Test report approval", "Uploaded the customer's approval email to the sale.", manual_reason=NO_DOCS),
    Item("7.3", "Test report approval", "After approval, unchecked 'Hold from Shipping'.", manual_reason="ERP line flag, not in the data"),
    Item("7.4", "Test report approval", "If the ship date passed or equals the approval date, changed the ship date to the next business day.",
         manual_reason="depends on the approval date, which is not in the data"),
    Item("7.5", "Test report approval", "Emailed inspection confirming approval is uploaded and the line may ship.",
         manual_reason="a person's email to inspection"),
    Item("8.1", "Final release", "All applicable quotes, approvals, forms, emails and supporting documents are uploaded to the sale.", manual_reason=NO_DOCS),
    Item("8.2", "Final release", "Order reviewed for completeness and is ready for release.",
         manual_reason="the agent's recommendation feeds this; a person signs off the release"),
]

BY_REF = {i.ref: i for i in CHECKLIST}
