# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from domain.enums import TransactionStatus
from services.project_files import commit as commit_module
from services.project_files import migrations as migrations_module
from services.project_files import models as models_module
from services.project_files.commit import (
    CommitJournalState,
    PendingProjectRecoveryError,
    ProjectCommitBoundary,
    ProjectCommitJournal,
)
from services.project_files.models import Project
from services.project_files.poller import ProjectPoller
from services.project_files.recovery import (
    AUDIT_LEDGER_FILE_NAME,
    ProjectCommitRecoveryCoordinator,
    ProjectRecoveryIntegrityError,
    RecoveryAction,
    _audit_runtime_dependencies,
)
from services.project_files.serialization import (
    canonical_json_bytes,
    load_project_document,
)
from services.project_files.store import ProjectStore
from services.runtime_files.atomic_store import (
    AtomicJsonRecordStore,
    json_document_bytes,
)

from .conftest import (
    PROJECT_ID,
    make_store,
    read_changeset,
    read_journal,
    read_review,
    read_round,
    read_state,
    recover,
    review_boundary,
    review_commit_kwargs,
    round_store,
    runtime_root,
    transaction_root,
)
from .test_audio_layering import _audio_element, _project_raw

pytestmark = pytest.mark.unit


class SimulatedProcessDeath(RuntimeError):
    pass


def _crash_with_project_replaced(
    store,
    base,
    monkeypatch,
    *,
    transaction_id,
    round_id,
    review=False,
):
    candidate = base.project.model_dump(mode="json")
    candidate["name"] = "Published before crash"
    if review:
        metadata = review_commit_kwargs(review_boundary(base))
    else:
        metadata = {"origin": "frontend_edit"}

    boundary_service = ProjectCommitBoundary(store)
    with monkeypatch.context() as patch:
        patch.setattr(
            boundary_service,
            "_record_review",
            lambda **_kwargs: (_ for _ in ()).throw(
                SimulatedProcessDeath("crash after Project replace"),
            ),
        )
        with pytest.raises(
            SimulatedProcessDeath,
            match="after Project replace",
        ):
            boundary_service.commit(
                base=base,
                candidate=candidate,
                transaction_id=transaction_id,
                round_id=round_id,
                **metadata,
            )

    assert store.read("project-1").project.name == "Published before crash"
    assert (
        read_journal(store, transaction_id).state
        is CommitJournalState.PROJECT_REPLACED
    )


def test_startup_recovery_reports_corrupt_project_without_blocking_healthy_one(
    tmp_path,
):
    store = ProjectStore(tmp_path.resolve())
    store.create(Project.new(project_id="project-good", name="Good"))
    store.create(Project.new(project_id="project-bad", name="Bad"))
    store.project_path("project-bad").write_text("{broken", encoding="utf-8")

    report = ProjectCommitRecoveryCoordinator(store).recover_all()

    assert [item.project_id for item in report.projects] == [
        "project-bad",
        "project-good",
    ]
    bad, good = report.projects
    assert not bad.ok
    assert bad.outcomes[0].action is RecoveryAction.INTEGRITY_ERROR
    assert good.ok


def test_new_commit_waits_for_unfinalized_publication_recovery(
    tmp_path,
    monkeypatch,
):
    store, base = make_store(tmp_path)
    _crash_with_project_replaced(
        store,
        base,
        monkeypatch,
        transaction_id="crashed-commit",
        round_id="crashed-round",
    )
    current = store.read("project-1")
    next_candidate = current.project.model_dump(mode="json")
    next_candidate["description"] = "must wait"

    with pytest.raises(PendingProjectRecoveryError, match="requires recovery"):
        ProjectCommitBoundary(store).commit(
            base=current,
            candidate=next_candidate,
            origin="runtime_task",
            transaction_id="blocked-next",
        )

    recover(store).raise_for_integrity()
    result = ProjectCommitBoundary(store).commit(
        base=current,
        candidate=next_candidate,
        origin="runtime_task",
        transaction_id="after-recovery",
    )
    assert result.snapshot.project.description == "must wait"


