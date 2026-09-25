-- Fundamentals Tracker schema.
-- Applied automatically by the Postgres container on first boot (docker-entrypoint-initdb.d)
-- and idempotently by the ingest job, so it is safe to re-run.
--
-- Layers:
--   raw        xbrl_fact                      as-reported facts, one row per fact per filing
--   canonical  annual_financial               one value per company/metric/fiscal year
--   text       filing, filing_section, filing_chunk (FTS), risk_factor
--   market     price_daily, stock_split
--   bonus      insider_transaction            SEC Form 4
--   ops        ingestion_run                  provenance for every pipeline step

CREATE TABLE IF NOT EXISTS company (
    id               SERIAL PRIMARY KEY,
    ticker           TEXT NOT NULL UNIQUE,
    cik              INTEGER NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    fiscal_year_end  CHAR(4),                 -- MMDD, from SEC submissions
    sic_description  TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- XBRL
CREATE TABLE IF NOT EXISTS xbrl_fact (
    id            BIGSERIAL PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    taxonomy      TEXT NOT NULL,              -- us-gaap | dei | ifrs-full
    concept       TEXT NOT NULL,
    unit          TEXT NOT NULL,
    period_start  DATE,                       -- NULL for instant facts
    period_end    DATE NOT NULL,
    value         NUMERIC NOT NULL,
    fy            INTEGER,                    -- fiscal year *of the filing*, not of the fact
    fp            TEXT,
    form          TEXT,
    accession     TEXT NOT NULL,
    filed         DATE NOT NULL,
    frame         TEXT,
    CONSTRAINT xbrl_fact_uq UNIQUE NULLS NOT DISTINCT
        (company_id, taxonomy, concept, unit, period_start, period_end, accession)
);
CREATE INDEX IF NOT EXISTS xbrl_fact_lookup ON xbrl_fact (company_id, concept, period_end);

CREATE TABLE IF NOT EXISTS annual_financial (
    company_id      INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    metric          TEXT NOT NULL,            -- canonical name from config/metrics.yaml
    fiscal_year     INTEGER NOT NULL,         -- company's own fiscal-year label
    period_start    DATE,
    period_end      DATE NOT NULL,
    value           NUMERIC NOT NULL,
    unit            TEXT NOT NULL,
    source_concept  TEXT,                     -- XBRL concept used (NULL when derived)
    derivation      TEXT,                     -- formula when not directly reported
    accession       TEXT,                     -- filing the value was taken from
    filed           DATE,
    PRIMARY KEY (company_id, metric, fiscal_year)
);

-- ---------------------------------------------------------------- filings
CREATE TABLE IF NOT EXISTS filing (
    id                SERIAL PRIMARY KEY,
    company_id        INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    accession         TEXT NOT NULL UNIQUE,
    form              TEXT NOT NULL,
    filing_date       DATE NOT NULL,
    report_date       DATE,                   -- period of report (fiscal year end)
    fiscal_year       INTEGER,
    primary_document  TEXT NOT NULL,
    url               TEXT NOT NULL,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS filing_section (
    id                 SERIAL PRIMARY KEY,
    filing_id          INTEGER NOT NULL REFERENCES filing(id) ON DELETE CASCADE,
    item               TEXT NOT NULL,         -- '1A' | '7'
    title              TEXT NOT NULL,
    text               TEXT NOT NULL,
    char_count         INTEGER NOT NULL,
    extraction_method  TEXT NOT NULL,         -- how the boundaries were found
    UNIQUE (filing_id, item)
);

CREATE TABLE IF NOT EXISTS filing_chunk (
    id          SERIAL PRIMARY KEY,
    section_id  INTEGER NOT NULL REFERENCES filing_section(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    heading     TEXT,                         -- nearest heading(s), gives the chunk context
    text        TEXT NOT NULL,
    tsv         tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('english', coalesce(heading, '')), 'A') ||
                    setweight(to_tsvector('english', text), 'B')
                ) STORED,
    UNIQUE (section_id, seq)
);
CREATE INDEX IF NOT EXISTS filing_chunk_tsv ON filing_chunk USING GIN (tsv);

-- Individual risk factors (the emphasized heading + its body) from Item 1A.
-- Enables a deterministic year-over-year diff instead of asking the LLM to compare
-- two 40-page documents.
CREATE TABLE IF NOT EXISTS risk_factor (
    id         SERIAL PRIMARY KEY,
    filing_id  INTEGER NOT NULL REFERENCES filing(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    category   TEXT,
    heading    TEXT NOT NULL,
    body       TEXT NOT NULL,
    UNIQUE (filing_id, seq)
);

-- ---------------------------------------------------------------- market data
CREATE TABLE IF NOT EXISTS price_daily (
    company_id  INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    date        DATE NOT NULL,
    open        NUMERIC,
    high        NUMERIC,
    low         NUMERIC,
    close       NUMERIC NOT NULL,             -- split-adjusted (Yahoo convention)
    adj_close   NUMERIC,                      -- split- and dividend-adjusted
    volume      BIGINT,
    source      TEXT NOT NULL,
    PRIMARY KEY (company_id, date)
);

CREATE TABLE IF NOT EXISTS stock_split (
    company_id  INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    date        DATE NOT NULL,
    ratio       NUMERIC NOT NULL,             -- e.g. 10 for a 10-for-1 split
    PRIMARY KEY (company_id, date)
);

-- ---------------------------------------------------------------- bonus: Form 4
CREATE TABLE IF NOT EXISTS insider_transaction (
    id                  BIGSERIAL PRIMARY KEY,
    company_id          INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    accession           TEXT NOT NULL,
    line_no             INTEGER NOT NULL,
    filing_date         DATE NOT NULL,
    insider_name        TEXT NOT NULL,
    insider_cik         TEXT,
    relationship        TEXT,
    transaction_date    DATE NOT NULL,
    security_title      TEXT,
    code                TEXT NOT NULL,        -- P=open-market buy, S=open-market sale, A=grant, M=exercise, F=tax withholding, G=gift ...
    acquired_disposed   CHAR(1),
    shares              NUMERIC,
    price               NUMERIC,
    shares_owned_after  NUMERIC,
    is_10b5_1           BOOLEAN,
    ownership           CHAR(1),              -- D=direct, I=indirect
    UNIQUE (accession, line_no)
);
CREATE INDEX IF NOT EXISTS insider_tx_lookup ON insider_transaction (company_id, transaction_date);

-- ---------------------------------------------------------------- ops
CREATE TABLE IF NOT EXISTS ingestion_run (
    id            SERIAL PRIMARY KEY,
    source        TEXT NOT NULL,              -- xbrl | filings | prices | insiders
    ticker        TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'running',   -- running | ok | error
    rows_written  INTEGER,
    detail        TEXT
);
