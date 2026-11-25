# -*- coding: utf-8 -*-
"""Project history retention, recovery, and document caches."""

from __future__ import annotations
import copy
import json
from datetime import datetime, timezone

import pytest
from services.project_files.auto_snapshot import (
    _AUTO_SNAPSHOT_DESCRIPTION,
    _AUTO_SNAPSHOT_RETENTION,
    auto_snapshot_timelines,
)
from services.project_files.migrations import migrate_project_document
from services.project_files.serialization import load_project_document
from services.project_files.commit import ProjectCommitBoundary
from services.project_files.models import Project
from services.project_files.recovery import (
    ProjectCommitRecoveryCoordinator,
    RecoveryAction,
)
from services.project_files.store import ProjectStore
from .conftest import (
    make_store,
    read_review,
    recover,
    runtime_root,
    transaction_root,
)
from .test_auto_snapshot import ELEM, TL, _element, _elems, _minimal_project
from .test_auto_snapshot import (
    TestSnapshotProjectValidation as SnapshotProjectValidation,
)
from .test_recovery import _crash_with_project_replaced

pytestmark = pytest.mark.unit


def test_cached_audit_repairs_a_lost_pending_review(tmp_path, monkeypatch):
    store, base = make_store(tmp_path)
    _crash_with_project_replaced(
        store,
        base,
        monkeypatch,
        transaction_id="tx-review",
        round_id="round-review",
        review=True,
    )
    assert recover(store).ok
    assert recover(store).ok
    path = (
        runtime_root(store) / "reviews" / "review-round-review" / "review.json"
    )
    path.unlink()
    assert recover(store).ok
    assert (
        read_review(store, "round-review").candidate_etag
        == store.read("project-1").etag
    )


