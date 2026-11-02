# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access,use-implicit-booleaness-not-comparison
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json

from agentscope.message import (
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultState,
)
from agentscope.model import DashScopeChatModel
from agentscope.model._model_response import ChatResponse
import pytest

from services.file_agent_runtime.model_client import (
    MAX_RATE_LIMIT_RETRIES,
    AgentModelError,
    AgentStreamCallbackError,
    AgentStreamCallbackPassthrough,
    AgentModelTurn,
    AgentScopeAgentChatClient,
    AgentScopeVlmChatClient,
    AgentToolCall,
    CallbackAgentChatClient,
    OverflowAfterOutputError,
    RateLimitExhaustedError,
    RateLimitRetryNotice,
    is_context_length_error,
    records_to_agentscope_messages,
)
from services.file_agent_runtime import model_client
from services.file_agent_runtime import media_vault
from services.file_agent_runtime import model_context
from services.workspace.content_store import ContentStore
from services.file_agent_runtime.model_context import (
    ModelContextBudgetError,
    _inline_media_relief,
    compact_conversation_history,
    prepare_model_messages,
)

pytestmark = pytest.mark.unit


def test_inline_media_is_charged_as_vision_tokens_not_base64_text():
    """A media-bearing contract message must fit the serialized budget.

    Regression guard: ``size()`` used to count the whole messages JSON as
    text, so one 2MB image inlined as a ~2.7MB Base64 data URL exceeded the
    384KB budget by itself. Media-bearing user messages are contract content
    that compaction may not drop, so the request was refused locally and never
    reached the provider — the understanding record just came back empty.
    """
    encoded = base64.b64encode(b"\x00" * (2 * 1024 * 1024)).decode()
    data_url = f"data:image/jpeg;base64,{encoded}"
    messages = [
        {"role": "system", "content": "项目创作契约"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "理解这张图"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]

    result = prepare_model_messages(messages, [])

    # Inline media is charged, never dropped or rewritten.
    assert result[1]["content"][1]["image_url"]["url"] == data_url
    assert result[0]["content"] == "项目创作契约"


def test_oversized_inline_media_is_still_refused():
    """The vision-token charge has a floor, so media is never free."""
    # 1MB decoded → charge = 1MB / 128 = 8KB, exactly the per-media floor.
    encoded = base64.b64encode(b"\x00" * (1024 * 1024)).decode()
    data_url = f"data:image/jpeg;base64,{encoded}"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]

    # Three floors are ~24KB of charged media. Under a 16KB budget nothing
    # fits and there is no droppable tool group, so the guard fails closed
    # instead of letting an oversized request reach the provider.
    with pytest.raises(ModelContextBudgetError):
        prepare_model_messages(messages, [], max_bytes=16 * 1024)


def test_long_production_compacts_automatic_history_but_keeps_human_input():
    human_sources = [
        "initial_goal",
        "user",
        "review_rejection_feedback",
        "custom",
    ]
    history = [
        {
            "role": "user",
            "source": source,
            "content": [{"type": "text", "text": f"{source}: 保留全部六集与对白"}],
        }
        for source in human_sources
    ]
    human_input = json.dumps(history, ensure_ascii=False)
    for index in range(80):
        history.append(
            {
                "role": "user",
                "source": (
                    "run_review_feedback"
                    if index % 2
                    else "render_review_feedback"
                ),
                "content": [
                    {
                        "type": "text",
                        "text": f"review-{index}:" + "自动审阅结果。" * 300,
                    },
                ],
            },
        )
    original = json.dumps(history, ensure_ascii=False)
    current = "继续第二集。不要重做已保留视频。"
    continuation = (
        "CONVERSATION_HISTORY_JSON="
        + original
        + "\n\nCURRENT_USER_REQUEST=\n"
        + current
    )
    result = prepare_model_messages(
        [
            {"role": "system", "content": "项目创作契约"},
            {"role": "user", "content": continuation},
        ],
        [],
    )
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 384 * 1024
    view, request = result[1]["content"].split("\n\nCURRENT_USER_REQUEST=\n")
    retained = json.loads(view.split("CONVERSATION_HISTORY_JSON=", 1)[1])
    assert request == current
    assert [
        item for item in retained if item.get("source") in human_sources
    ] == [item | {"metadata": {}} for item in json.loads(human_input)]
    assert retained[-1] == history[-1] | {"metadata": {}}
    assert retained[0]["elidedMessageCount"] > 0
    assert json.dumps(history, ensure_ascii=False) == original

    # Genuine user requirements remain protected, even when they cannot fit.
    for item in history:
        item["source"] = "user"
    with pytest.raises(ModelContextBudgetError):
        prepare_model_messages(
            [
                {
                    "role": "user",
                    "content": "CONVERSATION_HISTORY_JSON="
                    + json.dumps(history, ensure_ascii=False)
                    + "\n\nCURRENT_USER_REQUEST=\n"
                    + current,
                },
            ],
            [],
        )


def test_inline_media_relief_is_additive_across_elided_slots():
    """Compaction carries relief incrementally, which needs additivity.

    ``compact_conversation_history`` no longer re-walks every message to
    recompute relief on each pass; it subtracts the item it just elided.
    That is only exact because relief sums over list items and an elided
    slot is worth zero, so the invariant itself is what this guards.
    """
    encoded = base64.b64encode(b"\x00" * (512 * 1024)).decode()
    media = {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
    }
    first = {"role": "user", "content": [media]}
    second = {"role": "user", "content": [media, media]}
    plain = {"role": "user", "content": "plain text"}
    history = [first, second, plain]

    whole = _inline_media_relief(history)
    assert whole == sum(_inline_media_relief(item) for item in history)
    assert _inline_media_relief(None) == 0

    elided = [first, None, plain]
    assert _inline_media_relief(elided) == whole - _inline_media_relief(
        second,
    )


def test_media_bearing_automatic_history_is_elided_under_budget():
    """Droppable reviews that carry inline media must actually leave.

    Pins the budget accounting where relief changes mid-loop. If the
    carried total were left un-decremented, ``json_bytes`` would shrink
    while relief stayed huge, the check would go negative, and
    compaction would stop after the first message — handing the provider
    a request still over budget instead of reaching it.
    """
    encoded = base64.b64encode(b"\x00" * (512 * 1024)).decode()
    media = {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
    }
    history = [
        {
            "role": "user",
            "source": "initial_goal",
            "content": [{"type": "text", "text": "保留全部六集与对白"}],
        },
    ]
    for index in range(6):
        history.append(
            {
                "role": "user",
                "source": "run_review_feedback",
                "content": [
                    {"type": "text", "text": f"review-{index}"},
                    # Two blocks, so even the cheapest remaining review still
                    # overflows this budget: the guard is that they all leave,
                    # not that compaction stops wherever pricing happens to
                    # allow a single one to linger.
                    media,
                    media,
                ],
            },
        )

    result = compact_conversation_history(history, max_bytes=4096)

    assert result[0]["source"] == "context_compaction"
    assert result[0]["elidedMessageCount"] == 6
    assert [item.get("source") for item in result[1:]] == ["initial_goal"]


@pytest.mark.parametrize(
    ("project_id", "generation", "superseded"),
    [
        ("project-1", None, False),
        ("project-1", 19, False),
        ("project-2", 21, False),
        ("project-1", 20, True),
        ("project-1", 21, True),
    ],
)
def test_new_snapshot_frees_history_space_for_script_and_skills(
    project_id,
    generation,
    superseded,
):
    human = "Keep all six episodes and the accepted films. " + "h" * 700
    previous = {
        "project": {"project_id": "project-1", "description": "old" * 5200},
        "generation": 20,
        "etag": "old-etag",
    }
    history = [
        {
            "role": "user",
            "source": "user",
            "content": [{"type": "text", "text": human}],
        },
        {
            "role": "tool",
            "content": [{"type": "text", "text": json.dumps(previous)}],
        },
    ]
    messages = [
        {"role": "system", "content": "s" * 9000},
        {
            "role": "user",
            "content": "CONVERSATION_HISTORY_JSON="
            + json.dumps(history)
            + "\n\nCURRENT_USER_REQUEST=\nContinue automatically.",
        },
    ]
    calls = []
    if generation is not None:
        calls.append(
            (
                "snapshot",
                json.dumps(
                    {
                        "project": {
                            "project_id": project_id,
                            "description": "current" * 700,
                        },
                        "generation": generation,
                        "etag": "current-etag",
                    },
                ),
            ),
        )
    calls.extend([("skill", "skill" * 1300), ("script", "script" * 1100)])
    for call_id, content in calls:
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "Read " + call_id,
                    "tool_calls": [
                        AgentToolCall(
                            call_id=call_id,
                            name="read_project",
                            arguments={},
                        ).history_dict(),
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": content,
                },
            ],
        )
    original = json.dumps(messages)
    result = prepare_model_messages(messages, [], max_bytes=32 * 1024)
    continuation = result[1]["content"]
    assert human in continuation
    assert continuation.endswith(
        "CURRENT_USER_REQUEST=\nContinue automatically.",
    )
    assert ("old" * 5200 in continuation) is not superseded
    if superseded:
        assert [m["tool_call_id"] for m in result if m["role"] == "tool"] == [
            "snapshot",
            "skill",
            "script",
        ]
    assert json.dumps(messages) == original


