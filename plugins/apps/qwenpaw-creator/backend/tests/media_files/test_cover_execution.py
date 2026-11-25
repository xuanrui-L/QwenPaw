# -*- coding: utf-8 -*-
"""Interactive video cover generation and execution."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from domain.enums import TaskStatus
from services.media_files import cover_execution
from services.media_files.cover_execution import execute_file_cover_command
from services.media_files.cover_generation import (
    COVER_FILENAME,
    build_cover_prompt,
    cover_input_fingerprint,
    cover_is_current,
    cover_reference_version_ids,
    poster_frame_from_video,
    render_cover_bytes,
)
from services.project_files.assets import AssetFileStore
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project
from services.runtime_files.execution_store import ProjectExecutionStore

PROJECT_ID = "p-cover"
POSTER = b"\xff\xd8\xff\xe0 fake poster bytes"
TARGET = f"project:{PROJECT_ID}"

pytestmark = pytest.mark.unit


def _services(tmp_path) -> CreatorFileServices:
    services = CreatorFileServices.create(tmp_path.resolve())
    project = Project.new(project_id=PROJECT_ID, name="Cover Exec")
    services.projects.create(project)
    return services


def _run(services, key="dag-cover-1"):
    return asyncio.run(
        execute_file_cover_command(
            services,
            project_id=PROJECT_ID,
            target_ref=TARGET,
            arguments={},
            idempotency_key=key,
        ),
    )


def _stub_render(monkeypatch, payload=POSTER):
    calls: list[int] = []

    async def fake_render(_project, **_kwargs):
        calls.append(1)
        return payload

    monkeypatch.setattr(cover_execution, "render_cover_bytes", fake_render)
    return calls


def test_cover_command_persists_poster_and_pointer(tmp_path, monkeypatch):
    services = _services(tmp_path)
    calls = _stub_render(monkeypatch)

    result = _run(services)

    assert result.replayed is False
    assert calls == [1]
    project = services.projects.read(PROJECT_ID).project
    presentation = project.interactive_presentation
    checksum = hashlib.sha256(POSTER).hexdigest()
    assert presentation.cover_checksum == checksum
    assert presentation.cover_file_id
    indexed = project.assets.files_by_id[presentation.cover_file_id]
    assert indexed.sha256 == checksum
    assert indexed.media_type == "image/jpeg"
    store = AssetFileStore(services.projects.project_root(PROJECT_ID))
    with store.open_verified(indexed) as stream:
        assert stream.read() == POSTER


def test_same_inputs_replay_without_second_render(tmp_path, monkeypatch):
    services = _services(tmp_path)
    calls = _stub_render(monkeypatch)

    _run(services)
    replayed = _run(services)

    assert replayed.replayed is True
    assert calls == [1]


def test_regenerate_forces_fresh_render(tmp_path, monkeypatch):
    services = _services(tmp_path)
    calls = _stub_render(monkeypatch)
    _run(services)
    assert calls == [1]

    # An unchanged brief replays, but regenerate=True mints a fresh render and
    # a new transaction so an intentional "redo" is not swallowed by the cache.
    replayed = _run(services)
    assert replayed.replayed is True
    assert calls == [1]

    regen = asyncio.run(
        execute_file_cover_command(
            services,
            project_id=PROJECT_ID,
            target_ref=TARGET,
            arguments={"regenerate": True},
            idempotency_key="dag-cover-regen",
        ),
    )
    assert regen.replayed is False
    assert calls == [1, 1]


def test_render_failure_is_recorded_and_not_persisted(tmp_path, monkeypatch):
    services = _services(tmp_path)

    async def boom(project, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(cover_execution, "render_cover_bytes", boom)

    with pytest.raises(RuntimeError):
        _run(services)

    project = services.projects.read(PROJECT_ID).project
    assert project.interactive_presentation.cover_file_id is None
    tasks = ProjectExecutionStore(services.root).list_tasks(PROJECT_ID)
    assert any(
        task.kind == cover_execution.TaskKind.COVER_GENERATION
        and task.status is TaskStatus.FAILED
        for task in tasks
    )


def test_cover_fingerprint_marks_staleness(tmp_path, monkeypatch):
    services = _services(tmp_path)
    _stub_render(monkeypatch)
    _run(services)
    project = services.projects.read(PROJECT_ID).project
    expected = f"input_fingerprint={cover_input_fingerprint(project)}"
    assert project.interactive_presentation.cover_fingerprint == expected


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


def test_fingerprint_is_stable_and_input_sensitive() -> None:
    first = cover_input_fingerprint(_project())
    assert first == cover_input_fingerprint(_project())
    assert first != cover_input_fingerprint(_project(name="另一个名字"))


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
