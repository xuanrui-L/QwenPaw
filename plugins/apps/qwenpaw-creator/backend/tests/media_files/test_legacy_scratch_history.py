# -*- coding: utf-8 -*-
"""Retention of scratch written before the compatibility baseline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from domain.enums import TaskStatus
from services.media_files.task_scratch import (
    ORPHAN_RELEASE_GRACE,
    project_storage_profile,
    read_scratch_baseline,
    release_orphaned_scratch,
)
from services.runtime_files.execution_store import ProjectExecutionStore
from .test_task_scratch import (
    PROJECT_ID,
    T0,
    _media,
    _media_bytes,
    _moved,
    _needs_pinned_release,
    _scratch,
    _scratch_on_both_sides_of_baseline,
    _services,
    _task,
    _tree_bytes,
)


@_needs_pinned_release
def test_sweep_ignores_everything_from_before_the_baseline(
    tmp_path: Path,
) -> None:
    scratch = _scratch(tmp_path)
    store = ProjectExecutionStore(tmp_path)
    store.create_task(_task(TaskStatus.QUEUED))
    _moved(
        store,
        "task-1",
        frm=TaskStatus.QUEUED,
        to=TaskStatus.RUNNING,
        updated_at=T0 - timedelta(days=2),
    )
    _moved(
        store,
        "task-1",
        frm=TaskStatus.RUNNING,
        to=TaskStatus.SUCCEEDED,
        updated_at=T0 - timedelta(days=1),
    )

    swept = release_orphaned_scratch(_services(tmp_path), now=T0)

    assert swept.removed_files == 0
    assert read_scratch_baseline(tmp_path / PROJECT_ID) == T0
    assert len(_media(scratch)) == 2

    # A task that finished after the baseline is fair game once it is quiet.
    later = _scratch(tmp_path, "task-2")
    store.create_task(
        _task(TaskStatus.QUEUED).model_copy(update={"task_id": "task-2"}),
    )
    _moved(
        store,
        "task-2",
        frm=TaskStatus.QUEUED,
        to=TaskStatus.FAILED,
        updated_at=T0 + timedelta(minutes=5),
    )

    summary = release_orphaned_scratch(
        _services(tmp_path),
        now=T0 + ORPHAN_RELEASE_GRACE + timedelta(minutes=10),
    )

    assert summary.removed_files == 2
    assert _media(later) == []
    assert len(_media(scratch)) == 2


def test_scratch_brought_in_by_an_import_stays_protected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The stamp lands at first allocation, later than any archived Task, so
    # the "pre-upgrade bytes stay put" rule holds for a Project that arrived
    # through an archive rather than through creation.
    from utils.paths import media_task_scope, task_work_root

    legacy = _scratch(tmp_path, "task-legacy")
    now = datetime.now(timezone.utc)
    store = ProjectExecutionStore(tmp_path)
    store.create_task(
        _task(TaskStatus.QUEUED).model_copy(update={"task_id": "task-legacy"}),
    )
    _moved(
        store,
        "task-legacy",
        frm=TaskStatus.QUEUED,
        to=TaskStatus.FAILED,
        updated_at=now - timedelta(hours=1),
    )
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))

    with media_task_scope("task-1", project_id=PROJECT_ID):
        task_work_root("task-1")

    summary = release_orphaned_scratch(
        _services(tmp_path),
        now=now + ORPHAN_RELEASE_GRACE + timedelta(minutes=5),
    )

    assert summary.removed_files == 0
    assert len(_media(legacy)) == 2


def test_legacy_scratch_respects_the_baseline(
    tmp_path: Path,
) -> None:
    legacy, fresh, old_tx, new_tx = _scratch_on_both_sides_of_baseline(
        tmp_path
    )

    entry = project_storage_profile(
        _services(tmp_path),
        now=T0 + timedelta(hours=3),
    )["projects"][0]

    assert entry["baselineAt"] == T0.isoformat()
    assert entry["taskWorkLegacyBytes"] == _tree_bytes(legacy)
    assert entry["taskWorkBytes"] == _tree_bytes(fresh)
    # A finished Task whose scratch outlived the grace window is reclaimable,
    # counted as the media a release would take rather than the kept docs.
    assert entry["taskWorkReclaimableBytes"] == _media_bytes(fresh)
    assert entry["taskWorkReclaimableBytes"] < _tree_bytes(fresh)
    assert entry["transactionsLegacyBytes"] == _tree_bytes(old_tx)
    assert entry["transactionsBytes"] == _tree_bytes(new_tx)
    assert project_storage_profile(_services(tmp_path))["totals"][
        "taskWorkReclaimableBytes"
    ] == _media_bytes(fresh)
