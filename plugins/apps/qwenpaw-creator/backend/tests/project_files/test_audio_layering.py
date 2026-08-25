# -*- coding: utf-8 -*-
# flake8: noqa: E501
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from services.project_files.models import AudioCreation, Project


pytestmark = pytest.mark.unit

_AUDIO_URL = "https://example.com/bgm.mp3"
_AUDIO_CHECKSUM = hashlib.sha256(_AUDIO_URL.encode("utf-8")).hexdigest()


def _project_raw() -> dict[str, Any]:
    raw = Project.new(
        project_id="project-audio",
        name="Audio Project",
        now=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
    ).model_dump(mode="json")
    raw["assets"] = {
        "source_versions_by_id": {
            "audio-version-1": {
                "version_id": "audio-version-1",
                "logical_asset_id": "logical-audio-1",
                "name": "bgm.mp3",
                "file_id": None,
                "checksum": _AUDIO_CHECKSUM,
                "media_kind": "audio",
                "media_type": "audio/mpeg",
                "duration_seconds": 60,
                "created_at": "2026-08-24T08:00:00Z",
                "metadata": {
                    "publicSourceUrl": _AUDIO_URL,
                    "sourceKind": "remote_url",
                    "checksumKind": "source_url_sha256",
                },
            },
        },
    }
    return raw


def _r2v_element(
    element_id: str,
    *,
    start_tick: int,
    duration_tick: int,
    dialogue: str,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "element_id": element_id,
        "label": "R2V",
        "enabled": enabled,
        "span": {"start_tick": start_tick, "duration_tick": duration_tick},
        "location": {
            "coordinate_space": "normalized_canvas",
            "x": 0.5,
            "y": 0.5,
            "width": 1,
            "height": 1,
            "anchor_x": 0.5,
            "anchor_y": 0.5,
            "rotation_degrees": 0,
            "opacity": 1,
        },
        "z_index": 0,
        "creation": {
            "type": "r2v",
            "intent": "剧情",
            "shots": {
                "items": {
                    "shot-1": {
                        "shot_id": "shot-1",
                        "description": "角色开口说话",
                        "camera": "⊙ 静止",
                        "framing": "中景",
                        "dialogue": dialogue,
                        "duration_seconds": duration_tick / 1000,
                    },
                },
                "order": ["shot-1"],
            },
        },
    }


def _audio_element(
    element_id: str,
    *,
    start_tick: int,
    duration_tick: int,
    role: str,
) -> dict[str, Any]:
    return {
        "element_id": element_id,
        "label": "Audio",
        "enabled": True,
        "span": {"start_tick": start_tick, "duration_tick": duration_tick},
        "z_index": 0,
        "creation": {
            "type": "audio",
            "source_asset_version_id": "audio-version-1",
            "role": role,
        },
    }


def _with_elements(*elements: dict[str, Any]) -> dict[str, Any]:
    raw = _project_raw()
    raw["timelines"]["items"]["timeline:main"]["elements_by_id"] = {
        element["element_id"]: element for element in elements
    }
    return raw


def test_audio_creation_role_defaults_to_bgm() -> None:
    creation = AudioCreation(source_asset_version_id="audio-version-1")
    assert creation.role == "bgm"


def test_audio_creation_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError):
        AudioCreation(
            source_asset_version_id="audio-version-1",
            role="voiceover",
        )


def test_legacy_script_audio_mixes_as_narration() -> None:
    creation = AudioCreation(
        source_asset_version_id="audio-version-1",
        script="这是一段旁白",
    )
    assert creation.role == "bgm"
    assert creation.effective_role == "narration"


def test_explicit_roles_are_not_overridden_by_script() -> None:
    creation = AudioCreation(
        source_asset_version_id="audio-version-1",
        role="sfx",
        script="备注",
    )
    assert creation.effective_role == "sfx"


def test_narration_overlapping_dialogue_element_is_rejected() -> None:
    raw = _with_elements(
        _r2v_element(
            "r2v-1",
            start_tick=0,
            duration_tick=5_000,
            dialogue="我今天去了一个地方",
        ),
        _audio_element(
            "narration-1",
            start_tick=4_000,
            duration_tick=3_000,
            role="narration",
        ),
    )
    with pytest.raises(ValidationError, match="overlaps voiced"):
        Project.model_validate(raw)


def test_narration_clear_of_dialogue_is_accepted() -> None:
    raw = _with_elements(
        _r2v_element(
            "r2v-1",
            start_tick=0,
            duration_tick=5_000,
            dialogue="我今天去了一个地方",
        ),
        _audio_element(
            "narration-1",
            start_tick=5_000,
            duration_tick=3_000,
            role="narration",
        ),
    )
    project = Project.model_validate(raw)
    creation = (
        project.timelines.items["timeline:main"]
        .elements_by_id["narration-1"]
        .creation
    )
    assert isinstance(creation, AudioCreation)
    assert creation.role == "narration"


def test_narration_over_dialogue_free_clip_is_accepted() -> None:
    raw = _with_elements(
        _r2v_element(
            "r2v-1",
            start_tick=0,
            duration_tick=5_000,
            dialogue="",
        ),
        _audio_element(
            "narration-1",
            start_tick=0,
            duration_tick=5_000,
            role="narration",
        ),
    )
    Project.model_validate(raw)


def test_bgm_may_overlap_dialogue_elements() -> None:
    raw = _with_elements(
        _r2v_element(
            "r2v-1",
            start_tick=0,
            duration_tick=5_000,
            dialogue="我今天去了一个地方",
        ),
        _audio_element(
            "bgm-1",
            start_tick=0,
            duration_tick=8_000,
            role="bgm",
        ),
    )
    Project.model_validate(raw)


def test_disabled_dialogue_element_does_not_block_narration() -> None:
    raw = _with_elements(
        _r2v_element(
            "r2v-1",
            start_tick=0,
            duration_tick=5_000,
            dialogue="我今天去了一个地方",
            enabled=False,
        ),
        _audio_element(
            "narration-1",
            start_tick=0,
            duration_tick=5_000,
            role="narration",
        ),
    )
    Project.model_validate(raw)


def test_fades_are_agent_owned_and_default_unset() -> None:
    # Fades are a creative parameter: the model stores only the agent's
    # explicit choice; None means "adaptive role default at render time"
    # (bgm: min(2s, span/4), narration/sfx: hard edges).
    creation = AudioCreation(source_asset_version_id="audio-version-1")
    assert creation.fade_in_seconds is None
    assert creation.fade_out_seconds is None
    explicit = AudioCreation(
        source_asset_version_id="audio-version-1",
        role="bgm",
        fade_in_seconds=0.0,
        fade_out_seconds=5.0,
    )
    assert explicit.fade_in_seconds == 0.0
    assert explicit.fade_out_seconds == 5.0
