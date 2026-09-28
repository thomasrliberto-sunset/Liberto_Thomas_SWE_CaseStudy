import pytest

from app.api.routers.companies import get_fundamentals
from app.models import BadRequest
from app.services.fundamentals import compare_metric


def test_fundamentals_rejects_non_integer_fiscal_years():
    with pytest.raises(BadRequest, match="comma-separated list of integer years"):
        get_fundamentals("NVDA", object(), None, "2025,not-a-year", 5)  # type: ignore[arg-type]


def test_compare_rejects_unknown_ticker_in_requested_subset():
    class Result:
        def fetchall(self):
            return [{"id": 1, "ticker": "NVDA"}]

    class Connection:
        def execute(self, *_args):
            return Result()

    with pytest.raises(BadRequest, match=r"unknown tickers \['TSLA'\]"):
        compare_metric(Connection(), "revenue", tickers=["NVDA", "TSLA"])  # type: ignore[arg-type]
