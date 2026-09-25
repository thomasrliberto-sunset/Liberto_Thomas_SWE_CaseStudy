"""Postgres access. Plain SQL over psycopg 3 with a small connection pool.

The data model is small and the queries are analytical, so explicit SQL is easier to
review than an ORM layer; every query lives in app/services or app/ingest/store.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import ROOT, get_settings

SCHEMA_PATH = ROOT / "db" / "init" / "01_schema.sql"

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            get_settings().database_url,
            min_size=1,
            max_size=10,
            # No server-side prepared statements: keeps the service compatible with transaction-
            # pooling proxies (PgBouncer) and single-session Postgres emulators; the gain is negligible here.
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection; commits on success, rolls back on error."""
    with get_pool().connection() as conn:
        yield conn


def connect() -> psycopg.Connection:
    """A standalone connection (used by the ingest CLI)."""
    return psycopg.connect(get_settings().database_url, row_factory=dict_row, prepare_threshold=None)


def apply_schema(conn: psycopg.Connection, path: Path = SCHEMA_PATH) -> None:
    conn.execute(path.read_text())
    conn.commit()