def _spy_load_inputs(monkeypatch, calls):
    from services.project_files import recovery as recovery_module

    original = getattr(
        recovery_module.ProjectCommitRecoveryCoordinator,
        "_load_inputs",
    )

    def spy(self, *args, **kwargs):
        calls.append(args[2] if len(args) > 2 else kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(
        recovery_module.ProjectCommitRecoveryCoordinator,
        "_load_inputs",
        spy,
    )


def _finalized_store(tmp_path):
    store, base = make_store(tmp_path)
    candidate = base.project.model_dump(mode="json")
    candidate["name"] = "Committed"
    ProjectCommitBoundary(store).commit(
        base=base,
        candidate=candidate,
        origin="frontend_edit",
        transaction_id="tx-ledger",
        round_id="round-ledger",
    )
    return store


def test_a_second_boot_reuses_the_audit_ledger(tmp_path, monkeypatch):
    """The audit ran once; the bytes have not moved since, so it stands.

    Measured on the field Project: 455 terminal entries re-derived business
    diffs and ETags for 236 seconds at every boot, publishing nothing.
    """

    monkeypatch.setenv("CREATOR_RECOVERY_LIGHT_TERMINAL", "0")
    store = _finalized_store(tmp_path)
    calls: list = []
    _spy_load_inputs(monkeypatch, calls)

    first = recover(store)
    assert calls, "the first boot must really audit"
    assert first.outcomes[0].action is RecoveryAction.ALREADY_FINALIZED

    calls.clear()
    second = recover(store)

    assert not calls
    assert second.outcomes[0].action is first.outcomes[0].action
    assert "ledger hit" in (second.outcomes[0].detail or "")


def test_a_moved_snapshot_invalidates_the_ledger(tmp_path, monkeypatch):
    """The fingerprint is the promise: same bytes, same verdict."""

    import os

    monkeypatch.setenv("CREATOR_RECOVERY_LIGHT_TERMINAL", "0")
    store = _finalized_store(tmp_path)
    calls: list = []
    _spy_load_inputs(monkeypatch, calls)
    recover(store)
    calls.clear()

    frozen = transaction_root(store, "tx-ledger") / "final.json"
    stamp = os.stat(frozen).st_atime_ns
    os.utime(frozen, ns=(stamp, os.stat(frozen).st_mtime_ns + 5))

    recover(store)

    assert calls, "a touched snapshot must be audited again"


def test_the_ledger_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("CREATOR_RECOVERY_AUDIT_LEDGER", "0")
    monkeypatch.setenv("CREATOR_RECOVERY_LIGHT_TERMINAL", "0")
    store = _finalized_store(tmp_path)
    calls: list = []
    _spy_load_inputs(monkeypatch, calls)
    recover(store)
    calls.clear()

    recover(store)

    assert calls, "with the ledger off every boot audits in full"


def test_a_missing_runtime_state_invalidates_the_ledger(tmp_path, monkeypatch):
    """The ledger must notice when a Runtime file the audit read disappears.

    Review #243 r4548690109: the fingerprint used to cover only the seven
    transaction-directory files, but the cached ALREADY_FINALIZED conclusion
    is derived from ``state.json`` and the aggregate ``change-rounds/...
    /round.json`` as well. A crash that deleted those Runtime facts left
    every later boot hitting the ledger and skipping the repair branch
    that would have rebuilt them (turning into REPAIRED_FINALIZED_RUNTIME
    on a cold scan). Losing state.json or the aggregate round now changes
    the fingerprint, so the audit re-runs instead of reporting the old
    verdict.
    """

    monkeypatch.setenv("CREATOR_RECOVERY_LIGHT_TERMINAL", "0")
    store = _finalized_store(tmp_path)
    calls: list = []
    _spy_load_inputs(monkeypatch, calls)
    recover(store)  # build the ledger
    calls.clear()

    # Sanity check: without damage the second boot is a cache hit.
    recover(store)
    assert not calls
    calls.clear()

    # Simulate a crash that wiped runtime/state.json after the audit
    # finished. The transaction directory is untouched, so the old
    # fingerprint would keep matching and the ledger would keep saying
    # ALREADY_FINALIZED while a real scan would report
    # REPAIRED_FINALIZED_RUNTIME.
    runtime_state = runtime_root(store) / "state.json"
    runtime_state.unlink()

    report = recover(store)

    assert calls, (
        "deleting state.json must invalidate the cached verdict; the "
        "fingerprint still only sees the transaction directory"
    )
    assert report.outcomes[0].action is not None


def test_a_terminal_entry_is_decided_without_its_snapshots(
    tmp_path,
    monkeypatch,
):
    """The first boot pays stats and small Runtime facts, not 8 MB deepcopies.

    The ledger only helps the second boot; the imported Project's first boot
    after a copy is the one people wait minutes for.
    """

    store = _finalized_store(tmp_path)
    calls: list = []
    _spy_load_inputs(monkeypatch, calls)

    report = recover(store)

    assert not calls
    assert report.outcomes[0].action is RecoveryAction.ALREADY_FINALIZED


def test_incomplete_runtime_metadata_still_falls_through_to_repair(
    tmp_path,
    monkeypatch,
):
    """What the light audit cannot prove belongs to the full audit."""

    store = _finalized_store(tmp_path)
    (transaction_root(store, "tx-ledger") / "changeset.json").unlink()
    calls: list = []
    _spy_load_inputs(monkeypatch, calls)

    report = recover(store)

    assert calls, "a missing ChangeSet must defer to the full audit"
    action = report.outcomes[0].action
    assert action is RecoveryAction.REPAIRED_FINALIZED_RUNTIME


def test_the_light_terminal_audit_can_be_switched_off(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CREATOR_RECOVERY_LIGHT_TERMINAL", "0")
    store = _finalized_store(tmp_path)
    calls: list = []
    _spy_load_inputs(monkeypatch, calls)

    recover(store)

    assert calls, "with the light audit off the first boot audits in full"


def test_live_snapshot_cache_does_not_outlive_one_recovery_call(
    tmp_path,
) -> None:
    """A pass may reuse a live snapshot; the next pass may not inherit it.

    Review #243: this coordinator is long-lived, so a generation cached by the
    startup audit let a later compensation read judge an already-published
    transaction as still unpublished and mark it ABORTED, leaving the runtime
    one generation behind.
    """

    store = ProjectStore(tmp_path.resolve())
    store.create(Project.new(project_id="project-1", name="One"))
    coordinator = ProjectCommitRecoveryCoordinator(store)

    reads: list[str] = []
    real_read = store.read

    def spy(project_id):
        reads.append(project_id)
        return real_read(project_id)

    store.read = spy

    coordinator.recover_project("project-1")
    first = len(reads)
    coordinator.recover_project("project-1")

    # The second pass re-read rather than trusting the first pass's snapshot.
    assert len(reads) > first
    # pylint: disable-next=protected-access
    assert not coordinator._current_cache


class TestAutoSnapshotRetention:
    """Minting a baseline also bounds how many baselines the Project keeps."""

    @staticmethod
    def _seed_auto_snapshots(
        doc: dict,
        count: int,
        *,
        start: int = 1,
    ) -> list[str]:
        items = doc["timelines"]["items"]
        order = doc["timelines"]["order"]
        seeded = []
        for index in range(start, start + count):
            sid = f"snapshot:{TL}:{index}"
            frozen = copy.deepcopy(items[TL])
            frozen["timeline_id"] = sid
            frozen["description"] = _AUTO_SNAPSHOT_DESCRIPTION
            frozen["elements_by_id"] = {
                f"{sid}:{elem_id}": dict(
                    element,
                    element_id=f"{sid}:{elem_id}",
                )
                for elem_id, element in frozen["elements_by_id"].items()
            }
            items[sid] = frozen
            order.append(sid)
            seeded.append(sid)
        return seeded

    @staticmethod
    def _changed_candidate(base: dict) -> dict:
        candidate = copy.deepcopy(base)
        _elems(candidate)[ELEM]["label"] = "Modified"
        return candidate

    def test_within_cap_keeps_every_snapshot(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})
        self._seed_auto_snapshots(base, _AUTO_SNAPSHOT_RETENTION - 1)
        candidate = self._changed_candidate(base)

        assert auto_snapshot_timelines(base, candidate) == [TL]

        survivors = [
            key
            for key in candidate["timelines"]["items"]
            if key.startswith("snapshot:")
        ]
        assert len(survivors) == _AUTO_SNAPSHOT_RETENTION

    def test_oldest_auto_snapshots_are_dropped_first(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})
        self._seed_auto_snapshots(base, _AUTO_SNAPSHOT_RETENTION)
        candidate = self._changed_candidate(base)

        auto_snapshot_timelines(base, candidate)

        items = candidate["timelines"]["items"]
        # The seed filled the cap, so the new baseline evicted the oldest.
        assert f"snapshot:{TL}:1" not in items
        assert f"snapshot:{TL}:{_AUTO_SNAPSHOT_RETENTION + 1}" in items
        assert len(items) == 1 + _AUTO_SNAPSHOT_RETENTION

    def test_pruned_snapshots_leave_the_timeline_order(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})
        self._seed_auto_snapshots(base, _AUTO_SNAPSHOT_RETENTION + 2)
        candidate = self._changed_candidate(base)

        auto_snapshot_timelines(base, candidate)

        order = candidate["timelines"]["order"]
        items = candidate["timelines"]["items"]
        assert set(order) == set(items)
        assert _AUTO_SNAPSHOT_RETENTION == len(
            [key for key in order if key.startswith("snapshot:")],
        )

    def test_manual_snapshots_are_exempt(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})
        seeded = self._seed_auto_snapshots(base, _AUTO_SNAPSHOT_RETENTION + 1)
        manual = seeded[0]
        base["timelines"]["items"][manual]["description"] = "交付前留档"
        candidate = self._changed_candidate(base)

        auto_snapshot_timelines(base, candidate)

        items = candidate["timelines"]["items"]
        assert manual in items
        assert f"snapshot:{TL}:2" not in items

    def test_pruned_snapshots_do_not_reuse_ids(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})
        self._seed_auto_snapshots(base, _AUTO_SNAPSHOT_RETENTION + 5)
        candidate = self._changed_candidate(base)

        auto_snapshot_timelines(base, candidate)

        # _next_snapshot_id counts the highest surviving suffix, so eviction
        # must never mint an id a restored copy or an export still references.
        assert f"snapshot:{TL}:{_AUTO_SNAPSHOT_RETENTION + 6}" in (
            candidate["timelines"]["items"]
        )

    def test_project_stays_valid_after_pruning(self):
        from services.project_files.models import Project

        # Reuse the sibling test's fixture builder: it is test scaffolding,
        # not a production API, so the protected access is deliberate.
        # pylint: disable=protected-access
        base = SnapshotProjectValidation._project_with_output()
        self._seed_auto_snapshots(base, _AUTO_SNAPSHOT_RETENTION + 3)
        candidate = copy.deepcopy(base)
        candidate["timelines"]["items"][TL]["elements_by_id"]["element-1"][
            "label"
        ] = "Modified"

        auto_snapshot_timelines(base, candidate)

        Project.model_validate(candidate)
        # Live plus the retained window: the surplus history is gone from the
        # document, not merely hidden from the panel.
        assert (
            len(candidate["timelines"]["items"])
            == 1 + _AUTO_SNAPSHOT_RETENTION
        )
        assert set(candidate["timelines"]["order"]) == set(
            candidate["timelines"]["items"],
        )


