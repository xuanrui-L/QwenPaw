# -*- coding: utf-8 -*-
"""The startup transaction sweep.

Deletion needs the strictest reading of its own rules: a transaction survives
unless its journal positively says it settled, and history the Project arrived
with is never this build's to reclaim.
"""

# pylint: disable=protected-access

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import time

import pytest

from services.media_files.task_scratch import (
    keep_project_history,
    remember_scratch_baseline,
)
from services.project_files import transaction_gc
from services.project_files.transaction_gc import (
    collect_project_transactions,
    collect_terminal_transactions,
    request_project_sweep,
    transaction_gc_enabled,
)
from services.project_files.transaction_index import (
    TransactionJournalIndex,
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


def test_transactions_that_did_not_settle_are_kept(tmp_path):
    project_root = _managed(tmp_path)
    survivors = [
        _transaction(project_root, "prepared", "PREPARED", age_days=1),
        _transaction(project_root, "replaced", "PROJECT_REPLACED", age_days=1),
        _transaction(project_root, "no-journal", None, age_days=1),
        _transaction(project_root, "corrupt", None, age_days=1),
        _transaction(project_root, "settled", "RUNTIME_FINALIZED", age_days=1),
    ]

    removed, _size = collect_project_transactions(project_root, retention=0)

    # retention=0 drops settled history, but only what the journal vouches for.
    assert removed == 1
    assert [path.exists() for path in survivors[:4]] == [True] * 4


def test_an_unknown_state_is_not_treated_as_terminal(tmp_path):
    project_root = _managed(tmp_path)
    future = _transaction(
        project_root,
        "txn-future",
        "SOMETHING_NEW",
        age_days=1,
    )

    assert collect_project_transactions(project_root, retention=0) == (0, 0)
    assert future.is_dir()


def test_pre_baseline_history_is_reclaimed_by_default(tmp_path):
    """A customer inside a pod cannot press anything, so the date gate is
    gone by default: a settled transaction is settled whatever day it was.
    """

    project_root = _managed(tmp_path)
    legacy = _transaction(project_root, "txn-legacy", "ABORTED", age_days=-5)

    count, size = collect_project_transactions(project_root, retention=0)

    assert count == 1
    assert size > 0
    assert not legacy.is_dir()


def test_keeping_history_restores_the_baseline_gate(tmp_path):
    """The #217 promise survives as an explicit choice, per Project."""

    project_root = _managed(tmp_path)
    legacy = _transaction(project_root, "txn-legacy", "ABORTED", age_days=-5)
    keep_project_history(project_root, now=BASELINE)

    assert collect_project_transactions(project_root, retention=0) == (0, 0)
    assert legacy.is_dir()


def test_the_install_switch_keeps_every_projects_history(
    tmp_path,
    monkeypatch,
):
    """One env var restores the fleet-wide behaviour without a code change."""

    monkeypatch.setenv("CREATOR_STORAGE_KEEP_HISTORY", "1")
    project_root = _managed(tmp_path)
    legacy = _transaction(project_root, "txn-legacy", "ABORTED", age_days=-5)

    assert collect_project_transactions(project_root, retention=0) == (0, 0)
    assert legacy.is_dir()


def test_a_project_without_a_baseline_is_swept_now(tmp_path):
    """No baseline used to mean "nothing is provably new"; with the date gate
    no longer protecting history there is nothing left to wait for.
    """

    project_root = _runtime(tmp_path)
    directory = _transaction(
        project_root,
        "txn-1",
        "RUNTIME_FINALIZED",
        age_days=1,
    )

    assert collect_project_transactions(project_root, retention=0) != (0, 0)
    assert not directory.is_dir()


def test_the_default_sweep_bends_no_other_rule(tmp_path):
    """Reclaiming by default changes the date gate and nothing else.

    Field note: one imported Project held 618 terminal transactions -- 8.4 GB
    -- every one older than its baseline, so the gated sweep freed exactly
    nothing.  Retention and the terminal-state test still have to hold.
    """

    project_root = _managed(tmp_path)
    oldest = _transaction(project_root, "txn-oldest", "ABORTED", age_days=-9)
    recent = _transaction(project_root, "txn-recent", "ABORTED", age_days=-5)
    unsettled = _transaction(project_root, "txn-open", "PREPARED", age_days=-5)

    count, size = collect_project_transactions(project_root, retention=1)

    assert count == 1
    assert size > 0
    assert not oldest.is_dir()  # the oldest beyond the retention goes first
    assert recent.is_dir()  # the newest receipt is still kept
    assert unsettled.is_dir()  # an unsettled transaction is never touched


def test_the_settled_listing_is_invalidated(tmp_path):
    project_root = _managed(tmp_path)
    transactions_root = project_root / "runtime" / "transactions"
    _transaction(project_root, "txn-1", "RUNTIME_FINALIZED", age_days=1)
    _transaction(project_root, "txn-2", "RUNTIME_FINALIZED", age_days=2)
    record_transaction_scan(
        transactions_root,
        plan_transaction_scan(
            transactions_root,
            current_transaction_id="txn-2",
        ),
        {"txn-1": "RUNTIME_FINALIZED"},
    )

    collect_project_transactions(project_root, retention=1)

    assert not (transactions_root / ".journal-index.json").exists()


def test_a_no_op_sweep_keeps_the_index(tmp_path):
    project_root = _managed(tmp_path)
    transactions_root = project_root / "runtime" / "transactions"
    _transaction(project_root, "txn-1", "RUNTIME_FINALIZED", age_days=1)
    record_transaction_scan(
        transactions_root,
        plan_transaction_scan(
            transactions_root,
            current_transaction_id="txn-1",
        ),
        {"txn-1": "RUNTIME_FINALIZED"},
    )

    assert collect_project_transactions(project_root, retention=50) == (0, 0)

    stored = AtomicJsonRecordStore(
        transactions_root / ".journal-index.json",
        TransactionJournalIndex,
    ).read()
    # Untouched: the sweep removed nothing, so the commit path's cache still
    # describes the directory it was built from.
    assert stored.pending == ("txn-1",)


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


class _Services:
    """The two Project-store methods the sweep needs."""

    def __init__(self, root: Path, broken: tuple[str, ...] = ()):
        self.root = root
        self.broken = broken

        projects = self

        class _Projects:
            @staticmethod
            def discover_project_ids():
                return ["project-1", "project-2", "project-broken"]

            @staticmethod
            def project_root(project_id):
                if project_id in projects.broken:
                    raise RuntimeError("unreadable store")
                return projects.root / project_id

        self.projects = _Projects


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
        _Services(tmp_path, broken=("project-broken",)),
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
