# -*- coding: utf-8 -*-
"""Provider SSE requests, stream decoding, and partial response handling."""

from __future__ import annotations
import asyncio
import json
import pytest
from models import text_model
from models.sse import (
    aggregate_stream_to_completion,
    decode_chat_response,
    is_event_stream,
    parse_sse_frames,
    sse_stream_terminated,
)
from utils.exceptions import ModelError
from test_text_model_protocols import _fake_httpx, _patch_config

from models import vlm_model

pytestmark = pytest.mark.unit


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


def _frames(*payloads: object) -> str:
    """Render frames the way the proxy does: no space after ``data:``."""
    return "".join(
        f"data:{json.dumps(p, ensure_ascii=False)}\n\n"
        if not isinstance(p, str)
        else f"data:{p}\n\n"
        for p in payloads
    )


def _chunk(text=None, *, reasoning=None, finish=None, usage=None):
    payload = {
        "id": "c1",
        "model": "qwen3.8-flash",
        "object": "chat.completion.chunk",
    }
    if usage is not None:
        payload["usage"] = usage
    else:
        delta = {}
        if text is not None:
            delta["content"] = text
        if reasoning is not None:
            delta["reasoning_content"] = reasoning
        payload["choices"] = [
            {"delta": delta, "index": 0, "finish_reason": finish},
        ]
    return payload


def _decode(body: str, content_type: str = "text/event-stream") -> dict:
    return decode_chat_response(
        status_code=200,
        text=body,
        content_type=content_type,
        model_name="qwen3.8-flash",
        url="https://platform-pre.agentscope.io/v1/chat/completions",
    )


def test_is_event_stream_matches_only_the_stream_media_type() -> None:
    assert is_event_stream("text/event-stream")
    assert is_event_stream("Text/Event-Stream; charset=utf-8")
    assert not is_event_stream("application/json")
    assert not is_event_stream("")


def test_data_prefix_space_is_optional_and_done_terminates() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"你"},"index":0}]}\n\n'
        + _frames(_chunk("好"))
        + "data: [DONE]\n\n"
        + _frames({"choices": [{"delta": {"content": "忽略我"}}]})
    )

    completion = _decode(body)

    assert completion["choices"][0]["message"]["content"] == "你好"
    # Frames past the sentinel are not part of the answer.
    assert "忽略" not in completion["choices"][0]["message"]["content"]


def test_usage_from_the_final_frame_survives_the_fold() -> None:
    usage = {"prompt_tokens": 11, "completion_tokens": 60, "total_tokens": 71}
    completion = _decode(
        _frames(_chunk("答案"), _chunk(finish="stop"), _chunk(usage=usage)),
    )

    assert completion["usage"] == usage
    assert completion["choices"][0]["finish_reason"] == "stop"
    assert completion["model"] == "qwen3.8-flash"
    assert completion["id"] == "c1"


def test_reasoning_content_never_leaks_into_the_answer() -> None:
    completion = _decode(
        _frames(
            _chunk(reasoning="先想到 A 再想到 B"),
            _chunk("最终答案"),
            _chunk(reasoning="继续想"),
        ),
    )

    content = completion["choices"][0]["message"]["content"]
    assert content == "最终答案"
    assert "先想到" not in json.dumps(completion["choices"], ensure_ascii=False)
    # The trace is reported as dropped rather than silently discarded, so a
    # caller that expects chain-of-thought can notice it is gone.
    assert completion["_reasoning_content_dropped"] is True


def test_tool_call_fragments_are_merged_by_index() -> None:
    completion = _decode(
        _frames(
            {
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": None,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": '{"path":',
                                    },
                                },
                            ],
                        },
                    },
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"a.py"}'},
                                },
                                {
                                    "index": 1,
                                    "id": "call-2",
                                    "function": {"name": "read"},
                                },
                            ],
                        },
                    },
                ],
            },
        ),
    )

    calls = completion["choices"][0]["message"]["tool_calls"]
    assert completion["choices"][0]["finish_reason"] == "tool_calls"
    assert len(calls) == 2
    assert calls[0]["id"] == "call-1"
    assert calls[0]["function"]["name"] == "write_file"
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.py"}
    assert calls[1]["id"] == "call-2"
    assert calls[1]["function"]["name"] == "read"


def test_whole_message_in_first_frame_is_accepted() -> None:
    # Not every server streams a ``delta``; some put the finished message in
    # the first chunk and only send keepalives after it.
    completion = _decode(
        _frames(
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "完整"},
                        "finish_reason": "stop",
                    },
                ],
            },
        ),
    )

    assert completion["choices"][0]["message"]["content"] == "完整"


def test_multi_line_data_event_is_joined_before_parsing() -> None:
    body = 'data:{"choices":[{"delta":\ndata:{"content":"分段"}}]}\n\n'

    assert parse_sse_frames(body)[0]["choices"][0]["delta"]["content"] == "分段"


