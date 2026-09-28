# Design notes

## What I built, and for whom

The user is a PM or analyst who wants quick, **checkable** answers about a small coverage list:
reported numbers, valuation, and what management wrote. Figures carry their fiscal year, period end
and source filing; `/ask` returns citations, its tool trace, and a grounding report.

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
| Raw | `xbrl_fact` (125k rows) | As-reported facts with accession, filing date and SEC labels. New metrics do not require a re-fetch. |
| Canonical | `annual_financial` | One value per company, metric and fiscal year, with its concept or formula and accession. |
| Text | `filing`, `filing_section`, `filing_chunk` (`tsvector` + GIN), `risk_factor` | Heading-aware chunks for search; separate risk factors for diffing. |
| Market | `price_daily`, `stock_split` | Splits are needed by the valuation join. |
| Bonus / ops | `insider_transaction`, `ingestion_run` | Form 4 trades with 10b5-1 flag; status and row counts for every ingest step. |

Margins and growth are computed at read time, keeping formulas in `app/services/metrics.py` and
`config/metrics.yaml`.

## Integrating the three sources

**XBRL normalization** handles four tested edge cases:
- A fact's `fy` describes the filing, not every comparative period. Each period instead inherits the
  fiscal year of the annual filing whose current period ends on that date; NVIDIA's January year-end
  needs no special case.
- Only annual forms with 350–380-day durations count, including 52/53-week years.
- The latest-filed value wins, keeping restated and split-adjusted history on one basis.
- Concepts are tried per period in priority order; fallback formulas fill gaps and are marked derived
  (needed for Alphabet gross profit and Eaton operating income).

**10-K text.** EDGAR HTML is converted to lines that retain emphasis. The extractor takes the longest
`Item 1A`/`Item 7` span, avoiding table-of-contents matches. It also handles Eaton's cross-referenced
Item 7 and Microsoft's run-in italic risk headings.

**Cross-source join.** Trailing P/E is latest close ÷ annual diluted EPS; P/S is close × diluted
shares ÷ revenue. Yahoo closes are split-adjusted to today, so EPS is divided by splits after its
filing date. Because annual EPS can be stale, the service also computes TTM as
`FY + current 10-Q YTD − prior-year YTD`. Split adjustment applies only to per-share EPS components,
never revenue. The annual multiple remains the brief's headline figure.

"Highest margin last year" means **each company's latest fiscal year**, with an alignment note because
period ends here span about 9 months.

**Data quality** exposes derived formulas, extraction fallbacks, accounting identities, price gaps
and the latest ingest status at `/companies/{t}/quality`; all five tickers pass the identities.

## Where the LLM is, and where it deliberately isn't

The LLM is used at `/ask` to route in JSON mode, select from only the route's allowed tools, and
synthesize cited prose. Pydantic validates routing (with one repair retry), and ticker intersection
with the configured universe makes unknown companies out-of-scope.

It is not used for ingestion, parsing, metrics, arithmetic, or risk-factor comparison. Tools return
computed values, while a deterministic heading/body-similarity diff finds new risks. Out-of-scope
answers use a fixed template. A final deterministic pass verifies figures (including rounded and
scaled values) and citation IDs against the tool payloads actually shown to the model.

Other guards cover unknown tickers, tool errors, bounded tool loops, and provider 429/5xx responses.
Fallback models restart the whole question so one answer never mixes models. Responses report
latency, calls, and token use.

**Evaluation.** Fourteen questions cover the brief and eight variants. In-scope answers require the
expected route, successful tools, citations and grounding; four out-of-scope questions require zero
tools. `gemini-3.8-flash` passed three consecutive runs (42/42). The set caught model-computed
cross-company arithmetic and a filing-table extraction bug (`$ | 604`), both since fixed.

**Known weak spots:** grounding does not verify paraphrase or neutralize adversarial instructions in
filing text, and keyword FTS misses synonyms.

## Tradeoffs and cuts

| Decision | Why | Cost |
|---|---|---|
| Postgres FTS, not embeddings | No embedding model to host or proxy; deterministic; financial questions are keyword-heavy and chunk headings are weighted. | Weaker on paraphrase; pgvector would slot into `filing_chunk`. |
| Hand-rolled OpenAI-compatible client, no framework | ~100 lines, works against any proxy, every step visible in the trace. | No streaming or tracing UI. |
| Annual fundamentals; TTM only for valuation | Matches the questions and the annual-EPS P/E spec. | No quarterly series; quarterly questions are declined, not approximated. |
| Latest-filed (restated) values | Consistent basis across years and splits. | "As originally reported" stays in `xbrl_fact`, unexposed. |
| Seed as `COPY` SQL in `docker-entrypoint-initdb.d` | `docker compose up` is offline and deterministic. | 2.4 MB committed; refresh via ingest + `scripts/export_seed.py`. |

**Left out on purpose:** estimates; 10-Q/8-K text; segment data; auth and rate limiting; a UI
(`/docs` serves); incremental updates; scheduling; and migration tooling.

**Bonus:** Form 4 data adds open-market buys/sells, 10b5-1 share and top sellers through the same
EDGAR client and rate limiter.
