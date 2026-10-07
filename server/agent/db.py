"""Read-only access to the ERP (Postgres, schema `worktrial`).

Connection settings come from the standard PG* variables in .env. Text columns in the
ERP are space padded, so every string value is stripped on the way out.
"""
import threading
from contextlib import contextmanager
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from . import config


def _clean(value):
    if isinstance(value, str):
        return value.strip()
    return value


def _clean_row(row: dict) -> dict:
    return {k: _clean(v) for k, v in row.items()}


_SLOTS = threading.BoundedSemaphore(config.ERP_MAX_CONNECTIONS)


@contextmanager
def connect(read_only: bool = True):
    """One connection, read-only unless asked. The ERP allows 5 connections in total, so emails
    processed in parallel wait for a free slot here instead of failing."""
    with _SLOTS:
        conn = psycopg.connect(row_factory=dict_row)
        conn.read_only = read_only
        try:
            yield conn
        finally:
            conn.close()


class Erp:
    """Thin query helper bound to one connection."""

    def __init__(self, conn):
        self.conn = conn

    def rows(self, sql: str, params=None) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return [_clean_row(r) for r in cur.fetchall()]

    def row(self, sql: str, params=None) -> dict | None:
        rows = self.rows(sql, params)
        return rows[0] if rows else None


def to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))
