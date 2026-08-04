# -*- coding: utf-8 -*-
# pylint: disable=protected-access,unused-argument
# flake8: noqa: E501

from __future__ import annotations

import asyncio
import json

import pytest

from models import config as model_config
from models import tts_model


def test_require_text_rejects_empty_and_overlong() -> None:
    with pytest.raises(ValueError):
        tts_model._require_text("   ")
    with pytest.raises(ValueError, match="split the script"):
        tts_model._require_text("字" * (tts_model.TTS_MAX_TEXT_CHARS + 1))
    assert tts_model._require_text(" 你好 ") == "你好"


def test_normalize_preferred_name() -> None:
    assert tts_model._normalize_preferred_name("关 羽 Hero-01!") == "hero01"
    assert tts_model._normalize_preferred_name("Guan Yu!") == "guanyu"
    assert tts_model._normalize_preferred_name("！！！") == "creatorvoice"
    assert len(tts_model._normalize_preferred_name("a" * 64)) == 20


def test_synthesize_uses_system_voice_and_flash_model(monkeypatch) -> None:
    captured: dict = {}

    async def fake_post_json(url, *, api_key, payload, timeout_seconds):
        captured["url"] = url
        captured["payload"] = payload
        return {
            "output": {"audio": {"url": "https://example.com/a.wav"}},
            "usage": {"characters": 4},
        }

    def fake_download(url):
        captured["download"] = url
        return b"RIFFxxxx", "audio/wav"

    monkeypatch.setenv("TTS_API_KEY", "sk-test")
    monkeypatch.setattr(tts_model, "_post_json", fake_post_json)
    monkeypatch.setattr(tts_model, "_download_audio", fake_download)

    result = asyncio.run(tts_model.synthesize("你好世界", voice="Serena"))
    assert "multimodal-generation/generation" in captured["url"]
    assert captured["payload"]["model"] == model_config.get_tts_model_name()
    assert captured["payload"]["input"] == {
        "text": "你好世界",
        "voice": "Serena",
    }
    assert result.audio_bytes == b"RIFFxxxx"
    assert result.characters == 4


def test_synthesize_with_voice_id_switches_to_vc_model(monkeypatch) -> None:
    captured: dict = {}

    async def fake_post_json(url, *, api_key, payload, timeout_seconds):
        captured["payload"] = payload
        return {"output": {"audio": {"url": "https://example.com/a.wav"}}}

    monkeypatch.setenv("TTS_API_KEY", "sk-test")
    monkeypatch.setattr(tts_model, "_post_json", fake_post_json)
    monkeypatch.setattr(
        tts_model,
        "_download_audio",
        lambda url: (b"RIFF", "audio/wav"),
    )

    result = asyncio.run(
        tts_model.synthesize("台词", voice_id="myvoice-abc123"),
    )
    assert captured["payload"]["model"] == model_config.get_tts_vc_model_name()
    assert captured["payload"]["input"]["voice"] == "myvoice-abc123"
    assert result.voice == "myvoice-abc123"


def test_synthesize_without_key_raises(monkeypatch) -> None:
    monkeypatch.delenv("TTS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="creator_tts_model"):
        asyncio.run(tts_model.synthesize("你好"))


def test_synthesize_rejects_unknown_system_voice(monkeypatch) -> None:
    """A made-up voice must fail before reaching the provider."""

    async def fail_post_json(url, *, api_key, payload, timeout_seconds):
        raise AssertionError("provider must not be called")

    monkeypatch.setenv("TTS_API_KEY", "sk-test")
    monkeypatch.setattr(tts_model, "_post_json", fail_post_json)

    with pytest.raises(ValueError, match="available system voices"):
        asyncio.run(
            tts_model.synthesize("你好", voice="zh-CN-YunxiNeural"),
        )


def test_enroll_voice_builds_enrollment_payload(monkeypatch) -> None:
    captured: dict = {}

    async def fake_post_json(url, *, api_key, payload, timeout_seconds):
        captured["url"] = url
        captured["payload"] = payload
        return {"output": {"voice_id": "qwen-tts-vc-guanyu-xyz"}}

    async def fake_sample_url(sample, api_key, model):
        return "https://example.com/sample.wav"

    monkeypatch.setenv("TTS_API_KEY", "sk-test")
    monkeypatch.setattr(tts_model, "_post_json", fake_post_json)
    monkeypatch.setattr(tts_model, "_sample_url", fake_sample_url)

    enrollment = asyncio.run(
        tts_model.enroll_voice(
            "https://example.com/raw.wav",
            preferred_name="Guan Yu",
        ),
    )
    assert "audio/tts/customization" in captured["url"]
    body = captured["payload"]
    assert body["model"] == "qwen-voice-enrollment"
    assert body["input"]["action"] == "create"
    assert body["input"]["preferred_name"] == "guanyu"
    assert (
        body["input"]["target_model"] == model_config.get_tts_vc_model_name()
    )
    assert body["input"]["audio"] == {"data": "https://example.com/sample.wav"}
    assert enrollment.voice_id == "qwen-tts-vc-guanyu-xyz"


def test_is_tts_configured_gates_on_key(monkeypatch) -> None:
    monkeypatch.delenv("TTS_API_KEY", raising=False)
    assert model_config.is_tts_configured() is False
    monkeypatch.setenv("TTS_API_KEY", "sk-test")
    assert model_config.is_tts_configured() is True


def test_tts_api_key_falls_back_to_the_text_credential(
    tmp_path,
    monkeypatch,
) -> None:
    """TTS reuses the LLM key by default so it needs no separate entry."""

    monkeypatch.delenv("TTS_API_KEY", raising=False)
    monkeypatch.delenv("TEXT_API_KEY", raising=False)
    config_path = tmp_path / "config" / "model_config.json"
    config_path.parent.mkdir(parents=True)
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    monkeypatch.setenv("CREATOR_MODEL_CONFIG_PATH", str(config_path))

    def write(tts_section: dict) -> None:
        config_path.write_text(
            json.dumps(
                {
                    "llm": {
                        "enabled": True,
                        "api_key": "sk-shared",
                        "base_url": "https://example.test/v1",
                        "model_name": "qwen",
                    },
                    "tts": tts_section,
                },
            ),
            encoding="utf-8",
        )
        model_config._clear_user_config_cache()

    write({"enabled": True, "model_name": "qwen3-tts-flash"})
    assert model_config.get_tts_api_key() == "sk-shared"
    assert model_config.is_tts_configured()

    write(
        {
            "enabled": True,
            "model_name": "qwen3-tts-flash",
            "reuse_llm_key": False,
        },
    )
    assert model_config.get_tts_api_key() == ""

    write(
        {
            "enabled": True,
            "model_name": "qwen3-tts-flash",
            "reuse_llm_key": False,
            "api_key": "sk-own",
        },
    )
    assert model_config.get_tts_api_key() == "sk-own"
