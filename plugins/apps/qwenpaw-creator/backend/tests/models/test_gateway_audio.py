# -*- coding: utf-8 -*-
# pylint: disable=protected-access,unused-argument
# flake8: noqa: E501
"""Audio routes on the AgentScope model proxy.

Measured on platform-pre: TTS only answers on ``SpeechSynthesizer`` (the
multimodal-generation path creator uses for Bailian returns 400
MODEL_NOT_ALLOWED), the ``fun-asr`` transcription route is not deployed at
all (404), and ``qwen-audio-3.0-asr-flash`` puts the transcript in
``output.sentence`` / top-level ``text`` rather than in ``choices``.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from models import asr_model
from models import config
from models import tts_model
from utils.exceptions import ModelError


GATEWAY_BASE = "https://platform-pre.agentscope.io/v1"
PROTOCOL = "AgentScope Platform"
SPEECH_SUFFIX = "services/audio/tts/SpeechSynthesizer"


def _fake_post(captured: dict, response: dict):
    async def fake_post_json(url, *, api_key, payload, timeout_seconds):
        captured["url"] = url
        captured["payload"] = payload
        return response

    return fake_post_json


def _bind_gateway_tts(monkeypatch, *, model: str, voice: str) -> dict:
    """Point the TTS section at the proxy, pinned so no env can move it."""
    captured: dict = {}
    monkeypatch.setenv("TTS_API_KEY", "sk-as-test")
    monkeypatch.setattr(config, "get_tts_base_url", lambda: GATEWAY_BASE)
    monkeypatch.setattr(config, "get_tts_protocol", lambda: PROTOCOL)
    monkeypatch.setattr(config, "get_tts_model_name", lambda: model)
    monkeypatch.setattr(config, "get_tts_voice", lambda: voice)
    monkeypatch.setattr(config, "get_tts_timeout_seconds", lambda: 30)
    monkeypatch.setattr(
        tts_model,
        "_post_json",
        _fake_post(
            captured,
            {
                "output": {
                    "audio": {
                        "url": "http://dashscope-result-sgp.oss-ap-southeast-1"
                        ".aliyuncs.com/speech.wav",
                    },
                },
                "usage": {"characters": 4},
            },
        ),
    )
    monkeypatch.setattr(
        tts_model,
        "_download_audio",
        lambda url: (b"RIFFxxxx", "audio/wav"),
    )
    return captured


# -- TTS -----------------------------------------------------------------


def test_speech_endpoint_is_rebuilt_from_the_origin() -> None:
    """Bases are saved as a root or as a full endpoint; both must work."""
    assert tts_model._tts_endpoint(
        "https://dashscope.aliyuncs.com/api/v1",
        via_gateway=False,
    ) == (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
        "multimodal-generation/generation"
    )
    for base in (
        GATEWAY_BASE,
        f"{GATEWAY_BASE}/services/aigc/multimodal-generation/generation",
        "https://platform-pre.agentscope.io/api/v1",
    ):
        assert tts_model._tts_endpoint(base, via_gateway=True) == (
            f"https://platform-pre.agentscope.io/v1/{SPEECH_SUFFIX}"
        )
    with pytest.raises(ValueError, match="no host"):
        tts_model._tts_endpoint("", via_gateway=True)


def test_gateway_synthesis_uses_the_speech_route(monkeypatch) -> None:
    captured = _bind_gateway_tts(
        monkeypatch,
        model="qwen-audio-3.0-tts-flash",
        voice="longanhuan_v3.6",
    )

    result = asyncio.run(tts_model.synthesize("你好世界"))

    assert captured["url"].endswith(f"/v1/{SPEECH_SUFFIX}")
    # The leaner body is what the proxy was measured to accept; the local
    # catalogue is empty for this model, so the configured name is passed
    # through instead of being refused before the call.
    assert captured["payload"]["model"] == "qwen-audio-3.0-tts-flash"
    assert captured["payload"]["input"] == {
        "text": "你好世界",
        "voice": "longanhuan_v3.6",
    }
    assert result.audio_bytes == b"RIFFxxxx"
    assert result.characters == 4


def test_gateway_refuses_a_genuine_websocket_model(monkeypatch) -> None:
    """CosyVoice has no measured HTTP route here; fail before spending."""
    _bind_gateway_tts(
        monkeypatch,
        model="cosyvoice-v3.5-plus",
        voice="longanhuan_v3.6",
    )

    with pytest.raises(ModelError) as excinfo:
        asyncio.run(tts_model.synthesize("你好世界"))

    assert excinfo.value.retryable is False
    assert "WebSocket" in str(excinfo.value)


def test_bailian_keeps_refusing_an_unknown_voice(monkeypatch) -> None:
    """The pass-through is gateway-only; a typo must still fail fast."""
    captured: dict = {}
    monkeypatch.setenv("TTS_API_KEY", "sk-test")
    monkeypatch.setattr(config, "get_tts_protocol", lambda: "")
    monkeypatch.setattr(
        config,
        "get_tts_base_url",
        lambda: "https://dashscope.aliyuncs.com/api/v1",
    )
    monkeypatch.setattr(
        config,
        "get_tts_model_name",
        lambda: "qwen3-tts-flash",
    )
    monkeypatch.setattr(config, "get_tts_voice", lambda: "NotAVoice")
    monkeypatch.setattr(config, "get_tts_timeout_seconds", lambda: 30)
    monkeypatch.setattr(tts_model, "_post_json", _fake_post(captured, {}))

    with pytest.raises(ValueError, match="unknown voice"):
        asyncio.run(tts_model.synthesize("你好世界"))
    # Refused before the call, so nothing was sent and nothing was billed.
    assert not captured


# -- ASR -----------------------------------------------------------------


def _bind_gateway_asr(monkeypatch, *, model: str) -> None:
    monkeypatch.setattr(config, "get_asr_api_key", lambda: "sk-as-test")
    monkeypatch.setattr(config, "get_asr_model_name", lambda: model)
    monkeypatch.setattr(config, "get_asr_provider", lambda: "fun-asr")
    monkeypatch.setattr(config, "get_asr_language", lambda: "")
    monkeypatch.setattr(config, "get_asr_timeout_seconds", lambda: 30)
    monkeypatch.setattr(config, "get_asr_base_url", lambda: GATEWAY_BASE)
    monkeypatch.setattr(config, "get_asr_protocol", lambda: PROTOCOL)


def test_multimodal_route_recognises_both_naming_generations() -> None:
    """``fun-asr`` is 404 on the proxy, so its model must not route there."""
    assert asr_model._is_qwen_multimodal_asr("qwen-audio-3.0-asr-flash")
    assert asr_model._is_qwen_multimodal_asr("qwen3-asr-flash")
    assert not asr_model._is_qwen_multimodal_asr("paraformer-v2")
    assert not asr_model._is_qwen_multimodal_asr("fun-asr")
    assert not asr_model._is_qwen_multimodal_asr("")


def test_asr_endpoint_drops_the_token_portal_prefix(monkeypatch) -> None:
    _bind_gateway_asr(monkeypatch, model="qwen-audio-3.0-asr-flash")
    assert asr_model._qwen3_endpoint(GATEWAY_BASE) == (
        "https://platform-pre.agentscope.io/v1/services/aigc/"
        "multimodal-generation/generation"
    )
    monkeypatch.setattr(config, "get_asr_protocol", lambda: "")
    assert asr_model._qwen3_endpoint(
        "https://dashscope.aliyuncs.com/api/v1",
    ) == (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
        "multimodal-generation/generation"
    )


def _remote_url(url: str):
    """Stand in for the upload step: the call already has a fetchable URL."""

    async def fake(media_url, api_key, model):  # noqa: ARG001
        del media_url, api_key, model
        return url

    return fake


@respx.mock
def test_gateway_transcription_omits_the_oss_resolve_header(
    monkeypatch,
) -> None:
    """A proxy key cannot mint oss://, so the header advertises a lie."""
    _bind_gateway_asr(monkeypatch, model="qwen-audio-3.0-asr-flash")
    monkeypatch.setattr(asr_model, "_probe_duration_ms", lambda _s: 4_000)
    monkeypatch.setattr(
        asr_model,
        "_fun_asr_file_url",
        _remote_url("https://example.com/line.wav"),
    )
    route = respx.post(
        "https://platform-pre.agentscope.io/v1/services/aigc/"
        "multimodal-generation/generation",
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "text": "欢迎使用阿里云。",
                "output": {"sentence": {"text": "欢迎使用阿里云。"}},
                "usage": {"duration": 2},
            },
        ),
    )

    result = asyncio.run(asr_model.transcribe("https://example.com/line.wav"))

    assert [segment.text for segment in result.segments] == ["欢迎使用阿里云。"]
    sent = route.calls.last.request
    assert "x-dashscope-ossresourceresolve" not in {
        key.lower() for key in sent.headers
    }


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # The shape the proxy was measured to return.
        (
            {"text": "欢迎使用阿里云。", "output": {"sentence": {"text": "欢迎"}}},
            ["欢迎"],
        ),
        # Segmented transcription: one sentence object per segment.
        (
            {
                "output": {
                    "sentence": [{"text": "第一句。"}, {"text": "第二句。"}],
                },
            },
            ["第一句。", "第二句。"],
        ),
        # No output at all: the top-level text still carries the transcript.
        ({"text": "只有顶层。"}, ["只有顶层。"]),
        # Bailian's qwen3-asr shape must keep working unchanged.
        (
            {
                "output": {
                    "choices": [
                        {
                            "message": {
                                "content": [{"text": "A"}, {"text": "B"}],
                            },
                        },
                    ],
                },
            },
            ["A", "B"],
        ),
        ({}, []),
        ({"output": {}, "text": ""}, []),
    ],
)
def test_transcript_is_read_from_whichever_field_arrived(
    body: dict,
    expected: list[str],
) -> None:
    """An empty transcript for a call that succeeded is the failure mode."""
    assert asr_model._qwen3_sentences(body) == expected
