# -*- coding: utf-8 -*-
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest

from domain.enums import TaskKind
from services.runtime_files import atomic_store
from services.runtime_files.errors import CorruptRecordError
from services.runtime_files.execution_models import TaskRecord
from services.runtime_files.execution_store import (
    ExecutionStoreIntegrityError,
    ProjectExecutionStore,
    UnsafeExecutionPath,
)

pytestmark = pytest.mark.unit
PROJECT_ID = "project-publication"


def _store(tmp_path: Path) -> ProjectExecutionStore:
    project = tmp_path / PROJECT_ID
    project.mkdir()
    (project / "project.json").write_text("{}\n", encoding="utf-8")
    return ProjectExecutionStore(tmp_path)


def _task(task_id: str, *, offset: int = 0) -> TaskRecord:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=offset,
    )
    return TaskRecord(
        task_id=task_id,
        project_id=PROJECT_ID,
        kind=TaskKind.COMPOSE,
        request_fingerprint=f"sha256:{task_id}",
        created_at=timestamp,
        updated_at=timestamp,
    )


def _head(tmp_path: Path, task_id: str = "task-new") -> Path:
    return tmp_path / PROJECT_ID / "runtime" / "tasks" / task_id / "task.json"


def test_listing_remains_available_while_a_task_head_is_being_published(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path)
    store.create_task(_task("task-existing"))
    staged = Event()
    publish = Event()
    original_create = atomic_store.atomic_create_bytes

    def create_with_barrier(target, payload, **kwargs):
        if Path(target) == _head(tmp_path):

            def stage_hook(stage, _path):
                if stage == "temp_fsynced":
                    staged.set()
                    assert publish.wait(5), "test did not release publication"

            kwargs["stage_hook"] = stage_hook
        return original_create(target, payload, **kwargs)

    monkeypatch.setattr(
        atomic_store,
        "atomic_create_bytes",
        create_with_barrier,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(store.create_task, _task("task-new", offset=1))
        try:
            assert staged.wait(5)
            assert _head(tmp_path).parent.is_dir()
            assert not _head(tmp_path).exists()
            reader = pool.submit(store.list_tasks, PROJECT_ID)
            assert [item.task_id for item in reader.result(timeout=1)] == [
                "task-existing",
            ]
        finally:
            publish.set()
            writer.result(timeout=5)

    assert [item.task_id for item in store.list_tasks(PROJECT_ID)] == [
        "task-new",
        "task-existing",
    ]


def test_listing_ignores_an_empty_task_directory(tmp_path):
    store = _store(tmp_path)
    _head(tmp_path).parent.mkdir(parents=True)
    assert store.list_tasks(PROJECT_ID) == []


@pytest.mark.parametrize("payload", ['{"incomplete":', "{}"])
def test_listing_still_rejects_malformed_published_heads(tmp_path, payload):
    store = _store(tmp_path)
    head = _head(tmp_path)
    head.parent.mkdir(parents=True)
    head.write_text(payload, encoding="utf-8")
    with pytest.raises(CorruptRecordError):
        store.list_tasks(PROJECT_ID)


def test_listing_does_not_hide_permission_errors(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.create_task(_task("task-new"))
    original_read = Path.read_bytes

    def read_bytes(path):
        if path == _head(tmp_path):
            raise PermissionError("unreadable published task")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(PermissionError, match="unreadable published task"):
        store.list_tasks(PROJECT_ID)


@pytest.mark.parametrize("link_kind", ["directory", "head", "dangling-head"])
def test_listing_rejects_task_symlinks(tmp_path, link_kind):
    store = _store(tmp_path)
    head = _head(tmp_path)
    if link_kind == "directory":
        target = tmp_path / "external-task"
        target.mkdir()
        head.parent.parent.mkdir(parents=True)
        head.parent.symlink_to(target, target_is_directory=True)
    else:
        head.parent.mkdir(parents=True)
        target = tmp_path / "external-task.json"
        if link_kind == "head":
            target.write_text(_task("task-new").model_dump_json())
        head.symlink_to(target)
    with pytest.raises(UnsafeExecutionPath):
        store.list_tasks(PROJECT_ID)


@pytest.mark.parametrize(
    "overrides",
    [{"project_id": "another-project"}, {"task_id": "another-task"}],
)
def test_listing_still_validates_published_task_identity(tmp_path, overrides):
    store = _store(tmp_path)
    head = _head(tmp_path)
    head.parent.mkdir(parents=True)
    head.write_text(
        _task("task-new").model_copy(update=overrides).model_dump_json(),
        encoding="utf-8",
    )
    with pytest.raises(ExecutionStoreIntegrityError):
        store.list_tasks(PROJECT_ID)
