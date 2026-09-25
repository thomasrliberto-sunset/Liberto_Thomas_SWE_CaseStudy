"""HTTP client for SEC EDGAR: identifies itself, stays under the fair-access rate
limit, retries transient failures, and can cache raw responses on disk for dev.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

RETRY_STATUS = {403, 429, 500, 502, 503, 504}  # SEC answers 403 when throttling


class RateLimiter:
    def __init__(self, max_per_second: float) -> None:
        self._interval = 1.0 / max_per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
            self._next = max(now, self._next) + self._interval


class SecClient:
    def __init__(
        self,
        user_agent: str | None = None,
        max_rps: float | None = None,
        cache_dir: str | None = None,
        max_retries: int = 4,
    ) -> None:
        s = get_settings()
        self._client = httpx.Client(
            headers={"User-Agent": user_agent or s.sec_user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        )
        self._limiter = RateLimiter(max_rps or s.sec_max_rps)
        cache = cache_dir if cache_dir is not None else s.http_cache_dir
        self._cache = Path(cache) if cache else None
        self._max_retries = max_retries

    def get_bytes(self, url: str) -> bytes:
        cached = self._cache_path(url)
        if cached and cached.exists():
            return cached.read_bytes()

        delay = 1.0
        for attempt in range(1, self._max_retries + 1):
            self._limiter.wait()
            try:
                resp = self._client.get(url)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise
                log.warning("SEC transport error %s (attempt %d): %s", url, attempt, exc)
            else:
                if resp.status_code == 200:
                    if cached:
                        cached.parent.mkdir(parents=True, exist_ok=True)
                        cached.write_bytes(resp.content)
                    return resp.content
                if resp.status_code not in RETRY_STATUS or attempt == self._max_retries:
                    resp.raise_for_status()
                log.warning("SEC %s for %s (attempt %d); backing off", resp.status_code, url, attempt)
            time.sleep(delay)
            delay *= 2
        raise RuntimeError("unreachable")

    def get_json(self, url: str) -> dict:
        import json

        return json.loads(self.get_bytes(url))

    def get_text(self, url: str) -> str:
        return self.get_bytes(url).decode("utf-8", errors="replace")

    def close(self) -> None:
        self._client.close()

    def _cache_path(self, url: str) -> Path | None:
        if not self._cache:
            return None
        return self._cache / hashlib.sha1(url.encode()).hexdigest()
