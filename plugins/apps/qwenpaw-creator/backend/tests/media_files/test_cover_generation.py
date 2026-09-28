# -*- coding: utf-8 -*-
"""Whole-piece cover poster helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from services.media_files.cover_generation import (
    COVER_FILENAME,
    build_cover_prompt,
    cover_input_fingerprint,
    cover_is_current,
    cover_reference_version_ids,
    poster_frame_from_video,
    render_cover_bytes,
)

_MARKER = "input_fingerprint="


def _project(**overrides):
    base = SimpleNamespace(
        name="深夜末班地铁",
        description="都市悬疑互动短剧。",
        strategy=SimpleNamespace(creative_brief="一列末班地铁，一条匿名提醒。"),
        visual=SimpleNamespace(style="写实冷调", visual_bible="金属与荧光灯"),
        interactive_presentation=SimpleNamespace(
            cover_file_id=None,
            cover_checksum=None,
            cover_fingerprint="",
        ),
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_cover_filename_is_the_platform_contract() -> None:
    assert COVER_FILENAME == "cover.jpg"


def test_fingerprint_is_stable_and_input_sensitive() -> None:
    first = cover_input_fingerprint(_project())
    assert first == cover_input_fingerprint(_project())
    assert first != cover_input_fingerprint(_project(name="另一个名字"))


def test_prompt_names_the_project_and_landscape_intent() -> None:
    prompt = build_cover_prompt(_project())
    assert "深夜末班地铁" in prompt
    assert "16:9" in prompt or "横" in prompt


def test_reference_labels_shape_the_poster_prompt() -> None:
    prompt = build_cover_prompt(
        _project(),
        reference_labels=["关键场景", "主角"],
    )
    assert "图一是关键场景" in prompt and "图二是主角" in prompt
    # 无参考图时不出现图一/图二措辞（纯文生图）。
    assert "图一" not in build_cover_prompt(_project())


def test_reference_version_ids_pick_first_scene_and_character() -> None:
    def entity(entity_id, kind, version_id):
        return SimpleNamespace(
            entity_id=entity_id,
            kind=kind,
            canonical_variant_id=None,
            selected_artifact_version_id=version_id,
            variants=SimpleNamespace(items={}, order=[]),
        )

    project = _project(
        visual=SimpleNamespace(
            style="写实冷调",
            visual_bible="金属与荧光灯",
            entities=SimpleNamespace(
                items={
                    "char:1": entity("char:1", "character", "art:c1"),
                    "char:2": entity("char:2", "character", "art:c2"),
                    "scene:1": entity("scene:1", "scene", "art:s1"),
                },
                order=["char:1", "scene:1", "char:2"],
            ),
        ),
    )
    # 场景在前（图一），主角按声明顺序取第一个（图二）。
    assert cover_reference_version_ids(project) == [
        ("关键场景", "art:s1"),
        ("主角", "art:c1"),
    ]


def test_cover_is_current_requires_matching_fingerprint() -> None:
    project = _project()
    assert cover_is_current(project) is False

    fingerprint = cover_input_fingerprint(project)
    presentation = project.interactive_presentation
    presentation.cover_file_id = "file-cover-1"
    presentation.cover_checksum = "a" * 64
    presentation.cover_fingerprint = f"{_MARKER}{fingerprint}"
    assert cover_is_current(project) is True

    presentation.cover_fingerprint = f"{_MARKER}{fingerprint}stale"
    assert cover_is_current(project) is False


def test_render_cover_bytes_raises_when_the_model_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("models.image.generate_image", _raise)
    with pytest.raises(Exception):
        asyncio.run(render_cover_bytes(_project()))


def test_poster_frame_falls_back_to_none_without_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.runtime_files.runtime_dependencies.resolve_ffmpeg",
        lambda: None,
    )
    assert poster_frame_from_video(b"not-really-a-video") is None


def test_poster_frame_requires_video_bytes() -> None:
    assert poster_frame_from_video(b"") is None
