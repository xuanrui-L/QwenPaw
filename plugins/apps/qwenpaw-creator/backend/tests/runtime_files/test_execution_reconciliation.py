# -*- coding: utf-8 -*-
"""Recovery of queued executions and durability of idempotency claims."""

from __future__ import annotations
import json
import threading
import time
from pathlib import Path
import pytest
from domain.enums import SpecialistRole, SpecialistRunStatus, TaskKind
from services.runtime_files import idempotency_ledger
from services.runtime_files.execution_models import (
    SpecialistRunRecord,
    TaskRecord,
)
from services.runtime_files.execution_store import ProjectExecutionStore
from services.runtime_files.models import ReviewPolicy
from .test_execution_store import PROJECT_ID, _store

from services.runtime_files import IdempotencyRecordStore, IdempotencyStatus
from .test_runtime_primitives import KEY

pytestmark = pytest.mark.unit


def _bare_run(store: ProjectExecutionStore, suffix: str) -> str:
    run_id = f"run-orphan-{suffix}"
    store.create_run(
        SpecialistRunRecord(
            run_id=run_id,
            role=SpecialistRole.R2V_GENERATION_DIRECTOR,
            target_refs=[f"element:{suffix}"],
            project_id=PROJECT_ID,
            round_id=f"round-{suffix}",
            input_generation=1,
            input_etag="sha256:input",
            caused_by_request_id=f"request-{suffix}",
            review_policy=ReviewPolicy.AUTO_FIX,
        ),
    )
    return run_id


def test_a_queued_run_without_a_task_is_failed_at_startup(tmp_path: Path):
    """The admission window outlived its process; nothing can dispatch it.

    The field Project carried one such Run for seventeen days: the progress
    projection showed its storyboard as "waiting" forever, because no executor
    was alive to dispatch it and none was alive to terminalize it.
    """
    from datetime import timedelta

    from services.runtime_files.reconciliation import (
        reconcile_orphaned_queued_runs,
    )

    store = _store(tmp_path)
    orphan = _bare_run(store, "storyboard")

    run = store.get_run(PROJECT_ID, orphan)
    moment = run.updated_at + timedelta(minutes=6)
    report = reconcile_orphaned_queued_runs(tmp_path, [PROJECT_ID], now=moment)

    assert report.cancelled_run_ids == (orphan,)
    cancelled = store.get_run(PROJECT_ID, orphan)
    assert cancelled.status is SpecialistRunStatus.CANCELLED
    assert cancelled.final_marker == "CANCELLED"
    assert "ORPHANED_QUEUED_RUN" in (cancelled.final_summary_text or "")


def test_a_queued_run_with_a_task_is_left_alone(tmp_path: Path):
    from datetime import timedelta

    from services.runtime_files.reconciliation import (
        reconcile_orphaned_queued_runs,
    )

    store = _store(tmp_path)
    run_id = _bare_run(store, "claimed")
    store.create_task(
        TaskRecord(
            task_id="task-claimed",
            run_id=run_id,
            kind=TaskKind.IMAGE_GENERATION,
            request_fingerprint="sha256:claimed",
            project_id=PROJECT_ID,
            round_id="round-claimed",
            input_generation=1,
            input_etag="sha256:input",
            caused_by_request_id="request-claimed",
            review_policy=ReviewPolicy.AUTO_FIX,
        ),
    )

    run = store.get_run(PROJECT_ID, run_id)
    report = reconcile_orphaned_queued_runs(
        tmp_path,
        [PROJECT_ID],
        now=run.updated_at + timedelta(minutes=6),
    )

    assert report.cancelled_count == 0
    kept = store.get_run(PROJECT_ID, run_id)
    assert kept.status is SpecialistRunStatus.QUEUED


