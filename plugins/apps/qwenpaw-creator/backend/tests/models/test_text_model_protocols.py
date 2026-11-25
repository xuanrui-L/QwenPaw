# -*- coding: utf-8 -*-
# pylint: disable=line-too-long
# flake8: noqa: E501
"""Text provider protocols and output budgets."""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import json
import json as json_module

import httpx
import pytest

from api.model_routes import ModelConnectionTestRequest, _prepare_probe_payload
from models import config, output_budget, text_model, vlm_model
from utils.exceptions import ModelError

pytestmark = pytest.mark.unit


def _patch_config(monkeypatch, *, protocol: str, api_key: str) -> None:
    monkeypatch.setattr(
        text_model.model_config,
        "get_text_api_key",
        lambda: api_key,
    )
    monkeypatch.setattr(
        text_model.model_config,
        "get_text_model_name",
        lambda: "test-model",
    )
    monkeypatch.setattr(
        text_model.model_config,
        "get_text_protocol",
        lambda: protocol,
    )
    monkeypatch.setattr(
        text_model.model_config,
        "get_text_base_url",
        lambda: "https://gateway.example.com/v1",
    )


def _fake_httpx(
    monkeypatch,
    captured: dict,
    *,
    body_text: str = "",
    content_type: str = "application/json",
) -> None:
    class FakeResponse:
        # The chat decoder reads what httpx exposes (media type + body).
        headers = {"content-type": content_type}
        status_code = 200

        @property
        def text(self) -> str:
            return body_text or json_module.dumps(self.json())

        def json(self) -> dict:
            if body_text:
                return json_module.loads(body_text)
            return {
                "choices": [
                    {"message": {"content": "pong"}},
                ],
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, body=json)
            return FakeResponse()

    class FakeSlot:
        def __init__(self, _kind):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    monkeypatch.setattr(text_model.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(text_model, "model_slot", FakeSlot)


def test_keyless_openai_protocol_omits_authorization_header(
    monkeypatch,
) -> None:
    # OpenCode Zen ``*-free`` models accept unauthenticated requests; an
    # empty Bearer header would be rejected as an invalid key.
    _patch_config(monkeypatch, protocol="OpenCode", api_key="")
    captured: dict = {}
    _fake_httpx(monkeypatch, captured)

    result = asyncio.run(text_model.chat_completion("ping"))

    assert result == "pong"
    assert captured["url"] == "https://gateway.example.com/v1/chat/completions"
    assert "Authorization" not in captured["headers"]


def test_openai_protocol_sends_bearer_when_key_present(monkeypatch) -> None:
    _patch_config(monkeypatch, protocol="OpenAI 协议", api_key="sk-test")
    captured: dict = {}
    _fake_httpx(monkeypatch, captured)

    asyncio.run(text_model.chat_completion("ping"))

    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_anthropic_protocol_still_requires_api_key(monkeypatch) -> None:
    _patch_config(monkeypatch, protocol="Anthropic Claude", api_key="")

    with pytest.raises(ModelError):
        asyncio.run(text_model.chat_completion("ping"))


def test_gemini_protocol_still_requires_api_key(monkeypatch) -> None:
    _patch_config(monkeypatch, protocol="Google Gemini", api_key="")

    with pytest.raises(ModelError):
        asyncio.run(text_model.chat_completion("ping"))


def test_anthropic_protocol_dispatches_to_messages_endpoint(
    monkeypatch,
) -> None:
    _patch_config(monkeypatch, protocol="Anthropic Claude", api_key="sk-test")
    monkeypatch.setattr(
        text_model.model_config,
        "get_text_model_name",
        lambda: "MiniMax-M2.7",
    )
    captured: dict = {}

    class FakeResponse:
        # The chat decoder reads what httpx exposes (media type + body).
        headers = {"content-type": "application/json"}
        status_code = 200

        @property
        def text(self) -> str:
            return json_module.dumps(self.json())

        def json(self) -> dict:
            return {"content": [{"type": "text", "text": "pong"}]}

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, body=json)
            return FakeResponse()

    class FakeSlot:
        def __init__(self, _kind):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    monkeypatch.setattr(text_model.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(text_model, "model_slot", FakeSlot)

    result = asyncio.run(text_model.chat_completion("ping"))

    assert result == "pong"
    assert captured["url"] == "https://gateway.example.com/v1/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-test"


def test_kimi_k3_omits_unsupported_temperature(monkeypatch) -> None:
    # DashScope rejects ``temperature`` for kimi-k3; the OpenAI-compatible
    # request body must omit the field entirely.
    _patch_config(monkeypatch, protocol="DashScope（百炼）", api_key="test-key")
    monkeypatch.setattr(
        text_model.model_config,
        "get_text_model_name",
        lambda: "kimi-k3",
    )
    captured: dict = {}
    _fake_httpx(monkeypatch, captured)

    assert asyncio.run(text_model.chat_completion("Hello")) == "pong"
    assert "temperature" not in captured["body"]


def test_unsupported_temperature_learns_and_retries_without_it(
    monkeypatch,
) -> None:
    # A model this build has never seen may still reject ``temperature``. The
    # OpenAI-compatible path must learn the refusal, drop the field, and
    # re-issue the same turn so the caller never sees the failure; the next
    # call then omits it up front.
    from models import config as model_config
    from models.model_capability_cache import get_capability_cache

    _patch_config(monkeypatch, protocol="DashScope（百炼）", api_key="test-key")
    monkeypatch.setattr(
        text_model.model_config,
        "get_text_model_name",
        lambda: "brand-new-model",
    )
    cache = get_capability_cache()
    key = model_config._temperature_capability_key("brand-new-model")
    cache.clear(key)

    bodies: list[dict] = []
    ok_payload = json_module.dumps(
        {"choices": [{"message": {"content": "pong"}}]},
    )

    class FakeResponse:
        headers = {"content-type": "application/json"}

        def __init__(self, status_code: int, text: str) -> None:
            self.status_code = status_code
            self.text = text

        def json(self) -> dict:
            return json_module.loads(self.text)

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, *, headers, json):
            assert url == "https://gateway.example.com/v1/chat/completions"
            assert headers["Authorization"] == "Bearer test-key"
            bodies.append(dict(json))
            if len(bodies) == 1:
                return FakeResponse(
                    400,
                    "Parameter 'temperature'=0.2 is not supported for "
                    "brand-new-model",
                )
            return FakeResponse(200, ok_payload)

    class FakeSlot:
        def __init__(self, _kind):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(text_model.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(text_model, "model_slot", FakeSlot)

    try:
        assert asyncio.run(text_model.chat_completion("Hello")) == "pong"
        # First attempt carried temperature and was rejected; the retry
        # omitted it and succeeded.
        assert "temperature" in bodies[0]
        assert "temperature" not in bodies[1]
        # The refusal is now cached, so later calls omit it without a probe.
        assert model_config.chat_temperature("brand-new-model", 0.2) is None
    finally:
        cache.clear(key)


@pytest.mark.parametrize("kind", ["text", "vlm"])
@pytest.mark.parametrize(
    ("protocol", "model", "expected_limit"),
    [
        ("OpenAI", "deepseek-v4.1-flash", None),
        ("DashScope（百炼）", "qwen3.7-plus", None),
        ("Google Gemini", "gemini-2.5-pro", None),
        ("Anthropic Claude", "MiniMax-M2.7", 204800),
    ],
)
def test_model_requests_use_provider_output_budget(
    monkeypatch,
    kind,
    protocol,
    model,
    expected_limit,
):
    for key, value in {
        "api_key": "test-key",
        "base_url": "https://gateway.example",
        "model_name": model,
        "protocol": protocol,
    }.items():
        monkeypatch.setattr(
            config,
            f"get_{kind}_{key}",
            lambda value=value: value,
        )
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "pong"}}],
                "content": [{"type": "text", "text": "pong"}],
                "candidates": [{"content": {"parts": [{"text": "pong"}]}}],
            },
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(handle),
            **kwargs,
        ),
    )
    client = text_model if kind == "text" else vlm_model
    content = "ping" if kind == "text" else [{"type": "text", "text": "ping"}]
    assert asyncio.run(client.chat_completion(content)) == "pong"
    body = captured[0]
    if expected_limit is None:
        assert "max_tokens" not in body
    else:
        assert body["max_tokens"] == expected_limit
    assert "max_completion_tokens" not in body
    assert "maxOutputTokens" not in body.get("generationConfig", {})


