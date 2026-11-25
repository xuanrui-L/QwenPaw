# -*- coding: utf-8 -*-
"""On-demand history reclaim and its storage accounting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import pytest

from domain.enums import TaskStatus
from services.media_files import task_scratch
from services.media_files.task_scratch import (
    ORPHAN_RELEASE_GRACE,
    project_storage_profile,
    read_scratch_baseline,
    release_orphaned_scratch,
    release_scratch_media,
    remember_scratch_baseline,
)
from services.runtime_files.execution_store import ProjectExecutionStore
from media_files.test_task_scratch import (
    PROJECT_ID,
    T0,
    _media,
    _media_bytes,
    _moved,
    _needs_pinned_release,
    _scratch,
    _services,
    _stamp,
    _survivors,
    _task,
    _tree_bytes,
)

pytestmark = pytest.mark.unit


PROJECT_A = "project-" + "a" * 32


PROJECT_B = "project-" + "b" * 32


BASELINE = datetime(2026, 1, 10, tzinfo=timezone.utc)


def _seed_legacy_project(root: Path, project_id: str, *, count: int) -> Path:
    """A Project whose whole commit history predates its baseline.

    This is the imported-Project shape from the field: every transaction
    settled, none of it after the day the Project started being managed, so an
    unauthorized sweep frees nothing at all.
    """

    project = root / project_id
    transactions = project / "runtime" / "transactions"
    transactions.mkdir(parents=True)
    (project / "project.json").write_text("{}", encoding="utf-8")
    (project / "runtime" / ".storage-baseline.json").write_text(
        json.dumps({"version": 1, "since": BASELINE.isoformat()}),
        encoding="utf-8",
    )
    for index in range(count):
        directory = transactions / f"txn-{index:03d}"
        directory.mkdir()
        (directory / "journal.json").write_text(
            json.dumps({"state": "RUNTIME_FINALIZED"}),
            encoding="utf-8",
        )
        (directory / "final.json").write_bytes(b"x" * 64)
        moment = (BASELINE - timedelta(days=count - index)).timestamp()
        os.utime(directory, (moment, moment))
    return transactions


def test_reclaim_history_refuses_an_unconfirmed_press(
    api_request,
    app,
    api_runtime_root,
):
    transactions = _seed_legacy_project(api_runtime_root, PROJECT_A, count=60)

    missing = api_request(
        app,
        "POST",
        f"/storage/projects/{PROJECT_A}/reclaim-history",
        json={},
    )
    denied = api_request(
        app,
        "POST",
        f"/storage/projects/{PROJECT_A}/reclaim-history",
        json={"confirm": False},
    )

    assert missing.status_code == 400
    assert denied.status_code == 400
    assert missing.json()["code"] == "BAD_REQUEST"
    assert len(list(transactions.iterdir())) == 60


def test_reclaim_history_sweeps_only_the_requested_project(
    api_request,
    app,
    api_runtime_root,
):
    first = _seed_legacy_project(api_runtime_root, PROJECT_A, count=60)
    second = _seed_legacy_project(api_runtime_root, PROJECT_B, count=60)

    response = api_request(
        app,
        "POST",
        f"/storage/projects/{PROJECT_A}/reclaim-history",
        json={"confirm": True},
    )

    assert response.status_code == 200
    body = response.json()
    # The on-demand sweep is the boot sweep, so retention still holds: the
    # newest 50 receipts stay, only the surplus leaves.
    assert body["removedTransactions"] == 10
    assert body["transactionBytes"] > 0
    assert len(list(first.iterdir())) == 50
    assert len(list(second.iterdir())) == 60

    profile = api_request(app, "GET", "/storage/profile").json()
    rows = {item["projectId"]: item for item in profile["projects"]}
    assert rows[PROJECT_A]["historyKeptAt"] is None
    assert rows[PROJECT_B]["historyKeptAt"] is None


def test_reclaim_history_refuses_a_project_that_kept_its_history(
    api_request,
    app,
    api_runtime_root,
):
    """Opting out is answered with a refusal, not a silent zero.

    A button that reports success while deleting nothing teaches the person
    to press it again, so the kept case has to say so out loud.
    """

    from services.media_files.task_scratch import keep_project_history

    transactions = _seed_legacy_project(api_runtime_root, PROJECT_A, count=60)
    keep_project_history(
        api_runtime_root / PROJECT_A,
        now=datetime.now(timezone.utc),
    )

    response = api_request(
        app,
        "POST",
        f"/storage/projects/{PROJECT_A}/reclaim-history",
        json={"confirm": True},
    )

    assert response.status_code == 409
    assert len(list(transactions.iterdir())) == 60


def test_the_collapse_backups_are_counted_where_they_actually_sit(
    api_request,
    app,
    api_runtime_root,
):
    """The only storage this build adds sits outside every Project tree.

    A compressed pre-pass copy of a rewritten conversation is worth reporting:
    without it, the profile would claim a collapse freed bytes that the backup
    then quietly took back.
    """

    from services.media_files.task_scratch import ELISION_BACKUP_DIR_NAME

    backup = api_runtime_root / ELISION_BACKUP_DIR_NAME / "project-x"
    backup.mkdir(parents=True)
    handle = backup / "messages-20261102T120000.jsonl.gz"
    handle.write_bytes(b"\x1f\x8b" + b"z" * 100)

    body = api_request(app, "GET", "/storage/profile").json()

    assert body["elisionBackupBytes"] == 102


def test_requests_are_told_to_retry_while_recovery_runs():
    """A dead socket for minutes reads as a broken plugin, not a busy one.

    The first create() holds the registry lock for the whole recovery scan, so
    a request that arrives in that window must be answered, not queued.
    """

    from api.dependencies import project_file_services
    from domain.errors import RuntimePreparingError
    from services.project_files import facade

    facade.mark_runtime_preparing(True)
    try:
        with pytest.raises(RuntimePreparingError):
            project_file_services()
    finally:
        facade.mark_runtime_preparing(False)


def test_confirming_writes_a_receipt_the_sweeps_then_honour(
    api_request,
    app,
    api_runtime_root,
    monkeypatch,
):
    """The button is the confirmation the automatic pass waits for."""

    from services.media_files.task_scratch import (
        RECLAIM_CONFIRMATION_FILE_NAME,
    )

    monkeypatch.setenv("CREATOR_STORAGE_AUTO_RECLAIM_MAX_BYTES", "100")
    transactions = _seed_legacy_project(api_runtime_root, PROJECT_A, count=60)

    response = api_request(
        app,
        "POST",
        f"/storage/projects/{PROJECT_A}/reclaim-history",
        json={"confirm": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["confirmedAt"]
    assert body["removedTransactions"] == 10
    marker = (
        api_runtime_root
        / PROJECT_A
        / "runtime"
        / RECLAIM_CONFIRMATION_FILE_NAME
    )
    assert marker.is_file()
    assert len(list(transactions.iterdir())) == 50


def test_the_profile_carries_the_cap_and_the_pending_bytes(
    api_request,
    app,
    api_runtime_root,
    monkeypatch,
):
    """The console decides 'over the cap' from the report, not a constant.

    A threshold hardcoded in the UI drifts the moment the environment moves
    it, so the report travels with the number the sweeps actually honour.
    """

    monkeypatch.setenv("CREATOR_STORAGE_AUTO_RECLAIM_MAX_BYTES", "100")
    _seed_legacy_project(api_runtime_root, PROJECT_A, count=60)

    body = api_request(app, "GET", "/storage/profile").json()

    assert body["autoReclaimMaxBytes"] == 100
    row = next(
        item for item in body["projects"] if item["projectId"] == PROJECT_A
    )
    assert row["pendingReclaimBytes"] > 100
    assert row["reclaimConfirmedAt"] is None


def test_the_event_stream_size_is_reported_never_rewritten(
    api_request,
    app,
    api_runtime_root,
):
    """The durable event stream is surfaced, never collapsed in place.

    Its seqs are the SSE resume cursor, so rewriting it would desync every
    connected client; reporting the size is what lets an operator decide to
    rebuild by export instead of guessing why reconnects got slow.
    """
    created = api_request(
        app,
        "POST",
        "/projects",
        json={
            "clientRequestId": "event-stream-report",
            "name": "事件流",
            "scenario": "general",
            "aspectRatio": "16:9",
            "resolution": "720P",
        },
    )
    project_id = created.json()["projectId"]
    sessions = (
        api_runtime_root / project_id / "runtime" / "sessions" / "session-1"
    )
    sessions.mkdir(parents=True)
    (sessions / "events.jsonl").write_bytes(b"x" * 4096)

    body = api_request(app, "GET", "/storage/profile").json()
    row = next(
        item for item in body["projects"] if item["projectId"] == project_id
    )
    assert row["eventStreamBytes"] == 4096
    assert body["eventStreamReportMaxBytes"] == 64 * 1024 * 1024


def test_the_profile_scan_is_cached_per_reader(
    api_request,
    app,
    monkeypatch,
) -> None:
    """One walk of a 40 GB tree must not repeat for every reader.

    The home page asks for the profile on load and again on every return from
    a Project. Measured on the field's data root a single scan cost 1.4 s
    idle and up to 41 s while the process was busy -- which is how "open a
    Project, go back" turned into a 20+ second wait.
    """

    from api import storage_profile_routes

    calls: list[bool] = []
    real_scan = storage_profile_routes.project_storage_profile

    def spy_scan(services, *, include_orphans=False):
        calls.append(bool(include_orphans))
        return real_scan(services, include_orphans=include_orphans)

    monkeypatch.setattr(
        storage_profile_routes,
        "project_storage_profile",
        spy_scan,
    )
    storage_profile_routes.invalidate_storage_profile_cache()

    api_request(app, "GET", "/storage/profile")
    api_request(app, "GET", "/storage/profile")
    assert calls == [False]  # the second reader reused the scan

    monkeypatch.setenv("CREATOR_STORAGE_PROFILE_TTL_SECONDS", "0")
    api_request(app, "GET", "/storage/profile")
    api_request(app, "GET", "/storage/profile")
    assert calls == [False, False, False]  # TTL 0 scans per request again


def test_inflight_scan_cannot_repopulate_cache_after_reclaim(
    api_request, app, monkeypatch
):
    from api import storage_profile_routes as routes

    real_scan = routes.project_storage_profile
    calls = []

    def scan(services, *, include_orphans):
        profile = real_scan(services, include_orphans=include_orphans)
        calls.append(services.root)
        if len(calls) == 1:
            # A reclaim finishes after these bytes were measured.
            routes.invalidate_storage_profile_cache(str(services.root))
        return profile

    monkeypatch.setattr(routes, "project_storage_profile", scan)
    routes.invalidate_storage_profile_cache()
    for _ in range(3):
        assert api_request(app, "GET", "/storage/profile").status_code == 200
    assert len(calls) == 2


def test_invalidation_is_scoped_to_one_data_root() -> None:
    """A reclaim must clear its own root and leave another's scan alone.

    Serving a stale scan after a reclaim would bring back the "released but the
    button is still there" defect, while clearing every root would make two
    data roots fight over one cache.
    """

    from api import storage_profile_routes

    storage_profile_routes._profile_cache.clear()
    storage_profile_routes._profile_cache["/root-a"] = {False: (0.0, {})}
    storage_profile_routes._profile_cache["/root-b"] = {False: (0.0, {})}

    storage_profile_routes.invalidate_storage_profile_cache("/root-a")
    assert list(storage_profile_routes._profile_cache) == ["/root-b"]

    storage_profile_routes.invalidate_storage_profile_cache()
    assert not storage_profile_routes._profile_cache


@pytest.mark.parametrize("journal_removed", [False, True])
def test_interrupted_confirmed_transaction_cleanup_can_be_retried(
    api_request, app, api_runtime_root, monkeypatch, journal_removed
):
    from services.project_files import transaction_gc

    transactions = _seed_legacy_project(api_runtime_root, PROJECT_A, count=51)
    monkeypatch.setenv("CREATOR_STORAGE_AUTO_RECLAIM_MAX_BYTES", "10")
    route = f"/storage/projects/{PROJECT_A}/reclaim-history"

    def interrupt_removal(path, **_kwargs):
        if journal_removed:
            (path / "journal.json").unlink()

    with monkeypatch.context() as patch:
        patch.setattr(transaction_gc.shutil, "rmtree", interrupt_removal)
        result = api_request(app, "POST", route, json={"confirm": True})
    assert result.status_code == 200
    assert result.json()["transactionBytes"] == 0

    def profile():
        (row,) = api_request(app, "GET", "/storage/profile").json()["projects"]
        return row

    pending = profile()
    assert pending["reclaimConfirmedAt"] is not None
    assert pending["pendingReclaimBytes"] == 0
    (tombstone,) = transactions.glob(".deleted-*")
    remaining = sum(file.stat().st_size for file in tombstone.iterdir())
    assert remaining >= 64
    assert pending["pendingTransactionCleanupBytes"] == remaining
    result = api_request(app, "POST", route, json={"confirm": True})
    assert result.json()["transactionBytes"] == remaining
    assert profile()["pendingTransactionCleanupBytes"] == 0
    assert len(list(transactions.iterdir())) == 50


@_needs_pinned_release
def test_sweep_releases_pre_baseline_scratch_by_default(
    tmp_path: Path,
) -> None:
    """A settled Task is settled whatever day it was.

    An imported Project held 326 SUCCEEDED tasks -- 9.5 GB -- all older than
    its baseline; a gate that only opened on a click could not reach them, so
    the default released them and the click became an opt-out.
    """
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

    assert swept.removed_files == 2
    assert read_scratch_baseline(tmp_path / PROJECT_ID) == T0
    assert _media(scratch) == []

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
    assert _media(scratch) == []


@_needs_pinned_release
def test_keeping_history_spares_the_pre_baseline_scratch(
    tmp_path: Path,
) -> None:
    """The #217 promise, kept as a per-Project choice.

    Opting out restores the date gate and nothing else: a still-running or
    quarantined Task was never in scope to begin with.
    """

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
    task_scratch.remember_scratch_baseline(tmp_path / PROJECT_ID, now=T0)
    task_scratch.keep_project_history(tmp_path / PROJECT_ID, now=T0)

    swept = release_orphaned_scratch(
        _services(tmp_path),
        now=T0 + ORPHAN_RELEASE_GRACE + timedelta(minutes=5),
    )

    assert swept.removed_files == 0
    assert len(_media(scratch)) == 2
    # Documents are never scratch media, kept or not.
    assert "spec.json" in _survivors(scratch)
    assert "provider-tasks.jsonl" in _survivors(scratch)


def test_scratch_brought_in_by_an_import_is_reclaimed_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The stamp lands at first allocation, later than any archived Task, so
    # the old date gate used to protect a Project that arrived through an
    # archive -- precisely the imported 24 GB Projects that could not free a
    # byte on their own.  Settled scratch goes by default now; choosing to
    # keep the history restores the gate (see the opt-out case above).
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

    assert summary.removed_files == 2
    assert _media(legacy) == []


def test_reclaimable_scratch_matches_the_sweep_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _scratch(tmp_path, "task-legacy")
    transactions = tmp_path / PROJECT_ID / "runtime" / "transactions"
    old_tx = transactions / "tx-legacy"
    old_tx.mkdir(parents=True)
    (old_tx / "journal.json").write_text("{}", encoding="utf-8")
    _stamp(old_tx, T0 - timedelta(days=1))
    remember_scratch_baseline(tmp_path / PROJECT_ID, now=T0)

    fresh = _scratch(tmp_path, "task-fresh")
    new_tx = transactions / "tx-fresh"
    new_tx.mkdir(parents=True)
    (new_tx / "journal.json").write_bytes(b"{}\n")
    _stamp(new_tx, T0 + timedelta(hours=2))

    store = ProjectExecutionStore(tmp_path)
    for name, moment in (
        ("task-legacy", T0 - timedelta(days=1)),
        ("task-fresh", T0),
    ):
        store.create_task(
            _task(TaskStatus.QUEUED).model_copy(update={"task_id": name}),
        )
        _moved(
            store,
            name,
            frm=TaskStatus.QUEUED,
            to=TaskStatus.FAILED,
            updated_at=moment,
        )

    entry = project_storage_profile(
        _services(tmp_path),
        now=T0 + timedelta(hours=3),
    )["projects"][0]

    assert entry["baselineAt"] == T0.isoformat()
    assert entry["taskWorkLegacyBytes"] == _tree_bytes(legacy)
    assert entry["taskWorkBytes"] == _tree_bytes(fresh)
    # Reclaimable follows the sweep's rule, not the legacy label: a finished
    # Task past the grace window is reclaimable whether or not it predates the
    # baseline, counted as the media a release would take rather than the kept
    # docs.  The legacy split stays a display category only.
    assert entry["taskWorkReclaimableBytes"] == (
        _media_bytes(fresh) + _media_bytes(legacy)
    )
    assert entry["taskWorkReclaimableBytes"] < (
        _tree_bytes(fresh) + _tree_bytes(legacy)
    )
    assert entry["transactionsLegacyBytes"] == _tree_bytes(old_tx)
    assert entry["transactionsBytes"] == _tree_bytes(new_tx)
    assert project_storage_profile(_services(tmp_path))["totals"][
        "taskWorkReclaimableBytes"
    ] == _media_bytes(fresh) + _media_bytes(legacy)

    monkeypatch.setenv("CREATOR_SCRATCH_RELEASE_ENABLED", "0")
    disabled = project_storage_profile(_services(tmp_path))["projects"][0]
    assert disabled["taskWorkReclaimableBytes"] == 0
    assert disabled["taskWorkBytes"] == entry["taskWorkBytes"]


def test_the_scratch_sweep_stops_when_the_pass_budget_runs_out(
    tmp_path: Path,
) -> None:
    """One budget carries both sweeps, so the second can be refused.

    A Project with 326 finished Tasks is not one boot's work; leaving the rest
    for the next pass still frees most of it, while blocking the first paint
    frees nothing that anybody can see.
    """

    from services.media_files.task_scratch import SweepBudget

    first = _scratch(tmp_path)
    second = _scratch(tmp_path, "task-2")
    store = ProjectExecutionStore(tmp_path)
    store.create_task(_task(TaskStatus.QUEUED))
    store.create_task(
        _task(TaskStatus.QUEUED).model_copy(update={"task_id": "task-2"}),
    )
    for task_id in ("task-1", "task-2"):
        _moved(
            store,
            task_id,
            frm=TaskStatus.QUEUED,
            to=TaskStatus.RUNNING,
            updated_at=T0 - timedelta(days=2),
        )
        _moved(
            store,
            task_id,
            frm=TaskStatus.RUNNING,
            to=TaskStatus.SUCCEEDED,
            updated_at=T0 - timedelta(days=1),
        )

    swept = release_orphaned_scratch(
        _services(tmp_path),
        now=T0,
        budget=SweepBudget(max_bytes=1),
    )

    assert swept.released_directories == 1
    assert swept.stopped_early
    assert len(_media(first)) + len(_media(second)) == 2


def test_the_pass_budget_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An install that would rather wait can raise or drop the bound."""

    monkeypatch.delenv("CREATOR_STORAGE_SWEEP_MAX_BYTES", raising=False)
    monkeypatch.delenv("CREATOR_STORAGE_SWEEP_MAX_SECONDS", raising=False)

    budget = task_scratch.budget_from_environment(monotonic=100.0)

    assert budget.max_bytes == task_scratch.SWEEP_DEFAULT_MAX_BYTES
    assert budget.deadline == 100.0 + task_scratch.SWEEP_DEFAULT_MAX_SECONDS

    monkeypatch.setenv("CREATOR_STORAGE_SWEEP_MAX_BYTES", "1024")
    monkeypatch.setenv("CREATOR_STORAGE_SWEEP_MAX_SECONDS", "0")

    tuned = task_scratch.budget_from_environment()

    assert tuned.max_bytes == 1024
    # Zero seconds means "no clock bound", not "delete nothing".
    assert tuned.deadline is None


