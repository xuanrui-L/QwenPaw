# -*- coding: utf-8 -*-
# Pytest fixtures and contract probes retain exact types and private seams.
# pylint: disable=protected-access
"""Automatic history stays durable without becoming a user decision."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from services.project_files.auto_snapshot import auto_snapshot_timelines
from services.project_files.commit import (
    ProjectCommitBoundary,
    _runtime_change,
)
from services.project_files.json_pointer import diff_json
from services.project_files.review import ProjectReviewService
from services.project_files.review_bookkeeping import is_version_bookkeeping
from services.project_files.store import ProjectStore
from services.runtime_files.atomic_store import AtomicJsonRecordStore
from services.runtime_files.models import (
    ReviewOperation,
    ReviewOperationDecision,
    ReviewRecord,
    ReviewStatus,
)

from .conftest import (
    read_changeset,
    read_state,
    review_boundary,
    review_commit_kwargs,
    r2v_element,
    timeline_project_with,
)

pytestmark = pytest.mark.unit
PID = "project-1"


def _commit(tmp_path):
    store = ProjectStore(tmp_path.resolve())
    base = store.create(
        timeline_project_with(r2v_element("shot-one", start=0)),
    )
    before = base.project.model_dump(mode="json")
    after = deepcopy(before)
    after["timelines"]["items"]["timeline:main"]["elements_by_id"]["shot-one"][
        "creation"
    ]["video_prompt"] += "，最后停住"
    auto_snapshot_timelines(before, after)
    result = ProjectCommitBoundary(store).commit(
        base=base,
        candidate=after,
        round_id="round-with-history",
        **review_commit_kwargs(review_boundary(base)),
    )
    return store, base, result


def _legacy(store, base, result, *, content_accepted):
    # Reconstruct the records emitted before bookkeeping was exempted.
    changes = [
        _runtime_change(change)
        for change in diff_json(
            base.project.model_dump(mode="json"),
            result.snapshot.project.model_dump(mode="json"),
        )
    ]
    history = [
        ReviewOperation(
            **change.model_dump(mode="python"),
            operation_id=f"history-{index}",
        )
        for index, change in enumerate(changes)
        if is_version_bookkeeping(change)
    ]
    assert len(history) == 2
    operations = [
        operation.model_copy(
            update={
                "decision": ReviewOperationDecision.ACCEPTED
                if content_accepted
                else operation.decision,
            },
        )
        for operation in result.review.operations
    ]
    record = result.review.model_copy(
        update={"operations": [*operations, *history]},
    )
    path = (
        store.project_root(PID)
        / "runtime"
        / "reviews"
        / record.review_id
        / "review.json"
    )
    AtomicJsonRecordStore(path, ReviewRecord).write(record)
    return record, path


def test_new_commit_keeps_history_in_audit_but_not_in_human_review(tmp_path):
    store, _, result = _commit(tmp_path)
    assert any(
        key.startswith("snapshot:")
        for key in result.snapshot.project.timelines.items
    )
    assert result.review is not None
    assert all(
        not is_version_bookkeeping(op) for op in result.review.operations
    )
    assert any(
        "/video_prompt" in (op.json_pointer or "")
        for op in result.review.operations
    )
    assert any(
        is_version_bookkeeping(change)
        for change in read_changeset(store, result.round.round_id).changes
    )


@pytest.mark.parametrize("content_accepted", [False, True])
def test_legacy_history_retires_without_approval_or_project_rewrite(
    tmp_path,
    content_accepted,
):
    store, base, result = _commit(tmp_path)
    record, _ = _legacy(store, base, result, content_accepted=content_accepted)
    project_path = store.project_root(PID) / "project.json"
    before = project_path.read_bytes()
    accepted = read_state(store).accepted_generation
    service = ProjectReviewService(store)
    service.recover_project(PID)
    pending = service.all_pending(PID)
    updated = service.get(PID, record.review_id)
    assert all(
        op.decision is ReviewOperationDecision.ACCEPTED
        for op in updated.operations
        if is_version_bookkeeping(op)
    )
    assert bool(pending) is not content_accepted
    if not content_accepted:
        assert any(
            op.decision is ReviewOperationDecision.PENDING
            for op in updated.operations
        )
        assert read_state(store).accepted_generation == accepted
    else:
        assert updated.status is ReviewStatus.RESOLVED
        assert read_state(store).active_round_id is None
    assert project_path.read_bytes() == before
    first = updated.model_dump_json()
    service.all_pending(PID)
    assert service.get(PID, record.review_id).model_dump_json() == first


def test_legacy_history_does_not_block_if_history_list_has_since_changed(
    tmp_path,
):
    store, base, result = _commit(tmp_path)
    record, path = _legacy(store, base, result, content_accepted=True)
    # A stale history record must not try to restore its old ordering.
    operations = [
        op.model_copy(update={"after_hash": "obsolete-history-value"})
        if op.json_pointer == "/timelines/order"
        else op
        for op in record.operations
    ]
    AtomicJsonRecordStore(path, ReviewRecord).write(
        record.model_copy(update={"operations": operations}),
    )
    before = (store.project_root(PID) / "project.json").read_bytes()
    assert ProjectReviewService(store).all_pending(PID) == []
    assert (store.project_root(PID) / "project.json").read_bytes() == before


def test_system_decision_recovers_after_review_write_failure(
    tmp_path,
    monkeypatch,
):
    store, base, result = _commit(tmp_path)
    record, _ = _legacy(store, base, result, content_accepted=True)
    service = ProjectReviewService(store)
    original = service._write_review

    def fail(*args, **kwargs):
        raise OSError("controlled review persistence failure")

    monkeypatch.setattr(service, "_write_review", fail)
    with pytest.raises(OSError, match="controlled"):
        service.retire_internal_operations(PID)
    monkeypatch.setattr(service, "_write_review", original)
    service.recover_project(PID)
    assert service.all_pending(PID) == []
    events = (
        (
            store.project_root(PID)
            / "runtime"
            / "reviews"
            / record.review_id
            / "decisions.jsonl"
        )
        .read_text()
        .splitlines()
    )
    assert len(events) == 1
    assert "system-version-bookkeeping" in events[0]


@pytest.mark.parametrize(
    "before,after",
    [
        (["timeline:one", "timeline:two"], ["timeline:two", "timeline:one"]),
        (["timeline:one"], ["timeline:one", "timeline:two"]),
        (["timeline:one"], None),
    ],
)
def test_live_order_changes_are_not_bookkeeping(before, after):
    assert not is_version_bookkeeping(
        SimpleNamespace(
            json_pointer="/timelines/order",
            kind="reorder",
            before=before,
            after=after,
        ),
    )


@pytest.mark.parametrize(
    "kind,description",
    [
        ("create", "用户保存的方案"),
        ("update", "自动快照：修改前的时间轴副本"),
        ("delete", "自动快照：修改前的时间轴副本"),
    ],
)
def test_snapshot_looking_content_is_not_implicitly_accepted(
    kind,
    description,
):
    tid = "snapshot:timeline:main:1"
    assert not is_version_bookkeeping(
        SimpleNamespace(
            json_pointer=f"/timelines/items/{tid}",
            kind=kind,
            before=None,
            after={"timeline_id": tid, "description": description},
        ),
    )


@pytest.mark.parametrize("content_accepted", [False, True])
def test_legacy_shot_review_retires_but_current_content_still_needs_decision(
    tmp_path,
    content_accepted,
):
    store, base, result = _commit(tmp_path)
    record, path = _legacy(
        store,
        base,
        result,
        content_accepted=content_accepted,
    )
    legacy = record.operations[0].model_copy(
        update={
            "operation_id": "retired-shots",
            "json_pointer": (
                "/timelines/items/timeline:main/elements_by_id/shot-one"
                "/creation/shots"
            ),
            "before": {"invalid": True},
            "after": {"invalid": False},
            "decision": ReviewOperationDecision.PENDING,
        },
    )
    record = record.model_copy(
        update={"operations": [*record.operations, legacy]},
    )
    AtomicJsonRecordStore(path, ReviewRecord).write(record)
    project_path = store.project_root(PID) / "project.json"
    before = project_path.read_bytes()
    service = ProjectReviewService(store)
    service.recover_project(PID)
    pending = service.all_pending(PID)
    assert bool(pending) is not content_accepted
    updated = service.get(PID, record.review_id)
    retired = next(
        op for op in updated.operations if op.operation_id == "retired-shots"
    )
    assert retired.decision is ReviewOperationDecision.ACCEPTED
    assert project_path.read_bytes() == before


@pytest.mark.parametrize("content_accepted", [False, True])
def test_pending_read_under_lifecycle_lock_never_writes(
    tmp_path,
    monkeypatch,
    content_accepted,
):
    store, base, result = _commit(tmp_path)
    _, path = _legacy(store, base, result, content_accepted=content_accepted)
    before = path.read_bytes()
    service = ProjectReviewService(store)

    def unexpected(*args, **kwargs):
        raise AssertionError("A read must not settle review records")

    monkeypatch.setattr(service, "_settle_internal_operations", unexpected)
    with store.lifecycle_lock(PID):
        pending = service.all_pending(PID)
    assert bool(pending) is not content_accepted
    assert path.read_bytes() == before
