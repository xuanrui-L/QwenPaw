# -*- coding: utf-8 -*-
"""Manual (real-key) integration checks for the qwen3-asr branch.

Billed DashScope calls, so this is skipped unless CREATOR_ASR_REAL_TEST is set
and audio paths are provided. Run from an environment where the ASR model
resolves to qwen3-asr-flash (creator model_config.json or ASR_* env vars).

    CREATOR_ASR_REAL_TEST=1 \
    CREATOR_ASR_REAL_AUDIO=/path/short.mp3 \
    CREATOR_ASR_REAL_AUDIO_LONG=/path/long.mp3 \
    QWENPAW_WORKING_DIR=~/.qwenpaw-asr \
    CREATOR_DATA_ROOT=~/.qwenpaw-asr/creator-runtime \
    QWENPAW_KEYRING_ACCOUNT=<account> \
    python -m pytest tests/manual/test_real_asr_qwen3.py -s

Per the acceptance rule, transcript correctness is confirmed by reading the
printed text; the assertions only guard structural invariants.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from models import asr_model, config

_ENABLED = os.environ.get("CREATOR_ASR_REAL_TEST", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

pytestmark = pytest.mark.skipif(
    not _ENABLED,
    reason="set CREATOR_ASR_REAL_TEST=1 to run billed qwen3-asr checks",
)


def _require_audio(env_name: str) -> str:
    raw = os.environ.get(env_name, "").strip()
    if not raw or not Path(raw).is_file():
        pytest.skip(f"{env_name} must point to an existing audio file")
    return Path(raw).resolve().as_uri()


def _assert_qwen3(result: asr_model.ASRResult) -> None:
    assert result.model.casefold().startswith("qwen3-asr")
    assert result.provider == "fun-asr"
    assert result.segments, "expected at least one transcript segment"


def test_real_short_audio_transcribes() -> None:
    assert config.get_asr_model_name().casefold().startswith("qwen3-asr")
    result = asyncio.run(
        asr_model.transcribe(_require_audio("CREATOR_ASR_REAL_AUDIO")),
    )
    _assert_qwen3(result)
    for previous, current in zip(result.segments, result.segments[1:]):
        assert current.start_ms >= previous.start_ms
    print("\n[short] " + " / ".join(seg.text for seg in result.segments))


def test_real_long_audio_chunks_are_contiguous() -> None:
    result = asyncio.run(
        asr_model.transcribe(_require_audio("CREATOR_ASR_REAL_AUDIO_LONG")),
    )
    _assert_qwen3(result)
    # Cross-chunk offsets must join without gaps or overlaps so no spoken
    # content is dropped at a boundary (the A2 regression).
    for previous, current in zip(result.segments, result.segments[1:]):
        assert current.start_ms == previous.end_ms
    joined = "".join(seg.text for seg in result.segments)
    print(f"\n[long] {len(result.segments)} segments, {len(joined)} chars")
    print(joined)