def test_an_oversized_projects_scratch_waits_for_a_person(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap is per Project, so a small one next door is still swept."""

    from services.media_files.task_scratch import confirm_project_reclaim

    monkeypatch.setenv("CREATOR_STORAGE_AUTO_RECLAIM_MAX_BYTES", "10")
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

    withheld = release_orphaned_scratch(_services(tmp_path), now=T0)

    assert withheld.removed_files == 0
    assert withheld.skipped_projects == 1
    assert withheld.skipped_bytes > 10
    assert len(_media(scratch)) == 2

    confirm_project_reclaim(tmp_path / PROJECT_ID, now=T0)

    swept = release_orphaned_scratch(_services(tmp_path), now=T0)

    assert swept.removed_files == 2
    assert swept.skipped_projects == 0
    assert _media(scratch) == []


@_needs_pinned_release
def test_background_scratch_release_stops_during_one_directory(
    tmp_path,
    monkeypatch,
):
    scratch = _scratch(tmp_path)
    budget = task_scratch.SweepBudget()
    original = task_scratch._releasable_name

    def stop_after_one(name, mode):
        releasable = original(name, mode)
        if releasable:
            budget.stop_requested.set()
        return releasable

    monkeypatch.setattr(task_scratch, "_releasable_name", stop_after_one)
    result = release_scratch_media(
        scratch, reason="orphan-sweep", budget=budget
    )
    assert result.removed_files == 1
    assert len(_media(scratch)) == 1
    assert budget.stopped_early
