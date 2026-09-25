"""CLI: python -m app.ingest [--tickers NVDA,MSFT] [--sources xbrl,filings,prices,insiders]"""

from __future__ import annotations

import argparse
import json
import logging

from app.config import get_settings, load_metrics, load_universe
from app.db.connection import apply_schema, connect
from app.ingest.http import SecClient
from app.ingest.pipeline import SOURCES, Pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest SEC + market data into Postgres.")
    parser.add_argument("--tickers", help="comma-separated subset of the configured universe")
    parser.add_argument("--sources", default=",".join(SOURCES), help=f"comma-separated subset of {SOURCES}")
    args = parser.parse_args()

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    unknown = set(sources) - set(SOURCES)
    if unknown:
        parser.error(f"unknown sources: {sorted(unknown)}")
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None

    client = SecClient()
    with connect() as conn:
        apply_schema(conn)
        summary = Pipeline(conn, client, load_universe(), load_metrics()).run(tickers, sources)
    client.close()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
