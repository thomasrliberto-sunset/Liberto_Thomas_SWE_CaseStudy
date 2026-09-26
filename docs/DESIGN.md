# Design notes

## What I built, and for whom

The user is a PM or analyst who wants quick, **checkable** answers about a small coverage list: the
reported numbers, the valuation on those numbers, and what management wrote in the filing. Every
figure carries its fiscal year, period end and source filing. Every `/ask` answer returns citations,
the tool calls behind it, and a grounding report.

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

Ingestion and serving share only the schema. Ingestion is a separate compose job; the API is
read-only and runs against the committed snapshot with no live dependency.

## Data model

| Layer | Tables | Why |
|---|---|---|
| Raw | `xbrl_fact` (125k rows) | Every as-reported fact with accession, filing date, `fy`/`fp`/`form`. A new metric is a config change, not a re-fetch. |
| Canonical | `annual_financial` | One value per (company, metric, fiscal year), with the concept or formula used and the source accession. |
| Text | `filing`, `filing_section`, `filing_chunk` (generated `tsvector` + GIN), `risk_factor` | Chunks carry a heading path (`Risks Related to Our Industry > Competition could…`). Risk factors are split out so they can be diffed. |
| Market | `price_daily`, `stock_split` | Splits are needed by the valuation join. |
| Bonus / ops | `insider_transaction`, `ingestion_run` | Form 4 trades with 10b5-1 flag; status and row counts for every ingest step. |

Margins and growth are **computed at read time** from canonical values, so each formula lives in one
place (`app/services/metrics.py`, `config/metrics.yaml`).

## Integrating the three sources

**XBRL normalization** is where the messy data lives. Each case is handled explicitly and tested:
- *Fiscal-year labels.* A fact's `fy` is the fiscal year **of the filing**, so the FY2025 10-K tags
  its FY2023 comparative fy=2025. Each period takes the `fy` of the annual filing whose *current*
  period ends on that date, which also handles NVIDIA's January year-ends without special cases.
