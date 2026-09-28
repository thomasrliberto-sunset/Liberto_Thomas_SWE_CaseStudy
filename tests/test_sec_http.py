"""SEC HTTP behavior without network calls: identity, caching, retries, and errors."""

from pathlib import Path

import httpx
import pytest

from app.ingest import http as sec_http
from app.ingest.http import RETRY_STATUS, SecClient

URL = "https://data.sec.gov/submissions/CIK0000320193.json"
USER_AGENT = "FundamentalsTracker tests@example.com"


def make_client(
    monkeypatch,
    responses: list[httpx.Response | Exception],
    *,
    cache_dir: Path | None = None,
    max_retries: int = 4,
):
    """Build a SecClient whose requests and backoff waits are observable."""
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    limiter_waits: list[None] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = SecClient(
        user_agent=USER_AGENT,
        max_rps=10,
        cache_dir=str(cache_dir) if cache_dir is not None else "",
        max_retries=max_retries,
    )
    headers = client._client.headers
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler), headers=headers)
    monkeypatch.setattr(client._limiter, "wait", lambda: limiter_waits.append(None))
    monkeypatch.setattr(sec_http.time, "sleep", sleeps.append)
    return client, requests, sleeps, limiter_waits


def test_success_identifies_the_client_and_reuses_the_disk_cache(monkeypatch, tmp_path):
    body = b'{"cik": 320193, "ticker": "AAPL"}'
    client, requests, sleeps, limiter_waits = make_client(
        monkeypatch,
        [httpx.Response(200, content=body)],
        cache_dir=tmp_path / "sec-cache",
    )
    try:
        assert client.get_json(URL) == {"cik": 320193, "ticker": "AAPL"}
        assert client.get_bytes(URL) == body
    finally:
        client.close()

    assert len(requests) == 1
    assert requests[0].headers["user-agent"] == USER_AGENT
    assert "gzip" in requests[0].headers["accept-encoding"]
    assert limiter_waits == [None]  # a cache hit does not consume SEC rate-limit capacity
    assert sleeps == []
    cache_path = client._cache_path(URL)
    assert cache_path is not None and cache_path.read_bytes() == body


@pytest.mark.parametrize("status", sorted(RETRY_STATUS))
def test_each_transient_sec_status_is_retried(monkeypatch, status):
    client, requests, sleeps, limiter_waits = make_client(
        monkeypatch,
        [httpx.Response(status), httpx.Response(200, content=b"ok")],
        max_retries=2,
    )
    try:
        assert client.get_text(URL) == "ok"
    finally:
        client.close()

    assert len(requests) == 2
    assert sleeps == [1.0]
    assert len(limiter_waits) == 2


def test_repeated_transient_statuses_back_off_exponentially(monkeypatch):
    client, requests, sleeps, _ = make_client(
        monkeypatch,
        [httpx.Response(403), httpx.Response(429), httpx.Response(503), httpx.Response(200, content=b"ok")],
    )
    try:
        assert client.get_bytes(URL) == b"ok"
    finally:
        client.close()

    assert len(requests) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_transport_error_is_retried(monkeypatch):
    client, requests, sleeps, _ = make_client(
        monkeypatch,
        [httpx.ConnectError("connection reset"), httpx.Response(200, content=b"recovered")],
        max_retries=2,
    )
    try:
        assert client.get_bytes(URL) == b"recovered"
    finally:
        client.close()

    assert len(requests) == 2
    assert sleeps == [1.0]


def test_transport_error_exhaustion_reraises_the_original_error(monkeypatch):
    failures = [httpx.ConnectTimeout("timed out"), httpx.ConnectTimeout("timed out")]
    client, requests, sleeps, _ = make_client(monkeypatch, failures, max_retries=2)
    try:
        with pytest.raises(httpx.ConnectTimeout, match="timed out"):
            client.get_bytes(URL)
    finally:
        client.close()

    assert len(requests) == 2
    assert sleeps == [1.0]  # no sleep after the final failed attempt


def test_non_retryable_404_fails_immediately(monkeypatch):
    client, requests, sleeps, limiter_waits = make_client(
        monkeypatch,
        [httpx.Response(404, text="not found")],
    )
    try:
        with pytest.raises(httpx.HTTPStatusError, match="404"):
            client.get_bytes(URL)
    finally:
        client.close()

    assert len(requests) == 1
    assert sleeps == []
    assert limiter_waits == [None]
