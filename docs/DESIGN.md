# Design notes

## What I built, and for whom

The user is a PM or analyst who wants quick, **checkable** answers about a small coverage list.
They need the reported numbers, the valuation on those numbers, and what management actually
wrote in the filing. "Checkable" drove most of the choices below. Every figure carries its fiscal
year, period end and source filing. Every answer from `/ask` comes back with its citations, the tool
calls behind it, and a grounding report.

```
             ┌─ ingest (batch, on demand) ────────────┐       ┌─ serve ──────────────────────┐
 SEC XBRL ───┤ flatten facts → normalize (FY labels,  │       │ services: SQL + metric math  │
             │ restatements, tag fallbacks)           │       │   ├─ REST API (FastAPI)      │
 SEC 10-K ───┤ HTML → lines → Item 1A / Item 7 →      │  PG ─►│   └─ agent tools             │
             │ risk factors, heading-aware chunks     │       │        ▲                     │
 Yahoo ──────┤ daily OHLCV + split events             │       │ /ask: LLM router →           │
 SEC Form 4 ─┤ insider transactions (bonus)           │       │ tool loop → grounding check  │
             └─ each step logged in ingestion_run ────┘       └──────────────────────────────┘
```

Ingestion and serving share nothing except the schema. Ingestion runs as a separate compose
service (`docker compose run --rm ingest`). The API only reads, so it runs against the committed
snapshot with no live dependency.

## Data model

| Layer | Tables | Why |
|---|---|---|
| Raw | `xbrl_fact` (125k rows) | Every as-reported fact with accession, filing date, `fy`/`fp`/`form`. Nothing is lost, so a new metric is a config change plus a re-normalize, with no re-fetch. |
| Canonical | `annual_financial` | One value per (company, metric, fiscal year), with the concept or formula used and the source accession. |
| Text | `filing`, `filing_section`, `filing_chunk` (generated `tsvector` + GIN), `risk_factor` | Sections kept whole for provenance. Chunks carry a heading path such as `Risks Related to Our Industry > Competition could…`. Risk factors are split out so they can be diffed. |
| Market | `price_daily`, `stock_split` | Splits are stored because the valuation join needs them. |
| Bonus | `insider_transaction` | Form 4 non-derivative transactions with 10b5-1 flag. |
| Ops | `ingestion_run` | Status, row counts and warnings for every step (exposed at `/ingestion/runs`). |

**Data-quality report** (`/companies/{t}/quality`). Workarounds are disclosed, not hidden. The report lists:
- which metrics are derived, not tagged, and by what formula;
- which 10-K sections needed the title-heading fallback;
- accounting identities that failed: gross profit vs. revenue minus cost of revenue, margins within bounds,
  operating income no greater than gross profit, EPS roughly equal to net income ÷ diluted shares (a basis
  or split check);
- price freshness and gaps;
- the last ingest status for each source.

All five tickers pass the identity checks.

Ratios (margins) and growth are **computed at read time** from canonical values, so each formula
is defined in exactly one place (`app/services/metrics.py`, `config/metrics.yaml`).

## Integrating the three sources

**XBRL normalization** is where the messy data lives. Each problem below is handled explicitly and covered by a test:
- *Fiscal-year labels.* A fact's `fy` is the fiscal year **of the filing**, so the FY2025 10-K
  tags its FY2023 comparative as fy=2025. I label each period with the `fy` of the annual filing
  whose *current* period ends on that date. That also labels NVIDIA's January year-ends correctly,
  without special cases.
- *Annual vs quarterly.* Only annual forms with ~12-month durations count (350–380 days, so
  52/53-week years pass).