def test_error_frame_inside_a_200_stream_is_raised() -> None:
    body = _frames(_chunk("半句")) + _frames(
        {
            "error": {
                "code": "ASP.UPSTREAM.ERROR",
                "message": "上游返回错误",
                "retryable": True,
            },
            "request_id": "req-42",
        },
    )

    with pytest.raises(ModelError) as excinfo:
        _decode(body)

    message = str(excinfo.value)
    assert "ASP.UPSTREAM.ERROR" in message
    assert "req-42" in message
    # The proxy marks even deterministic stream errors retryable; the fold
    # decides, not the field.
    assert excinfo.value.retryable is False


def test_html_shell_is_reported_as_a_wrong_endpoint() -> None:
    shell = "<!doctype html>\n<html><head><title>AgentScope</title></head><body></body></html>"

    for content_type in ("text/html", "application/octet-stream"):
        with pytest.raises(ModelError) as excinfo:
            _decode(shell, content_type=content_type)
        assert "HTML" in str(excinfo.value)
        assert "/chat/completions" in str(excinfo.value)


def test_plain_json_body_passes_through_untouched() -> None:
    # Regression guard for every other provider on this code path: a normal
    # JSON completion must reach the caller exactly as it arrived.
    payload = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            },
        ],
        "usage": {"total_tokens": 3},
    }

    assert (
        _decode(json.dumps(payload), content_type="application/json")
        == payload
    )


def test_empty_or_unreadable_stream_fails_instead_of_returning_nothing() -> (
    None
):
    with pytest.raises(ModelError):
        _decode("")
    with pytest.raises(ModelError):
        _decode("data:not-json-at-all\n\n")

    completion = aggregate_stream_to_completion([])
    assert completion["choices"][0]["message"]["content"] == ""


def test_a_folded_stream_records_how_a_contentless_reply_ended() -> None:
    # Diagnosing "the reply had no text" needs the frame count and an honest
    # note about whether the upstream ever said how it ended. ``finish_reason``
    # keeps its "stop" default because the parsers outside this file expect the
    # non-streaming shape; the side channel is what tells a clean stop from a
    # stream the gateway never closed.
    answered = aggregate_stream_to_completion(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "hi"},
                        "finish_reason": "length",
                    },
                ],
            },
        ],
    )
    assert answered["_frame_count"] == 1
    assert answered["choices"][0]["finish_reason"] == "length"
    assert "_finish_reason_missing" not in answered

    silent = aggregate_stream_to_completion([])
    assert silent["choices"][0]["finish_reason"] == "stop"
    assert silent["_finish_reason_missing"] is True
    assert silent["_frame_count"] == 0


def test_a_stream_with_neither_ending_signal_is_marked_truncated() -> None:
    # The gateway stopped talking mid-answer: no finish reason and no closing
    # sentinel. Once callers ask to stream, this is the only thing that keeps a
    # half-written document from being accepted as a finished answer.
    body = 'data: {"choices": [{"delta": {"content": "<html>"}}]}\n\n'
    folded = aggregate_stream_to_completion(
        parse_sse_frames(body),
        terminated=sse_stream_terminated(body),
    )

    assert folded["_stream_truncated"] is True
    assert folded["choices"][0]["message"]["content"] == "<html>"


def test_a_polite_close_without_a_finish_reason_is_not_truncated() -> None:
    # Some compatible servers never report a finish reason yet still end with
    # [DONE]. Counting that as truncation would reject good answers.
    body = (
        'data: {"choices": [{"delta": {"content": "pong"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    folded = aggregate_stream_to_completion(
        parse_sse_frames(body),
        terminated=sse_stream_terminated(body),
    )

    assert "_stream_truncated" not in folded
    # The older side channel still records the absent reason, because that is
    # what the empty-content hint reads.
    assert folded["_finish_reason_missing"] is True


def test_a_reported_finish_reason_survives_a_missing_sentinel() -> None:
    body = (
        'data: {"choices": [{"delta": {"content": "x"}, '
        '"finish_reason": "stop"}]}\n\n'
    )
    folded = aggregate_stream_to_completion(
        parse_sse_frames(body),
        terminated=sse_stream_terminated(body),
    )

    assert "_stream_truncated" not in folded
    assert "_finish_reason_missing" not in folded


def test_sentinel_detection_tolerates_spacing_and_crlf() -> None:
    assert sse_stream_terminated("data:[DONE]\r\n") is True
    assert sse_stream_terminated("data:   [DONE]  \n") is True
    assert sse_stream_terminated('{"choices": []}\n') is False
    assert sse_stream_terminated('data: {"x": 1}\n\n') is False
    assert sse_stream_terminated("") is False
    assert sse_stream_terminated(None) is False


def test_partial_openai_vlm_stream_is_never_a_completed_answer():
    payload = {
        "_stream_truncated": True,
        "choices": [{"message": {"content": "only a prefix"}}],
    }
    with pytest.raises(ModelError) as error:
        vlm_model._parse_openai_response(payload, "test")
    assert error.value.retryable