@pytest.mark.parametrize(
    "legacy", [False, True], ids=["native", "repaired-legacy"]
)
@pytest.mark.parametrize(
    "published", [False, True], ids=["before-publish", "after-publish"]
)
def test_prepared_journal_recovers_the_project_publication(
    tmp_path,
    monkeypatch,
    legacy,
    published,
):
    create_store = _legacy_double_narration_store if legacy else make_store
    store, base = create_store(tmp_path)
    transaction_id, round_id = "tx-prepared", "round-prepared"
    journal_path = transaction_root(store, transaction_id) / "journal.json"
    candidate = base.project.model_dump(mode="json")
    field = "description" if published and not legacy else "name"
    candidate[field] = "Candidate before crash"
    original_write = AtomicJsonRecordStore.write

    def interrupt_transition(record_store, entry):
        if record_store.path == journal_path and isinstance(
            entry, ProjectCommitJournal
        ):
            interrupted = (
                entry.state is not CommitJournalState.PREPARED
                if published
                else entry.state is CommitJournalState.ABORTED
            )
            if interrupted:
                raise SimulatedProcessDeath(
                    "journal transition was not durable"
                )
        return original_write(record_store, entry)

    def interrupt_replace(*_args, **_kwargs):
        raise SimulatedProcessDeath("project was not published")

    with monkeypatch.context() as patch:
        if not published:
            patch.setattr(store, "replace", interrupt_replace)
        patch.setattr(AtomicJsonRecordStore, "write", interrupt_transition)
        with pytest.raises(SimulatedProcessDeath, match="journal transition"):
            ProjectCommitBoundary(store).commit(
                base=base,
                candidate=candidate,
                origin="frontend_edit",
                transaction_id=transaction_id,
                round_id=round_id,
            )

    current = store.read(PROJECT_ID)
    assert (
        read_journal(store, transaction_id).state
        is CommitJournalState.PREPARED
    )
    if published:
        assert getattr(current.project, field) == candidate[field]
        if legacy:
            elements = current.source_document["timelines"]["items"][
                "timeline:main"
            ]["elements_by_id"]
            assert elements["narr-old"]["enabled"] is False
    else:
        assert current.etag == base.etag

    report = recover(store)
    assert report.ok
    state = read_journal(store, transaction_id).state
    assert store.read(PROJECT_ID).etag == current.etag
    if not published:
        assert report.outcomes[0].action is RecoveryAction.ABORTED_UNPUBLISHED
        assert state is CommitJournalState.ABORTED
        assert read_round(store, round_id).status is TransactionStatus.ABORTED
        return

    assert report.outcomes[0].action is RecoveryAction.FINALIZED_RUNTIME
    assert state is CommitJournalState.RUNTIME_FINALIZED
    changeset = read_changeset(store, round_id)
    assert [change.json_pointer for change in changeset.changes] == [
        f"/{field}"
    ]
    assert changeset.base_etag == base.etag
    assert changeset.final_etag == current.etag
    result = ProjectCommitBoundary(store).commit(
        base=current,
        candidate={
            **current.project.model_dump(mode="json"),
            "description": "edit after recovery",
        },
        origin="runtime_task",
        transaction_id="after-recovery",
    )
    assert result.snapshot.project.description == "edit after recovery"


def _legacy_double_narration_store(tmp_path):
    """``project-1`` on disk as a pre-gate v9 doc: two enabled narrations.

    The write gate rejects two overlapping narration tracks, so this document
    is persisted directly -- exactly as a runtime predating the gate would have
    left it. Loading repairs it (mutes the older take), which is what makes
    ``base.project`` diverge from the source ``base.etag`` was hashed from:
    the crash-recovery hazard. ``audio-version-2`` is created later, so
    ``narr-new`` is the take that stays enabled.
    """

    store = ProjectStore(tmp_path.resolve())
    store.create(Project.new(project_id=PROJECT_ID, name="Initial"))
    raw = _project_raw()
    raw["project_id"] = PROJECT_ID
    raw["name"] = "Initial"
    raw["timelines"]["items"]["timeline:main"]["elements_by_id"] = {
        "narr-old": _audio_element(
            "narr-old",
            start_tick=0,
            duration_tick=8_000,
            role="narration",
        ),
        "narr-new": _audio_element(
            "narr-new",
            start_tick=0,
            duration_tick=8_000,
            role="narration",
            version_id="audio-version-2",
        ),
    }
    versions = raw["assets"]["source_versions_by_id"]
    versions["audio-version-2"] = {
        **versions["audio-version-1"],
        "version_id": "audio-version-2",
        "logical_asset_id": "logical-audio-2",
        "created_at": "2026-09-01T08:00:00Z",
    }
    # Round-trip through the model so every defaulted field is materialized,
    # then restore the legacy both-enabled state the repair had muted. A
    # complete canonical source isolates the enabled-flip divergence: base.json
    # and the candidate then differ ONLY by that flip and the user's edit, so
    # the three-way merge stays conflict-free -- the shape of a real pre-gate
    # project, not a hand-built partial document.
    full = load_project_document(raw).model_dump(mode="json")
    full["timelines"]["items"]["timeline:main"]["elements_by_id"]["narr-old"][
        "enabled"
    ] = True
    store.project_path(PROJECT_ID).write_bytes(canonical_json_bytes(full))
    base = store.read(PROJECT_ID)
    elements = base.project.timelines.items["timeline:main"].elements_by_id
    # The load repair fired: the older take is muted in memory while the source
    # on disk still has both enabled -- the exact base.etag/base.json split.
    assert elements["narr-old"].enabled is False
    assert elements["narr-new"].enabled is True
    assert base.source_document is not None
    return store, base


