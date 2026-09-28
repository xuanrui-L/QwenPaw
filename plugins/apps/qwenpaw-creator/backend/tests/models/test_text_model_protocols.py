# -*- coding: utf-8 -*-
"""Text model protocol dispatch and keyless free-tier support."""

# pylint: disable=protected-access
# The response doubles have to expose httpx's ``json()`` method, which
# shadows the stdlib module this file also imports.
# pylint: disable=redefined-outer-name

from __future__ import annotations

import asyncio
import json

import pytest

from models import text_model
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
            return body_text or json.dumps(self.json())

        def json(self) -> dict:
            if body_text:
                return json.loads(body_text)
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


def test_openai_protocol_asks_the_provider_to_stream(monkeypatch) -> None:
    """A non-streaming request dies at the gateway, not at our own budget.

    Measured: platform-pre's nginx answered 504 at 180.36s for a four-screen
    presentation while our timeout for that call was 600s. Nothing had been
    written back yet, so the proxy read a slow generation as a dead connection.
    Streaming keeps bytes moving, which resets that clock, and the decoder
    folds the stream back into the single completion callers here expect.
    """
    _patch_config(monkeypatch, protocol="OpenAI 协议", api_key="sk-test")
    captured: dict = {}
    _fake_httpx(monkeypatch, captured)

    asyncio.run(text_model.chat_completion("ping"))

    assert captured["body"]["stream"] is True


def test_stream_that_ends_without_saying_so_is_reported_truncated(
    monkeypatch,
) -> None:
    """A prefix must not be handed on as an answer.

    Turning streaming on makes this load-bearing: a gateway that severs the
    stream mid-document would otherwise return half a work page, which is worse
    than the 504 it replaced because nothing looks wrong downstream.
    """
    _patch_config(monkeypatch, protocol="OpenAI 协议", api_key="sk-test")
    captured: dict = {}
    _fake_httpx(
        monkeypatch,
        captured,
        content_type="text/event-stream",
        body_text='data: {"choices": [{"delta": {"content": "<html>"}}]}\n\n',
    )

    with pytest.raises(ModelError) as raised:
        asyncio.run(text_model.chat_completion("ping"))

    assert "响应流被截断" in str(raised.value)
    assert raised.value.retryable is True


def test_politely_ended_stream_without_a_finish_reason_is_accepted(
    monkeypatch,
) -> None:
    """The other half of the rule: ``[DONE]`` alone is a normal ending.

    Some compatible servers never report a finish reason yet still close the
    stream properly. Treating that as truncation would reject good answers, so
    only a stream that ends with neither signal counts as cut short.
    """
    _patch_config(monkeypatch, protocol="OpenAI 协议", api_key="sk-test")
    captured: dict = {}
    _fake_httpx(
        monkeypatch,
        captured,
        content_type="text/event-stream",
        body_text=(
            'data: {"choices": [{"delta": {"content": "pon"}}]}\n\n'
            'data: {"choices": [{"delta": {"content": "g"}}]}\n\n'
            "data: [DONE]\n\n"
        ),
    )

    assert asyncio.run(text_model.chat_completion("ping")) == "pong"


@pytest.mark.parametrize(
    ("model", "host", "budget", "expected"),
    [
        ("qwen3.7-plus", "dashscope.aliyuncs.com", 2048, 2048),
        ("qwen3.8-max", "deployment.maas.aliyuncs.com", 2048, 2048),
        ("qwen3.7-plus", "gateway.example.com", 2048, None),
        ("other-model", "dashscope.aliyuncs.com", 2048, None),
        ("qwen3.7-plus", "dashscope.aliyuncs.com", None, None),
    ],
)
def test_optional_reasoning_budget_preserves_gateway_contract(
    monkeypatch,
    model,
    host,
    budget,
    expected,
) -> None:
    _patch_config(monkeypatch, protocol="OpenAI 协议", api_key="sk-test")
    monkeypatch.setattr(
        text_model.model_config,
        "get_text_model_name",
        lambda: model,
    )
    monkeypatch.setattr(
        text_model.model_config,
        "get_text_base_url",
        lambda: f"https://{host}/v1",
    )
    captured: dict = {}
    _fake_httpx(monkeypatch, captured)
    asyncio.run(text_model.chat_completion("ping", thinking_budget=budget))
    if expected is None:
        assert "thinking_budget" not in captured["body"]
    else:
        assert captured["body"]["thinking_budget"] == expected


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
            return json.dumps(self.json())

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
