from __future__ import annotations

from collections.abc import Iterator

import psycopg

from app.db.connection import connection


def get_conn() -> Iterator[psycopg.Connection]:
    with connection() as conn:
        yield conn


def csv_list(value: str | None, upper: bool = False) -> list[str] | None:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return [v.upper() for v in items] if upper else items
