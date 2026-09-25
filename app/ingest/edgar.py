"""SEC EDGAR endpoints: ticker->CIK resolution, submissions history, document URLs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.ingest.http import SecClient

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"


@dataclass(frozen=True)
class CompanyInfo:
    ticker: str
    cik: int
    name: str
    fiscal_year_end: str | None
    sic_description: str | None


@dataclass(frozen=True)
class FilingRef:
    accession: str
    form: str
    filing_date: date
    report_date: date | None
    primary_document: str
    cik: int

    @property
    def url(self) -> str:
        return document_url(self.cik, self.accession, self.primary_document)


def document_url(cik: int, accession: str, document: str) -> str:
    return ARCHIVE_URL.format(cik=cik, acc_nodash=accession.replace("-", ""), doc=document)


def resolve_ciks(client: SecClient) -> dict[str, int]:
    """Map every SEC-listed ticker to its CIK."""
    data = client.get_json(TICKERS_URL)
    return {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}


def fetch_submissions(client: SecClient, cik: int) -> dict:
    return client.get_json(SUBMISSIONS_URL.format(cik=cik))


def company_info(ticker: str, cik: int, submissions: dict) -> CompanyInfo:
    return CompanyInfo(
        ticker=ticker,
        cik=cik,
        name=submissions.get("name") or ticker,
        fiscal_year_end=submissions.get("fiscalYearEnd"),
        sic_description=submissions.get("sicDescription"),
    )


def list_filings(submissions: dict, cik: int, forms: set[str]) -> list[FilingRef]:
    """Filings of the given form types from the 'recent' block, newest first.

    'recent' holds at least the last year / 1,000 filings, which comfortably covers two
    annual reports and a year of Form 4s for these issuers.
    """
    recent = submissions["filings"]["recent"]
    out: list[FilingRef] = []
    for i, form in enumerate(recent["form"]):
        if form not in forms:
            continue
        report = recent["reportDate"][i]
        out.append(
            FilingRef(
                accession=recent["accessionNumber"][i],
                form=form,
                filing_date=date.fromisoformat(recent["filingDate"][i]),
                report_date=date.fromisoformat(report) if report else None,
                primary_document=recent["primaryDocument"][i],
                cik=cik,
            )
        )
    out.sort(key=lambda f: f.filing_date, reverse=True)
    return out


def fetch_companyfacts(client: SecClient, cik: int) -> dict:
    return client.get_json(COMPANYFACTS_URL.format(cik=cik))