def _configure_text_model(
    monkeypatch: pytest.MonkeyPatch,
    *,
    protocol: str = "",
) -> None:
    monkeypatch.setattr(
        model_client.model_config,
        "get_text_api_key",
        lambda: "test-api-key",
    )
    monkeypatch.setattr(
        model_client.model_config,
        "get_text_base_url",
        lambda: "https://model.example/v1",
    )
    monkeypatch.setattr(
        model_client.model_config,
        "get_text_model_name",
        lambda: "qwen3.7-plus",
    )
    monkeypatch.setattr(
        model_client.model_config,
        "get_text_protocol",
        lambda: protocol,
    )


def _tools() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_project",
                "parameters": {
                    "type": "object",
                    "properties": {"projectId": {"type": "string"}},
                    "required": ["projectId"],
                },
            },
        },
    ]


def test_default_client_constructs_real_agentscope_dashscope_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_text_model(monkeypatch)

    configured = AgentScopeAgentChatClient()._configured_model()

    assert isinstance(configured, DashScopeChatModel)


@pytest.mark.parametrize(
    ("name", "override", "expected"),
    [
        ("qwen3.8-max", None, 4096),
        ("qwen3-vl-plus", "8192", 8192),
        ("qwen3.8-max", "invalid", 4096),
        ("qwen3.8-max", "0", 4096),
        ("other-compatible-model", None, None),
    ],
)
def test_reasoning_budget_reaches_sdk_request_without_limiting_tool_output(
    monkeypatch,
    name,
    override,
    expected,
):
    _configure_text_model(monkeypatch)
    monkeypatch.setattr(
        model_client.model_config,
        "get_text_model_name",
        lambda: name,
    )
    if override is None:
        monkeypatch.delenv("CREATOR_AGENT_THINKING_BUDGET", raising=False)
    else:
        monkeypatch.setenv("CREATOR_AGENT_THINKING_BUDGET", override)
    configured = AgentScopeAgentChatClient()._configured_model()
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("request captured before transport")

    monkeypatch.setattr(configured.client.chat.completions, "create", capture)
    with pytest.raises(RuntimeError, match="request captured"):
        asyncio.run(
            configured._call_api(
                model_name=name,
                messages=records_to_agentscope_messages(
                    [
                        {
                            "role": "user",
                            "content": "Plan a short branching story",
                        },
                    ],
                ),
                tools=_tools(),
            ),
        )
    assert captured["extra_body"].get("thinking_budget") == expected
    assert "max_tokens" not in captured
    assert captured["tools"][0]["function"]["name"] == "read_project"


@pytest.mark.parametrize(
    ("model_name", "expected_temperature"),
    [("qwen3.7-plus", 0.2), ("kimi-k3", None)],
)
def test_kimi_k3_omits_unsupported_temperature(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    expected_temperature: float | None,
) -> None:
    # DashScope rejects an explicit ``temperature`` for kimi-k3, so the client
    # must drop it and let the gateway apply the model default; every other
    # model keeps the Creator sampling default.
    _configure_text_model(monkeypatch)
    monkeypatch.setattr(
        model_client.model_config,
        "get_text_model_name",
        lambda: model_name,
    )

    configured = AgentScopeAgentChatClient()._configured_model()

    assert isinstance(configured, DashScopeChatModel)
    assert configured.parameters.temperature == expected_temperature


def test_temperature_refusal_rebuilds_cached_model_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A first-turn refusal must invalidate the cached model so the *next*
    # Agent turn is rebuilt without a temperature. ``_configured_model()``
    # keys only on the connection tuple, which the capability flip does not
    # change, so the client has to drop ``model``/``_configuration`` itself.
    # pylint: disable=protected-access
    from models.model_capability_cache import get_capability_cache

    model_config = model_client.model_config
    _configure_text_model(monkeypatch)
    monkeypatch.setattr(
        model_config,
        "get_text_model_name",
        lambda: "brand-new-model",
    )
    cache = get_capability_cache()
    key = model_config._temperature_capability_key("brand-new-model")
    cache.clear(key)
    try:
        client = AgentScopeAgentChatClient()
        first = client._configured_model()
        assert isinstance(first, DashScopeChatModel)
        # Before any refusal the sampling default is still sent.
        assert first.parameters.temperature == 0.2

        async def _refuse(self, *_args, **_kwargs):
            raise RuntimeError(
                "Parameter 'temperature'=0.2 is not supported "
                "for brand-new-model",
            )

        monkeypatch.setattr(DashScopeChatModel, "__call__", _refuse)
        with pytest.raises(AgentModelError):
            asyncio.run(
                client.complete(
                    messages=[{"role": "user", "content": "ping"}],
                    tools=[],
                ),
            )

        # The refusal was learned and the cached model dropped, so the next
        # turn rebuilds and omits the temperature entirely.
        assert model_config.chat_temperature("brand-new-model", 0.2) is None
        assert client.model is None
        second = client._configured_model()
        assert second is not first
        assert second.parameters.temperature is None
    finally:
        cache.clear(key)


@pytest.mark.parametrize(
    ("protocol", "base_url"),
    [
        ("Anthropic Claude", "https://api.anthropic.com"),
        ("MiniMax", "https://api.minimaxi.com/anthropic"),
    ],
)
def test_anthropic_compatible_protocol_constructs_anthropic_chat_model(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    base_url: str,
) -> None:
    from agentscope.model import AnthropicChatModel

    _configure_text_model(monkeypatch, protocol=protocol)
    monkeypatch.setattr(
        model_client.model_config,
        "get_text_base_url",
        lambda: base_url,
    )
    assert isinstance(
        AgentScopeAgentChatClient()._configured_model(),
        AnthropicChatModel,
    )


def test_anthropic_agent_uses_model_limit_instead_of_sdk_default(monkeypatch):
    _configure_text_model(monkeypatch, protocol="Anthropic Claude")
    monkeypatch.setattr(
        model_client.model_config,
        "get_text_model_name",
        lambda: "MiniMax-M2.7",
    )
    client = AgentScopeAgentChatClient()
    configured = client._configured_model()
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("request captured before transport")

    monkeypatch.setattr(configured.client.messages, "create", capture)
    with pytest.raises(AgentModelError, match="request captured"):
        asyncio.run(
            client.complete(
                messages=[{"role": "user", "content": "ping"}],
                tools=[],
            ),
        )
    assert captured["max_tokens"] == 204800


def test_gemini_protocol_constructs_gemini_chat_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentscope.model import GeminiChatModel

    _configure_text_model(
        monkeypatch,
        protocol="Google Gemini",
    )
    monkeypatch.setattr(
        model_client.model_config,
        "get_text_base_url",
        lambda: "https://generativelanguage.googleapis.com",
    )

    configured = AgentScopeAgentChatClient()._configured_model()

    assert isinstance(configured, GeminiChatModel)