def test_legacy_double_narration_project_replaced_recovery_stays_consistent(
    tmp_path,
    monkeypatch,
):
    """A legacy (source != normalized) Project that dies after the Project is
    replaced must recover, rebuilding the same business ChangeSet commit
    persisted -- not one polluted by the load-time repair.

    Regression: recovery recomputed business changes from the raw
    ``latest.json``/``base.json`` source while commit used the migrated
    ``model_dump``, so the enable-flip repair surfaced as a phantom change,
    the ChangeSet disagreed with the snapshots, and a recoverable crash became
    a permanent integrity error (also on the idempotent second pass).
    """

    store, base = _legacy_double_narration_store(tmp_path)
    round_id = "round-dn-replaced"
    _crash_with_project_replaced(
        store,
        base,
        monkeypatch,
        transaction_id="tx-dn-replaced",
        round_id=round_id,
        review=True,
    )

    first = recover(store)
    assert first.ok
    assert first.outcomes[0].action is RecoveryAction.FINALIZED_RUNTIME
    second = recover(store)
    assert second.ok
    assert second.outcomes[0].action is RecoveryAction.ALREADY_FINALIZED

    changeset = read_changeset(store, round_id)
    assert [c.json_pointer for c in changeset.changes] == ["/name"]
    review = read_review(store, round_id)
    assert [o.json_pointer for o in review.operations] == ["/name"]


def test_unnormalized_source_project_replaced_recovery_has_no_phantom_change(
    tmp_path,
    monkeypatch,
):
    """An imported Project whose source omits an optional field must recover
    with only the real user edit in its ChangeSet/Review.

    The model back-fills the missing ``script_approval_fingerprint`` on load;
    that normalization is not a user edit and must never appear as a business
    change. Same root cause as the double-narration case above: recovery must
    diff the migrated ``model_dump``, not the raw source.
    """

    store, _base = make_store(tmp_path)
    path = store.project_path(PROJECT_ID)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["settings"].pop("script_approval_fingerprint", None)
    path.write_text(json.dumps(raw), encoding="utf-8")
    base = store.read(PROJECT_ID)
    assert base.source_document != base.project.model_dump(mode="json")

    round_id = "round-approval-replaced"
    _crash_with_project_replaced(
        store,
        base,
        monkeypatch,
        transaction_id="tx-approval-replaced",
        round_id=round_id,
        review=True,
    )

    report = recover(store)
    assert report.ok
    assert report.outcomes[0].action is RecoveryAction.FINALIZED_RUNTIME
    review = read_review(store, round_id)
    assert [o.json_pointer for o in review.operations] == ["/name"]
    changeset = read_changeset(store, round_id)
    assert [c.json_pointer for c in changeset.changes] == ["/name"]


def _enter_pre_narration_gate_runtime(monkeypatch):
    """Load/commit as the pre-gate Runtime (merge base 6e44d30) did.

    The narration self-heal and the double-voice write gate both landed after
    the merge base. A pre-upgrade Runtime therefore loaded and committed a
    legacy double-narration Project verbatim: ``base.json``,
    ``candidate.json``, ``latest.json``, ``final.json`` and the published
    ``project.json`` all kept ``narr-old.enabled=True``, and
    ``touched_pointers`` was the source diff -- just the user's rename.
    Recovery must replay that exact representation, so
    the cross-version regressions below build the transaction with both repairs
    disabled and then strip the version field the old Runtime never wrote.
    """

    monkeypatch.setattr(
        migrations_module,
        "_deconflict_overlapping_narration",
        lambda document, *, step: document,
    )
    monkeypatch.setattr(
        models_module,
        "_validate_narration_overlap",
        lambda *_args, **_kwargs: None,
    )


