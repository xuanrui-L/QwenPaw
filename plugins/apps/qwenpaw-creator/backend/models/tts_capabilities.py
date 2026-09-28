# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Capability table for the supported DashScope speech-synthesis models.

The provider exposes two families that share nothing but the credential:

- ``qwen-tts``: synthesis over HTTP ``multimodal-generation``; voice cloning and
  voice design live on separate companion models (``-vc-`` / ``-vd-``) and are
  managed through ``qwen-voice-enrollment`` / ``qwen-voice-design``.
- ``cosyvoice``: synthesis over WebSocket; one ``voice-enrollment`` surface
  handles both cloning and design, and the newest models ship no system voices
  at all, so a character voice must be created before anything can be spoken.
- ``qwen-audio``: shares the CosyVoice enrollment surface but does ship system
  voices, and the set is per model - ``-plus`` carries two flagship voices and
  ``-flash`` a dozen social/companion ones. Voices never cross between the two,
  so each model needs its own list. These are also the only speech models the
  model proxy routes, which is what ``providers`` records.

Keeping the differences in one table lets the rest of the backend ask
capability questions ("does this model have system voices?", "which model do I
enroll against?", "can this endpoint serve it?") instead of pattern-matching
model names, and lets the UI ask for a model list without duplicating the
knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TtsFamily = Literal["qwen-tts", "cosyvoice"]
TtsTransport = Literal["http", "websocket"]
# ``bailian`` is the direct DashScope endpoint, ``gateway`` the AgentScope model
# proxy. A model listed under neither would be unselectable everywhere, so the
# default is the endpoint this table was written for.
TtsProvider = Literal["bailian", "gateway"]


@dataclass(frozen=True, slots=True)
class TtsModelCapability:
    """What one synthesis model supports and which models it enrolls against."""

    model: str
    family: TtsFamily
    transport: TtsTransport
    label: str
    # System (preset) voices usable without creating a voice first.
    system_voices: tuple[str, ...]
    # Model that cloned voices are bound to; empty means "this model itself".
    clone_target: str = ""
    # Model that designed voices are bound to; empty means "not supported".
    design_target: str = ""
    # Endpoints that actually serve this model. The proxy publishes its own
    # model list and answers anything outside it with
    # ``ASP.BIZ.MODEL_NOT_ALLOWED``, so offering a Bailian-only name there is
    # a guaranteed failure the user has to discover by hand.
    providers: tuple[TtsProvider, ...] = ("bailian",)

    @property
    def has_system_voices(self) -> bool:
        return bool(self.system_voices)

    def clone_model(self) -> str:
        return self.clone_target or self.model

    def design_model(self) -> str:
        return self.design_target or self.model

    @property
    def supports_design(self) -> bool:
        return bool(self.design_target) or self.family == "cosyvoice"


# Only the current generation of each family is supported: older revisions add
# configuration surface without adding capability.
_QWEN_TTS_VOICES = (
    "Cherry",
    "Serena",
    "Ethan",
    "Chelsie",
    "Dylan",
    "Jada",
    "Sunny",
    "Nofish",
    "Marcus",
    "Roy",
)

# From the Qwen-Audio-TTS voice list: a voice outside its own model's list is
# rejected upstream, and the model proxy reports that rejection as 502 with
# ``retryable: true``, so a wrong name here reads as a broken endpoint.
_QWEN_AUDIO_PLUS_VOICES = (
    "longanlingxin",
    "longanlufeng",
)

_QWEN_AUDIO_FLASH_VOICES = (
    "longanfengyue",
    "longanyuanfei",
    "longanlingxi",
    "longanxiaoxin",
    "longanhuan_v3.6",
    "longjielidou_v3.6",
    "longpaopao_v3.6",
    "longhuohuo_v3.6",
    "longchuanshu_v3.6",
    "loongmary",
    "loongeva_v3.6",
    "loongjohn",
)

_CAPABILITIES: tuple[TtsModelCapability, ...] = (
    TtsModelCapability(
        model="qwen3-tts-flash",
        family="qwen-tts",
        transport="http",
        label="Qwen3 TTS Flash（系统音色，快速）",
        system_voices=_QWEN_TTS_VOICES,
        clone_target="qwen3-tts-vc-2026-01-22",
        design_target="qwen3-tts-vd-2026-01-26",
    ),
    TtsModelCapability(
        model="qwen3-tts-instruct-flash",
        family="qwen-tts",
        transport="http",
        label="Qwen3 TTS Instruct Flash（系统音色，可控情绪语速）",
        system_voices=_QWEN_TTS_VOICES,
        clone_target="qwen3-tts-vc-2026-01-22",
        design_target="qwen3-tts-vd-2026-01-26",
    ),
    TtsModelCapability(
        model="cosyvoice-v3.5-plus",
        family="cosyvoice",
        transport="websocket",
        label="CosyVoice 3.5 Plus（无系统音色，需先设计或复刻音色）",
        system_voices=(),
    ),
    TtsModelCapability(
        model="qwen-audio-3.0-tts-flash",
        family="cosyvoice",
        transport="websocket",
        label="Qwen-Audio 3.0 TTS Flash（12 个系统音色，快速）",
        system_voices=_QWEN_AUDIO_FLASH_VOICES,
        providers=("bailian", "gateway"),
    ),
    # Listed after its cheaper sibling on purpose: a protocol switch that has
    # to pick a replacement model takes the first available one, and that
    # should never be the flagship.
    TtsModelCapability(
        model="qwen-audio-3.0-tts-plus",
        family="cosyvoice",
        transport="websocket",
        label="Qwen-Audio 3.0 TTS Plus（旗舰音色，2 个系统音色）",
        system_voices=_QWEN_AUDIO_PLUS_VOICES,
        providers=("bailian", "gateway"),
    ),
)

_BY_MODEL = {item.model: item for item in _CAPABILITIES}

DEFAULT_TTS_MODEL = "qwen3-tts-flash"


def supported_models() -> tuple[TtsModelCapability, ...]:
    return _CAPABILITIES


def models_for(provider: TtsProvider) -> tuple[TtsModelCapability, ...]:
    """Models one endpoint actually serves, in table order."""

    return tuple(item for item in _CAPABILITIES if provider in item.providers)


def capability_for(model: str) -> TtsModelCapability | None:
    """Capability of ``model``, or None when it is not a supported model."""

    return _BY_MODEL.get((model or "").strip())


def require_capability(model: str) -> TtsModelCapability:
    """Capability of ``model``, falling back to the default synthesis model.

    A deployment can carry a model name this build does not know (an older
    config, or a hand-edited file). Falling back keeps narration working
    instead of failing the whole run, and the caller logs the substitution.
    """

    found = capability_for(model)
    if found is not None:
        return found
    return _BY_MODEL[DEFAULT_TTS_MODEL]


__all__ = [
    "DEFAULT_TTS_MODEL",
    "TtsFamily",
    "TtsModelCapability",
    "TtsProvider",
    "TtsTransport",
    "capability_for",
    "models_for",
    "require_capability",
    "supported_models",
]