def test_history_is_rehydrated_as_agentscope_tool_blocks() -> None:
    messages = records_to_agentscope_messages(
        [
            {"role": "system", "content": "schema"},
            {"role": "user", "content": "读取项目"},
            {
                "role": "assistant",
                "content": "先读取。",
                "tool_calls": [
                    {
                        "id": "call-read-1",
                        "type": "function",
                        "function": {
                            "name": "read_project",
                            "arguments": '{"projectId":"project-1"}',
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-read-1",
                "name": "read_project",
                "content": '{"ok":false}',
                "failed": True,
            },
        ],
    )

    assert messages[0].role == "system"
    assert messages[1].role == "user"
    assert messages[2].role == "assistant"
    call = messages[2].content[-1]
    assert isinstance(call, ToolCallBlock)
    assert call.id == "call-read-1"
    assert call.name == "read_project"
    assert json.loads(call.input) == {"projectId": "project-1"}
    assert messages[3].role == "assistant"
    result = messages[3].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.id == call.id
    assert result.name == call.name
    assert result.output == '{"ok":false}'
    assert result.state == ToolResultState.ERROR.value


def test_vlm_anthropic_protocol_constructs_anthropic_chat_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentscope.model import AnthropicChatModel

    monkeypatch.setattr(
        model_client.model_config,
        "get_vlm_api_key",
        lambda: "vlm-key",
    )
    monkeypatch.setattr(
        model_client.model_config,
        "get_vlm_base_url",
        lambda: "https://api.anthropic.com",
    )
    monkeypatch.setattr(
        model_client.model_config,
        "get_vlm_model_name",
        lambda: "claude-sonnet-4-20250514",
    )
    monkeypatch.setattr(
        model_client.model_config,
        "get_vlm_timeout_seconds",
        lambda: 180,
    )
    monkeypatch.setattr(
        model_client.model_config,
        "get_vlm_protocol",
        lambda: "Anthropic Claude",
    )

    configured = AgentScopeVlmChatClient()._configured_model()

    assert isinstance(configured, AnthropicChatModel)


def test_agentscope_client_streams_native_blocks_and_raw_argument_deltas() -> (
    None
):
    class StreamingAgentScopeModel:
        model = "qwen3.7-plus"

        def __init__(self) -> None:
            self.messages = None
            self.tools = None

        async def __call__(self, messages, *, tools=None):
            self.messages = messages
            self.tools = tools

            async def chunks():
                yield ChatResponse(
                    id="response-stream-1",
                    content=[
                        ThinkingBlock(thinking="先"),
                        TextBlock(text="项目"),
                        ToolCallBlock(
                            id="call-read-1",
                            name="read_project",
                            input='{"project',
                        ),
                    ],
                    is_last=False,
                )
                yield ChatResponse(
                    id="response-stream-1",
                    content=[
                        ThinkingBlock(thinking="分析"),
                        TextBlock(text="已读取"),
                        ToolCallBlock(
                            id="call-read-1",
                            name="read_project",
                            input='Id":"project-1"}',
                        ),
                    ],
                    is_last=False,
                )
                yield ChatResponse(
                    id="response-stream-1",
                    content=[
                        ThinkingBlock(thinking="先分析"),
                        TextBlock(text="项目已读取"),
                        ToolCallBlock(
                            id="call-read-1",
                            name="read_project",
                            input='{"projectId":"project-1"}',
                        ),
                    ],
                    is_last=True,
                )

            return chunks()

    text_deltas: list[str] = []
    thinking_deltas: list[str] = []
    tool_deltas: list[tuple[str, str, str]] = []

    async def on_text_delta(delta: str) -> None:
        text_deltas.append(delta)

    async def on_thinking_delta(delta: str) -> None:
        thinking_deltas.append(delta)

    async def on_tool_call_delta(
        call_id: str,
        name: str,
        arguments_delta: str,
    ) -> None:
        tool_deltas.append((call_id, name, arguments_delta))

    async def scenario():
        provider = StreamingAgentScopeModel()
        client = AgentScopeAgentChatClient(provider)  # type: ignore[arg-type]
        turn = await client.complete(
            messages=[
                {"role": "system", "content": "schema"},
                {"role": "user", "content": "读取项目"},
            ],
            tools=_tools(),
            on_text_delta=on_text_delta,
            on_thinking_delta=on_thinking_delta,
            on_tool_call_delta=on_tool_call_delta,
        )
        return provider, turn

    provider, turn = asyncio.run(scenario())

    assert provider.messages is not None
    assert [message.role for message in provider.messages] == [
        "system",
        "user",
    ]
    assert provider.tools == _tools()
    assert turn == AgentModelTurn(
        content="项目已读取",
        thinking="先分析",
        tool_calls=(
            AgentToolCall(
                call_id="call-read-1",
                name="read_project",
                arguments={"projectId": "project-1"},
            ),
        ),
        provider_message_id="response-stream-1",
    )
    assert text_deltas == ["项目", "已读取"]
    assert thinking_deltas == ["先", "分析"]
    assert tool_deltas == [
        ("call-read-1", "read_project", '{"project'),
        ("call-read-1", "read_project", 'Id":"project-1"}'),
    ]
    assert turn.tool_calls[0].raw_arguments == '{"projectId":"project-1"}'
    assert turn.tool_calls[0].raw_arguments_bytes == 25
    assert turn.tool_calls[0].provider_chunk_count == 2
    assert turn.finish_reason == "completed"


@pytest.mark.parametrize("ending", ["text", "tool", "final", "provider_error"])
def test_thinking_chunks_are_coalesced_without_losing_order_or_tail(
    monkeypatch,
    ending,
):
    clock = [0.0]
    monkeypatch.setattr(model_client, "monotonic", lambda: clock[0])
    fragments = [f"片段{i:03d}。" for i in range(120)]
    observed = []

    async def thinking(delta):
        observed.append(("thinking", delta))

    async def text(delta):
        observed.append(("text", delta))

    async def tool(_call_id, _name, delta):
        observed.append(("tool", delta))

    class Provider:
        calls = 0

        async def __call__(self, _messages, *, tools=None):
            self.calls += 1

            async def chunks():
                for fragment in fragments:
                    clock[0] += 0.01
                    yield ChatResponse(
                        id="coalesced",
                        is_last=False,
                        content=[ThinkingBlock(thinking=fragment)],
                    )
                if ending == "provider_error":
                    raise ValueError("invalid provider response")
                result = (
                    ToolCallBlock(
                        id="read",
                        name="read_project",
                        input="{}",
                    )
                    if ending == "tool"
                    else TextBlock(text="完成")
                )
                if ending != "final":
                    yield ChatResponse(
                        id="coalesced",
                        is_last=False,
                        content=[result],
                    )
                yield ChatResponse(
                    id="coalesced",
                    is_last=True,
                    content=[
                        ThinkingBlock(thinking="".join(fragments)),
                        result,
                    ],
                )

            return chunks()

    async def scenario():
        provider = Provider()
        request = AgentScopeAgentChatClient(provider).complete(
            messages=[],
            tools=_tools(),
            on_thinking_delta=thinking,
            on_text_delta=text,
            on_tool_call_delta=tool,
        )
        if ending == "provider_error":
            with pytest.raises(
                AgentModelError,
                match="invalid provider response",
            ):
                await request
        else:
            turn = await request
            assert turn.thinking == "".join(fragments)
        assert provider.calls == 1

    asyncio.run(scenario())
    thinking_parts = [delta for kind, delta in observed if kind == "thinking"]
    assert "".join(thinking_parts) == "".join(fragments)
    assert thinking_parts[0] == fragments[0]  # First activity is immediate.
    assert len(thinking_parts) == 4  # 120 durable writes become four.
    if ending in {"text", "tool"}:
        assert all(kind == "thinking" for kind, _ in observed[:-1])
        assert observed[-1][0] == ending


@pytest.mark.parametrize("failure", ["cancel", "persistence"])
def test_buffered_thinking_preserves_stop_and_persistence_failures(failure):
    observed = []

    async def callback(delta):
        observed.append(delta)
        if delta == "tail" and failure == "persistence":
            raise OSError("disk write failed")

    class Provider:
        calls = 0

        async def __call__(self, _messages, *, tools=None):
            self.calls += 1

            async def chunks():
                for delta in ("first", "tail"):
                    yield ChatResponse(
                        id="stopped",
                        is_last=False,
                        content=[ThinkingBlock(thinking=delta)],
                    )
                if failure == "cancel":
                    raise asyncio.CancelledError()
                yield ChatResponse(
                    id="stopped",
                    is_last=True,
                    content=[TextBlock(text="finished")],
                )

            return chunks()

    async def scenario():
        provider = Provider()
        expected = (
            asyncio.CancelledError
            if failure == "cancel"
            else AgentStreamCallbackError
        )
        with pytest.raises(expected):
            await AgentScopeAgentChatClient(provider).complete(
                messages=[],
                tools=[],
                on_thinking_delta=callback,
            )
        assert provider.calls == 1

    asyncio.run(scenario())
    assert observed == (
        ["first"] if failure == "cancel" else ["first", "tail"]
    )


def test_agentscope_client_repairs_truncated_native_tool_argument_json() -> (
    None
):
    class MalformedToolArgumentsModel:
        model = "qwen3.7-plus"

        async def __call__(self, messages, *, tools=None):
            del messages
            assert tools
            return ChatResponse(
                id="response-repaired-tool",
                content=[
                    ToolCallBlock(
                        id="call-read-repaired",
                        name="read_project",
                        input='{"projectId":"project-1","baseEtag":"etag-truncated',
                    ),
                ],
                is_last=True,
            )

    async def scenario() -> AgentModelTurn:
        client = AgentScopeAgentChatClient(
            MalformedToolArgumentsModel(),  # type: ignore[arg-type]
        )
        return await client.complete(
            messages=[{"role": "user", "content": "读取项目"}],
            tools=_tools(),
        )

    turn = asyncio.run(scenario())

    assert turn.tool_calls == (
        AgentToolCall(
            call_id="call-read-repaired",
            name="read_project",
            arguments={
                "projectId": "project-1",
                "baseEtag": "etag-truncated",
            },
        ),
    )
    repaired_call = turn.tool_calls[0]
    assert repaired_call.arguments_repaired is True
    assert repaired_call.raw_arguments_bytes == len(
        '{"projectId":"project-1","baseEtag":"etag-truncated'.encode(
            "utf-8",
        ),
    )
    assert repaired_call.strict_json_error is not None


def _final_tool_call_model(raw_input: str):
    class FinalToolCallModel:
        model = "qwen3.7-plus"

        async def __call__(self, messages, *, tools=None):
            async def chunks():
                yield ChatResponse(
                    id="response-broken-1",
                    content=[
                        ToolCallBlock(
                            id="call-jq-1",
                            name="read_project",
                            input=raw_input,
                        ),
                    ],
                    is_last=True,
                )

            return chunks()

    return FinalToolCallModel()


def test_unrecoverable_tool_arguments_degrade_to_parse_error() -> None:
    async def scenario():
        client = AgentScopeAgentChatClient(
            _final_tool_call_model('"oops'),  # type: ignore[arg-type]
        )
        return await client.complete(messages=[], tools=_tools())

    turn = asyncio.run(scenario())
    call = turn.tool_calls[0]
    assert call.arguments == {}
    assert call.parse_error is not None
    assert "无法自动修复" in call.parse_error
    assert "oops" in call.parse_error


def test_agentscope_client_preserves_stream_control_exceptions() -> None:
    class StreamingAgentScopeModel:
        model = "qwen3.7-plus"

        async def __call__(self, _messages, *, tools=None):
            del tools

            async def chunks():
                yield ChatResponse(
                    id="response-control",
                    content=[TextBlock(text="部分输出")],
                    is_last=False,
                )
                yield ChatResponse(
                    id="response-control",
                    content=[TextBlock(text="完整输出")],
                    is_last=True,
                )

            return chunks()

    class StopStreaming(AgentStreamCallbackPassthrough):
        pass

    async def stop(_delta: str) -> None:
        raise StopStreaming("run revoked")

    async def scenario() -> None:
        client = AgentScopeAgentChatClient(StreamingAgentScopeModel())  # type: ignore[arg-type]
        with pytest.raises(StopStreaming, match="run revoked"):
            await client.complete(
                messages=[{"role": "user", "content": "开始"}],
                tools=[],
                on_text_delta=stop,
            )

    asyncio.run(scenario())


def test_agentscope_client_returns_unknown_native_tool_for_model_correction() -> (
    None
):
    class UnknownToolModel:
        model = "qwen3.7-plus"

        async def __call__(self, messages, *, tools=None):
            del messages
            assert tools
            return ChatResponse(
                id="response-unknown-tool",
                content=[
                    ToolCallBlock(
                        id="call-unknown",
                        name="invented_tool",
                        input='{"projectId":"project-1"}',
                    ),
                ],
                is_last=True,
            )

    async def scenario():
        client = AgentScopeAgentChatClient(  # type: ignore[arg-type]
            UnknownToolModel(),
        )
        return await client.complete(
            messages=[{"role": "user", "content": "读取"}],
            tools=_tools(),
        )

    turn = asyncio.run(scenario())
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == "invented_tool"
    assert turn.tool_calls[0].arguments == {"projectId": "project-1"}


@pytest.mark.parametrize(
    "markup",
    [
        "<function=glob_search><parameter=pattern>story/outline.md</parameter></function>",
        '<tool_call>{"name":"read_project","arguments":{}}</tool_call>',
    ],
)
def test_agentscope_client_rejects_textual_tool_markup_before_text_callback(
    markup: str,
) -> None:
    class TextualToolModel:
        model = "qwen3.7-plus"

        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, messages, *, tools=None):
            del messages
            assert tools
            self.calls += 1
            return ChatResponse(
                id="response-text-tool",
                content=[TextBlock(text=markup)],
                is_last=True,
            )

    text_deltas: list[str] = []

    async def collect(delta: str) -> None:
        text_deltas.append(delta)

    async def scenario() -> TextualToolModel:
        provider = TextualToolModel()
        client = AgentScopeAgentChatClient(provider)  # type: ignore[arg-type]
        with pytest.raises(AgentModelError, match="ToolCallBlock"):
            await client.complete(
                messages=[{"role": "user", "content": "读取"}],
                tools=_tools(),
                on_text_delta=collect,
            )
        return provider

    provider = asyncio.run(scenario())
    # Markup degradation is stochastic, so the turn is retried before the
    # run is failed: original attempt plus four retries.
    assert provider.calls == 5
    assert text_deltas == []


def test_callback_client_keeps_stream_callback_failures_outside_model_errors() -> (
    None
):
    async def callback(_messages, _tools) -> AgentModelTurn:
        return AgentModelTurn(content="即将持久化")

    async def fail_persistence(_delta: str) -> None:
        raise OSError("runtime lock timeout")

    async def scenario() -> None:
        with pytest.raises(AgentStreamCallbackError) as raised:
            await CallbackAgentChatClient(callback).complete(
                messages=[{"role": "user", "content": "开始"}],
                tools=[],
                on_text_delta=fail_persistence,
            )
        assert isinstance(raised.value.cause, OSError)
        assert str(raised.value.cause) == "runtime lock timeout"

    asyncio.run(scenario())


def test_agentscope_client_retries_rate_limited_turns_until_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model_client,
        "_rate_limit_retry_delay",
        lambda _attempt: 0.0,
    )

    class ThrottledModel:
        model = "qwen3.7-plus"

        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, messages, *, tools=None):
            del messages, tools
            self.calls += 1
            raise RuntimeError(
                "<429> Throttling.RateQuota: Requests rate limit exceeded",
            )

    notices: list[RateLimitRetryNotice] = []

    async def on_retry(notice: RateLimitRetryNotice) -> None:
        notices.append(notice)

    async def scenario():
        provider = ThrottledModel()
        client = AgentScopeAgentChatClient(provider)  # type: ignore[arg-type]
        with pytest.raises(RateLimitExhaustedError) as raised:
            await client.complete(
                messages=[{"role": "user", "content": "开始"}],
                tools=_tools(),
                on_rate_limit_retry=on_retry,
            )
        return provider, raised.value

    provider, error = asyncio.run(scenario())

    # One original attempt plus every retry, then the run is given up on.
    assert provider.calls == MAX_RATE_LIMIT_RETRIES + 1
    assert error.retries == MAX_RATE_LIMIT_RETRIES
    assert [(notice.attempt, notice.max_attempts) for notice in notices] == [
        (attempt, MAX_RATE_LIMIT_RETRIES) for attempt in range(1, 6)
    ]