def _strip_diff_semantics(store, transaction_id):
    """Drop ``diff_semantics`` so the journal parses as a pre-field record.

    A base-6e44d30 journal predates the field entirely; recovery must treat an
    absent field as ``DIFF_SEMANTICS_SOURCE`` and replay the raw source diff.
    """

    path = transaction_root(store, transaction_id) / "journal.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("diff_semantics", None)
    path.write_bytes(json_document_bytes(raw))


def test_commit_stamps_migrated_diff_semantics(tmp_path):
    """A current commit records ``DIFF_SEMANTICS_MIGRATED``.

    This is the version marker that lets recovery keep diffing the migrated
    ``model_dump`` for new transactions while the cross-version tests below
    strip it to replay legacy source semantics.
    """

    store, base = make_store(tmp_path)
    candidate = base.project.model_dump(mode="json")
    candidate["name"] = "Stamped"
    ProjectCommitBoundary(store).commit(
        base=base,
        candidate=candidate,
        origin="runtime_task",
        transaction_id="tx-stamp",
    )
    journal = read_journal(store, "tx-stamp")
    assert journal.diff_semantics == commit_module.DIFF_SEMANTICS_MIGRATED


def test_pre_narration_gate_finalized_transaction_recovers(
    tmp_path,
    monkeypatch,
):
    """Cross-version, completed: a pre-gate ``RUNTIME_FINALIZED`` transaction
    for a legacy double-narration Project recovers as ``ALREADY_FINALIZED``.

    The old journal recorded ``touched_pointers=['/name']`` from a source diff
    over ``base.json``/``candidate.json`` that both kept
    ``narr-old.enabled=True``. Current recovery loads ``base.json`` and
    self-heals that flag to ``False``; diffing the healed ``model_dump``
    against the raw candidate would add a phantom
    ``/timelines/.../narr-old/enabled`` and reject the journal with "journal
    touched_pointers do not match its snapshots". The absent
    ``diff_semantics`` selects the source representation so the recompute
    matches what the old Runtime wrote.
    """

    store, _base = _legacy_double_narration_store(tmp_path)
    transaction_id = "tx-xgate-finalized"
    round_id = "round-xgate-finalized"
    with monkeypatch.context() as patch:
        _enter_pre_narration_gate_runtime(patch)
        base = store.read(PROJECT_ID)
        elements = base.project.timelines.items["timeline:main"].elements_by_id
        assert elements["narr-old"].enabled is True  # no self-heal pre-gate
        candidate = base.project.model_dump(mode="json")
        candidate["name"] = "Finalized before upgrade"
        ProjectCommitBoundary(store).commit(
            base=base,
            candidate=candidate,
            transaction_id=transaction_id,
            round_id=round_id,
            **review_commit_kwargs(review_boundary(base)),
        )
    _strip_diff_semantics(store, transaction_id)
    journal = read_journal(store, transaction_id)
    assert journal.state is CommitJournalState.RUNTIME_FINALIZED
    assert journal.touched_pointers == ["/name"]
    assert journal.diff_semantics == commit_module.DIFF_SEMANTICS_SOURCE

    report = recover(store)
    assert report.ok
    assert report.outcomes[0].action is RecoveryAction.ALREADY_FINALIZED
    assert store.read(PROJECT_ID).project.name == "Finalized before upgrade"
    # Idempotent: a second startup pass stays ALREADY_FINALIZED.
    again = recover(store)
    assert again.ok
    assert again.outcomes[0].action is RecoveryAction.ALREADY_FINALIZED