@pytest.mark.parametrize("kind", ["llm", "vlm"])
@pytest.mark.parametrize(
    "protocol",
    ["OpenAI", "Google Gemini", "Anthropic Claude"],
)
def test_connection_probe_follows_same_output_budget(kind, protocol):
    _, _, body = asyncio.run(
        _prepare_probe_payload(
            ModelConnectionTestRequest(
                type=kind,
                protocol=protocol,
                model_name="MiniMax-M2.7",
                base_url="https://gateway.example",
                api_key="test-key",
            ),
        ),
    )
    if protocol == "Anthropic Claude":
        assert body["max_tokens"] == 204800
    else:
        assert "max_tokens" not in body
    assert "maxOutputTokens" not in body.get("generationConfig", {})


def test_claude_dated_alias_reads_sdk_capability():
    from agentscope.model import AnthropicChatModel

    card = next(iter(AnthropicChatModel.list_models()))
    assert (
        output_budget.known_anthropic_output_limit(card.name)
        == card.output_size
    )
    assert (
        output_budget.known_anthropic_output_limit(card.name + "-20260916")
        == card.output_size
    )


@pytest.mark.parametrize("limit", [150000, None, -1, True])
def test_unknown_anthropic_model_uses_models_api_or_reports_missing_capability(
    monkeypatch,
    limit,
):
    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(200, json={"max_tokens": limit})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(handle),
            **kwargs,
        ),
    )
    call = output_budget.anthropic_output_limit(
        "custom-model",
        base_url="https://gateway.example/v1",
        api_key="test-key",
    )
    if limit == 150000:
        assert asyncio.run(call) == limit
    else:
        with pytest.raises(ModelError, match="输出上限") as failed:
            asyncio.run(call)
        assert not failed.value.retryable
    assert captured[0].method == "GET"
    assert (
        str(captured[0].url)
        == "https://gateway.example/v1/models/custom-model"
    )


def test_empty_reasoning_response_preserves_diagnostics_without_reasoning_text(
    monkeypatch,
):
    for key, value in {
        "api_key": "test-key",
        "base_url": "https://gateway.example",
        "model_name": "deepseek-v4.1-flash",
        "protocol": "OpenAI",
    }.items():
        monkeypatch.setattr(
            config,
            f"get_text_{key}",
            lambda value=value: value,
        )
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "finish_reason": "length",
                                "message": {
                                    "content": "",
                                    "reasoning_content": "private reasoning",
                                },
                            },
                        ],
                        "usage": {"completion_tokens": 12000},
                    },
                ),
            ),
            **kwargs,
        ),
    )
    with pytest.raises(ModelError) as failed:
        asyncio.run(text_model.chat_completion("generate"))
    assert "length" in str(failed.value)
    assert "12000" in str(failed.value)
    assert "仅返回了推理内容" in str(failed.value)
    assert "private reasoning" not in str(failed.value)
