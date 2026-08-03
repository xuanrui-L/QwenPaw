# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access
"""Unit tests for the qwen3-asr protocol branch (respx-stubbed, no billing)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from models import config
from models import asr_model
from models.asr_model import ASRResult

ENDPOINT = (
    "https://asr.example/api/v1/services/aigc/"
    "multimodal-generation/generation"
)


def _bind_qwen3(monkeypatch, *, language: str = "") -> None:
    monkeypatch.setattr(config, "get_asr_api_key", lambda: "asr-key")
    monkeypatch.setattr(
        config,
        "get_asr_model_name",
        lambda: "qwen3-asr-flash",
    )
    monkeypatch.setattr(config, "get_asr_provider", lambda: "fun-asr")
    monkeypatch.setattr(config, "get_asr_language", lambda: language)
    monkeypatch.setattr(config, "get_asr_timeout_seconds", lambda: 30)
    monkeypatch.setattr(
        config,
        "get_asr_base_url",
        lambda: "https://asr.example/api/v1/services/audio/asr/transcription",
    )


def _response(sentences: list[str]) -> dict:
    return {
        "output": {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": [{"text": text} for text in sentences],
                    },
                },
            ],
        },
        "request_id": "req-1",
    }


def test_transcribe_dispatches_qwen3_models_to_new_branch(
    monkeypatch,
) -> None:
    _bind_qwen3(monkeypatch)
    observed = {}

    async def fake_qwen3(media_url: str) -> ASRResult:
        observed["url"] = media_url
        return ASRResult("fun-asr", "qwen3-asr-flash", ())

    monkeypatch.setattr(asr_model, "_qwen3_asr", fake_qwen3)
    result = asyncio.run(asr_model.transcribe("https://cdn.test/a.mp3"))

    assert observed["url"] == "https://cdn.test/a.mp3"
    assert result.model == "qwen3-asr-flash"


def test_transcribe_keeps_fun_asr_for_other_models(monkeypatch) -> None:
    _bind_qwen3(monkeypatch)
    monkeypatch.setattr(config, "get_asr_model_name", lambda: "fun-asr")
    observed = {}

    async def fake_fun_asr(media_url: str) -> ASRResult:
        observed["url"] = media_url
        return ASRResult("dashscope", "fun-asr", ())

    monkeypatch.setattr(asr_model, "_fun_asr", fake_fun_asr)
    asyncio.run(asr_model.transcribe("https://cdn.test/a.mp3"))

    assert observed["url"] == "https://cdn.test/a.mp3"


def test_transcribe_keeps_whisper_provider_priority(monkeypatch) -> None:
    _bind_qwen3(monkeypatch)
    monkeypatch.setattr(config, "get_asr_provider", lambda: "whisper")
    observed = {}

    async def fake_whisper(media_url: str) -> ASRResult:
        observed["url"] = media_url
        return ASRResult("openai", "whisper-1", ())

    monkeypatch.setattr(asr_model, "_whisper", fake_whisper)
    asyncio.run(asr_model.transcribe("https://cdn.test/a.mp3"))

    assert observed["url"] == "https://cdn.test/a.mp3"


@respx.mock
def test_qwen3_single_chunk_spreads_estimated_timestamps(
    monkeypatch,
) -> None:
    _bind_qwen3(monkeypatch)
    monkeypatch.setattr(asr_model, "_probe_duration_ms", lambda _s: 10_000)

    async def fake_file_url(_media_url, key, model):
        assert (key, model) == ("asr-key", "qwen3-asr-flash")
        return "oss://dashscope-instant/audio.mp3"

    monkeypatch.setattr(asr_model, "_fun_asr_file_url", fake_file_url)
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_response(["你好。", "再见。"])),
    )

    result = asyncio.run(asr_model._qwen3_asr("https://cdn.test/a.mp3"))

    assert result.provider == "fun-asr"
    assert result.model == "qwen3-asr-flash"
    assert [
        (seg.start_ms, seg.end_ms, seg.text, seg.confidence)
        for seg in result.segments
    ] == [
        (0, 5_000, "你好。", 0.0),
        (5_000, 10_000, "再见。", 0.0),
    ]
    request = route.calls.last.request
    assert request.headers["X-DashScope-OssResourceResolve"] == "enable"
    body = json.loads(request.content)
    assert body["model"] == "qwen3-asr-flash"
    assert body["input"]["messages"][0]["content"] == [
        {"audio": "oss://dashscope-instant/audio.mp3"},
    ]
    assert body["parameters"] == {"result_format": "message"}


@respx.mock
def test_qwen3_chunking_applies_cumulative_offsets(
    monkeypatch,
    tmp_path,
) -> None:
    _bind_qwen3(monkeypatch)
    chunks = [
        tmp_path / "qwen3-chunk-0000.mp3",
        tmp_path / "qwen3-chunk-0001.mp3",
    ]
    for chunk in chunks:
        chunk.write_bytes(b"audio")
    durations = {
        "source.mp3": 540_000,
        "qwen3-chunk-0000.mp3": 270_000,
        "qwen3-chunk-0001.mp3": 270_000,
    }
    monkeypatch.setattr(
        asr_model,
        "_probe_duration_ms",
        lambda source: durations[source.rsplit("/", 1)[-1]],
    )
    monkeypatch.setattr(
        asr_model,
        "_local_media_path",
        lambda *_args: tmp_path / "source.mp3",
    )
    monkeypatch.setattr(
        asr_model,
        "_split_audio_chunks",
        lambda *_args: chunks,
    )

    async def fake_upload(path, *, api_key, model_name, media_type):
        assert (api_key, model_name) == ("asr-key", "qwen3-asr-flash")
        assert media_type == "audio/mpeg"
        return f"oss://dashscope-instant/{path.name}"

    monkeypatch.setattr(
        asr_model,
        "upload_local_file_to_dashscope_temp",
        fake_upload,
    )
    responses = iter(
        [
            httpx.Response(200, json=_response(["第一块前半。", "第一块后半。"])),
            httpx.Response(200, json=_response(["第二块。"])),
        ],
    )
    respx.post(ENDPOINT).mock(side_effect=lambda _request: next(responses))

    result = asyncio.run(
        asr_model._qwen3_asr((tmp_path / "source.mp3").as_uri()),
    )

    assert [
        (seg.start_ms, seg.end_ms, seg.text) for seg in result.segments
    ] == [
        (0, 135_000, "第一块前半。"),
        (135_000, 270_000, "第一块后半。"),
        (270_000, 540_000, "第二块。"),
    ]
    assert all(seg.confidence == 0.0 for seg in result.segments)


@respx.mock
def test_qwen3_retries_throttling_with_backoff(monkeypatch) -> None:
    _bind_qwen3(monkeypatch)
    monkeypatch.setattr(asr_model, "_probe_duration_ms", lambda _s: 5_000)

    async def fake_file_url(*_args):
        return "oss://dashscope-instant/audio.mp3"

    monkeypatch.setattr(asr_model, "_fun_asr_file_url", fake_file_url)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asr_model.asyncio, "sleep", fake_sleep)
    responses = iter(
        [
            httpx.Response(
                429,
                json={"code": "Throttling.RateQuota", "message": "slow down"},
            ),
            httpx.Response(200, json=_response(["成功。"])),
        ],
    )
    route = respx.post(ENDPOINT).mock(
        side_effect=lambda _request: next(responses),
    )

    result = asyncio.run(asr_model._qwen3_asr("https://cdn.test/a.mp3"))

    assert [seg.text for seg in result.segments] == ["成功。"]
    assert route.call_count == 2
    assert len(sleeps) == 1 and sleeps[0] >= 2.0


@respx.mock
def test_qwen3_throttling_exhaustion_raises(monkeypatch) -> None:
    _bind_qwen3(monkeypatch)
    monkeypatch.setattr(asr_model, "_probe_duration_ms", lambda _s: 5_000)

    async def fake_file_url(*_args):
        return "oss://dashscope-instant/audio.mp3"

    monkeypatch.setattr(asr_model, "_fun_asr_file_url", fake_file_url)

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asr_model.asyncio, "sleep", fake_sleep)
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            429,
            json={"code": "Throttling.RateQuota", "message": "slow down"},
        ),
    )

    with pytest.raises(asr_model._ThrottlingError):
        asyncio.run(asr_model._qwen3_asr("https://cdn.test/a.mp3"))
    assert route.call_count == 4


@respx.mock
def test_qwen3_retries_transient_errors_linearly(monkeypatch) -> None:
    _bind_qwen3(monkeypatch)
    monkeypatch.setattr(asr_model, "_probe_duration_ms", lambda _s: 5_000)

    async def fake_file_url(*_args):
        return "oss://dashscope-instant/audio.mp3"

    monkeypatch.setattr(asr_model, "_fun_asr_file_url", fake_file_url)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asr_model.asyncio, "sleep", fake_sleep)
    responses = iter(
        [
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json=_response(["恢复。"])),
        ],
    )
    route = respx.post(ENDPOINT).mock(
        side_effect=lambda _request: next(responses),
    )

    result = asyncio.run(asr_model._qwen3_asr("https://cdn.test/a.mp3"))

    assert [seg.text for seg in result.segments] == ["恢复。"]
    assert route.call_count == 2
    assert sleeps == [2.0]


@respx.mock
def test_qwen3_client_errors_do_not_retry(monkeypatch) -> None:
    _bind_qwen3(monkeypatch)
    monkeypatch.setattr(asr_model, "_probe_duration_ms", lambda _s: 5_000)

    async def fake_file_url(*_args):
        return "oss://dashscope-instant/audio.mp3"

    monkeypatch.setattr(asr_model, "_fun_asr_file_url", fake_file_url)
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(400, json={"message": "bad request"}),
    )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(asr_model._qwen3_asr("https://cdn.test/a.mp3"))
    assert route.call_count == 1


@respx.mock
def test_qwen3_empty_audio_returns_no_segments(monkeypatch) -> None:
    _bind_qwen3(monkeypatch)
    monkeypatch.setattr(asr_model, "_probe_duration_ms", lambda _s: 5_000)

    async def fake_file_url(*_args):
        return "oss://dashscope-instant/silence.mp3"

    monkeypatch.setattr(asr_model, "_fun_asr_file_url", fake_file_url)
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_response([])),
    )

    result = asyncio.run(asr_model._qwen3_asr("https://cdn.test/silence.mp3"))

    assert result.segments == ()
    assert result.provider == "fun-asr"


@respx.mock
def test_qwen3_language_is_forwarded_via_asr_options(monkeypatch) -> None:
    _bind_qwen3(monkeypatch, language="zh")
    monkeypatch.setattr(asr_model, "_probe_duration_ms", lambda _s: 5_000)

    async def fake_file_url(*_args):
        return "oss://dashscope-instant/audio.mp3"

    monkeypatch.setattr(asr_model, "_fun_asr_file_url", fake_file_url)
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_response(["你好。"])),
    )

    asyncio.run(asr_model._qwen3_asr("https://cdn.test/a.mp3"))

    body = json.loads(route.calls.last.request.content)
    assert body["parameters"]["asr_options"] == {"language": "zh"}


def test_qwen3_endpoint_derives_from_asr_base_host() -> None:
    assert (
        asr_model._qwen3_endpoint(
            "https://asr.example/api/v1/services/audio/asr/transcription",
        )
        == ENDPOINT
    )
    assert asr_model._qwen3_endpoint(
        "https://dashscope.aliyuncs.com/api/v1",
    ) == (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
        "multimodal-generation/generation"
    )
    with pytest.raises(ValueError):
        asr_model._qwen3_endpoint("not-a-url")


def test_spread_segments_marks_estimates_and_clamps() -> None:
    segments = asr_model._spread_segments(["a", "b", "c"], 1_000, 2)
    assert len(segments) == 3
    assert all(seg.confidence == 0.0 for seg in segments)
    assert all(seg.end_ms > seg.start_ms for seg in segments)
    assert segments[0].start_ms == 1_000
    assert not asr_model._spread_segments([], 0, 1_000)