def test_pre_narration_gate_project_replaced_transaction_recovers(
    tmp_path,
    monkeypatch,
):
    """Cross-version, interrupted: a pre-gate transaction that died after the
    Project was replaced but before Review bookkeeping recovers to
    ``FINALIZED_RUNTIME``, and the next real edit commits instead of 503.

    Same source-vs-migrated divergence as above, on the ``PROJECT_REPLACED``
    path where recovery rebuilds the business ChangeSet/Review from the
    snapshots. Without the version marker the phantom ``/enabled`` made
    recovery fail, the next commit raise ``PendingProjectRecoveryError`` and a
    real rename PATCH return 503 ``STORAGE_INTEGRITY_ERROR``.
    """

    store, _base = _legacy_double_narration_store(tmp_path)
    transaction_id = "tx-xgate-replaced"
    round_id = "round-xgate-replaced"
    with monkeypatch.context() as patch:
        _enter_pre_narration_gate_runtime(patch)
        base = store.read(PROJECT_ID)
        narr_old = base.project.timelines.items["timeline:main"].elements_by_id
        assert narr_old["narr-old"].enabled is True
        _crash_with_project_replaced(
            store,
            base,
            monkeypatch,
            transaction_id=transaction_id,
            round_id=round_id,
            review=True,
        )
    _strip_diff_semantics(store, transaction_id)
    journal = read_journal(store, transaction_id)
    assert journal.state is CommitJournalState.PROJECT_REPLACED
    assert journal.touched_pointers == ["/name"]

    report = recover(store)
    assert report.ok
    assert report.outcomes[0].action is RecoveryAction.FINALIZED_RUNTIME
    changeset = read_changeset(store, round_id)
    assert [c.json_pointer for c in changeset.changes] == ["/name"]
    review = read_review(store, round_id)
    assert [o.json_pointer for o in review.operations] == ["/name"]

    # The CR's follow-up: a real rename after recovery must commit instead of
    # raising PendingProjectRecoveryError (surfaced as 503 to the API).
    after = store.read(PROJECT_ID)
    next_candidate = after.project.model_dump(mode="json")
    next_candidate["name"] = "Renamed after cross-version recovery"
    result = ProjectCommitBoundary(store).commit(
        base=after,
        candidate=next_candidate,
        origin="runtime_task",
        transaction_id="after-xgate-replaced",
    )
    assert (
        result.snapshot.project.name == "Renamed after cross-version recovery"
    )


def test_project_replaced_recovery_is_idempotent_and_rebuilds_runtime_state(
    tmp_path,
    monkeypatch,
):
    store, base = make_store(tmp_path)
    transaction_id = "tx-project-replaced"
    round_id = "round-project-replaced"
    _crash_with_project_replaced(
        store,
        base,
        monkeypatch,
        transaction_id=transaction_id,
        round_id=round_id,
    )

    coordinator = ProjectCommitRecoveryCoordinator(store)
    first = coordinator.recover_project("project-1")
    second = coordinator.recover_project("project-1")
    current = store.read("project-1")

    assert first.outcomes[0].action is RecoveryAction.FINALIZED_RUNTIME
    assert second.outcomes[0].action is RecoveryAction.ALREADY_FINALIZED
    state = read_state(store)
    assert state.last_project_generation == current.generation
    assert state.last_project_etag == current.etag
    assert state.accepted_generation == current.generation
    assert read_round(store, round_id).status is TransactionStatus.COMMITTED


def test_recovery_recreates_pending_review_from_durable_boundary(
    tmp_path,
    monkeypatch,
):
    store, base = make_store(tmp_path)
    round_id = "round-review"
    _crash_with_project_replaced(
        store,
        base,
        monkeypatch,
        transaction_id="tx-review",
        round_id=round_id,
        review=True,
    )

    report = recover(store)
    current = store.read("project-1")
    review = read_review(store, round_id)
    state = read_state(store)

    assert report.ok
    assert review.request_id == "request-2"
    assert review.candidate_etag == current.etag
    assert [o.json_pointer for o in review.operations] == ["/name"]
    assert state.active_round_id == round_id
    assert state.accepted_generation == base.generation
    assert (
        read_round(store, round_id).status is TransactionStatus.PENDING_REVIEW
    )


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


def test_finalized_journal_repairs_missing_runtime_records(tmp_path):
    store, base = make_store(tmp_path)
    transaction_id = "tx-finalized-repair"
    round_id = "round-finalized-repair"
    candidate = base.project.model_dump(mode="json")
    candidate["name"] = "Committed"
    ProjectCommitBoundary(store).commit(
        base=base,
        candidate=candidate,
        origin="frontend_edit",
        transaction_id=transaction_id,
        round_id=round_id,
    )
    rt_root = runtime_root(store)
    (rt_root / "change-rounds" / round_id / "changeset.json").unlink()
    (rt_root / "state.json").unlink()
    rounds = round_store(store, round_id)
    rounds.write(
        rounds.read().model_copy(update={"status": TransactionStatus.ACTIVE}),
    )

    report = recover(store)

    assert (
        report.outcomes[0].action is RecoveryAction.REPAIRED_FINALIZED_RUNTIME
    )
    assert (
        read_changeset(store, round_id).final_etag
        == store.read("project-1").etag
    )
    assert read_state(store).last_project_etag == store.read("project-1").etag
    assert read_round(store, round_id).status is TransactionStatus.COMMITTED


