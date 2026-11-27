# -*- coding: utf-8 -*-
"""Transaction retention, cleanup, and journal indexing."""

# pylint: disable=protected-access

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.media_files.task_scratch import (
    confirm_project_reclaim,
    keep_project_history,
    remember_scratch_baseline,
)
from services.project_files import transaction_gc
from services.project_files.commit import ProjectCommitBoundary
from services.project_files.transaction_gc import (
    collect_project_transactions,
    collect_terminal_transactions,
    request_project_sweep,
    transaction_gc_enabled,
)
from services.project_files.transaction_index import (
    INDEX_FILE_NAME,
    TransactionJournalIndex,
    drop_transaction_index,
    plan_transaction_scan,
    record_transaction_scan,
)
from services.runtime_files.atomic_store import AtomicJsonRecordStore

pytestmark = pytest.mark.unit

BASELINE = datetime(2026, 1, 10, tzinfo=timezone.utc)


def _runtime(tmp_path) -> Path:
    root = tmp_path / "project-1"
    (root / "runtime" / "transactions").mkdir(parents=True)
    return root


def _transaction(
    project_root: Path,
    name: str,
    state: str | None,
    *,
    age_days: int,
    body_bytes: int = 128,
) -> Path:
    directory = project_root / "runtime" / "transactions" / name
    directory.mkdir(parents=True)
    if state is not None:
        (directory / "journal.json").write_text(
            json.dumps({"state": state}),
            encoding="utf-8",
        )
    elif name == "corrupt":
        (directory / "journal.json").write_text("{broken", encoding="utf-8")
    if body_bytes:
        (directory / "final.json").write_bytes(b"x" * body_bytes)
    moment = (BASELINE + timedelta(days=age_days)).timestamp()
    os.utime(directory, (moment, moment))
    return directory


def _managed(tmp_path) -> Path:
    project_root = _runtime(tmp_path)
    remember_scratch_baseline(project_root, now=BASELINE)
    return project_root


def test_only_the_newest_terminal_transactions_survive(tmp_path):
    project_root = _managed(tmp_path)
    newest = _transaction(
        project_root,
        "txn-new",
        "RUNTIME_FINALIZED",
        age_days=3,
    )
    middle = _transaction(project_root, "txn-mid", "ABORTED", age_days=2)
    oldest = _transaction(
        project_root,
        "txn-old",
        "RUNTIME_FINALIZED",
        age_days=1,
    )

    removed, removed_bytes = collect_project_transactions(
        project_root,
        retention=2,
    )

    # The whole directory is reclaimed, journal and snapshots alike.
    assert removed == 1
    assert removed_bytes >= 128
    assert newest.is_dir() and middle.is_dir()
    assert not oldest.exists()


def test_transactions_without_a_terminal_state_are_kept(tmp_path):
    project_root = _managed(tmp_path)
    survivors = [
        _transaction(project_root, name, state, age_days=1)
        for name, state in (
            ("prepared", "PREPARED"),
            ("replaced", "PROJECT_REPLACED"),
            ("no-journal", None),
            ("corrupt", None),
            ("future", "SOMETHING_NEW"),
        )
    ]
    settled = _transaction(
        project_root, "settled", "RUNTIME_FINALIZED", age_days=1
    )

    assert collect_project_transactions(project_root, retention=0)[0] == 1
    assert all(path.is_dir() for path in survivors)
    assert not settled.exists()


@pytest.mark.parametrize(
    "policy", ["default", "project", "install", "no-baseline"]
)
def test_reclaim_of_pre_baseline_history(tmp_path, monkeypatch, policy):
    project_root = (
        _runtime(tmp_path) if policy == "no-baseline" else _managed(tmp_path)
    )
    oldest = _transaction(project_root, "oldest", "ABORTED", age_days=-9)
    recent = _transaction(project_root, "recent", "ABORTED", age_days=-5)
    unsettled = _transaction(project_root, "open", "PREPARED", age_days=-5)
    if policy == "project":
        keep_project_history(project_root, now=BASELINE)
    elif policy == "install":
        monkeypatch.setenv("CREATOR_STORAGE_KEEP_HISTORY", "1")

    reclaimed = policy in {"default", "no-baseline"}
    assert collect_project_transactions(project_root, retention=1)[0] == int(
        reclaimed
    )
    assert oldest.exists() is not reclaimed
    assert recent.is_dir() and unsettled.is_dir()


