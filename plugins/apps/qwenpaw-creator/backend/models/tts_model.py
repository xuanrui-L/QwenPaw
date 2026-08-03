# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Text-to-speech client for Creator narration and character voices.

Two DashScope surfaces share the ``creator_tts_model`` configuration:

- synthesis (``qwen3-tts-flash`` family) renders text with a system voice or
  an enrolled custom voice id;
- voice enrollment (``qwen-voice-enrollment``) clones a character voice from
  a short audio sample and returns a ``voice_id`` bound to the VC model.
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from models import config
from models.media_transport import upload_local_file_to_dashscope_temp
from utils.logger import setup_logger
from utils.paths import local_path_from_file_url
from utils.remote_download import download_remote_file

logger = setup_logger("models.tts")

# qwen3-tts caps one request at ~512 tokens; a conservative character bound
# keeps requests inside the limit for mixed Chinese/English scripts.
TTS_MAX_TEXT_CHARS = 800

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 2.0
_PREFERRED_NAME_PATTERN = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class TTSSynthesis:
    audio_bytes: bytes
    media_type: str
    model: str
    voice: str
    characters: int


@dataclass(frozen=True, slots=True)
class VoiceEnrollment:
    voice_id: str
    target_model: str


def _endpoint(base_url: str, suffix: str) -> str:
    return f"{base_url.rstrip('/')}/{suffix.lstrip('/')}"


def _require_text(text: str) -> str:
    value = text.strip()
    if not value:
        raise ValueError("TTS text must not be empty")
    if len(value) > TTS_MAX_TEXT_CHARS:
        raise ValueError(
            f"TTS text is too long ({len(value)} chars > {TTS_MAX_TEXT_CHARS}); "
            "split the script at sentence boundaries and synthesize each part",
        )
    return value


def _require_key() -> str:
    key = config.get_tts_api_key()
    if not key:
        raise ValueError(
            "TTS requires an API key; configure creator_tts_model or set "
            "TTS_API_KEY",
        )
    return key


async def _post_json(
    url: str,
    *,
    api_key: str,
    payload: Mapping[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-OssResourceResolve": "enable",
    }
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30, read=timeout_seconds),
    ) as client:
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            response = await client.post(url, headers=headers, json=payload)
            if (
                response.status_code in _RETRY_STATUS
                and attempt < _RETRY_ATTEMPTS
            ):
                wait = _RETRY_BACKOFF_SECONDS * attempt
                logger.warning(
                    "TTS request got HTTP %d (attempt %d/%d), retrying in %.0fs",
                    response.status_code,
                    attempt,
                    _RETRY_ATTEMPTS,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
    raise RuntimeError("TTS request retries exhausted")


def _download_audio(url: str) -> tuple[bytes, str]:
    with tempfile.TemporaryDirectory(prefix="creator-tts-") as directory:
        target = Path(directory) / "speech.wav"
        download_remote_file(url, str(target))
        content = target.read_bytes()
    if not content:
        raise RuntimeError("TTS returned an empty audio payload")
    media_type = mimetypes.guess_type(urlparse(url).path)[0] or "audio/wav"
    if not media_type.startswith("audio/"):
        media_type = "audio/wav"
    return content, media_type


async def synthesize(
    text: str,
    *,
    voice: str | None = None,
    voice_id: str | None = None,
) -> TTSSynthesis:
    """Render ``text`` to speech; ``voice_id`` selects an enrolled clone."""

    value = _require_text(text)
    key = _require_key()
    if voice_id:
        model = config.get_tts_vc_model_name()
        active_voice = voice_id
    else:
        model = config.get_tts_model_name() or "qwen3-tts-flash"
        active_voice = (voice or "").strip() or config.get_tts_voice()
    logger.info(
        "TTS synthesize: model=%s voice=%s chars=%d cloned=%s",
        model,
        active_voice,
        len(value),
        bool(voice_id),
    )
    payload: dict[str, Any] = {
        "model": model,
        "input": {"text": value, "voice": active_voice},
    }
    data = await _post_json(
        _endpoint(
            config.get_tts_base_url(),
            "services/aigc/multimodal-generation/generation",
        ),
        api_key=key,
        payload=payload,
        timeout_seconds=config.get_tts_timeout_seconds(),
    )
    output = data.get("output") or {}
    audio = output.get("audio") or {}
    audio_url = str(audio.get("url") or "")
    if not audio_url:
        raise RuntimeError(f"TTS response has no audio url: {output}")
    content, media_type = await asyncio.to_thread(_download_audio, audio_url)
    usage = data.get("usage") or {}
    return TTSSynthesis(
        audio_bytes=content,
        media_type=media_type,
        model=model,
        voice=active_voice,
        characters=int(usage.get("characters") or len(value)),
    )


def _normalize_preferred_name(name: str) -> str:
    value = _PREFERRED_NAME_PATTERN.sub("", name.strip().casefold())
    return (value or "creatorvoice")[:20]


async def _sample_url(sample_media_url: str, api_key: str, model: str) -> str:
    """Return a URL the enrollment API can fetch, uploading local samples."""

    parsed = urlparse(sample_media_url)
    if parsed.scheme == "file":
        local_path = local_path_from_file_url(sample_media_url)
        media_type = mimetypes.guess_type(local_path.name)[0] or "audio/wav"
        return await upload_local_file_to_dashscope_temp(
            local_path,
            api_key=api_key,
            model_name=model,
            media_type=media_type,
        )
    if parsed.scheme in {"http", "https", "oss"}:
        return sample_media_url
    raise ValueError(
        "voice sample must be a local file or HTTP(S)/OSS media URL",
    )


async def enroll_voice(
    sample_media_url: str,
    *,
    preferred_name: str,
) -> VoiceEnrollment:
    """Clone a voice from a 10-20s audio sample and return its voice id."""

    key = _require_key()
    target_model = config.get_tts_vc_model_name()
    audio_url = await _sample_url(sample_media_url, key, target_model)
    logger.info(
        "TTS enroll_voice: target_model=%s name=%s",
        target_model,
        preferred_name,
    )
    data = await _post_json(
        _endpoint(
            config.get_tts_base_url(),
            "services/audio/tts/customization",
        ),
        api_key=key,
        payload={
            "model": "qwen-voice-enrollment",
            "input": {
                "action": "create",
                "target_model": target_model,
                "preferred_name": _normalize_preferred_name(preferred_name),
                "audio": {"data": audio_url},
            },
        },
        timeout_seconds=config.get_tts_timeout_seconds(),
    )
    output = data.get("output") or {}
    voice_value = str(output.get("voice_id") or output.get("voice") or "")
    if not voice_value:
        raise RuntimeError(f"voice enrollment returned no voice id: {output}")
    return VoiceEnrollment(voice_id=voice_value, target_model=target_model)


async def delete_voice(voice_id: str) -> bool:
    """Best-effort deletion of an enrolled voice; returns success."""

    try:
        await _post_json(
            _endpoint(
                config.get_tts_base_url(),
                "services/audio/tts/customization",
            ),
            api_key=_require_key(),
            payload={
                "model": "qwen-voice-enrollment",
                "input": {"action": "delete", "voice": voice_id},
            },
            timeout_seconds=config.get_tts_timeout_seconds(),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - unbind must not fail the caller
        logger.warning("voice deletion failed for %s: %s", voice_id, exc)
        return False


__all__ = [
    "TTS_MAX_TEXT_CHARS",
    "TTSSynthesis",
    "VoiceEnrollment",
    "delete_voice",
    "enroll_voice",
    "synthesize",
]
