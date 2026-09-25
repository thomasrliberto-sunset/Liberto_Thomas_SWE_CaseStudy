"""Runtime settings (environment) and domain configuration (YAML).

Everything environment-specific, including the LLM endpoint, comes from env vars.
Everything domain-specific (which companies, which XBRL concepts) comes from
config/*.yaml so the universe can change without code changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://tracker:tracker@localhost:5432/fundamentals"

    # LLM: any OpenAI-compatible chat-completions endpoint.
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: str = ""
    llm_model: str = "gemini-3.8-flash"
    llm_timeout_s: float = 60.0
    llm_max_tool_rounds: int = 6
    llm_fallback_models: str = ""  # comma-separated; tried in order if the primary is rate-limited/overloaded
    llm_max_retries: int = 6  # 429/5xx are retried with exponential backoff (free tiers 503 under load)

    # SEC asks automated clients to identify themselves with a contact.
    sec_user_agent: str = "FundamentalsTracker admin@example.com"
    sec_max_rps: float = 8.0  # SEC fair-access limit is 10 req/s
    http_cache_dir: str | None = None  # optional on-disk cache of raw responses (dev only)

    universe_path: Path = ROOT / "config" / "universe.yaml"
    metrics_path: Path = ROOT / "config" / "metrics.yaml"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ----------------------------------------------------------------------------- universe


@dataclass(frozen=True)
class CompanyConfig:
    ticker: str
    cik: int | None = None


@dataclass(frozen=True)
class IngestConfig:
    tenk_filings: int = 2
    price_history_years: int = 6
    insider_lookback_days: int = 365
    sections: tuple[str, ...] = ("1A", "7")


@dataclass(frozen=True)
class Universe:
    companies: tuple[CompanyConfig, ...]
    ingest: IngestConfig = field(default_factory=IngestConfig)

    @property
    def tickers(self) -> list[str]:
        return [c.ticker for c in self.companies]


def load_universe(path: Path | None = None) -> Universe:
    raw = yaml.safe_load((path or get_settings().universe_path).read_text())
    companies = tuple(CompanyConfig(ticker=c["ticker"].upper(), cik=c.get("cik")) for c in raw["companies"])
    ing = raw.get("ingest", {}) or {}
    ingest = IngestConfig(
        tenk_filings=ing.get("tenk_filings", 2),
        price_history_years=ing.get("price_history_years", 6),
        insider_lookback_days=ing.get("insider_lookback_days", 365),
        sections=tuple(str(s).upper() for s in ing.get("sections", ["1A", "7"])),
    )
    return Universe(companies=companies, ingest=ingest)


# ----------------------------------------------------------------------------- metrics


@dataclass(frozen=True)
class ReportedMetric:
    name: str
    label: str
    unit: str
    concepts: tuple[str, ...]
    fallback: str | None = None


@dataclass(frozen=True)
class DerivedMetric:
    name: str
    label: str
    numerator: str
    denominator: str


@dataclass(frozen=True)
class MetricCatalog:
    reported: dict[str, ReportedMetric]
    derived: dict[str, DerivedMetric]

    @property
    def all_names(self) -> list[str]:
        return [*self.reported, *self.derived]

    def label(self, name: str) -> str:
        if name in self.reported:
            return self.reported[name].label
        return self.derived[name].label


@lru_cache
def load_metrics(path: Path | None = None) -> MetricCatalog:
    raw = yaml.safe_load((path or get_settings().metrics_path).read_text())
    reported = {
        name: ReportedMetric(
            name=name,
            label=m["label"],
            unit=m["unit"],
            concepts=tuple(m.get("concepts") or ()),
            fallback=m.get("fallback"),
        )
        for name, m in raw["reported"].items()
    }
    derived = {
        name: DerivedMetric(name=name, label=m["label"], numerator=m["numerator"], denominator=m["denominator"])
        for name, m in (raw.get("derived") or {}).items()
    }
    return MetricCatalog(reported=reported, derived=derived)