def test_current_document_repair_keeps_an_untouched_timeline():
    raw = Project.new(
        project_id="project-unchanged", name="History"
    ).model_dump(
        mode="json",
    )
    original = raw["timelines"]["items"]["timeline:main"]
    repaired = migrate_project_document(raw)
    assert repaired["timelines"]["items"]["timeline:main"] is original


def _cache_probe_document(project_id: str):
    project = Project.new(
        project_id=project_id,
        name="Cached",
        now=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
    )
    raw = project.model_dump(mode="json")
    return raw, load_project_document(raw)


def test_document_etag_cache_returns_the_same_digest_and_canonicalizes_once(
    monkeypatch,
):
    from services.project_files import serialization

    raw, validated = _cache_probe_document("project-cached")
    payload = json.dumps(raw, ensure_ascii=False).encode("utf-8")
    calls: list[int] = []
    # The spy has to reach the canonicalizer itself: counting it is the point.
    # pylint: disable=protected-access
    original = serialization._source_document_data

    def spy(value, model_value):
        calls.append(1)
        return original(value, model_value)

    monkeypatch.setattr(serialization, "_source_document_data", spy)

    first = serialization.project_document_etag(
        raw,
        project=validated,
        source_payload=payload,
    )
    canonicalized = len(calls)
    assert canonicalized > 0  # the walk is recursive, one call per container

    second = serialization.project_document_etag(
        raw,
        project=validated,
        source_payload=payload,
    )

    assert first == second
    assert len(calls) == canonicalized  # the repeat read hit the cache
    # The cached value is the same digest the uncached path produces, so
    # journals and If-Match comparisons keep addressing identical bytes.
    assert first == serialization.project_document_etag(raw, project=validated)