@pytest.mark.parametrize("retention", [1, 50])
def test_transaction_index_is_invalidated_only_after_a_deletion(
    tmp_path, retention
):
    project_root = _managed(tmp_path)
    root = project_root / "runtime" / "transactions"
    _transaction(project_root, "txn-1", "RUNTIME_FINALIZED", age_days=1)
    _transaction(project_root, "txn-2", "RUNTIME_FINALIZED", age_days=2)
    record_transaction_scan(
        root,
        plan_transaction_scan(root, current_transaction_id="txn-2"),
        {"txn-1": "RUNTIME_FINALIZED"},
    )

    removed, removed_bytes = collect_project_transactions(
        project_root, retention=retention
    )
    assert removed == int(retention == 1)
    index = root / INDEX_FILE_NAME
    if removed:
        assert not index.exists()
    else:
        assert removed_bytes == 0
        assert _read_index(root).pending == ("txn-2",)


def test_switch_off_keeps_every_directory(tmp_path, monkeypatch):
    project_root = _managed(tmp_path)
    directory = _transaction(
        project_root,
        "txn-1",
        "RUNTIME_FINALIZED",
        age_days=1,
    )
    monkeypatch.setenv("CREATOR_TRANSACTION_GC_ENABLED", "0")
    assert not transaction_gc_enabled()

    assert collect_project_transactions(project_root, retention=0) == (0, 0)
    assert directory.is_dir()


def _services(root, *projects, broken=()):
    def project_root(project_id):
        if project_id in broken:
            raise RuntimeError("unreadable store")
        return root / project_id

    return SimpleNamespace(
        projects=SimpleNamespace(
            discover_project_ids=lambda: projects,
            project_root=project_root,
        )
    )


def test_sweep_covers_every_project_and_never_raises(tmp_path):
    first = tmp_path / "project-1" / "runtime" / "transactions"
    first.mkdir(parents=True)
    remember_scratch_baseline(tmp_path / "project-1", now=BASELINE)
    _transaction(tmp_path / "project-1", "p1-old", "ABORTED", age_days=1)
    _transaction(tmp_path / "project-1", "p1-new", "ABORTED", age_days=2)
    second = tmp_path / "project-2" / "runtime" / "transactions"
    second.mkdir(parents=True)
    # No baseline for project-2: its history is not provably this build's.
    _transaction(tmp_path / "project-2", "p2-old", "ABORTED", age_days=1)

    summary = collect_terminal_transactions(
        _services(
            tmp_path,
            "project-1",
            "project-2",
            "project-broken",
            broken=("project-broken",),
        ),
        retention=1,
    )

    assert summary.projects == 3
    assert summary.swept_projects == 1
    assert summary.removed_transactions == 1
    assert not (first / "p1-old").exists()
    assert (first / "p1-new").is_dir()
    assert (second / "p2-old").is_dir()


def _wait_for_idle(timeout: float = 5.0) -> None:
    """The sweep runs on its own thread; tests need its outcome, not timing."""

    deadline = time.monotonic() + timeout
    while transaction_gc._sweeps_in_flight:
        if time.monotonic() > deadline:
            pytest.fail("background transaction sweep never settled")
        time.sleep(0.01)


def test_a_growing_project_sweeps_itself_without_waiting_for_a_restart(
    tmp_path,
) -> None:
    project_root = _managed(tmp_path)
    oldest = _transaction(project_root, "txn-old", "ABORTED", age_days=1)
    middle = _transaction(project_root, "txn-mid", "ABORTED", age_days=2)
    newest = _transaction(project_root, "txn-new", "ABORTED", age_days=3)

    started = request_project_sweep(
        project_root,
        directory_count=3,
        threshold=2,
        retention=2,
    )
    _wait_for_idle()

    assert started is True
    assert not oldest.exists()
    assert middle.is_dir() and newest.is_dir()


def test_a_project_below_the_threshold_is_not_swept(tmp_path) -> None:
    project_root = _managed(tmp_path)
    kept = _transaction(project_root, "txn-old", "ABORTED", age_days=1)

    started = request_project_sweep(
        project_root,
        directory_count=1,
        threshold=2,
        retention=0,
    )

    assert started is False
    assert kept.is_dir()


