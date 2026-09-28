# Fundamentals Tracker

A small service that tracks fundamentals for a configurable universe (NVDA, MSFT, AAPL, GOOGL, ETN) from
**SEC XBRL**, the **10-K narrative** (Item 1A Risk Factors, Item 7 MD&A), **Yahoo Finance** prices and,
as a bonus, **SEC Form 4** insider trades. It serves them through a FastAPI REST API and a routed,
tool-using natural-language endpoint (`POST /ask`) whose answers cite their sources and are checked for
ungrounded numbers.

Design rationale, tradeoffs and cuts are in **[docs/DESIGN.md](docs/DESIGN.md)**.

## Run it (one command)

```bash
cp .env.example .env          # then set LLM_API_KEY (only /ask needs it)
docker compose up --build     # Postgres + API; the DB loads the committed snapshot on first boot
```

- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

The database is seeded from `db/init/02_seed.sql.gz`, a snapshot of a live ingest taken on
**2026-09-25**. Everything, including `/ask`, runs without reaching SEC or Yahoo.

Refresh from the live sources whenever you like:

```bash
docker compose run --rm ingest                                   # all tickers, all sources (~2 min)
docker compose run --rm ingest --tickers NVDA --sources prices   # subset
```

Run the tests. They're unit tests, plus API tests against the seeded database:

```bash
docker compose run --rm --entrypoint pytest api
```

To reset to the snapshot, run `docker compose down -v && docker compose up`.

`make up`, `make test`, `make lint`, `make ingest`, `make eval` and `make seed` wrap the same commands.