def test_a_queued_run_inside_the_grace_window_is_left_alone(tmp_path: Path):
    """A restart loop must not reap an admission whose Task write is slow."""
    from datetime import timedelta

    from services.runtime_files.reconciliation import (
        reconcile_orphaned_queued_runs,
    )

    store = _store(tmp_path)
    run_id = _bare_run(store, "fresh")

    run = store.get_run(PROJECT_ID, run_id)
    report = reconcile_orphaned_queued_runs(
        tmp_path,
        [PROJECT_ID],
        now=run.updated_at + timedelta(seconds=30),
    )

    assert report.cancelled_count == 0
    kept = store.get_run(PROJECT_ID, run_id)
    assert kept.status is SpecialistRunStatus.QUEUED


def _record(root: Path, name: str, record_id: str, status: str) -> Path:
    path = root / f"{name}.json"
    payload = {"record_id": record_id, "status": status}
    if status == "COMPLETED":
        payload["response"] = {"ok": True}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_ledger_tracks_open_reservations(tmp_path) -> None:
    root = tmp_path / "idempotency"

    assert idempotency_ledger.open_transaction_ids(root) == frozenset()

    idempotency_ledger.mark_open(root, "txn-1")
    idempotency_ledger.mark_open(root, "txn-2")
    assert idempotency_ledger.open_transaction_ids(root) == frozenset(
        {"txn-1", "txn-2"},
    )

    idempotency_ledger.mark_closed(root, "txn-1")
    assert idempotency_ledger.open_transaction_ids(root) == frozenset(
        {"txn-2"},
    )


def test_a_damaged_ledger_is_rebuilt_from_the_records(tmp_path) -> None:
    """An unreadable set is rebuilt, not trusted and not left empty.

    Trusting a partial set would delete a transaction that an IN_PROGRESS
    reservation still needs as the only durable witness for its response.
    """

    root = tmp_path / "idempotency"
    root.mkdir(parents=True)
    _record(root, "rec-open", "txn-open", "IN_PROGRESS")
    _record(root, "rec-done", "txn-done", "COMPLETED")
    ledger = root / idempotency_ledger.LEDGER_FILE_NAME
    ledger.write_text("{corrupt", encoding="utf-8")

    assert idempotency_ledger.open_transaction_ids(root) == frozenset(
        {"txn-open"},
    )
    # The rebuild is persisted, so the next sweep reads instead of scanning.
    assert "txn-open" in json.loads(ledger.read_text(encoding="utf-8"))["open"]