@pytest.mark.parametrize(
    "failure, delays, reason",
    [
        ("429 Too Many Requests", [2, 4], "rate_limit"),
        ("Connection reset by peer", [1], "transient"),
        ("empty", [0], "empty_response"),
    ],
    ids=["rate-limit", "connection-reset", "empty-response"],
)
def test_model_retry_recovery_reports_real_cause_and_backoff(
    monkeypatch,
    failure,
    delays,
    reason,
):
    notices = []
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(model_client.asyncio, "sleep", sleep)

    class RecoveringModel:
        model = "test-model"
        calls = 0

        async def __call__(self, messages, *, tools=None):
            self.calls += 1
            if self.calls <= len(delays):
                if failure == "empty":
                    return ChatResponse(id="empty", content=[], is_last=True)
                raise RuntimeError(failure)
            return ChatResponse(
                id="recovered",
                content=[TextBlock(text="继续生成")],
                is_last=True,
            )

    async def on_retry(notice):
        notices.append(notice)

    provider = RecoveringModel()
    result = asyncio.run(
        AgentScopeAgentChatClient(provider).complete(
            messages=[{"role": "user", "content": "继续"}],
            tools=_tools(),
            on_rate_limit_retry=on_retry,
        ),
    )
    assert result.content == "继续生成"
    assert provider.calls == len(delays) + 1
    assert [(n.reason, n.attempt, n.delay_seconds) for n in notices] == [
        (reason, attempt, delay) for attempt, delay in enumerate(delays, 1)
    ]
    assert sleeps == [delay for delay in delays if delay]