**CI** (`.github/workflows/ci.yml`) runs two jobs on every push:
- **lint, types and tests:** ruff, mypy and pytest against Postgres 16 loaded with the schema and snapshot;
- **stack check:** `docker compose up --wait` followed by an API smoke test.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` | Any OpenAI-compatible `/chat/completions` base URL |
| `LLM_API_KEY` | *(empty)* | Bearer token for that endpoint. Without it, `/ask` returns 503 and everything else works. |
| `LLM_MODEL` | `gemini-3.8-flash` | Model name passed through to the endpoint |
| `LLM_FALLBACK_MODELS` | *(empty)* | Optional comma-separated models; if the primary is rate-limited or overloaded (429/5xx after retries), the *whole question* is retried on the next one, so a conversation never mixes models |
| `LLM_TIMEOUT_S`, `LLM_MAX_TOOL_ROUNDS`, `LLM_MAX_RETRIES` | `60`, `6`, `6` | Per-call timeout; cap on tool-calling rounds; retries with backoff |
| `SEC_USER_AGENT` | `FundamentalsTracker admin@example.com` | SEC asks for a contact in the UA (live ingest only) |
| `DB_PORT` | `5432` | Host port for Postgres, if 5432 is taken |

**Model used:** Google **`gemini-3.8-flash`** (paid tier) through Google AI Studio's OpenAI-compatible
endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`), with `temperature: 0`. It passes
all 14 questions in [docs/eval_results.md](docs/eval_results.md).
- Google [limits `gemini-2.5-flash` access](https://ai.google.dev/gemini-api/docs/deprecations) to
  existing active users and directs new projects to newer models, so this submission uses
  `gemini-3.8-flash`.
- During development the same questions also passed on `gemini-3.5-flash-lite`. The agent doesn't lean
  on model strength: routing is constrained, and the arithmetic and diffs are done in code.

**To point it at your proxy,** set `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` in `.env` (or the
shell) and restart the `api` service. The client uses only standard chat-completions features:
`tools` with `tool_choice: auto`, `response_format: {"type": "json_object"}` and `temperature: 0`.

**Universe and metrics are config.** Tickers live in `config/universe.yaml`; CIKs are resolved from
SEC's ticker list. Metric → XBRL concept mappings, with per-period fallbacks, live in
`config/metrics.yaml`. To add a company, add one line and run `docker compose run --rm ingest`.

## API

| Endpoint | What it returns |
|---|---|
| `GET /companies` | Universe, fiscal-year ends and data coverage |
| `GET /companies/{t}/fundamentals?metrics=&last_n=&fiscal_years=` | Annual reported metrics plus margins and YoY, each with source concept or formula and accession |
| `GET /compare/{metric}?fiscal_year=&tickers=` | Cross-company ranking, with a fiscal-year alignment note |
| `GET /companies/{t}/valuation` | Trailing P/E and P/S (latest close × latest annual 10-K, split-adjusted). Also the same multiples on **TTM** figures (fiscal year rolled forward with 10-Q year-to-date data), and P/E at past fiscal year-ends |
| `GET /companies/{t}/prices?start=&end=` | Daily OHLCV |
| `GET /companies/{t}/filings` | Ingested 10-Ks, extracted sections, risk-factor counts |
| `GET /companies/{t}/filings/search?q=&section=risk_factors\|mdna&which=latest\|prior\|all` | Full-text search over 10-K narrative |
| `GET /companies/{t}/risk-factors/diff` | New, reworded and removed risk factors, latest vs prior 10-K |
| `GET /companies/{t}/insiders?days=365` | Form 4 open-market buys and sells, 10b5-1 share, top sellers (bonus) |
| `GET /companies/{t}/quality` | Data-quality checks: coverage, derived-vs-reported metrics, accounting identities, section-extraction fallbacks, price gaps, ingest status |
| `GET /metrics`, `GET /ingestion/runs`, `GET /health` | Metric catalog, pipeline provenance, health |
| `POST /ask` `{"question": "..."}` | Answer, route, citations, tool trace and grounding report |

## Example interactions

All output below is real, from the committed snapshot.

**[docs/eval_results.md](docs/eval_results.md)** has full transcripts of a 14-question evaluation run, with
routes, tool calls, citations and grounding for each. The set is the brief's six questions plus eight
variants: a cross-company comparison, insider activity, a cross-modal Eaton question, the Apple tariff
risk narrative, a P/E ranking, and declines for Tesla, quarterly data and "should I buy".

The committed transcript is the latest completed live run and passed **14/14**. The grounding checks
were then tightened further and covered with regression tests. Three earlier stability runs also passed
42/42 questions. In the current evaluator, a pass means:
- the route is as expected;
- the four out-of-scope questions are declined with zero tool calls;
- every in-scope answer has successful tool calls and citations, and passes the grounding check.

Rerun the evaluation with `make eval`.

### REST

```bash
curl -s "localhost:8000/companies/NVDA/fundamentals?metrics=revenue,gross_margin&last_n=2"
```
```json
{"ticker": "NVDA", "periods": [
  {"fiscal_year": 2026, "period_start": "2025-01-27", "period_end": "2026-01-25", "accession": "0001045810-26-000021",
   "metrics": {
     "revenue":      {"value": 215938000000.0, "display": "$215.94B", "unit": "USD", "yoy_growth": 0.6547,
                      "source": "us-gaap:Revenues", "derived": false},
     "gross_margin": {"value": 0.7107, "display": "71.1%", "unit": "ratio", "yoy_change_pp": -3.92,
                      "source": "gross_profit / revenue", "derived": false}}},
  "..."]}
```

```bash
curl -s localhost:8000/companies/AAPL/valuation    # cross-source join: Yahoo close x 10-K / 10-Q EPS
```
```json
{"price": 340.37, "price_date": "2026-09-25", "eps_fiscal_year": 2025, "eps_period_end": "2025-09-27",
 "eps_diluted_adjusted": 7.46, "split_adjustment": 1.0, "trailing_pe": 45.63, "price_to_sales": 12.27,
 "ttm": {"period_start": "2025-06-28", "period_end": "2026-06-27", "eps_diluted": 8.72, "pe": 39.03, "price_to_sales": 10.94,
         "method": "FY2025 + 9-month YTD from the latest 10-Q - the same period a year earlier. ..."},
 "notes": ["..."], "history": ["P/E at each FY end ..."]}
```
Apple's latest annual EPS is a year old, so the TTM P/E (39.0x) is lower than the annual-EPS
P/E (45.6x) the brief specifies. Both are returned, each with its period stated.

```bash
curl -s localhost:8000/companies/ETN/quality
```
```json
{"ticker": "ETN", "status": "info", "checks": [
  {"check": "derived_metrics", "status": "info", "detail": "gross_profit = revenue - cost_of_revenue; operating_income = gross_profit - sga_expense - rnd_expense (no XBRL tag reported)"},
  {"check": "filing_text", "status": "info", "detail": "FY2025 Item 7 located via title-heading fallback; ..."}, "..."]}
```

```bash
curl -s localhost:8000/compare/operating_margin
```
```json
{"basis": "each company's latest reported fiscal year",
 "rows": [{"rank": 1, "ticker": "NVDA", "fiscal_year": 2026, "period_end": "2026-01-25", "display": "60.4%"},
          {"rank": 2, "ticker": "MSFT", "fiscal_year": 2026, "period_end": "2026-06-30", "display": "46.8%"},
          {"rank": 3, "ticker": "GOOGL", "fiscal_year": 2025, "period_end": "2025-12-31", "display": "32.0%"}, "..."],
 "alignment_note": "Fiscal years are not calendar-aligned (period ends span 276 days: NVDA FY2026 ended 2026-01-25, ...)"}
```

Also try `/companies/NVDA/risk-factors/diff`,
`/companies/MSFT/filings/search?q=Azure+revenue+growth&section=mdna` and `/companies/NVDA/insiders`.

### Natural language

```bash
curl -s localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "How did MSFT'"'"'s revenue grow last year, and what did management attribute it to?"}'
```
Abridged response:
```json
{"route": {"route": "both", "tickers": ["MSFT"], "rationale": "..."},
 "tool_calls": [{"name": "get_financials", "arguments": {"tickers": ["MSFT"], "metrics": ["revenue"], "last_n": 3}, "ok": true, "sources": ["F1"]},
                {"name": "search_filings", "arguments": {"ticker": "MSFT", "section": "mdna", "query": "revenue increased driven by Azure Intelligent Cloud Productivity", "filing": "latest"}, "ok": true, "sources": ["S1", "..."]},
                "... one additional search_filings call ..."],
 "answer": "In fiscal year 2026 (ended June 30, 2026), Microsoft’s revenue reached **$331.84 billion**, representing a year-over-year increase of **17.8%** [F1] (reported in MD&A as an increase of **$50.1 billion or 18%**) [S2]. ... Management attributed the overall increase primarily to growth in **Microsoft Cloud** ... [S2, S5] ...",
 "citations": [{"id": "F1", "kind": "financials", "description": "MSFT annual financials FY2024-FY2026 from SEC XBRL company facts (10-K accessions 0001193125-26-323660)", "...": "..."},
               {"id": "S2", "kind": "filing_text", "description": "MSFT FY2026 10-K (filed 2026-07-29), Item 7 MD&A - SUMMARY RESULTS OF OPERATIONS", "url": "https://www.sec.gov/Archives/edgar/data/789019/..."}, "..."],
 "grounding": {"numbers_checked": 32, "citations_checked": 6, "unverified_numbers": [], "unknown_citations": [], "issues": [], "grounded": true},
 "model": "gemini-3.8-flash", "latency_ms": 8550, "usage": {"calls": 4, "...": "..."}}
```

Two more from the evaluation:
- *"What new risk factors did NVDA add in its latest 10-K versus the prior year?"* routes to `narrative` and
  calls `diff_risk_factors`. The answer is one new risk factor, "Commercial arrangements expose us to
  counterparty risks" (long-term capacity commitments, guarantees and requests to finance customers'
  datacenter build-outs), cited `[R1]`.
