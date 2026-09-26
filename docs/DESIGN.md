# Design notes

## What I built, and for whom

A PM or analyst wants quick, **checkable** answers about a small coverage list: the reported numbers,
the valuation on them, and what management wrote in the filing. Every figure carries its fiscal year,
period end and source filing. Every `/ask` answer returns citations, the tool calls behind it, and a
grounding report.

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
read-only and serves the committed snapshot with no live dependency.

## Data model

| Layer | Tables | Why |
|---|---|---|
| Raw | `xbrl_fact` (125k rows) | Every as-reported fact with accession and filing date. A new metric is a config change, not a re-fetch. |
| Canonical | `annual_financial` | One value per (company, metric, fiscal year), with the concept or formula used and the source accession. |
| Text | `filing`, `filing_section`, `filing_chunk` (`tsvector` + GIN), `risk_factor` | Chunks carry a heading path. Risk factors are split out so they can be diffed. |
| Market | `price_daily`, `stock_split` | Splits are needed by the valuation join. |
| Bonus / ops | `insider_transaction`, `ingestion_run` | Form 4 trades; status and row counts for every ingest step. |

Margins and growth are **computed at read time**, so each formula lives in one place
(`config/metrics.yaml`, `app/services/metrics.py`).

## Integrating the three sources

**Period alignment in XBRL.** Each case is handled explicitly and tested:
- *Fiscal-year labels.* A fact's `fy` is the fiscal year of the **filing**, so the FY2025 10-K tags its
  FY2023 comparative fy=2025. Each period takes the `fy` of the annual filing whose *current* period
  ends on that date, which also handles NVIDIA's January year-ends.
- *Annual only.* 10-K facts with 350–380-day durations (52/53-week years pass).
- *Restatements and splits.* The latest-filed value wins, so history is on today's basis.
- *Tag drift.* Concepts are tried in priority order per period. Fallback formulas are flagged as
  derived (Alphabet has no `GrossProfit` tag, Eaton no `OperatingIncomeLoss`).

**10-K text.** EDGAR HTML has no section markup. I convert it to lines that keep emphasis flags, then
take the *longest* span from an `Item 1A`/`Item 7` heading to the next Item heading, which skips the
table of contents. Eaton's Item 7 is a one-line cross-reference (fallback: the MD&A title later in the
document), and Microsoft's risk headings are run-in italic sentences.

**Cross-source join.** Trailing P/E = latest close ÷ latest annual diluted EPS; P/S = close × FY
diluted shares ÷ FY revenue. Three alignment problems:
1. *Share basis.* Yahoo closes are split-adjusted to today; EPS is on the share count at filing time,
   so EPS is divided by the splits after its filing date.
2. *Staleness.* Annual EPS can be 15 months old. The annual P/E stays the headline; the service also
   computes **TTM** (`FY + current 10-Q YTD − prior-year YTD`), split-adjusting only the per-share
   components. Apple: 45.6x on EPS through Sep-2025, 39.0x through Jun-2026.
3. *Mismatched year-ends.* "Highest margin last year" means each company's latest fiscal year, with
   an alignment note (period ends here span about 9 months).

**Data quality** (`/companies/{t}/quality`) discloses the workarounds: derived metrics, section
fallbacks, accounting identities (gross profit = revenue − cost of revenue, EPS ≈ net income ÷ diluted
shares), price freshness, and the last ingest status per source. All five tickers pass.

## Where the LLM is, and where it deliberately isn't

The LLM **is** used at `/ask` for:
1. **Routing.** A JSON-mode call returns `{route: numbers|narrative|both|out_of_scope, tickers,
   rationale}`, validated with Pydantic. A ticker outside the universe makes the question out-of-scope.
2. **Tool selection.** The loop exposes *only the route's tools*, so routing is enforced, not advisory.
3. **Synthesis.** Prose from tool results, citing their `source_id`s.

It is **not** used for:
- **Parsing, metrics or arithmetic.** Tools return pre-computed, pre-formatted growth, margins and
  multiples, so the model copies numbers instead of calculating them.
- **Risk-factor comparison.** "New risk factors" is a deterministic diff (heading similarity, rescued
  by body overlap for retitled risks). The model only summarizes it.
- **Declining.** Out-of-scope returns a fixed message, with no LLM call that might invent guidance.
- **Checking itself.** A grounding pass matches every figure in the answer to tool output (tolerating
  rounding and scale) and verifies every citation id. Failures are returned, not hidden.

**Failure modes:** hallucinated numbers (grounding check), unknown tickers (universe check), tool
errors (returned to the model as data and traced), runaway loops (`LLM_MAX_TOOL_ROUNDS`), provider
429/5xx (backoff, then `LLM_FALLBACK_MODELS` retries the whole question on the next model).

**Evaluation.** `scripts/eval_questions.py` runs 14 questions (the brief's six plus eight variants,
four of which must be declined) and checks the route, that declines call no tools, and grounding.
`gemini-3.8-flash` scored 14/14 in three consecutive runs. It drove two fixes: the prompt forbids
cross-company arithmetic (grounding caught a model-computed "13 percentage points"), and table cells
are re-joined (`$ | 604` → `$604`) so MD&A figures verify.

**Weak spots:** grounding verifies numbers and citations, not paraphrase; keyword FTS misses synonyms.

## Tradeoffs and cuts

| Decision | Why | Cost |
|---|---|---|
| Postgres FTS, not embeddings | Nothing extra to host; deterministic; filing questions are keyword-heavy. | Weaker on paraphrase; pgvector would slot into `filing_chunk`. |
| Hand-rolled OpenAI-compatible client | ~100 lines, works with any proxy, every step in the trace. | No streaming. |
| Annual fundamentals; TTM only for valuation | Matches the questions and the annual-EPS P/E spec. | Quarterly questions are declined, not approximated. |
| Latest-filed (restated) values | Consistent basis across years and splits. | As-originally-reported stays in `xbrl_fact`, unexposed. |
| Seed as `COPY` SQL in `docker-entrypoint-initdb.d` | `docker compose up` is offline and deterministic. | 2.4 MB committed; refresh via ingest + `scripts/export_seed.py`. |

**Left out on purpose:** forward estimates, 10-Q/8-K text, segment data (not in companyfacts), auth
and rate limiting, a UI (`/docs` serves), incremental updates and scheduling, migration tooling.

**Bonus: Form 4 insider trades**, through the same EDGAR client and rate limiter: open-market buys vs
sells, the share of sales under 10b5-1 plans, and top sellers. PMs check this next to valuation.