def test_history_this_build_will_not_touch_is_not_rescanned_every_commit(
    tmp_path,
) -> None:
    project_root = _managed(tmp_path)
    # Older than the reclaim baseline: the sweep legitimately refuses it.
    ancient = (
        project_root / "runtime" / "transactions" / "arrived-with-project"
    )
    ancient.mkdir(parents=True)
    (ancient / "journal.json").write_text(
        json.dumps({"state": "ABORTED"}),
        encoding="utf-8",
    )
    moment = (BASELINE - timedelta(days=5)).timestamp()
    os.utime(ancient, (moment, moment))

    first = request_project_sweep(
        project_root,
        directory_count=3,
        threshold=2,
        retention=1,
    )
    _wait_for_idle()
    again = request_project_sweep(
        project_root,
        directory_count=4,
        threshold=2,
        retention=1,
    )
    further = request_project_sweep(
        project_root,
        directory_count=6,
        threshold=2,
        retention=1,
    )
    _wait_for_idle()

    assert first is True
    # One commit past the mark is not worth another full scan of the same tree.
    assert again is False
    assert further is True
    assert ancient.is_dir()


def test_a_disabled_gc_starts_no_sweep(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CREATOR_TRANSACTION_GC_ENABLED", "0")
    project_root = _managed(tmp_path)
    kept = _transaction(project_root, "txn-old", "ABORTED", age_days=1)

    assert (
        request_project_sweep(project_root, directory_count=9, threshold=2)
        is False
    )
    assert kept.is_dir()


def test_a_failing_sweep_never_reaches_the_committer(
    tmp_path,
    monkeypatch,
) -> None:
    project_root = _managed(tmp_path)
    _transaction(project_root, "txn-old", "ABORTED", age_days=1)
    attempts: list[int] = []

    def explode(*_args, **_kwargs):
        attempts.append(1)
        raise RuntimeError("store is unreadable")

    monkeypatch.setattr(
        transaction_gc,
        "collect_project_transactions",
        explode,
    )

    assert (
        request_project_sweep(project_root, directory_count=3, threshold=2)
        is True
    )
    _wait_for_idle()
    # The failure is swallowed on the worker thread and still released its
    # slot: a commit near the same count is not retried, one a threshold later
    # is.
    assert (
        request_project_sweep(project_root, directory_count=4, threshold=2)
        is False
    )
    assert (
        request_project_sweep(project_root, directory_count=6, threshold=2)
        is True
    )
    _wait_for_idle()
    assert len(attempts) == 2


def test_one_pass_stops_at_its_byte_budget_and_says_so(tmp_path):
    """A boot must not wait on a tree bigger than itself.

    The field Project held 8.4 GB of settled transactions; unlinking all of
    them is minutes of work, and a startup hook that waits those minutes looks
    exactly like a hang to the person who just upgraded.
    """

    from services.media_files.task_scratch import SweepBudget

    project_root = _managed(tmp_path)
    oldest = _transaction(
        project_root,
        "txn-oldest",
        "ABORTED",
        age_days=-9,
        body_bytes=4096,
    )
    recent = _transaction(
        project_root,
        "txn-recent",
        "ABORTED",
        age_days=-5,
        body_bytes=4096,
    )
    # The budget is one whole transaction, journal included -- what the sweep
    # measures is the directory, not the payload it was seeded with.
    one = sum(f.stat().st_size for f in recent.rglob("*") if f.is_file())
    budget = SweepBudget(max_bytes=one, deadline=time.monotonic() + 60)

    count, size = collect_project_transactions(
        project_root,
        retention=0,
        budget=budget,
    )

    # One fits, the second is refused -- and the pass admits it stopped short.
    assert count == 1
    assert size == one
    assert budget.stopped_early
    assert recent.is_dir() != oldest.is_dir()


def test_an_expired_pass_deletes_nothing(tmp_path):
    """The clock bound is checked before the first directory, not after."""

    from services.media_files.task_scratch import SweepBudget

    project_root = _managed(tmp_path)
    kept = _transaction(project_root, "txn-1", "ABORTED", age_days=-5)
    budget = SweepBudget(deadline=time.monotonic() - 1)

    outcome = collect_project_transactions(
        project_root,
        retention=0,
        budget=budget,
    )

    assert outcome == (0, 0)
    assert kept.is_dir()
    assert budget.stopped_early


@pytest.mark.parametrize("confirmation", ["project", "on-demand"])
def test_automatic_reclaim_cap_requires_confirmation(
    tmp_path, monkeypatch, confirmation
):
    monkeypatch.setenv("CREATOR_STORAGE_AUTO_RECLAIM_MAX_BYTES", "1000")
    project_root = _managed(tmp_path)
    newest = _transaction(project_root, "new", "ABORTED", age_days=-4)
    oldest = _transaction(
        project_root, "old", "ABORTED", age_days=-5, body_bytes=4096
    )
    report = {}

    assert collect_project_transactions(
        project_root, retention=1, report=report
    ) == (0, 0)
    assert report["skipped_bytes"] >= 4096
    assert collect_project_transactions(project_root, retention=1) == (0, 0)
    assert oldest.is_dir()

    if confirmation == "project":
        confirm_project_reclaim(project_root, now=BASELINE)
        assert collect_project_transactions(project_root, retention=1)[0] == 1
    else:
        summary = collect_terminal_transactions(
            _services(tmp_path, project_root.name),
            retention=1,
            enforce_cap=False,
        )
        assert summary.removed_transactions == 1
        assert summary.skipped_projects == 0
    assert not oldest.exists()
    assert newest.is_dir()


def test_profile_and_sweep_agree_when_listing_order_is_not_mtime_order(
    tmp_path,
    monkeypatch,
) -> None:
    """One sorted surplus, computed once, answers both questions.

    Review #243: the profile truncated the raw directory listing while the
    collector kept the newest entries by mtime. With the oldest transaction
    also the largest, the home page reported a pending size under the cap and
    offered no way to confirm it, while the sweep stopped on a surplus above
    that cap.
    """

    project_root = _managed(tmp_path)
    newest = _transaction(
        project_root,
        "txn-newest-small",
        "RUNTIME_FINALIZED",
        age_days=30,
    )
    oldest_big = _transaction(
        project_root,
        "txn-oldest-large",
        "RUNTIME_FINALIZED",
        age_days=1,
        body_bytes=8_000,
    )
    # Directory order that disagrees with mtime order, which is what made the
    # two paths report different numbers.
    monkeypatch.setattr(
        transaction_gc,
        "_eligible_candidates",
        lambda root, baseline, kept, budget=None: [
            (transaction_gc._mtime(oldest_big), oldest_big, 8_000),
            (transaction_gc._mtime(newest), newest, 128),
        ],
    )
    monkeypatch.setenv(
        "CREATOR_STORAGE_AUTO_RECLAIM_MAX_BYTES",
        "1000",
    )

    reported: dict = {}
    collect_project_transactions(
        project_root,
        retention=1,
        report=reported,
    )

    assert reported["skipped_bytes"] == 8_000
    assert (
        transaction_gc.surplus_transaction_bytes(
            project_root,
            retention=1,
        )
        == reported["skipped_bytes"]
    )


def test_an_unsettled_reservation_keeps_its_transaction(
    tmp_path,
    monkeypatch,
) -> None:
    """GC must not delete the witness a retrying PATCH still needs.

    Review #243: the Project published, then the idempotency completion
    failed, leaving the record IN_PROGRESS. Sweeping its transaction turned
    the retry into a 409 and wrote FAILED over work that had really completed.
    """

    project_root = _managed(tmp_path)
    protected = _transaction(
        project_root,
        "txn-open",
        "RUNTIME_FINALIZED",
        age_days=1,
    )
    settled = _transaction(
        project_root,
        "txn-plain",
        "RUNTIME_FINALIZED",
        age_days=2,
    )
    monkeypatch.setattr(
        transaction_gc,
        "_open_reservation_ids",
        lambda _root, budget=None: frozenset({"txn-open"}),
    )

    removed, _removed_bytes = collect_project_transactions(
        project_root,
        retention=0,
    )

    assert removed == 1
    assert protected.is_dir()
    assert not settled.exists()


def test_gc_reclaims_through_an_atomic_rename_not_a_partial_rmtree(
    tmp_path, monkeypatch
):
    project_root = _managed(tmp_path)
    doomed = _transaction(
        project_root, "doomed", "RUNTIME_FINALIZED", age_days=2
    )
    keep = _transaction(project_root, "keep", "RUNTIME_FINALIZED", age_days=3)
    for directory in (doomed, keep):
        path = directory / "journal.json"
        moment = BASELINE.isoformat()
        journal = json.loads(path.read_text())
        journal.update(
            transaction_id=directory.name,
            project_id=project_root.name,
            base_etag="sha256:base",
            candidate_etag="sha256:candidate",
            temp_path=f"runtime/transactions/{directory.name}/tmp.json",
            touched_pointers=[],
            created_at=moment,
            updated_at=moment,
        )
        path.write_text(json.dumps(journal))

    real_rename = transaction_gc.os.rename
    tombstones = []

    def scan_after_rename(src, dst):
        real_rename(src, dst)
        if src == doomed:
            tombstones.append(Path(dst))
            assert not doomed.exists()
            assert (Path(dst) / "journal.json").is_file()
            ProjectCommitBoundary._assert_no_pending_publication(
                runtime_root=project_root / "runtime",
                current_transaction_id="txn-live",
                authority_etag="sha256:whatever",
            )

    monkeypatch.setattr(transaction_gc.os, "rename", scan_after_rename)
    assert collect_project_transactions(project_root, retention=1)[0] == 1
    (tombstone,) = tombstones
    assert not tombstone.exists()
    assert keep.is_dir()


def test_a_partially_deleted_tombstone_is_reclaimed_by_the_next_sweep(
    tmp_path, monkeypatch
):
    project_root = _managed(tmp_path)
    doomed = _transaction(
        project_root,
        "doomed",
        "RUNTIME_FINALIZED",
        age_days=2,
        body_bytes=4096,
    )
    real_rmtree = transaction_gc.shutil.rmtree

    def partial_rmtree(path, *args, **kwargs):
        if path.name.startswith(transaction_gc.TOMBSTONE_PREFIX):
            (path / "journal.json").unlink()
        else:
            real_rmtree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(transaction_gc.shutil, "rmtree", partial_rmtree)
        # Renamed successfully; the bytes have yet to be freed.
        assert collect_project_transactions(project_root, retention=0) == (
            1,
            0,
        )

    assert not doomed.exists()
    (tombstone,) = doomed.parent.glob(f"{transaction_gc.TOMBSTONE_PREFIX}*")
    assert not (tombstone / "journal.json").exists()
    assert (tombstone / "final.json").stat().st_size == 4096
    removed, freed = collect_project_transactions(project_root, retention=0)
    assert removed >= 1 and freed >= 4096
    assert not tombstone.exists()


def test_cancelled_candidate_scan_cannot_authorize_partial_retention(
    tmp_path,
    monkeypatch,
):
    from services.media_files.task_scratch import SweepBudget

    root = _managed(tmp_path)
    for index in range(55):
        _transaction(root, f"txn-{index}", "RUNTIME_FINALIZED", age_days=index)
    budget = SweepBudget()
    scanned = 0
    real_size = transaction_gc._directory_bytes

    def cancel_during_scan(entry, current_budget):
        nonlocal scanned
        scanned += 1
        if scanned == 52:
            budget.stop_requested.set()
        return real_size(entry, current_budget)

    with monkeypatch.context() as patch:
        patch.setattr(transaction_gc, "_directory_bytes", cancel_during_scan)
        removed = collect_project_transactions(
            root,
            budget=budget,
            enforce_cap=False,
        )
    assert removed == (0, 0)
    assert scanned == 52
    assert budget.stopped_early
    assert len(list((root / "runtime" / "transactions").glob("txn-*"))) == 55
    assert collect_project_transactions(root, enforce_cap=False)[0] == 5


def test_cancelled_tombstone_scan_keeps_remaining_bytes(tmp_path, monkeypatch):
    from services.media_files.task_scratch import SweepBudget

    root = _managed(tmp_path)
    tombstone = root / "runtime" / "transactions" / ".deleted-prior"
    tombstone.mkdir()
    (tombstone / "final.json").write_bytes(b"remaining")
    budget = SweepBudget()
    real_size = transaction_gc._directory_bytes

    def cancel_after_size(entry, current_budget):
        size = real_size(entry, current_budget)
        budget.stop_requested.set()
        return size

    with monkeypatch.context() as patch:
        patch.setattr(transaction_gc, "_directory_bytes", cancel_after_size)
        assert collect_project_transactions(root, budget=budget) == (0, 0)
    assert (tombstone / "final.json").read_bytes() == b"remaining"
    assert collect_project_transactions(root) == (1, len(b"remaining"))


def _read_index(transactions_root: Path) -> TransactionJournalIndex:
    return AtomicJsonRecordStore(
        transactions_root / INDEX_FILE_NAME,
        TransactionJournalIndex,
    ).read()


def _transactions_root(tmp_path) -> Path:
    root = tmp_path / "transactions"
    root.mkdir()
    return root


def test_index_records_the_transaction_in_flight_as_pending(tmp_path):
    # A commit that dies after writing PREPARED leaves the directory the next
    # commit has to find, so the in-flight id must be carried forward rather
    # than assumed settled.
    transactions_root = _transactions_root(tmp_path)
    plan = plan_transaction_scan(
        transactions_root,
        current_transaction_id="txn-9",
    )
    assert not plan.read_ids
    record_transaction_scan(transactions_root, plan, {})

    stored = _read_index(transactions_root)
    assert stored.pending == ("txn-9",)
    assert stored.directory_count == 1

    (transactions_root / "txn-9").mkdir()
    next_plan = plan_transaction_scan(
        transactions_root,
        current_transaction_id="txn-10",
    )
    assert next_plan.cached
    assert next_plan.read_ids == ("txn-9",)


def test_terminal_states_learned_by_a_scan_are_not_read_again(tmp_path):
    transactions_root = _transactions_root(tmp_path)
    for name in ("txn-1", "txn-2", "txn-3", "txn-4"):
        (transactions_root / name).mkdir()
    plan = plan_transaction_scan(
        transactions_root,
        current_transaction_id="txn-4",
    )
    assert not plan.cached
    assert plan.read_ids == ("txn-1", "txn-2", "txn-3")

    record_transaction_scan(
        transactions_root,
        plan,
        {
            "txn-1": "RUNTIME_FINALIZED",
            "txn-2": "ABORTED",
            "txn-3": "PROJECT_REPLACED",
        },
    )
    assert _read_index(transactions_root).pending == ("txn-3", "txn-4")

    # The next commit reads only what was open; settled ones stay shut.
    (transactions_root / "txn-5").mkdir()
    cached = plan_transaction_scan(
        transactions_root,
        current_transaction_id="txn-5",
    )
    assert cached.cached
    assert cached.read_ids == ("txn-3", "txn-4")


def test_an_unexpected_directory_falls_back_to_the_full_scan(tmp_path):
    # Directory count is the integrity check: a transaction appearing without a
    # commit in between (recovery, a crash, tooling) must cost a scan, never
    # hide a pending publication.
    transactions_root = _transactions_root(tmp_path)
    (transactions_root / "txn-1").mkdir()
    record_transaction_scan(
        transactions_root,
        plan_transaction_scan(
            transactions_root,
            current_transaction_id="txn-1",
        ),
        {"txn-1": "RUNTIME_FINALIZED"},
    )

    (transactions_root / "txn-stray").mkdir()
    plan = plan_transaction_scan(
        transactions_root,
        current_transaction_id="txn-2",
    )
    assert not plan.cached
    assert plan.read_ids == ("txn-1", "txn-stray")


def test_a_corrupt_index_falls_back_and_is_rebuilt(tmp_path):
    transactions_root = _transactions_root(tmp_path)
    (transactions_root / "txn-1").mkdir()
    (transactions_root / INDEX_FILE_NAME).write_text(
        "{broken",
        encoding="utf-8",
    )

    plan = plan_transaction_scan(
        transactions_root,
        current_transaction_id="txn-2",
    )
    assert not plan.cached
    record_transaction_scan(transactions_root, plan, {"txn-1": "ABORTED"})
    assert _read_index(transactions_root).pending == ("txn-2",)


def test_dropping_the_index_forces_the_next_full_scan(tmp_path):
    transactions_root = _transactions_root(tmp_path)
    for name in ("txn-1", "txn-2"):
        (transactions_root / name).mkdir()
    record_transaction_scan(
        transactions_root,
        plan_transaction_scan(
            transactions_root,
            current_transaction_id="txn-2",
        ),
        {"txn-1": "RUNTIME_FINALIZED"},
    )
    (transactions_root / "txn-3").mkdir()
    assert plan_transaction_scan(
        transactions_root,
        current_transaction_id="txn-3",
    ).cached

    # Garbage collection settles this by hand: removed directories would
    # otherwise look like the transaction a commit is expected to add.
    drop_transaction_index(transactions_root)
    plan = plan_transaction_scan(
        transactions_root,
        current_transaction_id="txn-4",
    )
    assert not plan.cached
    assert plan.read_ids == ("txn-1", "txn-2", "txn-3")