- *Restatements and splits.* If several filings report a period, the latest-filed value wins.
  History is therefore on the current basis (NVIDIA's pre-2024 EPS comes back split-adjusted).
- *Tag drift.* Concepts are tried in priority order **per period**. Fallback formulas fill gaps
  and are flagged as derived: Alphabet has no `GrossProfit` tag, and Eaton has no
  `OperatingIncomeLoss` tag.

**10-K text.** EDGAR HTML has no section markup. I convert it to lines, keeping bold, italic and
underline flags, then take the *longest* span from an `Item 1A`/`Item 7` heading to the next Item
heading. That skips the table of contents. Two real-world cases needed extra handling:
- Eaton's Item 7 is a one-line cross-reference, so the extractor falls back to the bare MD&A
  title heading later in the document.
- Microsoft writes risk-factor headings as run-in italic sentences, so lines also record their
  emphasized lead text.

Page numbers and running footers are stripped.

**Cross-source join (valuation).** Trailing P/E = latest close ÷ latest annual diluted EPS.
P/S = (close × FY diluted shares) ÷ FY revenue. The two sources disagree in two ways:
1. *Share basis.* Yahoo closes are split-adjusted to today, while EPS is on the share count at
   filing time. EPS is divided by the product of splits after the filing date. The same rule
   gives a historical P/E at each fiscal-year end.
2. *Staleness.* Annual EPS can be up to 15 months old. The brief's P/E, on the latest annual
   EPS, is the headline figure. Alongside it the service computes **TTM** figures:
   `FY + current 10-Q YTD − prior-year YTD`, each component split-adjusted by its own filing date.
   For Apple that moves the P/E from 45.6x (EPS through Sep-2025) to 39.0x (through Jun-2026).
   Every figure carries its period.

"Which company had the highest margin last year" defaults to **each company's latest fiscal year**
and returns an alignment note when period ends differ (they span about 9 months here).

## Where the LLM is, and where it deliberately isn't

The LLM **is** used for three things, all at `/ask`:
1. **Routing.** A structured-output call returns `{route: numbers|narrative|both|out_of_scope,
   tickers, rationale}`. It is validated with Pydantic and repaired once on bad JSON. Tickers are
   intersected with the configured universe, so an unknown company becomes out-of-scope.
2. **Tool selection and arguments.** The loop exposes *only the tools for the chosen route*, so
   routing is enforced rather than advisory (tested).
3. **Synthesis.** It writes prose from tool results and cites their `source_id`s.

The LLM is **not** used for:
- **Ingestion, parsing and metrics.** These are deterministic, testable and cheap.
- **Any arithmetic.** Tools return pre-computed growth, margins and multiples, pre-formatted with
  units, so the model copies numbers instead of calculating them.
- **Risk-factor comparison.** "New risk factors vs last year" is a deterministic diff: heading
  similarity, rescued by body overlap for retitled risks. The model only summarizes the diff. It
  never reads two 40-page sections and guesses.
- **Declining.** An out-of-scope route returns a fixed message listing what the service *can*
  answer, with no second LLM call that might invent guidance.
- **Checking its own work.** After the answer, a grounding pass extracts every figure and checks
  it against the tool outputs, tolerating rounding and scale such as `$130.50B` against 1.30497e11.
  It also verifies every citation id. Failures are returned in `grounding`, not hidden.

**Failure modes I designed for:**
- Hallucinated numbers: caught by the grounding check.
- Wrong or unknown tickers: caught by universe validation.
- Tool errors: returned to the model as data so it can adapt, and recorded in the trace.
- Runaway loops: capped at `LLM_MAX_TOOL_ROUNDS`, then the model is forced to answer.
- Provider quirks: raw assistant messages are echoed back verbatim, and 429/5xx are retried
  with backoff.
- Provider outages: `LLM_FALLBACK_MODELS` retries the *whole question* on the next model, so one
  conversation never mixes models, whose thought signatures differ.

**Evaluation.** `scripts/eval_questions.py` runs 14 questions: the brief's six plus eight variants,
including four questions that should be declined. It checks the route, checks that declines make no
tool calls, and checks grounding. On `gemini-3.8-flash` it scored 14/14 in each of three consecutive
runs (42/42), at temperature 0. Transcripts are in `docs/eval_results.md`.

Two changes came directly from the evaluation:
- The prompt now forbids cross-company arithmetic. The grounding check had caught the model computing
  "13 percentage points" on its own.
- Table cells are re-joined during extraction (`$ | 604` becomes `$604`), so figures quoted from MD&A
  tables can be verified.

**Known weak spots:**
- The grounding check is a heuristic. It cannot verify a paraphrase, only numbers and citations.
- Keyword FTS misses synonyms.

## Tradeoffs and cuts

| Decision | Why | Cost |
|---|---|---|
| Postgres full-text search over embeddings | No embedding model to host or proxy. Deterministic. Financial questions are keyword-heavy, and chunks carry headings (weight A). | Weaker on paraphrase. pgvector would slot into `filing_chunk` if needed. |
| Hand-rolled OpenAI-compatible client, no agent framework | About 100 lines. Works against any proxy. Every step is visible in the trace. | No streaming or tracing UI. |
| Annual fundamentals; TTM only for valuation | Matches the questions and the "latest annual EPS" P/E spec. TTM EPS and revenue are derived from 10-Q year-to-date facts for the valuation multiples. | No standalone quarterly series. Quarterly questions are declined, not approximated. |
| Latest-filed (restated) values | Consistent basis for comparisons and splits. | "As originally reported" is still available in `xbrl_fact`, but not exposed. |
| Seed as `COPY` SQL in `docker-entrypoint-initdb.d` | `docker compose up` is fully offline and deterministic. | 2.4 MB committed. Refresh with the ingest job plus `scripts/export_seed.py`. |

**Left out on purpose:**
- Forward estimates.
- 10-Q and 8-K text.
- Segment data (dimensional XBRL is not in companyfacts).
- Auth and rate limiting.
- A UI (`/docs` is the UI).
- Incremental XBRL updates (a full re-fetch is about 2 s per company).
- Scheduling (cron or a workflow runner would own this in production).
- Migrations tooling (a single idempotent DDL file is enough for a prototype).

**Bonus data: Form 4 insider trades.** Same EDGAR client and rate limiter. Summarized as
open-market buys vs sells, the share of sales under 10b5-1 plans, and the top sellers. PMs
routinely check this next to valuation, and it's another numbers tool the router can pick.
