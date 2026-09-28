"""Minimal client for any OpenAI-compatible /chat/completions endpoint.

Deliberately not an SDK or agent framework: the reviewers point this at their own LLM
proxy, and a ~100-line client over the wire format keeps the dependency surface and
the failure modes obvious (retries, timeouts, provider quirks are all right here).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMNotConfigured(LLMError):
    pass


class LLMUnavailable(LLMError):
    """Rate-limited or overloaded after retries (429/5xx): worth trying a fallback model."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str


@dataclass
class ChatResult:
    content: str | None
    tool_calls: list[ToolCall]
    raw_message: dict[str, Any]  # echoed back verbatim (keeps provider extras like thought signatures)
    usage: dict[str, Any] = field(default_factory=dict)


class ChatModel(Protocol):
    models: list[str]  # primary first, then fallbacks

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        model: str | None = None,
    ) -> ChatResult: ...


class OpenAICompatibleChat:
    def __init__(self, settings: Settings | None = None) -> None:
        s = settings or get_settings()
        if not s.llm_api_key:
            raise LLMNotConfigured("LLM_API_KEY is not set; /ask is unavailable (all other endpoints work)")
        fallbacks = [m.strip() for m in s.llm_fallback_models.split(",") if m.strip()]
        self.models = list(dict.fromkeys([s.llm_model, *fallbacks]))
        self._url = s.llm_base_url.rstrip("/") + "/chat/completions"
        self._client = httpx.Client(
            timeout=httpx.Timeout(s.llm_timeout_s, connect=10.0),
            headers={"Authorization": f"Bearer {s.llm_api_key}", "Content-Type": "application/json"},
        )
        self._max_retries = max(1, s.llm_max_retries)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        model: str | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {"model": model or self.models[0], "messages": messages, "temperature": 0}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        data = self._post(body)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected LLM response shape: {str(data)[:300]}") from exc

        calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            raw = tc.get("function", {}).get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {"__invalid_json__": raw}
            calls.append(
                ToolCall(
                    id=tc.get("id") or f"call_{len(calls)}",
                    name=tc["function"]["name"],
                    arguments=args,
                    raw_arguments=raw if isinstance(raw, str) else json.dumps(raw),
                )
            )
        return ChatResult(
            content=message.get("content"),
            tool_calls=calls,
            raw_message={k: v for k, v in message.items() if v is not None},
            usage=data.get("usage") or {},
        )

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST with retries. 5xx and transport errors back off exponentially. A 429 is waited
        out when the provider says it's a short (per-minute) window; a daily quota is surfaced
        immediately as LLMUnavailable so the caller can move to a fallback model."""
        delay = 2.0
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = self._client.post(self._url, json=body)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise LLMUnavailable(f"LLM endpoint unreachable: {exc}") from exc
                status, wait = "transport error", delay
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise LLMError(f"LLM returned invalid JSON: {resp.text[:300]}") from exc
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise LLMError(f"LLM returned {resp.status_code}: {resp.text[:500]}")
                status, wait = str(resp.status_code), delay
                if resp.status_code == 429:
                    hint = _retry_after(resp)
                    if "PerDay" in resp.text or (hint is not None and hint > 60):
                        raise LLMUnavailable(f"{body['model']} quota exhausted: {resp.text[:300]}")
                    wait = hint + 1 if hint is not None else delay
                if attempt == self._max_retries:
                    raise LLMUnavailable(f"{body['model']} returned {resp.status_code}: {resp.text[:300]}")
            log.warning("LLM %s on %s (attempt %d), retrying in %.0fs", status, body["model"], attempt, wait)
            time.sleep(wait)
            delay = min(delay * 2, 30.0)
        raise LLMError("unreachable")


_RETRY_IN = re.compile(r"retry in ([\d.]+)\s*s", re.I)


def _retry_after(resp: httpx.Response) -> float | None:
    header = resp.headers.get("retry-after")
    if header and header.replace(".", "", 1).isdigit():
        return float(header)
    m = _RETRY_IN.search(resp.text)
    return float(m.group(1)) if m else None