- *Annual vs quarterly.* Only annual forms with 350–380-day durations count (52/53-week years pass).
- *Restatements and splits.* The latest-filed value wins, so history is on the current basis
  (NVIDIA's pre-2024 EPS comes back split-adjusted).
- *Tag drift.* Concepts are tried in priority order **per period**; fallback formulas fill gaps and
  are flagged as derived (Alphabet has no `GrossProfit` tag, Eaton no `OperatingIncomeLoss`).

**10-K text.** EDGAR HTML has no section markup. I convert it to lines that keep bold/italic/underline
flags, then take the *longest* span from an `Item 1A`/`Item 7` heading to the next Item heading, which
skips the table of contents. Two filers needed more: Eaton's Item 7 is a one-line cross-reference
(fallback: the bare MD&A title later in the document), and Microsoft writes risk headings as run-in
italic sentences (lines also record their emphasized lead text).

**Cross-source join.** Trailing P/E = latest close ÷ latest annual diluted EPS; P/S = close × FY
diluted shares ÷ FY revenue. Two alignment problems:
1. *Share basis.* Yahoo closes are split-adjusted to today; EPS is on the share count at filing time.
   EPS is divided by the splits after its filing date (the same rule gives P/E at past FY-ends).
2. *Staleness.* Annual EPS can be 15 months old. The brief's annual P/E stays the headline figure,
   and the service also computes **TTM**: `FY + current 10-Q YTD − prior-year YTD`, each component
   split-adjusted by its own filing date. For Apple: 45.6x on EPS through Sep-2025, 39.0x through Jun-2026.

"Highest margin last year" means **each company's latest fiscal year**, with an alignment note because
period ends here span about 9 months.

**Data quality** (`/companies/{t}/quality`) discloses the workarounds instead of hiding them: derived
metrics and their formulas, section-extraction fallbacks, accounting identities (gross profit vs.
revenue − cost of revenue, margin bounds, EPS ≈ net income ÷ diluted shares as a split/basis check),
price freshness and gaps, and the last ingest status per source. All five tickers pass the identities.

## Where the LLM is, and where it deliberately isn't

The LLM **is** used at `/ask` for:
1. **Routing.** A JSON-mode call returns `{route: numbers|narrative|both|out_of_scope, tickers,
   rationale}`, validated with Pydantic (one repair retry). Tickers are intersected with the universe,
   so an unknown company becomes out-of-scope.
2. **Tool selection.** The loop exposes *only the route's tools*, so routing is enforced, not advisory.
3. **Synthesis.** Prose from tool results, citing their `source_id`s.

It is **not** used for:
- **Ingestion, parsing, metrics or arithmetic.** Tools return pre-computed, pre-formatted growth,
  margins and multiples, so the model copies numbers instead of calculating them.
- **Risk-factor comparison.** "New risk factors" is a deterministic diff (heading similarity, rescued
  by body overlap for retitled risks). The model summarizes the diff; it never compares two 40-page
  sections itself.
- **Declining.** Out-of-scope returns a fixed message listing what the service *can* answer, with no
  second LLM call that might invent guidance.
- **Checking its own work.** A grounding pass matches every figure in the answer to the tool outputs
  (tolerating rounding and scale, e.g. `$130.50B` vs 1.30497e11) and verifies every citation id.
  Failures are returned in `grounding`, not hidden.

**Failure modes handled:** hallucinated numbers (grounding check); unknown tickers (universe check);
tool errors (returned to the model as data, recorded in the trace); runaway loops
(`LLM_MAX_TOOL_ROUNDS`); provider 429/5xx (backoff, then `LLM_FALLBACK_MODELS` retries the *whole*
question on the next model so a conversation never mixes models).

**Evaluation.** `scripts/eval_questions.py` runs 14 questions: the brief's six plus eight variants,
four of which must be declined. It checks the route, that declines make no tool calls, and grounding.
On `gemini-3.8-flash`: 14/14 in each of three consecutive runs (42/42). Two fixes came from it: the
prompt now forbids cross-company arithmetic (the check caught a model-computed "13 percentage
points"), and table cells are re-joined at extraction (`$ | 604` → `$604`) so MD&A figures verify.

**Known weak spots:** the grounding check verifies numbers and citations, not paraphrase; keyword FTS
misses synonyms.

## Tradeoffs and cuts

| Decision | Why | Cost |
|---|---|---|
| Postgres FTS, not embeddings | No embedding model to host or proxy; deterministic; financial questions are keyword-heavy and chunk headings are weighted. | Weaker on paraphrase; pgvector would slot into `filing_chunk`. |
| Hand-rolled OpenAI-compatible client, no framework | ~100 lines, works against any proxy, every step visible in the trace. | No streaming or tracing UI. |
| Annual fundamentals; TTM only for valuation | Matches the questions and the annual-EPS P/E spec. | No quarterly series; quarterly questions are declined, not approximated. |
| Latest-filed (restated) values | Consistent basis across years and splits. | "As originally reported" stays in `xbrl_fact`, unexposed. |
| Seed as `COPY` SQL in `docker-entrypoint-initdb.d` | `docker compose up` is offline and deterministic. | 2.4 MB committed; refresh via ingest + `scripts/export_seed.py`. |

**Left out on purpose:** forward estimates; 10-Q/8-K text; segment data (dimensional XBRL isn't in
companyfacts); auth and rate limiting; a UI (`/docs` serves); incremental XBRL updates (a full
re-fetch is ~2 s per company); scheduling; migration tooling (one idempotent DDL file suffices here).

**Bonus data: Form 4 insider trades**, through the same EDGAR client and rate limiter: open-market
buys vs sells, the share of sales under 10b5-1 plans, and top sellers. PMs check this next to
valuation, and it gives the router another numbers tool.
