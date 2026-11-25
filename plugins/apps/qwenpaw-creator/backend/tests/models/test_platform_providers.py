# -*- coding: utf-8 -*-
# pylint: disable=protected-access,unused-argument
# flake8: noqa: E501
"""AgentScope Platform media endpoints, model configuration, and request routing."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from models import asr_model
from models import config
from models import tts_model
from models.tts_capabilities import capability_for
from utils.exceptions import ModelError

from models.image import _detect_backend_from_names, image_backend_for_protocol

from models.image.dashscope_provider import DashScopeImageModel
from test_async_image_providers import _model

from models import config as model_config
from models.tts_capabilities import (
    DEFAULT_TTS_MODEL,
    models_for,
    require_capability,
    supported_models,
)
from test_tts_model import _fake_post

GATEWAY_BASE = "https://platform-pre.agentscope.io/v1"
PROTOCOL = "AgentScope Platform"
SPEECH_SUFFIX = "services/audio/tts/SpeechSynthesizer"


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
    # The leaner body is what the proxy was measured to accept; the voice is a
    # deployment default that this model does publish, so it survives the local
    # check untouched and goes out as configured.
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
    """The pass-through is gateway-only; a name the caller typed must still
    fail fast."""
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
    monkeypatch.setattr(config, "get_tts_voice", lambda: "Cherry")
    monkeypatch.setattr(config, "get_tts_timeout_seconds", lambda: 30)
    monkeypatch.setattr(tts_model, "_post_json", _fake_post(captured, {}))

    with pytest.raises(ValueError, match="unknown voice"):
        asyncio.run(tts_model.synthesize("你好世界", voice="NotAVoice"))
    # Refused before the call, so nothing was sent and nothing was billed.
    assert not captured


def test_bailian_replaces_a_default_voice_the_model_cannot_speak(
    monkeypatch,
) -> None:
    """A deployment default is not a caller's typo.

    One default voice is configured across every model while each model
    publishes its own list, so refusing a name the switch left behind would
    silence every narration in that deployment.
    """
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
        lambda: "qwen-audio-3.1-tts-flash",
    )
    monkeypatch.setattr(config, "get_tts_voice", lambda: "NotAVoice")
    monkeypatch.setattr(config, "get_tts_timeout_seconds", lambda: 30)

    def fake_ws(*, model, voice, text, api_key, speech_rate=1.0):
        captured.update(model=model, voice=voice)
        return b"MP3xxxx", "audio/mpeg"

    monkeypatch.setattr(tts_model, "_synthesize_over_websocket", fake_ws)

    result = asyncio.run(tts_model.synthesize("你好世界"))
    assert (
        captured["voice"]
        == capability_for(
            "qwen-audio-3.1-tts-flash",
        ).system_voices[0]
    )
    assert result.voice == captured["voice"]


def test_gateway_also_replaces_a_default_voice_the_model_cannot_speak(
    monkeypatch,
) -> None:
    """No catalogue, not more permissive.

    The proxy rejects a voice outside a model's list the same way Bailian does
    - as a retryable 502 that reads like a broken route - so the local
    substitution has to happen on that endpoint too, not only on Bailian.
    """
    captured = _bind_gateway_tts(
        monkeypatch,
        model="qwen-audio-3.0-tts-flash",
        voice="Cherry",
    )

    asyncio.run(tts_model.synthesize("你好世界"))

    assert (
        captured["payload"]["input"]["voice"]
        == capability_for(
            "qwen-audio-3.0-tts-flash",
        ).system_voices[0]
    )


def test_gateway_still_forwards_a_voice_the_caller_named(
    monkeypatch,
) -> None:
    """A name the caller typed stays the caller's business: with no published
    catalogue the proxy owns that judgement, so it is forwarded rather than
    second guessed here."""
    captured = _bind_gateway_tts(
        monkeypatch,
        model="qwen-audio-3.0-tts-flash",
        voice="Cherry",
    )

    asyncio.run(tts_model.synthesize("你好世界", voice="StudioVoice"))

    assert captured["payload"]["input"]["voice"] == "StudioVoice"


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

    assert [segment.text for segment in result.segments] == [
        "欢迎使用阿里云。",
    ]
    sent = route.calls.last.request
    assert "x-dashscope-ossresourceresolve" not in {
        key.lower() for key in sent.headers
    }


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # The shape the proxy was measured to return.
        (
            {
                "text": "欢迎使用阿里云。",
                "output": {"sentence": {"text": "欢迎"}},
            },
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


@pytest.mark.unit
def test_agentscope_proxy_resolves_on_the_v1_api_root(monkeypatch) -> None:
    # Measured on platform-pre: /compatible-mode/v1 is not deployed there (it
    # answers the frontend HTML shell), while /v1 serves chat, media, tasks
    # and uploads alike - so the plain API root has to build every endpoint.
    assert config.video_backend_for_protocol("AgentScope Platform") == "wan"
    assert image_backend_for_protocol("agentscope platform") == "DASHSCOPE"
    assert (
        _detect_backend_from_names("", "https://platform-pre.agentscope.io/v1")
        == "DASHSCOPE"
    )
    monkeypatch.setattr(config, "get_video_backend", lambda: "wan")
    monkeypatch.setattr(
        config,
        "get_video_base_url",
        lambda: "https://platform-pre.agentscope.io/v1",
    )
    assert config.get_video_submit_url() == (
        "https://platform-pre.agentscope.io/v1/"
        "services/aigc/video-generation/video-synthesis"
    )
    assert config.get_video_task_url("task-proxy") == (
        "https://platform-pre.agentscope.io/v1/tasks/task-proxy"
    )
    assert (
        config.chat_url_for(
            "https://platform-pre.agentscope.io/v1",
            "openai",
        )
        == "https://platform-pre.agentscope.io/v1/chat/completions"
    )


@pytest.mark.unit
def test_platform_proxy_skips_the_async_probe_and_keeps_the_deadline(
    monkeypatch,
) -> None:
    """The AgentScope proxy is driven synchronously, on the render deadline.

    Measured on platform-pre: it ignores ``X-DashScope-Async`` and answers the
    generation call in 48-59 seconds. Probing async first would spend
    ``SUBMIT_REQUEST_TIMEOUT`` (60s) on a connection that can never carry a
    task id, and a client-side timeout there abandons a render the provider
    has already billed.
    """
    monkeypatch.setattr(DashScopeImageModel, "_async_unsupported", False)
    model = _model(
        base_url=(
            "https://platform-pre.agentscope.io/v1/services/aigc/"
            "multimodal-generation/generation"
        ),
        timeout=900,
    )
    observed: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(
            {
                "headers": dict(request.headers),
                "timeout": dict(request.extensions.get("timeout") or {}),
            },
        )
        return httpx.Response(
            200,
            json={"output": {"choices": [{"message": {}}]}},
        )

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            return await model._request(client, "p", "16:9", [])

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert len(observed) == 1  # no async probe, no resubmit
    assert "x-dashscope-async" not in observed[0]["headers"]
    # No oss:// reference can exist on this transport, so there is nothing to
    # resolve - claiming otherwise would hide the real failure elsewhere.
    assert "x-dashscope-ossresourceresolve" not in observed[0]["headers"]
    assert observed[0]["timeout"].get("read") == 900


def test_gateway_refuses_a_speech_model_it_has_not_published(
    monkeypatch,
) -> None:
    """The proxy gate reads the table's providers, not the model's name.

    Qwen-Audio 3.1 shares the prefix the old name check matched on while the
    proxy publishes only the 3.0 pair, so matching on the name would POST 3.1
    to a route that never listed it and report the refusal as a broken
    synthesis path.
    """

    monkeypatch.setenv("TTS_API_KEY", "sk-test")
    monkeypatch.setenv("TTS_MODEL_NAME", "qwen-audio-3.1-tts-flash")
    monkeypatch.setattr(
        model_config,
        "is_agentscope_gateway",
        lambda **kwargs: True,
    )

    with pytest.raises(ModelError, match="未发布"):
        asyncio.run(tts_model.synthesize("你好世界", voice="longanhuan_v3.1"))


def test_gateway_routes_a_published_speech_model_over_http(
    monkeypatch,
) -> None:
    """Guard on the guard: the pair the proxy does publish still speaks."""

    captured: dict = {}
    monkeypatch.setenv("TTS_API_KEY", "sk-test")
    monkeypatch.setenv("TTS_MODEL_NAME", "qwen-audio-3.0-tts-flash")
    monkeypatch.setattr(
        model_config,
        "is_agentscope_gateway",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(
        tts_model,
        "_post_json",
        _fake_post(
            captured,
            {
                "output": {"audio": {"url": "https://example.com/a.wav"}},
                "usage": {"characters": 4},
            },
        ),
    )
    monkeypatch.setattr(
        tts_model,
        "_download_audio",
        lambda url: (b"RIFFxxxx", "audio/wav"),
    )

    result = asyncio.run(
        tts_model.synthesize("你好世界", voice="longanfengyue"),
    )
    assert captured["payload"]["model"] == "qwen-audio-3.0-tts-flash"
    assert result.voice == "longanfengyue"


def test_qwen_audio_models_carry_their_own_voice_lists() -> None:
    """Each Qwen-Audio revision speaks only its own voices, and is its own row.

    ``-plus`` was absent from the table and ``-flash`` carried none, so
    ``require_capability`` silently substituted the default model and sent a
    Qwen3-TTS voice name to a Qwen-Audio request. The provider rejects that as
    an upstream 400, which the model proxy re-reports as a retryable 502 - a
    wrong voice name therefore reads exactly like a broken endpoint.
    """

    plus = require_capability("qwen-audio-3.0-tts-plus")
    flash = require_capability("qwen-audio-3.0-tts-flash")
    assert plus.model == "qwen-audio-3.0-tts-plus"
    assert flash.model == "qwen-audio-3.0-tts-flash"
    assert plus.has_system_voices
    assert flash.has_system_voices

    assert not set(plus.system_voices) & set(flash.system_voices)
    default = require_capability(DEFAULT_TTS_MODEL)
    inherited = set(flash.system_voices) & set(default.system_voices)
    # A Qwen3-TTS voice name must never reach a Qwen-Audio request: the
    # provider rejects it as an upstream 400 that the proxy re-reports as a
    # retryable 502.
    assert not inherited


def test_gateway_offers_only_the_models_it_routes() -> None:
    """The proxy answers unlisted speech names with MODEL_NOT_ALLOWED.

    Measured: ``qwen3-tts-flash`` against the platform-pre base returns
    ``ASP.BIZ.MODEL_NOT_ALLOWED`` in under a second. A choice list that
    includes it therefore recommends a guaranteed failure, so the endpoint
    each model is served on has to stay in the table.
    """

    assert {item.model for item in models_for("gateway")} == {
        "qwen-audio-3.0-tts-flash",
        "qwen-audio-3.0-tts-plus",
    }
    # Nothing disappears from the endpoint this table was written for.
    assert {item.model for item in models_for("bailian")} == {
        item.model for item in supported_models()
    }
    # A protocol switch auto-selects the first gateway model, so that one has
    # to be the cheaper revision and speakable without creating a voice first.
    first = models_for("gateway")[0]
    assert first.model == "qwen-audio-3.0-tts-flash"
    assert first.has_system_voices