def test_document_etag_cache_keys_on_the_exact_stored_bytes(monkeypatch):
    from services.project_files import serialization

    raw, validated = _cache_probe_document("project-cache-miss")
    calls: list[int] = []
    # pylint: disable=protected-access
    original = serialization._source_document_data

    def spy(value, model_value):
        calls.append(1)
        return original(value, model_value)

    monkeypatch.setattr(serialization, "_source_document_data", spy)

    serialization.project_document_etag(
        raw,
        project=validated,
        source_payload=b"{...revision-one...}",
    )
    canonicalized = len(calls)
    serialization.project_document_etag(
        raw,
        project=validated,
        source_payload=b"{...revision-two...}",
    )

    assert len(calls) > canonicalized  # different bytes must not share a slot


def test_import_replay_survives_pruning_transactions_with_receipts(
    tmp_path,
    monkeypatch,
):
    from services.project_files.transaction_gc import (
        collect_project_transactions,
    )
    from .test_script_import import (
        PROJECT_ID,
        _exercise_import_replay_after_crash,
    )

    def prune(store):
        removed, _ = collect_project_transactions(
            store.project_root(PROJECT_ID),
            retention=0,
        )
        assert removed == 1  # Only the second import has a durable receipt.

    _exercise_import_replay_after_crash(
        tmp_path,
        monkeypatch,
        after_second_commit=prune,
    )
