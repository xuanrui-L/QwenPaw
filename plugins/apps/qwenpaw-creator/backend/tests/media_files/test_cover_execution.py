# -*- coding: utf-8 -*-
"""Cover generation task: persist the poster and drive replay/failure paths."""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from services.media_files import cover_execution
from services.media_files.cover_execution import execute_file_cover_command
from services.media_files.cover_generation import cover_input_fingerprint
from services.project_files.assets import AssetFileStore
from services.project_files.facade import CreatorFileServices
from services.runtime_files.execution_store import ProjectExecutionStore
from services.project_files.models import Project

from domain.enums import TaskStatus

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