def test_recovery_recreates_aggregate_after_transaction_publish_crash(
    tmp_path,
    monkeypatch,
) -> None:
    store, base = make_store(tmp_path)
    transaction_id = "tx-before-aggregate"
    round_id = "round-before-aggregate"
    candidate = base.project.model_dump(mode="json")
    candidate["name"] = "Not published"
    transactions_root = runtime_root(store) / "transactions"
    aggregate_path = (
        runtime_root(store) / "change-rounds" / round_id / "round.json"
    )
    original_fsync_directory = commit_module.fsync_directory
    failed = False

    def fail_after_transaction_rename(path):
        nonlocal failed
        if Path(path) == transactions_root and not failed:
            failed = True
            raise SimulatedProcessDeath("crash before aggregate projection")
        return original_fsync_directory(path)

    with monkeypatch.context() as patch:
        patch.setattr(
            commit_module,
            "fsync_directory",
            fail_after_transaction_rename,
        )
        with pytest.raises(SimulatedProcessDeath, match="before aggregate"):
            ProjectCommitBoundary(store).commit(
                base=base,
                candidate=candidate,
                origin="runtime_task",
                transaction_id=transaction_id,
                round_id=round_id,
            )

    assert (transaction_root(store, transaction_id) / "journal.json").is_file()
    assert not aggregate_path.exists()

    report = recover(store)

    assert report.ok
    assert report.outcomes[0].action is RecoveryAction.ABORTED_UNPUBLISHED
    assert (
        read_journal(store, transaction_id).state is CommitJournalState.ABORTED
    )
    assert read_round(store, round_id).status is TransactionStatus.ABORTED
    assert store.read("project-1") == base


def test_etag_mismatch_is_reported_without_overwriting_current_or_last_good(
    tmp_path,
    monkeypatch,
):
    store, base = make_store(tmp_path)
    transaction_id = "tx-etag-mismatch"
    _crash_with_project_replaced(
        store,
        base,
        monkeypatch,
        transaction_id=transaction_id,
        round_id="round-etag-mismatch",
    )
    published = store.read("project-1")
    raw = published.project.model_dump(mode="python")
    raw["generation"] += 1
    raw["description"] = "newer unrelated current"
    raw["updated_at"] = raw["updated_at"] + timedelta(seconds=1)
    newer = store.replace(
        "project-1",
        Project.model_validate(raw),
        expected_etag=published.etag,
    )
    poller = ProjectPoller(store)
    last_good = poller.open("project-1")

    report = recover(store)

    assert not report.ok
    assert report.outcomes[0].action is RecoveryAction.INTEGRITY_ERROR
    assert "ETag" in (report.outcomes[0].detail or "")
    assert store.read("project-1") == newer
    assert poller.cached("project-1") == last_good
    assert (
        read_journal(store, transaction_id).state
        is CommitJournalState.PROJECT_REPLACED
    )
    with pytest.raises(ProjectRecoveryIntegrityError):
        report.raise_for_integrity()


def test_frozen_auto_fix_restores_state_and_round(tmp_path):
    import zipfile

    archive = Path(__file__).parent / "fixtures" / "schema9-auto-fix.zip"
    with zipfile.ZipFile(archive) as source:
        source.extractall(tmp_path)
    store = ProjectStore(tmp_path)
    recovery = ProjectCommitRecoveryCoordinator(store).recover_project(
        "project-probe",
    )
    assert recovery.ok
    runtime = store.project_root("project-probe") / "runtime"
    state = AtomicJsonRecordStore(runtime / "state.json").read()
    assert state["last_project_etag"] == store.read("project-probe").etag
    change_round = AtomicJsonRecordStore(
        runtime / "change-rounds" / "round" / "round.json",
    ).read()
    assert change_round["status"] == TransactionStatus.COMMITTED.value


@pytest.fixture
def frozen_pending_review(tmp_path):
    """Unmodified 851a3cab2 files after publication and before review creation."""
    import zipfile

    archive = Path(__file__).parent / "fixtures" / "schema9-pending-review.zip"
    with zipfile.ZipFile(archive) as source:
        source.extractall(tmp_path)
    return ProjectStore(tmp_path)


@pytest.mark.parametrize("decision", ["ACCEPT", "REJECT"])
def test_old_writer_review_can_be_decided(frozen_pending_review, decision):
    from services.project_files.review import (
        ProjectReviewService,
        ReviewDecisionItem,
    )

    store = frozen_pending_review
    assert recover(store).ok
    review = read_review(store, "review-round")
    assert review.candidate_etag == store.read("project-1").etag
    assert read_state(store).active_round_id == "review-round"
    resolved = ProjectReviewService(store).decide(
        project_id="project-1",
        review_id=review.review_id,
        decision_token=review.decision_token,
        decisions=[
            ReviewDecisionItem(operation_id=op.operation_id, decision=decision)
            for op in review.operations
        ],
    )
    assert resolved.status.value == "RESOLVED"
    assert store.read("project-1").project.name == (
        "before" if decision == "REJECT" else "Published before crash"
    )
    assert recover(store).ok