def test_a_failed_ledger_write_invalidates_the_file(
    tmp_path,
    monkeypatch,
) -> None:
    """A half-written ledger is worse than none, so it is dropped instead.

    The reservation the failed write would have recorded is lost from the set,
    which is exactly the dangerous direction -- so the file is invalidated and
    the next reader rebuilds from the records themselves.
    """

    root = tmp_path / "idempotency"
    root.mkdir(parents=True)
    _record(root, "rec", "txn-open", "IN_PROGRESS")

    def boom(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(idempotency_ledger.os, "replace", boom)
    idempotency_ledger.mark_open(root, "txn-new")  # must not raise
    monkeypatch.undo()

    assert idempotency_ledger.open_transaction_ids(root) == frozenset(
        {"txn-open"},
    )


def test_an_unreadable_record_is_still_treated_as_open(tmp_path) -> None:
    """Fail toward keeping: a damaged head cannot prove the request settled."""

    root = tmp_path / "idempotency"
    root.mkdir(parents=True)
    (root / "rec.json").write_bytes(
        b'{"record_id": "txn-mangled", "status": "IN_PROGRESS"',
    )

    assert idempotency_ledger.open_transaction_ids(root) == frozenset(
        {"rec"},
    )


def test_mark_open_serializes_the_shared_load_modify_save(
    tmp_path,
    monkeypatch,
) -> None:
    """Two reservations for different keys must both survive.

    Regression for review #243 P1: ``mark_open`` is called under each
    caller's per-key idempotency lock, so two concurrent writes for
    different keys do not overlap there -- but they do overlap on the
    single shared ledger file. Without a further lock, thread A loads the
    empty set, pauses, thread B loads the same empty set and publishes
    ``{B}``, then A's write clobbers B. A lost id means GC treats that
    transaction as unprotected and deletes the durable witness an
    IN_PROGRESS reservation still needs to replay its response.
    """

    root = tmp_path / "idempotency"
    root.mkdir(parents=True)

    inside_a = threading.Event()
    leave_a = threading.Event()
    real_read = idempotency_ledger._read_ledger

    def pausing_read(path_root: Path) -> dict[str, str] | None:
        result = real_read(path_root)
        # Only thread A's first read is delayed; when A resumes and
        # releases the lock, thread B enters and its read must see A's
        # already-published entry -- proof the shared update was actually
        # serialized rather than lucky scheduling.
        if not inside_a.is_set():
            inside_a.set()
            leave_a.wait(timeout=5)
        return result

    monkeypatch.setattr(idempotency_ledger, "_read_ledger", pausing_read)

    b_finished = threading.Event()

    def thread_a() -> None:
        idempotency_ledger.mark_open(root, "txn-A")

    def thread_b() -> None:
        inside_a.wait(timeout=5)
        idempotency_ledger.mark_open(root, "txn-B")
        b_finished.set()

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start()
    assert inside_a.wait(timeout=5)
    tb.start()
    # Give B a window to bypass the lock if it were unprotected. Without
    # the lock it would finish here and A's later write would drop its
    # entry; with the lock, B is blocked in acquire.
    time.sleep(0.2)
    assert not b_finished.is_set(), (
        "thread B completed while A was inside mark_open -- "
        "the shared load-modify-save is not serialized"
    )
    leave_a.set()
    ta.join(timeout=5)
    tb.join(timeout=5)

    assert idempotency_ledger.open_transaction_ids(root) == frozenset(
        {"txn-A", "txn-B"},
    )


def test_mark_closed_waits_for_the_in_flight_mark_open(
    tmp_path,
    monkeypatch,
) -> None:
    """A settle that raced past its own add still removes the entry.

    Without the shared lock, ``mark_closed`` would read an empty ledger
    (A had not written yet), silently return, and A's write would leave
    ``txn-A`` behind forever -- GC would then treat the settled
    transaction as an open witness and never reclaim it.
    """

    root = tmp_path / "idempotency"
    root.mkdir(parents=True)

    inside_a = threading.Event()
    leave_a = threading.Event()
    real_read = idempotency_ledger._read_ledger

    def pausing_read(path_root: Path) -> dict[str, str] | None:
        result = real_read(path_root)
        if not inside_a.is_set():
            inside_a.set()
            leave_a.wait(timeout=5)
        return result

    monkeypatch.setattr(idempotency_ledger, "_read_ledger", pausing_read)

    def thread_a() -> None:
        idempotency_ledger.mark_open(root, "txn-A")

    def thread_b() -> None:
        inside_a.wait(timeout=5)
        idempotency_ledger.mark_closed(root, "txn-A")

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start()
    assert inside_a.wait(timeout=5)
    tb.start()
    leave_a.set()
    ta.join(timeout=5)
    tb.join(timeout=5)

    # With the lock, B entered after A had written {txn-A}, found the
    # id there and popped it. The file remains as an empty ledger rather
    # than being recreated by a later scan.
    assert idempotency_ledger.open_transaction_ids(root) == frozenset()


def test_cancelled_rebuild_never_publishes_partial_reservations(tmp_path):
    root = tmp_path / "idempotency"
    root.mkdir()
    _record(root, "first", "txn-1", "IN_PROGRESS")
    _record(root, "second", "txn-2", "IN_PROGRESS")
    decisions = iter((True, False))
    with pytest.raises(idempotency_ledger.IncompleteLedgerScan):
        idempotency_ledger.open_transaction_ids(
            root, can_continue=lambda: next(decisions)
        )
    assert not (root / idempotency_ledger.LEDGER_FILE_NAME).exists()
    assert idempotency_ledger.open_transaction_ids(root) == {"txn-1", "txn-2"}


def test_a_reserve_retry_republishes_an_entry_the_first_attempt_never_wrote(
    tmp_path,
    monkeypatch,
) -> None:
    """Crash window between record write and mark_open must self-heal.

    Review #243 blocking #2: the original reserve called ``mark_open``
    only when ``created`` was true. A process that wrote the record as
    IN_PROGRESS and died before publishing the ledger entry left the
    retry seeing ``created=False`` while the ledger still missed the
    id, so GC would delete the finalized transaction the pending
    record still needed to replay its response. ``mark_open`` is now
    unconditional; this test pre-seeds the ledger so the rebuild-scan
    safety net is not the one closing the window, silences mark_open
    for the first attempt, and asserts the retry republishes the
    entry on the second attempt.
    """

    from services.runtime_files import idempotency_ledger

    root = tmp_path / "runtime" / "commands" / "idempotency"
    # Seed an unrelated reservation so the ledger file exists and the
    # scan-based fallback cannot mask the crash window we are trying
    # to reproduce: without the unconditional mark_open, the retry
    # would see a readable ledger missing this id and return the
    # seeded set unchanged.
    idempotency_ledger.mark_open(root, "txn-unrelated")

    request_hash = IdempotencyRecordStore.request_hash({"value": 1})
    real_mark_open = idempotency_ledger.mark_open
    suppress = [True]

    def flaky_mark_open(path_root, record_id):
        if suppress[0]:
            return
        return real_mark_open(path_root, record_id)

    monkeypatch.setattr(idempotency_ledger, "mark_open", flaky_mark_open)

    first = IdempotencyRecordStore(root).reserve(
        **KEY,
        request_hash=request_hash,
    )
    suppress[0] = False
    assert first.created is True
    # The crash window: the record is IN_PROGRESS on disk but the
    # ledger still only names the unrelated seed. Without the fix the
    # retry below would leave it that way and GC could delete the
    # witness.
    assert idempotency_ledger.open_transaction_ids(root) == frozenset(
        {"txn-unrelated"},
    )

    second = IdempotencyRecordStore(root).reserve(
        **KEY,
        request_hash=request_hash,
    )
    assert second.created is False
    # The retry must republish. Without the unconditional mark_open
    # this assertion fails and the transaction becomes GC-eligible
    # while the reservation is still IN_PROGRESS.
    assert idempotency_ledger.open_transaction_ids(root) == frozenset(
        {"txn-unrelated", second.record.record_id},
    )


def test_a_reserve_replay_of_a_settled_record_does_not_reopen_the_ledger(
    tmp_path,
) -> None:
    """Terminal replays must not resurrect a settled txn in the ledger.

    Follow-up on review #243: making mark_open unconditional to close
    the crash window also fired on reserve() calls that hit an already
    COMPLETED / FAILED record. project_file_routes replays the cached
    response for those terminal records without running complete()
    again, so nothing ever removes the id from the ledger afterwards
    and the storage-reclaim sweep this PR exists to make reliable
    silently keeps settled transactions forever. Only an IN_PROGRESS
    record needs the durable witness, and only IN_PROGRESS republishes
    the entry.
    """

    from services.runtime_files import idempotency_ledger

    root = tmp_path / "runtime" / "commands" / "idempotency"
    request_hash = IdempotencyRecordStore.request_hash({"value": 1})
    store = IdempotencyRecordStore(root)

    reserved = store.reserve(**KEY, request_hash=request_hash)
    store.complete(
        **KEY,
        request_hash=request_hash,
        response={"body": {"ok": True}},
        response_status=200,
    )
    # complete() settles the ledger; nothing else should re-add it.
    assert idempotency_ledger.open_transaction_ids(root) == frozenset()

    replay = store.reserve(**KEY, request_hash=request_hash)
    assert replay.created is False
    assert replay.record.status is IdempotencyStatus.COMPLETED
    # Without the status guard the ledger contains the id again and
    # GC sees the transaction as permanently protected.
    assert idempotency_ledger.open_transaction_ids(root) == frozenset()
    # Sanity: the record still refers to the same reservation, so a
    # future caller can still find its response.
    assert replay.record.record_id == reserved.record.record_id
