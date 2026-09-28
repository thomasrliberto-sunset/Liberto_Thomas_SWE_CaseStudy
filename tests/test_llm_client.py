"""The OpenAI-compatible client against a mocked HTTP transport: wire format, retries,
rate-limit handling and the failover signal the agent relies on. No network, and the
backoff sleeps are recorded instead of taken."""

import json
from types import SimpleNamespace

import httpx
import pytest

from app.agent import llm as llm_mod
from app.agent.llm import LLMError, LLMNotConfigured, LLMUnavailable, OpenAICompatibleChat
from app.config import Settings

OK_BODY = {"choices": [{"message": {"role": "assistant", "content": "hello"}}], "usage": {"prompt_tokens": 3}}
MESSAGES = [{"role": "user", "content": "hi"}]


def make_chat(monkeypatch, responses, max_retries=3):
    """A client whose HTTP calls return `responses` in order (an exception is raised instead)."""
    sleeps: list[float] = []
    requests: list[httpx.Request] = []
    monkeypatch.setattr(llm_mod, "time", SimpleNamespace(sleep=sleeps.append))
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    chat = OpenAICompatibleChat(
        Settings(
            llm_base_url="http://llm.test/v1/",
            llm_api_key="test-key",
            llm_model="primary",
            llm_fallback_models="backup, primary",
            llm_max_retries=max_retries,
        )
    )
    headers = chat._client.headers
    chat._client.close()
    chat._client = httpx.Client(transport=httpx.MockTransport(handler), headers=headers)
    return chat, requests, sleeps


def test_missing_api_key_means_not_configured():
    with pytest.raises(LLMNotConfigured):
        OpenAICompatibleChat(Settings(llm_api_key=""))


def test_request_uses_the_openai_wire_format_and_parses_tool_calls(monkeypatch):
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "get_financials", "arguments": '{"tickers": ["MSFT"]}'},
                        },
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {"name": "search_filings", "arguments": "{not json"},
                        },
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    chat, requests, _ = make_chat(monkeypatch, [httpx.Response(200, json=body)])
    tools = [{"type": "function", "function": {"name": "get_financials", "parameters": {"type": "object"}}}]
    result = chat.chat(MESSAGES, tools=tools, json_mode=True)

    assert chat.models == ["primary", "backup"]  # primary first, fallbacks de-duplicated
    req = requests[0]
    assert str(req.url) == "http://llm.test/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer test-key"
    sent = json.loads(req.content)
    assert sent["model"] == "primary" and sent["temperature"] == 0 and sent["messages"] == MESSAGES
    assert sent["tools"] == tools and sent["tool_choice"] == "auto"
    assert sent["response_format"] == {"type": "json_object"}

    assert [c.name for c in result.tool_calls] == ["get_financials", "search_filings"]
    assert result.tool_calls[0].arguments == {"tickers": ["MSFT"]}
    assert result.tool_calls[1].arguments == {"__invalid_json__": "{not json"}  # reported back to the model
    assert "content" not in result.raw_message  # null fields dropped; the rest is echoed back verbatim
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 2}


def test_server_errors_are_retried_with_exponential_backoff(monkeypatch):
    chat, requests, sleeps = make_chat(
        monkeypatch, [httpx.Response(503, text="overloaded"), httpx.Response(500), httpx.Response(200, json=OK_BODY)]
    )
    result = chat.chat(MESSAGES, model="backup")
    assert result.content == "hello"
    assert len(requests) == 3 and sleeps == [2.0, 4.0]
    sent = json.loads(requests[0].content)
    assert sent["model"] == "backup"
    assert "tools" not in sent and "response_format" not in sent


def test_per_minute_rate_limit_waits_for_the_providers_hint(monkeypatch):
    chat, _, sleeps = make_chat(
        monkeypatch,
        [
            httpx.Response(429, text="Quota exceeded for requests per minute. Please retry in 7.5s."),
            httpx.Response(429, headers={"retry-after": "3"}, text="slow down"),
            httpx.Response(200, json=OK_BODY),
        ],
    )
    assert chat.chat(MESSAGES).content == "hello"
    assert sleeps == [8.5, 4.0]  # hint + 1s, not the generic backoff


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, text='{"error": {"details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]}}'),
        httpx.Response(429, headers={"retry-after": "3600"}, text="try again later"),
    ],
    ids=["daily-quota", "long-retry-after"],
)
def test_long_rate_limits_fail_over_immediately(monkeypatch, response):
    chat, requests, sleeps = make_chat(monkeypatch, [response])
    with pytest.raises(LLMUnavailable, match="quota exhausted"):
        chat.chat(MESSAGES)
    assert len(requests) == 1 and sleeps == []  # no point waiting; the agent moves to the next model


def test_persistent_overload_surfaces_as_unavailable_after_the_retry_budget(monkeypatch):
    chat, requests, sleeps = make_chat(monkeypatch, [httpx.Response(503)] * 3, max_retries=3)
    with pytest.raises(LLMUnavailable, match="503"):
        chat.chat(MESSAGES)
    assert len(requests) == 3 and sleeps == [2.0, 4.0]


def test_transport_errors_are_retried_then_surface_as_unavailable(monkeypatch):
    refused = httpx.ConnectError("connection refused")
    chat, _, sleeps = make_chat(monkeypatch, [refused, httpx.Response(200, json=OK_BODY)])
    assert chat.chat(MESSAGES).content == "hello" and sleeps == [2.0]

    chat, requests, _ = make_chat(monkeypatch, [refused, refused], max_retries=2)
    with pytest.raises(LLMUnavailable, match="unreachable"):
        chat.chat(MESSAGES)
    assert len(requests) == 2


def test_client_errors_are_not_retried_and_do_not_trigger_failover(monkeypatch):
    chat, requests, sleeps = make_chat(monkeypatch, [httpx.Response(400, text="unknown model name")])
    with pytest.raises(LLMError, match="400") as exc:
        chat.chat(MESSAGES)
    assert not isinstance(exc.value, LLMUnavailable)
    assert len(requests) == 1 and sleeps == []


def test_unexpected_response_shape_is_an_llm_error(monkeypatch):
    chat, _, _ = make_chat(monkeypatch, [httpx.Response(200, json={"error": "proxy misconfigured"})])
    with pytest.raises(LLMError, match="unexpected LLM response shape"):
        chat.chat(MESSAGES)


def test_successful_http_response_with_invalid_json_is_an_llm_error(monkeypatch):
    chat, requests, sleeps = make_chat(monkeypatch, [httpx.Response(200, text="proxy returned HTML")])
    with pytest.raises(LLMError, match="returned invalid JSON"):
        chat.chat(MESSAGES)
    assert len(requests) == 1 and sleeps == []