def test_unprovable_old_pending_review_retains_its_evidence(
    frozen_pending_review,
):
    store = frozen_pending_review
    candidate = AtomicJsonRecordStore(
        transaction_root(store, "review-v9") / "candidate.json",
    )
    raw = candidate.read()
    raw["name"] = "Unrelated edit"
    candidate.write(raw)
    assert not recover(store).ok
    assert (
        read_journal(store, "review-v9").state
        is CommitJournalState.PROJECT_REPLACED
    )
    assert read_round(store, "review-round").status is TransactionStatus.ACTIVE
    assert not (runtime_root(store) / "state.json").exists()


def test_first_migrated_publication_finishes_runtime_after_a_crash(
    tmp_path,
    monkeypatch,
):
    store, base = make_store(tmp_path)
    raw = base.project.model_dump(mode="json")
    raw["schema_version"] = 9
    store.project_path("project-1").write_bytes(canonical_json_bytes(raw))
    base = store.read("project-1")
    _crash_with_project_replaced(
        store,
        base,
        monkeypatch,
        transaction_id="first-upgrade",
        round_id="upgrade-round",
    )
    report = recover(store)
    assert report.outcomes[0].action is RecoveryAction.FINALIZED_RUNTIME
    assert read_state(store).last_project_etag == store.read("project-1").etag
    assert (
        read_round(store, "upgrade-round").status
        is TransactionStatus.COMMITTED
    )


def test_legacy_schema_transaction_recovers_without_fail_closing(
    tmp_path,
    monkeypatch,
):
    """A pre-migration PROJECT_REPLACED transaction must not fail-close the
    Project after a schema upgrade: recovery advances its journal so the
    commit-time pending-publication guard accepts new writes again."""

    store, base = make_store(tmp_path)
    _crash_with_project_replaced(
        store,
        base,
        monkeypatch,
        transaction_id="legacy-tx",
        round_id="round-legacy",
    )
    base_store = AtomicJsonRecordStore(
        transaction_root(store, "legacy-tx") / "base.json",
    )
    base_data = base_store.read()
    base_data["schema_version"] = 999
    base_store.write(base_data)

    report = recover(store)
    assert report.ok
    outcome = next(
        item for item in report.outcomes if item.transaction_id == "legacy-tx"
    )
    assert outcome.action is RecoveryAction.ALREADY_FINALIZED
    assert "frozen-model" in (outcome.detail or "")
    assert (
        read_journal(store, "legacy-tx").state
        is CommitJournalState.RUNTIME_FINALIZED
    )

    # New commits pass the pending-publication guard again.
    current = store.read("project-1")
    candidate = current.project.model_dump(mode="json")
    candidate["name"] = "After legacy recovery"
    ProjectCommitBoundary(store).commit(
        base=current,
        candidate=candidate,
        origin="frontend_edit",
        transaction_id="post-legacy-commit",
    )
    assert store.read("project-1").project.name == "After legacy recovery"


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


def test_a_corrupt_audit_ledger_degrades_to_a_full_audit(
    tmp_path,
    monkeypatch,
):
    """The ledger is a cache: corrupt bytes cost time, never false alarms.

    A damaged ledger file used to raise on every transaction lookup,
    turning an intact Project into a wall of integrity errors.
    """

    monkeypatch.setenv("CREATOR_RECOVERY_LIGHT_TERMINAL", "0")
    store = _finalized_store(tmp_path)
    ledger = runtime_root(store) / AUDIT_LEDGER_FILE_NAME
    ledger.write_text("{not json", encoding="utf-8")

    first = recover(store)

    assert first.ok
    assert first.outcomes[0].action is RecoveryAction.ALREADY_FINALIZED

    # The damaged file was replaced, so the next boot is a cache hit again.
    calls: list = []
    _spy_load_inputs(monkeypatch, calls)
    second = recover(store)

    assert not calls
    assert second.outcomes[0].action is RecoveryAction.ALREADY_FINALIZED


