"""Typed ERP lookups. Each hides the schema's traps: padded text, integer keys, PO numbers
reused across customers, and the many-sourced country of origin."""
from __future__ import annotations

from .db import Erp


def sales_order(erp: Erp, order_no: int) -> dict | None:
    return erp.row("select * from ord_hedr where order_no = %s", (order_no,))


def sales_orders_for_po(erp: Erp, cust_po: str) -> list[dict]:
    """All SOs carrying this customer PO number. PO numbers are reused across customers
    (277 times in this ERP), so callers must resolve the customer before picking one."""
    return erp.rows(
        "select h.*, trim(c.cust_name) as cust_name from ord_hedr h left join customer c on trim(c.cust_no) = trim(h.cust_no) "
        "where trim(h.cust_po) = %s order by h.order_no",
        (cust_po.strip(),),
    )


def so_lines(erp: Erp, order_no: int) -> list[dict]:
    rows = erp.rows("select * from ord_detl where order_no = %s order by lin_no", (order_no,))
    for r in rows:  # numeric in the ERP; line numbers are integers everywhere else
        r["lin_no"] = int(r["lin_no"])
        if r.get("gen_quot_lin") is not None:
            r["gen_quot_lin"] = int(r["gen_quot_lin"])
    return rows


def customer(erp: Erp, cust_no: str) -> dict | None:
    return erp.row("select * from customer where trim(cust_no) = %s", (cust_no.strip(),))


def customers_by_name(erp: Erp, like: str) -> list[dict]:
    return erp.rows("select cust_no, cust_name from customer where cust_name ilike %s", (f"%{like}%",))


def term_codes(erp: Erp) -> dict[str, str]:
    return {r["terms_code"]: r["terms_code_desc"] for r in erp.rows("select terms_code, terms_code_desc from term_codes")}


def quote(erp: Erp, quote_no: int) -> dict | None:
    return erp.row("select * from quote where quote_no = %s", (quote_no,))


def quote_lines(erp: Erp, quote_no: int) -> list[dict]:
    return erp.rows("select * from quo_detl where quote_no = %s order by lin_no", (quote_no,))


def open_order_value(erp: Erp, cust_no: str) -> float:
    """Value of the customer's open orders. Assumes statuses O, I and H are open (not confirmed)."""
    r = erp.row(
        "select coalesce(sum(d.ord_qty * d.unit_price), 0) as v from ord_detl d join ord_hedr h on h.order_no = d.order_no "
        "where trim(h.cust_no) = %s and trim(h.status) in ('O', 'I', 'H')",
        (cust_no.strip(),),
    )
    return float(r["v"])


def ar_balance(erp: Erp, cust_no: str) -> float:
    """Net of invoices, payments and credits in `arhist`."""
    r = erp.row("select coalesce(sum(amount), 0) as v from arhist where trim(cust_no) = %s", (cust_no.strip(),))
    return float(r["v"])


def items(erp: Erp, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """Item master rows keyed by (item_no, manu_no)."""
    out = {}
    for item_no, manu_no in keys:
        r = erp.row(
            "select item_no, manu_no, eccn, deflt_cnty_of_origin, ncnr, schedule_b from item where trim(item_no) = %s and trim(manu_no) = %s",
            (item_no, manu_no),
        )
        if r:
            out[(item_no, manu_no)] = r
    return out


def linked_po_lines(erp: Erp, order_no: int) -> list[dict]:
    return erp.rows(
        "select p.*, h.vend_no, h.po_dt from po_detl p join po_hedr h on h.po_no = p.po_no where p.so_order_no = %s order by p.po_no, p.lin_no",
        (order_no,),
    )


def inventory(erp: Erp, item_no: str, manu_no: str) -> dict | None:
    """The `itm_inv` summary row (one warehouse, AT, in this ERP)."""
    return erp.row("select * from itm_inv where trim(item_no) = %s and trim(manu_no) = %s", (item_no, manu_no))


def lots(erp: Erp, item_no: str, manu_no: str) -> list[dict]:
    """Lots with their origin and date code. Lot balances often disagree with `itm_inv`."""
    return erp.rows(
        "select h.lot_no, h.whse_cd, h.vend_no, h.bal_of_lot, h.qty_commit, h.date_code, h.create_dt::date as received, "
        "i.cnty_of_origin, i.src_cntry, i.manu_date_code, i.tariff_amt "
        "from lot_hedr h left join lot_info i on i.li_int_id = h.li_int_id "
        "where trim(h.item_no) = %s and trim(h.manu_no) = %s order by h.create_dt",
        (item_no, manu_no),
    )


def open_po_lines_for_item(erp: Erp, item_no: str, manu_no: str) -> list[dict]:
    """Open supplier PO lines for an item: linked to this SO, unallocated, or committed to another SO."""
    return erp.rows(
        "select p.*, trim(h.vend_no) as vend_no, h.po_dt from po_detl p join po_hedr h on h.po_no = p.po_no "
        "where trim(p.item_no) = %s and trim(p.manu_no) = %s and trim(p.status) = 'O' order by p.due_date",
        (item_no, manu_no),
    )


def po_lines(erp: Erp, po_no: int) -> list[dict]:
    return erp.rows(
        "select p.*, trim(h.vend_no) as vend_no, h.po_dt from po_detl p join po_hedr h on h.po_no = p.po_no where p.po_no = %s order by p.lin_no",
        (po_no,),
    )


def vendor(erp: Erp, vend_no: str) -> dict | None:
    return erp.row(
        "select vend_no, po_name, terms_code, pay_hold, iso_cert, iso_cert_exp, po_notes from vendor where trim(vend_no) = %s",
        (vend_no.strip(),),
    )


READ_ONLY_SQL = r"^\s*(select|with)\b"


def query(erp: Erp, sql: str, limit: int = 200) -> list[dict]:
    """Read-only escape hatch for the agent: SELECT/WITH only, on a read-only connection, row-limited."""
    import re

    if not re.match(READ_ONLY_SQL, sql, re.I) or ";" in sql.strip().rstrip(";"):
        raise ValueError("only a single SELECT (or WITH ... SELECT) statement is allowed")
    with erp.conn.cursor() as cur:
        cur.execute("set local statement_timeout = '20s'")
        cur.execute(f"select * from ({sql.strip().rstrip(';')}) q limit {int(limit)}")
        return [{k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()} for r in cur.fetchall()]
