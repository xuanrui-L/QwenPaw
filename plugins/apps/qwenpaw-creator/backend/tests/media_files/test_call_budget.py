# -*- coding: utf-8 -*-
"""Media task statistics never limit further production."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from domain.enums import TaskKind, TaskStatus
from services.media_files.call_budget import media_call_count
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project
from services.runtime_files.execution_models import TaskRecord
from services.runtime_files.execution_store import ProjectExecutionStore


pytestmark = pytest.mark.unit

PROJECT_ID = "media-usage-project"


def _services(tmp_path, monkeypatch) -> CreatorFileServices:
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    services = CreatorFileServices.create(tmp_path.resolve())
    services.projects.create(Project.new(project_id=PROJECT_ID, name="Usage"))
    return services


def _seed_task(services, index: int, kind: TaskKind, status: TaskStatus):
    store = ProjectExecutionStore(services.root)
    task = store.create_task(
        TaskRecord(
            task_id=f"task-{index}",
            project_id=PROJECT_ID,
            kind=kind,
            status=TaskStatus.QUEUED,
            request_fingerprint=f"sha256:{index:064d}",
        ),
    )
    if status is TaskStatus.QUEUED:
        return
    store.transition_task(
        PROJECT_ID,
        task.task_id,
        expected_status={TaskStatus.QUEUED},
        status=TaskStatus.RUNNING,
    )
    if status is not TaskStatus.RUNNING:
        store.transition_task(
            PROJECT_ID,
            task.task_id,
            expected_status={TaskStatus.RUNNING},
            status=status,
        )


def test_count_includes_media_failures_but_not_local_composition(
    tmp_path,
    monkeypatch,
):
    services = _services(tmp_path, monkeypatch)
    _seed_task(services, 1, TaskKind.IMAGE_GENERATION, TaskStatus.QUEUED)
    _seed_task(services, 2, TaskKind.R2V_GENERATION, TaskStatus.RUNNING)
    _seed_task(services, 3, TaskKind.IMAGE_GENERATION, TaskStatus.FAILED)
    _seed_task(services, 4, TaskKind.COMPOSE, TaskStatus.RUNNING)
    assert media_call_count(services, PROJECT_ID) == 3


@pytest.fixture(name="large_project")
def _large_project(tmp_path, monkeypatch):
    services = _services(tmp_path, monkeypatch)
    config = tmp_path / "legacy-model-config.json"
    config.write_text('{"agent_runtime":{"media_call_budget":1}}')
    monkeypatch.setenv("CREATOR_MODEL_CONFIG_PATH", str(config))
    for index in range(201):
        _seed_task(
            services,
            index,
            TaskKind.R2V_GENERATION,
            TaskStatus.SUCCEEDED,
        )
    return services


@pytest.mark.parametrize("kind", ["image", "r2v", "s2v"])
def test_media_entrypoints_allow_more_than_200_historical_tasks(
    large_project,
    monkeypatch,
    kind,
):
    from services.media_files import image_execution, r2v_execution

    execute = AsyncMock(return_value="accepted")
    worker = SimpleNamespace(execute=execute, dispatch=execute)
    common = {
        "project_id": PROJECT_ID,
        "target_ref": "element:new-scene",
        "arguments": {},
        "idempotency_key": f"next-{kind}",
    }
    if kind == "image":
        monkeypatch.setattr(
            image_execution,
            "file_image_execution_service",
            lambda *_args, **_kwargs: worker,
        )
        call = image_execution.execute_file_image_command(
            large_project,
            command="GENERATE_STORYBOARD_IMAGE",
            **common,
        )
    else:
        monkeypatch.setattr(
            r2v_execution,
            "file_r2v_execution_service",
            lambda *_args, **_kwargs: worker,
        )
        entrypoint = (
            r2v_execution.execute_file_s2v_command
            if kind == "s2v"
            else r2v_execution.execute_file_r2v_command
        )
        call = entrypoint(large_project, **common)
    assert asyncio.run(call) == "accepted"
    execute.assert_awaited_once()
    assert media_call_count(large_project, PROJECT_ID) == 201


def test_work_graph_reports_usage_without_any_limit(large_project):
    from api.work_graph_routes import _graph_payload

    payload = _graph_payload(PROJECT_ID, large_project)
    assert payload["mediaCalls"] == 201
    assert "mediaCallBudget" not in payload