def test_a_forged_non_terminal_verdict_in_the_ledger_is_not_trusted(
    tmp_path,
    monkeypatch,
):
    """A planted verdict must never stand in for the real audit.

    Only terminal, repair-free actions are cacheable; anything else in
    the file is ignored so the deep audit's repair branches still run.
    """

    monkeypatch.setenv("CREATOR_RECOVERY_LIGHT_TERMINAL", "0")
    store = _finalized_store(tmp_path)
    recover(store)  # build a ledger with a real fingerprint

    from services.project_files.recovery import _audit_fingerprint

    tx_root = transaction_root(store, "tx-ledger")
    ledger = runtime_root(store) / AUDIT_LEDGER_FILE_NAME
    table = json.loads(ledger.read_text(encoding="utf-8"))
    table[tx_root.name] = {
        "fingerprint": _audit_fingerprint(tx_root, runtime_root(store)),
        "action": RecoveryAction.INTEGRITY_ERROR.value,
        "transaction_id": "tx-ledger",
        "state_before": None,
        "state_after": None,
    }
    ledger.write_text(json.dumps(table), encoding="utf-8")

    calls: list = []
    _spy_load_inputs(monkeypatch, calls)
    report = recover(store)

    assert calls, "a forged verdict must fall through to the real audit"
    assert report.outcomes[0].action is RecoveryAction.ALREADY_FINALIZED


def test_a_ledger_entry_with_an_invalid_state_enum_is_not_trusted(
    tmp_path,
    monkeypatch,
):
    """The ledger is a cache: an entry whose ``state_before`` is not a real
    ``CommitJournalState`` must be treated as a miss (so the real audit runs),
    not raise ``ValueError`` and fail-close the whole Project as one integrity
    error."""

    monkeypatch.setenv("CREATOR_RECOVERY_LIGHT_TERMINAL", "0")
    store = _finalized_store(tmp_path)
    recover(store)  # build a ledger with a real fingerprint

    from services.project_files.recovery import _audit_fingerprint

    tx_root = transaction_root(store, "tx-ledger")
    ledger = runtime_root(store) / AUDIT_LEDGER_FILE_NAME
    table = json.loads(ledger.read_text(encoding="utf-8"))
    table[tx_root.name] = {
        "fingerprint": _audit_fingerprint(tx_root, runtime_root(store)),
        "action": RecoveryAction.ALREADY_FINALIZED.value,
        "transaction_id": "tx-ledger",
        "state_before": "not_a_real_journal_state",
        "state_after": None,
    }
    ledger.write_text(json.dumps(table), encoding="utf-8")

    calls: list = []
    _spy_load_inputs(monkeypatch, calls)
    report = recover(store)

    assert report.ok, "an invalid enum must not fail-close the Project"
    assert calls, "an invalid enum entry must fall through to a real audit"
    assert report.outcomes[0].action is RecoveryAction.ALREADY_FINALIZED


def test_a_changed_project_json_invalidates_the_ledger(tmp_path, monkeypatch):
    """``_audit_terminal_inputs`` cross-checks the live project.json ETag, so
    project.json must join the fingerprint: otherwise one that moved or
    corrupted on its own -- every Runtime fact untouched -- replays a cached
    ALREADY_FINALIZED on every hot boot instead of surfacing the conflict."""

    import os

    monkeypatch.setenv("CREATOR_RECOVERY_LIGHT_TERMINAL", "0")
    store = _finalized_store(tmp_path)
    calls: list = []
    _spy_load_inputs(monkeypatch, calls)
    recover(store)  # build the ledger
    calls.clear()

    # Sanity check: without damage the second boot is still a cache hit, so
    # fingerprinting project.json does not cost the steady-state fast boot.
    recover(store)
    assert not calls
    calls.clear()

    # Touch project.json alone (mtime), leaving every Runtime fact intact.
    project_json = runtime_root(store).parent / "project.json"
    stamp = os.stat(project_json).st_atime_ns
    os.utime(project_json, ns=(stamp, os.stat(project_json).st_mtime_ns + 5))

    recover(store)

    assert calls, "a touched project.json must invalidate the cached verdict"


def test_dependency_fingerprint_rejects_an_unsafe_round_id(tmp_path):
    """round.json bytes must not steer the fingerprint outside Runtime."""

    tx_root = tmp_path / "tx"
    tx_root.mkdir()
    (tx_root / "round.json").write_text(
        json.dumps({"round_id": "../secret"}),
        encoding="utf-8",
    )

    deps = _audit_runtime_dependencies(tx_root, tmp_path / "runtime")

    assert set(deps) == {"runtime-state", "project-document"}


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