def test_inline_media_relief_does_not_walk_plain_strings(monkeypatch):
    """A string cannot lead to a data URL, so it must not cost a call.

    The estimate runs over the whole history on every turn, and the walk used
    to descend into every string it passed — including the megabytes of a
    serialized script — to return zero each time.
    """

    original = model_context._inline_media_relief
    visited: list = []

    def counting(node, *, budget_bytes=model_context.MODEL_INPUT_BYTES):
        visited.append(node)
        return original(node, budget_bytes=budget_bytes)

    monkeypatch.setattr(model_context, "_inline_media_relief", counting)
    history = [
        {"role": "user", "content": "beat sheet " * 4000},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "notes " * 4000}],
        },
    ]

    assert counting(history) == 0
    assert [node for node in visited if isinstance(node, str)] == []

    # What the walk is for still works: a data URL under a ``url`` key.
    encoded = base64.b64encode(b"\x00" * (256 * 1024)).decode()
    media = {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
    }
    assert original([{"role": "user", "content": [media]}]) > 0


def test_one_huge_screenshot_can_no_longer_refuse_its_own_request():
    """A single media block must never outgrow the entire input budget.

    Regression guard: the inline-media ceiling was an absolute 512KB, which
    is larger than ``MODEL_INPUT_BYTES`` itself. One big screenshot therefore
    overflowed the request all by itself, while compaction was forbidden from
    dropping the contract message carrying it — the turn got refused locally
    and never reached the provider.
    """
    encoded = base64.b64encode(b"\x00" * (8 * 1024 * 1024)).decode()
    data_url = f"data:image/png;base64,{encoded}"
    messages = [
        {"role": "system", "content": "项目创作契约"},
        {
            "role": "user",
            "source": "user",
            "content": [
                {"type": "text", "text": "理解这张图"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]

    result = prepare_model_messages(messages, [])

    # It fits now, and nothing about the human attachment was rewritten.
    assert result[1]["content"][1]["image_url"]["url"] == data_url
    ceiling = model_context._media_charge_bounds(
        model_context.MODEL_INPUT_BYTES,
    )[1]
    assert ceiling < model_context.MODEL_INPUT_BYTES


def test_derived_inline_media_demotes_to_a_pointer_and_spares_human_input():
    """Only media a tool produced can leave; a human attachment cannot."""
    encoded = base64.b64encode(b"\x00" * (4 * 1024 * 1024)).decode()
    data_url = f"data:image/jpeg;base64,{encoded}"
    messages = [
        {
            "role": "tool",
            "tool_call_id": "call-frame",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
        {
            "role": "user",
            "source": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]

    result, receipt = model_context.demote_derived_inline_media(messages)

    stub = result[0]["content"][0]["text"]
    assert result[0]["content"][0]["type"] == "text"
    assert "image/jpeg" in stub
    assert "fingerprint=sha256:" in stub
    # The human upload stays byte-for-byte: no durable handle exists yet.
    assert result[1]["content"][0]["image_url"]["url"] == data_url
    assert receipt["demoted_blocks"] == 1
    assert receipt["human_attachment_blocks"] == 1


def test_budget_failure_names_the_blocks_that_overflowed():
    """A refusal has to say what it blamed, not just that it refused."""
    encoded = base64.b64encode(b"\x00" * (1024 * 1024)).decode()
    data_url = f"data:image/jpeg;base64,{encoded}"
    media = {"type": "image_url", "image_url": {"url": data_url}}
    messages = [
        {"role": "user", "source": "user", "content": [media, media, media]},
    ]

    with pytest.raises(ModelContextBudgetError) as caught:
        prepare_model_messages(messages, [], max_bytes=16 * 1024)

    info = caught.value.diagnostics
    assert info["media_block_count"] == 3
    assert info["max_bytes"] == 16 * 1024
    assert info["used_bytes"] > info["max_bytes"]
    assert (
        info["media_blocks"][0]["charge"] == info["media_blocks"][-1]["charge"]
    )
    text = model_context.format_budget_failure(caught.value)
    assert "内联媒体 3 块" in text
    assert "image/jpeg" in text
    assert "user 16" in text or "user" in text


def test_media_clamp_ratio_env_cannot_disable_the_guard(monkeypatch):
    """The escape hatch tunes the ceiling; nonsense falls back to default."""
    monkeypatch.setenv("CREATOR_MEDIA_CLAMP_RATIO", "0.25")
    assert model_context._media_charge_bounds(100_000)[1] == 25_000
    for broken in ("0", "-1", "abc", ""):
        if broken:
            monkeypatch.setenv("CREATOR_MEDIA_CLAMP_RATIO", broken)
        else:
            monkeypatch.delenv("CREATOR_MEDIA_CLAMP_RATIO", raising=False)
        assert model_context._media_charge_bounds(100_000)[1] == 50_000


def _screenshot_data_url(width: int, height: int) -> str:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 120, 120)).save(
        buffer,
        format="PNG",
    )
    encoded = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{encoded}"


@pytest.mark.skipif(
    not model_context._HOST_CONTEXT_ESTIMATOR_AVAILABLE,
    reason="host estimator is optional for standalone plugin installs",
)
def test_media_is_priced_by_the_hosts_vision_estimate():
    """One shared answer for "what will this image cost".

    The byte conversion saw a 3840x2160 screenshot as roughly 2K tokens, so a
    compactor watching it kept concluding there was room left. The host
    estimate counts Qwen-VL 28px patches and puts the same image near 11.8K.
    """
    from qwenpaw.agents.utils.media_token_estimate import (
        estimate_data_url_tokens,
    )

    data_url = _screenshot_data_url(3840, 2160)

    assert model_context._data_url_tokens(
        data_url,
    ) == estimate_data_url_tokens(data_url)
    # Explicitly above what the old /4 byte conversion could ever report.
    assert estimate_data_url_tokens(data_url) > 8_192 // 4


def test_token_budget_compacts_what_the_byte_guard_cannot_see():
    """A request can be small on the wire and enormous to the model.

    Inline media is floored in the byte budget, so a 4K screenshot plus one
    completed tool exchange stays well under 384KB while costing the provider
    tens of thousands of tokens. Only the window guard removes the exchange.
    """
    data_url = _screenshot_data_url(3840, 2160)
    verbose_result = "镜头描述 " * 12_000
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "read"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": verbose_result,
        },
        {
            "role": "user",
            "source": "user",
            "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]

    # Without the window guard the exchange stays: bytes never crossed.
    unguarded = prepare_model_messages(messages, [])
    assert [item["role"] for item in unguarded].count("tool") == 1

    guarded = prepare_model_messages(messages, [], max_tokens=20_000)
    assert [item["role"] for item in guarded].count("tool") == 0
    assert model_context.estimate_input_tokens(guarded) <= 20_000


def test_input_token_budget_never_invents_an_unpublished_window():
    """The reserve leaves room for the reply; silence disables the guard."""
    client = AgentScopeAgentChatClient.__new__(AgentScopeAgentChatClient)

    class _SizedModel:
        context_size = 32_000

    class _BlindModel:
        pass

    client._configured_model = _SizedModel
    assert client._input_token_budget() == 32_000 - max(1024, 32_000 // 8)

    client._configured_model = _BlindModel
    assert client._input_token_budget() is None

    def broken():
        raise RuntimeError("model not configured")

    client._configured_model = broken
    assert client._input_token_budget() is None


# ---------------------------------------------------------------------------
# P2: a demoted block stays recallable, and only proven pressure moves a
# human upload.
# ---------------------------------------------------------------------------

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _data_url(payload: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def _blob_vault(tmp_path):
    """A vault over the real content store, so handles name real bytes."""
    store = ContentStore(tmp_path)
    return media_vault.make_vault(store), store


def _attachment(data_url: str, *, seq: int = 7) -> dict:
    return {
        "role": "user",
        "source": "user",
        "messageSeq": seq,
        "content": [
            {"type": "text", "text": "按这张参考图改分镜"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }


def test_a_demoted_human_attachment_reads_back_byte_for_byte(tmp_path):
    """Pressure may move a person's upload, but never without its bytes.

    The pointer left in the vacated slot is the only answer to "what was the
    reference image", so the exact payload behind it has to come back. A
    demotion that could not promise that would be rewriting a requirement and
    calling the loss recallable.
    """
    payload = _PNG_MAGIC + b"frame" * 40_000
    data_url = _data_url(payload)
    vault, store = _blob_vault(tmp_path)
    digest = hashlib.sha256(payload).hexdigest()

    result, receipt = model_context.demote_human_inline_media(
        [_attachment(data_url)],
        vault=vault,
    )

    assert receipt["demoted_blocks"] == 1
    assert receipt["stored_blocks"] == 1
    assert receipt["refused_blocks"] == 0
    stub = result[0]["content"][1]
    assert stub["type"] == "text"
    # Announced as what it was: an unexplained hole where a person's screenshot
    # was reads like the person withdrew the requirement.
    assert "user attachment moved out of this request" in stub["text"]
    assert f"handle=sha256:{digest}" in stub["text"]
    assert "recall_media" in stub["text"]
    assert result[0]["content"][0]["text"] == "按这张参考图改分镜"

    recorded = result[0]["metadata"][model_context.DEMOTED_MEDIA_METADATA_KEY][
        0
    ]
    assert recorded["handle"] == digest
    assert recorded["mime"] == "image/png"
    assert recorded["decodedBytes"] == len(payload)
    assert recorded["messageSeq"] == 7
    assert recorded["estimatedTokens"] > 0

    restored = media_vault.load_by_handle(f"sha256:{digest}", store=store)
    assert restored is not None
    assert base64.b64decode(restored[0].split(";base64,", 1)[1]) == payload
    assert restored[1] == len(payload)


def test_a_human_attachment_without_a_durable_copy_is_never_rewritten():
    """The budget is allowed to fail; an upload is not allowed to vanish.

    Without a vault, or when the write itself fails, a demotion would leave a
    pointer to bytes nobody stored. Refusing keeps the person's input intact
    and the failure honest instead of trading one for the other.
    """
    payload = _PNG_MAGIC + b"frame" * 40_000
    data_url = _data_url(payload)
    messages = [_attachment(data_url)]

    for label, vault in (
        ("no vault at all", None),
        ("a vault that cannot write", lambda _url: None),
    ):
        result, receipt = model_context.demote_human_inline_media(
            messages,
            vault=vault,
        )

        assert receipt["demoted_blocks"] == 0, label
        assert receipt["refused_blocks"] == 1, label
        assert result[0]["content"][1]["image_url"]["url"] == data_url, label
        assert "metadata" not in result[0], label


def test_a_partially_storable_attachment_is_demoted_whole_or_not_at_all():
    """A requirement cut in half reads like a person who changed their mind."""
    first = _data_url(_PNG_MAGIC + b"frame" * 40_000)
    second = _data_url(b"second reference")
    first_digest = hashlib.sha256(_PNG_MAGIC + b"frame" * 40_000).hexdigest()

    def store_only_first(url: str) -> str | None:
        return first_digest if url == first else None

    message = {
        "role": "user",
        "source": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": first}},
            {"type": "image_url", "image_url": {"url": second}},
        ],
    }

    result, receipt = model_context.demote_human_inline_media(
        [message],
        vault=store_only_first,
    )

    assert receipt["demoted_blocks"] == 0
    assert receipt["refused_blocks"] == 2
    assert result[0]["content"][0]["image_url"]["url"] == first
    assert result[0]["content"][1]["image_url"]["url"] == second


def test_a_recalled_block_is_derived_media_again():
    """Recall must not re-pin the window it just freed.

    Bytes the Agent brought back are the runtime's own observation: the
    pointer that answered once can answer again, so the next tight budget has
    to be able to move them without reaching for a human message.
    """
    data_url = _data_url(b"\x00" * (2 * 1024 * 1024), mime="image/jpeg")
    messages = [
        {
            "role": "user",
            "source": model_context.MEDIA_RECALL_SOURCE,
            "content": [{"type": "image_url", "image_url": {"url": data_url}}],
        },
    ]

    result, receipt = model_context.demote_derived_inline_media(messages)

    assert receipt["demoted_blocks"] == 1
    assert receipt["human_attachment_blocks"] == 0
    assert result[0]["content"][0]["type"] == "text"
    assert "inline media left this context" in result[0]["content"][0]["text"]


def test_history_compaction_keeps_the_handle_and_drops_the_rest():
    """A pointer outlives the turns between demotion and the recall.

    The handle is the only place a demoted attachment can be found from, so
    history compaction has to carry it; the tracing noise next to it does not
    belong in the model's input at all.
    """
    history = [
        {
            "role": "user",
            "source": "user",
            "messageSeq": 3,
            "content": [{"type": "text", "text": "参考图已移出本次请求"}],
            "metadata": {
                model_context.DEMOTED_MEDIA_METADATA_KEY: [
                    {"handle": "a" * 64},
                ],
                "streamingDiagnostics": {"chunks": 128},
            },
        },
    ]

    result = compact_conversation_history(history)

    kept = result[0]["metadata"]
    assert kept[model_context.DEMOTED_MEDIA_METADATA_KEY] == [
        {"handle": "a" * 64},
    ]
    assert "streamingDiagnostics" not in kept


def _ladder_payload() -> list[dict]:
    """A contract text plus one derived observation and one human upload."""
    data_url = _data_url(b"\x00" * (2 * 1024 * 1024), mime="image/jpeg")
    return [
        {"role": "system", "content": "A" * 5_000},
        {
            "role": "tool",
            "tool_call_id": "call-frame",
            "content": [{"type": "image_url", "image_url": {"url": data_url}}],
        },
        {
            "role": "user",
            "source": "user",
            "content": [{"type": "image_url", "image_url": {"url": data_url}}],
        },
    ]


def _ladder_client(vault) -> AgentScopeAgentChatClient:
    client = AgentScopeAgentChatClient.__new__(AgentScopeAgentChatClient)
    built: list[str] = []

    def build_vault():
        built.append("vault")
        return vault

    client._media_vault = build_vault
    client._input_token_budget = lambda: None
    return client


def _refusal(max_bytes: int) -> ModelContextBudgetError:
    try:
        prepare_model_messages(_ladder_payload(), [], max_bytes=max_bytes)
    except ModelContextBudgetError as exc:
        return exc
    raise AssertionError("the ladder payload was expected to overflow")


def test_the_refusal_ladder_gives_up_its_own_observations_first():
    """A person's upload moves only after cheaper relief provably failed."""
    asked: list[str] = []

    def vault(url: str) -> str:
        asked.append(url)
        return "b" * 64

    failure = _refusal(12_000)
    client = _ladder_client(vault)

    prepared, _note = client._fit_refused_request(
        _ladder_payload(),
        [],
        max_bytes=12_000,
        failure=failure,
    )

    # The tool observation left; the attachment stayed exactly as sent.
    assert prepared[1]["content"][0]["type"] == "text"
    assert prepared[2]["content"][0]["image_url"]["url"] == (
        _ladder_payload()[2]["content"][0]["image_url"]["url"]
    )
    assert len(asked) == 1


def test_the_ladder_reaches_a_human_upload_only_under_proven_pressure():
    """When the runtime's own media is not enough, the upload goes recallably."""
    failure = _refusal(9_000)
    client = _ladder_client(lambda _url: "c" * 64)

    prepared, _note = client._fit_refused_request(
        _ladder_payload(),
        [],
        max_bytes=9_000,
        failure=failure,
    )

    assert [part["type"] for part in prepared[2]["content"]] == ["text"]
    assert "user attachment moved out of this request" in (
        prepared[2]["content"][0]["text"]
    )
    assert prepared[0]["content"] == "A" * 5_000
    assert prepared[1]["metadata"][model_context.DEMOTED_MEDIA_METADATA_KEY]
    assert prepared[2]["metadata"][model_context.DEMOTED_MEDIA_METADATA_KEY]


def test_a_refusal_that_survived_both_steps_says_what_it_gave_up():
    """A refusal after two steps has to name what it already sacrificed.

    The input that keeps failing is no longer the input the caller sent, so
    the error without the demotion record would be describing a request that
    nobody assembled.
    """
    failure = _refusal(5_000)
    client = _ladder_client(lambda _url: "d" * 64)

    with pytest.raises(AgentModelError) as caught:
        client._fit_refused_request(
            _ladder_payload(),
            [],
            max_bytes=5_000,
            failure=failure,
        )

    text = str(caught.value)
    assert "媒体降级记录" in text
    assert "派生观察媒体 1 块（已存档 1 块）" in text
    assert "真人附件媒体 1 块（已存档 1 块）" in text
    assert "sha256:" + "d" * 12 in text


def test_turning_demotion_off_restores_the_hard_local_refusal(monkeypatch):
    """The escape hatch is a switch, not a preference."""
    monkeypatch.setenv(
        model_context.MEDIA_OBSERVATION_DEGRADE_ENV,
        "0",
    )
    asked: list[str] = []
    client = _ladder_client(lambda url: asked.append(url) or "e" * 64)

    with pytest.raises(AgentModelError) as caught:
        client._fit_refused_request(
            _ladder_payload(),
            [],
            max_bytes=9_000,
            failure=_refusal(9_000),
        )

    assert "预算" in str(caught.value)
    assert "媒体降级记录" not in str(caught.value)
    assert asked == []


# ---------------------------------------------------------------------------
# P3: a provider that answers "too long" is recovered in two steps, and the
# classification vocabulary stays the platform's.
# ---------------------------------------------------------------------------

_OVERFLOW_MESSAGE = (
    "Error code: 400: This model's maximum context length is 32768 tokens. "
    "Your input has 51200 tokens."
)

# One block priced at 13,000 of the byte budget, so a 40,000-byte request holds
# the contract text plus two of them, while a halved request cannot hold the
# contract text plus even one.
_OVERFLOW_BLOCK_BYTES = 13_000 * model_context._INLINE_MEDIA_BYTES_DIVISOR
_OVERFLOW_CONTRACT_BYTES = 12_000
_OVERFLOW_BUDGET = 40_000


def _overflow_payload() -> tuple[list[dict], str, str]:
    """A contract text, one observed frame and one human upload."""
    derived = _data_url(b"\x11" * _OVERFLOW_BLOCK_BYTES, mime="image/jpeg")
    human = _data_url(b"\x22" * _OVERFLOW_BLOCK_BYTES, mime="image/jpeg")
    messages = [
        {"role": "system", "content": "A" * _OVERFLOW_CONTRACT_BYTES},
        {
            "role": "user",
            "source": model_context.MEDIA_RECALL_SOURCE,
            "messageSeq": 9,
            "content": [{"type": "image_url", "image_url": {"url": derived}}],
        },
        _attachment(human, seq=11),
    ]
    return messages, derived, human


class _OverflowProvider:
    """Refuses the first ``failures`` calls as too long, then answers."""

    model = "qwen3.7-plus"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.requests: list[list] = []

    async def __call__(self, messages, *, tools=None):
        del tools
        self.calls += 1
        self.requests.append(list(messages))
        if self.calls <= self.failures:
            raise RuntimeError(_OVERFLOW_MESSAGE)
        return ChatResponse(
            id="response-overflow-recovered",
            content=[TextBlock(text="降级后的输入仍可用")],
            is_last=True,
        )


def _overflow_client(failures: int):
    """A client whose vault only records, so every demotion is auditable."""
    asked: list[str] = []

    def vault(url: str) -> str:
        asked.append(url)
        return hashlib.sha256(url.encode()).hexdigest()

    provider = _OverflowProvider(failures)
    client = AgentScopeAgentChatClient(provider)  # type: ignore[arg-type]
    client._media_vault = lambda: vault
    client._input_token_budget = lambda: None
    forced: list[int] = []
    original_fit = client._fit_refused_request

    def fitted(messages, tools, *, max_bytes, failure):
        forced.append(max_bytes)
        return original_fit(
            messages, tools, max_bytes=max_bytes, failure=failure,
        )

    client._fit_refused_request = fitted
    return client, provider, asked, forced


def _walk_content(node, wanted_key: str, found: list[str]) -> None:
    dump = getattr(node, "model_dump", None)
    if callable(dump):
        # AgentScope blocks are models, so the request has to be read the way
        # the provider will serialize it.
        node = dump()
    if isinstance(node, dict):
        value = node.get(wanted_key)
        if isinstance(value, str):
            found.append(value)
        for child in node.values():
            _walk_content(child, wanted_key, found)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _walk_content(item, wanted_key, found)


def _media_id(data_url: str) -> str:
    """A cheap, comparable name for one inline payload."""
    mime, _, encoded = data_url.partition(";base64,")
    return f"{mime.removeprefix('data:')}:{_b64_id(encoded)}"


def _b64_id(encoded: str) -> str:
    return hashlib.sha256(encoded.encode()).hexdigest()[:12]


def _request_media(native_messages) -> list[str]:
    """Which inline payloads one request the provider actually saw still had."""
    found: list[str] = []
    for message in native_messages:
        blocks = getattr(message, "content", None)
        for block in blocks if isinstance(blocks, (list, tuple)) else []:
            dump = getattr(block, "model_dump", None)
            shape = dump() if callable(dump) else block
            if not isinstance(shape, dict) or shape.get("type") != "data":
                continue
            source = shape.get("source") or {}
            found.append(
                f"{source.get('media_type')}:{_b64_id(str(source.get('data')))}",
            )
    return found


def _request_text(native_messages) -> str:
    found: list[str] = []
    for message in native_messages:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            found.append(content)
        _walk_content(content, "text", found)
    return "\n".join(found)


def test_a_provider_overflow_is_answered_by_gentle_relief_and_one_replay():
    """Only the runtime's own observation is traded for the second attempt.

    The provider said the request was too long, not that the conversation was
    wrong, so the retry gives up the frame a tool can produce again and keeps
    the person's upload byte for byte. The forced step belongs to a request
    that overflowed as well, not to this one.
    """
    messages, derived, human = _overflow_payload()

    async def scenario():
        client, provider, asked, forced = _overflow_client(failures=1)
        notices: list[RateLimitRetryNotice] = []

        async def on_retry(notice: RateLimitRetryNotice) -> None:
            notices.append(notice)

        turn = await client.complete(
            messages=messages,
            tools=[],
            on_rate_limit_retry=on_retry,
            _context_budget_bytes=_OVERFLOW_BUDGET,
        )
        return provider, asked, forced, notices, turn

    provider, asked, forced, notices, turn = asyncio.run(scenario())

    assert turn.content == "降级后的输入仍可用"
    assert provider.calls == 2
    assert asked == [derived]
    assert _request_media(provider.requests[0]) == [
        _media_id(derived),
        _media_id(human),
    ]
    assert _request_media(provider.requests[1]) == [_media_id(human)]
    assert "inline media left this context" in _request_text(
        provider.requests[1],
    )
    assert "user attachment moved out of this request" not in _request_text(
        provider.requests[1],
    )
    assert forced == []
    # An overflow is deterministic: it is answered with a smaller request and
    # never with a wait.
    assert [(n.reason, n.delay_seconds) for n in notices] == [
        ("context_recovery", 0),
    ]


def test_the_gentle_retry_overflowing_too_forces_the_whole_ladder():
    """The second step is the one that may move a person's upload."""
    messages, derived, human = _overflow_payload()

    async def scenario():
        client, provider, asked, forced = _overflow_client(failures=2)
        notices: list[RateLimitRetryNotice] = []

        async def on_retry(notice: RateLimitRetryNotice) -> None:
            notices.append(notice)

        turn = await client.complete(
            messages=messages,
            tools=[],
            on_rate_limit_retry=on_retry,
            _context_budget_bytes=_OVERFLOW_BUDGET,
        )
        return provider, asked, forced, notices, turn

    provider, asked, forced, notices, turn = asyncio.run(scenario())

    assert turn.content == "降级后的输入仍可用"
    assert provider.calls == 3
    assert asked == [derived, derived, human]
    # Halved, so compaction also gives up the old tool exchanges.
    assert forced == [_OVERFLOW_BUDGET // 2]
    assert _request_media(provider.requests[2]) == []
    assert "user attachment moved out of this request" in _request_text(
        provider.requests[2],
    )
    # One recovery is one notice: the forced step is the tail of the same
    # boundary, not a second retry budget spending the first.
    assert [n.reason for n in notices] == ["context_recovery"]


def test_a_refusal_that_survived_both_recovery_steps_says_what_it_gave_up():
    """A provider that keeps saying no must not hide the degraded input.

    "It just keeps erroring" was unlocatable because the refusal described a
    request nobody assembled: the recovery had already moved media out of it.
    The final error carries that record, and stops after the two steps.
    """
    messages, derived, human = _overflow_payload()

    async def scenario():
        client, provider, asked, forced = _overflow_client(failures=99)
        with pytest.raises(AgentModelError) as caught:
            await client.complete(
                messages=messages,
                tools=[],
                _context_budget_bytes=_OVERFLOW_BUDGET,
            )
        return provider, asked, forced, caught

    provider, asked, forced, caught = asyncio.run(scenario())

    # The recovery is bounded: original attempt, gentle retry, forced retry.
    assert provider.calls == 3
    assert asked == [derived, derived, human]
    assert forced == [_OVERFLOW_BUDGET // 2]
    text = str(caught.value)
    assert "maximum context length" in text
    assert "媒体降级记录" in text
    assert "真人附件媒体 1 块（已存档 1 块）" in text


def test_a_provider_failure_that_is_not_about_length_leaves_the_input_alone():
    """Recovery degrades input for a refusal that means "too long", only.

    Re-running a request the provider already answered another way would pay
    for the tokens a second time, and a content-policy refusal has nothing to
    gain from a smaller request.
    """
    messages, _derived, _human = _overflow_payload()

    class _PolicyRefusal:
        model = "qwen3.7-plus"

        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, messages_, *, tools=None):
            del messages_, tools
            self.calls += 1
            raise RuntimeError("Error code: 400: content policy refused this")

    async def scenario():
        client, _provider, asked, forced = _overflow_client(failures=99)
        provider = _PolicyRefusal()
        client.model = provider  # type: ignore[assignment]
        with pytest.raises(AgentModelError) as caught:
            await client.complete(
                messages=messages,
                tools=[],
                _context_budget_bytes=_OVERFLOW_BUDGET,
            )
        return provider, asked, forced, caught

    provider, asked, forced, caught = asyncio.run(scenario())

    assert provider.calls == 1
    assert asked == []
    assert forced == []
    assert "content policy refused this" in str(caught.value)


def test_an_overflow_after_streamed_output_is_not_replayed():
    """Recovering would say the same answer twice, so it refuses instead.

    The platform boundary never replays output a model already spoke; the
    plugin reaches it through the same rule rather than a second one.
    """
    messages, _derived, _human = _overflow_payload()

    class _StreamThenOverflow:
        model = "qwen3.7-plus"

        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, messages_, *, tools=None):
            del messages_, tools
            self.calls += 1

            async def chunks():
                yield ChatResponse(
                    id="response-partial",
                    content=[
                        ToolCallBlock(
                            id="call-read-1",
                            name="read_project",
                            input='{"proj',
                        ),
                    ],
                    is_last=False,
                )
                raise RuntimeError(_OVERFLOW_MESSAGE)

            return chunks()

    async def scenario():
        client, _provider, asked, forced = _overflow_client(failures=99)
        provider = _StreamThenOverflow()
        client.model = provider  # type: ignore[assignment]
        streamed: list[tuple[str, str, str]] = []

        async def on_tool_call_delta(
            call_id: str,
            name: str,
            delta: str,
        ) -> None:
            streamed.append((call_id, name, delta))

        with pytest.raises(OverflowAfterOutputError) as caught:
            await client.complete(
                messages=messages,
                tools=_tools(),
                on_tool_call_delta=on_tool_call_delta,
                _context_budget_bytes=_OVERFLOW_BUDGET,
            )
        return provider, asked, forced, streamed, caught

    provider, asked, forced, streamed, caught = asyncio.run(scenario())

    # The user already watched a tool call start; a second attempt would say
    # it twice.
    assert streamed == [("call-read-1", "read_project", '{"proj')]
    assert provider.calls == 1
    assert asked == []
    assert forced == []
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert "maximum context length" in str(caught.value)


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (400, "This model's maximum context length is 32768 tokens"),
        (400, "Range of input length should be [1, 30720]"),
        (400, "too many input tokens in this request"),
        (
            400,
            "The provided tokens (51200) exceeds the available context size "
            "(32768)",
        ),
        (None, "prompt is too long for the selected model"),
        (413, "input is too long for this gateway"),
        (422, "context_length_exceeded"),
    ],
)
def test_a_length_refusal_is_recognised_in_the_words_the_provider_uses(
    status,
    message,
):
    """One classifier, the platform's, widened only for transport reality.

    The platform's marker list is the single vocabulary here, so a gateway
    that answers 413/422 or leaks only text is still understood, while the
    plugin does not maintain a second list to drift from the first.
    """

    class _Refusal(RuntimeError):
        pass

    exc = _Refusal(f"Error code: {status}: {message}" if status else message)
    if status is not None:
        exc.status_code = status

    assert is_context_length_error(exc)


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 500: maximum context length backend is unhealthy",
        "Error code: 503: too many input tokens queued",
    ],
)
def test_a_server_side_failure_is_not_read_as_a_too_long_refusal(message):
    """A hiccup must not be answered by sacrificing the conversation.

    A 5xx says the provider failed, not that the input costs too much; the
    transient budget retries it as it is, and degrading would trade away
    media for nothing.
    """

    class _ServerFault(RuntimeError):
        pass

    exc = _ServerFault(message)
    exc.status_code = int(message.split("Error code: ", 1)[1][:3])

    assert not is_context_length_error(exc)


@pytest.mark.skipif(
    not model_client._HOST_OVERFLOW_RECOVERY_AVAILABLE,
    reason="the platform package is optional for standalone plugin installs",
)
def test_overflow_recovery_is_the_platform_function():
    """The boundary is imported, not reimplemented.

    A second overflow boundary in the plugin is a second place where "one
    retry" and "never replay output" can disagree with the platform.
    """
    from qwenpaw.agents.context.overflow_recovery import (
        CONTEXT_OVERFLOW_MARKERS as host_markers,
    )
    from qwenpaw.agents.context.overflow_recovery import (
        call_with_overflow_recovery as host_call,
    )
    from qwenpaw.agents.context.overflow_recovery import (
        is_context_overflow_error as host_classify,
    )

    assert model_client._HOST_OVERFLOW_RECOVERY_AVAILABLE
    assert model_client.CONTEXT_OVERFLOW_MARKERS is host_markers
    assert model_client.call_with_overflow_recovery is host_call
    assert model_client.is_context_overflow_error is host_classify


def test_turning_demotion_off_declines_the_provider_overflow_too(monkeypatch):
    """The escape hatch has to cover the recovery path, not just the local one.

    A deployment that refuses demotion expects the provider's own refusal to
    come back unchanged instead of a quietly rewritten second request.
    """
    monkeypatch.setenv(
        model_context.MEDIA_OBSERVATION_DEGRADE_ENV,
        "0",
    )
    messages, _derived, _human = _overflow_payload()

    async def scenario():
        client, provider, asked, forced = _overflow_client(failures=1)
        with pytest.raises(RuntimeError, match="maximum context length"):
            await client.complete(
                messages=messages,
                tools=[],
                _context_budget_bytes=_OVERFLOW_BUDGET,
            )
        return provider, asked, forced

    provider, asked, forced = asyncio.run(scenario())

    assert provider.calls == 1
    assert asked == []
    assert forced == []