- *"What is the company's forward guidance for next quarter?"* routes to `out_of_scope`. The service
  returns a fixed decline in about 1.5 s with no tool calls: *"I can't answer that from the data this
  service has..."*, then lists what it can answer.

## Project layout

```
app/
  ingest/      SEC client (rate limit, UA, retries), XBRL normalization, 10-K section/risk-factor
               extraction, prices, Form 4, pipeline + CLI (python -m app.ingest)
  services/    read-side queries + pure metric math, shared by the API and the agent
  api/         FastAPI app and routers
  agent/       LLM client, router, tools, tool loop, grounding check, prompts
config/        universe.yaml, metrics.yaml
db/init/       01_schema.sql (DDL), 02_seed.sql.gz (snapshot)
scripts/       export_seed.py (DB -> snapshot), eval_questions.py (/ask evaluation set)
tests/         parsers, normalization, metrics, LLM/tool safety and provider fallbacks;
               seeded-Postgres API, snapshot-contract and ingestion tests
docs/          DESIGN.md, eval_results.md
.github/       CI: lint + types + tests on Postgres 16, and a docker compose smoke test
```

To refresh the committed snapshot after a live ingest:
`docker compose run --rm -v "$PWD/db/init:/app/db/init" --entrypoint python api scripts/export_seed.py`
