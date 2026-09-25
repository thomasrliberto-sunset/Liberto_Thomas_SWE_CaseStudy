from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends

from app.db.connection import DbConn, connection


def get_conn() -> Iterator[DbConn]:
    with connection() as conn:
        yield conn


Conn = Annotated[DbConn, Depends(get_conn)]


def csv_list(value: str | None, upper: bool = False) -> list[str] | None:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return [v.upper() for v in items] if upper else items
