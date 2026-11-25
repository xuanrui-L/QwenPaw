# -*- coding: utf-8 -*-
# pylint: disable=protected-access,use-implicit-booleaness-not-comparison
"""Project commit and patch validation across the write boundary."""

from __future__ import annotations

import json
import threading

import pytest

from services.project_files.agent_tools import (
    AgentProjectToolContext,
    AgentProjectToolError,
    AgentProjectTools,
)
from services.project_files.commit import (
    ProjectCommitBoundary,
    ProjectCommitError,
    ProtectedFieldError,
)
from services.project_files.json_pointer import JsonCasConflict
from services.project_files.models import Project
from services.project_files.patch_ops import PatchOpError, apply_patch_ops
from services.project_files.review import (
    ProjectReviewService,
    ReviewDecisionItem,
)
from services.project_files.store import ProjectStore
from services.runtime_files.errors import FieldBlockConflictError
from services.runtime_files.field_blocks import FieldBlockStore
from services.runtime_files.models import (
    ReviewOperationDecision,
    ReviewStatus,
)

from .conftest import (
    make_store,
    read_review,
    read_state,
    review_boundary,
    review_commit_kwargs,
    runtime_root,
)


def test_commit_publishes_project_changeset_and_accepted_state(
    tmp_path,
) -> None:
    store, base = make_store(tmp_path)
    candidate = base.project.model_dump(mode="json")
    candidate["name"] = "Updated"

    result = ProjectCommitBoundary(store).commit(
        base=base,
        candidate=candidate,
        origin="frontend_edit",
    )

    assert result.snapshot.generation == 1
    assert store.read("project-1").project.name == "Updated"
    assert [c.json_pointer for c in result.changeset.changes] == ["/name"]
    state = read_state(store)
    assert state.accepted_generation == 1
    assert state.accepted_etag == result.snapshot.etag
    assert result.review is None


def test_disjoint_commits_from_one_base_merge(tmp_path) -> None:
    store, base = make_store(tmp_path)
    first = base.project.model_dump(mode="json")
    first["name"] = "Name A"
    ProjectCommitBoundary(store).commit(
        base=base,
        candidate=first,
        origin="frontend_edit",
    )

    second = base.project.model_dump(mode="json")
    second["description"] = "Description B"
    result = ProjectCommitBoundary(store).commit(
        base=base,
        candidate=second,
        origin="runtime_task",
    )

    assert result.snapshot.generation == 2
    assert result.snapshot.project.name == "Name A"
    assert result.snapshot.project.description == "Description B"


def test_runtime_finalization_is_serialized_in_project_generation_order(
    tmp_path,
    monkeypatch,
) -> None:
    store, base = make_store(tmp_path)
    boundary = ProjectCommitBoundary(store)
    first_candidate = base.project.model_dump(mode="json")
    first_candidate["name"] = "First"
    second_candidate = base.project.model_dump(mode="json")
    second_candidate["description"] = "Second"
    first_finalizing = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    errors: list[BaseException] = []
    original_record_review = boundary._record_review

    def delayed_record_review(**kwargs):
        if kwargs["snapshot"].generation == 1:
            first_finalizing.set()
            assert release_first.wait(timeout=2)
        return original_record_review(**kwargs)

    monkeypatch.setattr(boundary, "_record_review", delayed_record_review)

    def run_commit(candidate, origin, done=None):
        try:
            boundary.commit(base=base, candidate=candidate, origin=origin)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            if done is not None:
                done.set()

    first_thread = threading.Thread(
        target=run_commit,
        args=(first_candidate, "runtime_task"),
    )
    second_thread = threading.Thread(
        target=run_commit,
        args=(second_candidate, "frontend_edit", second_done),
    )
    first_thread.start()
    assert first_finalizing.wait(timeout=2)
    second_thread.start()
    assert not second_done.wait(timeout=0.1)
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not errors
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    current = store.read("project-1")
    state = read_state(store)
    assert current.generation == 2
    assert current.project.name == "First"
    assert current.project.description == "Second"
    assert state.last_project_generation == 2
    assert state.accepted_generation == 2


def test_same_field_commits_from_one_base_conflict(tmp_path) -> None:
    store, base = make_store(tmp_path)
    first = base.project.model_dump(mode="json")
    first["name"] = "Name A"
    ProjectCommitBoundary(store).commit(
        base=base,
        candidate=first,
        origin="frontend_edit",
    )
    second = base.project.model_dump(mode="json")
    second["name"] = "Name B"

    with pytest.raises(JsonCasConflict) as caught:
        ProjectCommitBoundary(store).commit(
            base=base,
            candidate=second,
            origin="runtime_task",
        )
    assert caught.value.conflicts[0]["pointer"] == "/name"


def test_identical_retry_archives_safe_aborted_transaction(
    tmp_path,
    monkeypatch,
) -> None:
    store, base = make_store(tmp_path)
    boundary = ProjectCommitBoundary(store)
    candidate = base.project.model_dump(mode="json")
    candidate["name"] = "Retried"
    original_replace = store.replace

    def fail_before_publish(*args, **kwargs):
        raise RuntimeError("injected pre-publish failure")

    monkeypatch.setattr(store, "replace", fail_before_publish)
    with pytest.raises(RuntimeError, match="pre-publish"):
        boundary.commit(
            base=base,
            candidate=candidate,
            origin="runtime_task",
            round_id="retry-round",
            transaction_id="retry-transaction",
        )
    monkeypatch.setattr(store, "replace", original_replace)

    result = boundary.commit(
        base=base,
        candidate=candidate,
        origin="runtime_task",
        round_id="retry-round",
        transaction_id="retry-transaction",
    )

    assert result.snapshot.project.name == "Retried"
    assert result.snapshot.generation == 1
    history = runtime_root(store) / "transaction-history" / "aborted"
    archived = list(history.iterdir())
    assert len(archived) == 1
    assert archived[0].name.startswith("retry-transaction.")


def test_commit_boundary_enforces_field_block_at_publish_time(
    tmp_path,
) -> None:
    store, base = make_store(tmp_path)
    blocks = FieldBlockStore(runtime_root(store) / "locks" / "fields")
    block = blocks.acquire(
        project_id="project-1",
        json_pointer="/name",
        owner_kind="user",
        owner_id="browser-1",
        base_field_hash="sha256:before",
        ttl_seconds=30,
    )
    candidate = base.project.model_dump(mode="json")
    candidate["name"] = "Blocked"

    with pytest.raises(FieldBlockConflictError):
        ProjectCommitBoundary(store).commit(
            base=base,
            candidate=candidate,
            origin="runtime_task",
        )
    assert store.read("project-1") == base

    result = ProjectCommitBoundary(store).commit(
        base=base,
        candidate=candidate,
        origin="frontend_edit",
        block_token=block.token,
    )
    assert result.snapshot.project.name == "Blocked"


def test_candidate_cannot_change_runtime_metadata(tmp_path) -> None:
    store, base = make_store(tmp_path)
    candidate = base.project.model_dump(mode="json")
    candidate["generation"] = 99

    with pytest.raises(ProtectedFieldError) as caught:
        ProjectCommitBoundary(store).commit(
            base=base,
            candidate=candidate,
            origin="runtime_task",
        )
    assert caught.value.pointers == ["/generation"]


def test_character_voice_binding_is_runtime_only(tmp_path) -> None:
    """Only Runtime tasks may write voice bindings; agents cannot fabricate
    a voice_id through generic JSON edits after a failed enrollment."""

    store, base = make_store(tmp_path)
    seeded = base.project.model_dump(mode="json")
    seeded["visual"]["entities"]["items"]["char:hero"] = {
        "entity_id": "char:hero",
        "kind": "character",
        "name": "Hero",
        "description": "",
        "continuity": "",
        "required_variant_ids": [],
        "variants": {"items": {}, "order": []},
        "selected_artifact_version_id": None,
        "voice": None,
    }
    seeded["visual"]["entities"]["order"] = ["char:hero"]
    base = (
        ProjectCommitBoundary(store)
        .commit(
            base=base,
            candidate=seeded,
            origin="runtime_task",
        )
        .snapshot
    )

    candidate = base.project.model_dump(mode="json")
    binding = {
        "voice_id": "fabricated-voice-001",
        "target_model": "qwen3-tts-vc",
        "preferred_name": "hero",
        "sample_source_version_id": None,
        "enrollment_key": "k",
        "created_at": "2026-07-30T00:00:00Z",
    }
    candidate["visual"]["entities"]["items"]["char:hero"]["voice"] = binding

    for origin in ("agentdock_interrupt", "frontend_edit"):
        with pytest.raises(ProtectedFieldError):
            ProjectCommitBoundary(store).commit(
                base=base,
                candidate=candidate,
                origin=origin,
            )

    # The enrollment executor path (a Runtime task) stays allowed.
    result = ProjectCommitBoundary(store).commit(
        base=base,
        candidate=candidate,
        origin="runtime_task",
    )
    written = result.snapshot.project.visual.entities.items["char:hero"]
    assert written.voice is not None
    assert written.voice.voice_id == "fabricated-voice-001"


def test_commit_ids_cannot_escape_runtime_directories(tmp_path) -> None:
    store, base = make_store(tmp_path)
    candidate = base.project.model_dump(mode="json")
    candidate["name"] = "Unsafe"
    outside = tmp_path / "escaped"

    for arguments in (
        {"transaction_id": "../escaped"},
        {"transaction_id": "safe", "round_id": "x/../../escaped"},
        {"transaction_id": "safe", "round_id": "bad\\segment"},
        {"transaction_id": "safe", "round_id": "bad\x00segment"},
    ):
        with pytest.raises(ProjectCommitError, match="unsafe Runtime path"):
            ProjectCommitBoundary(store).commit(
                base=base,
                candidate=candidate,
                origin="runtime_task",
                **arguments,
            )

    assert not outside.exists()


def test_only_agentdock_active_interrupt_creates_review(tmp_path) -> None:
    store, base = make_store(tmp_path)
    candidate = base.project.model_dump(mode="json")
    candidate["description"] = "AgentDock requested change"

    result = ProjectCommitBoundary(store).commit(
        base=base,
        candidate=candidate,
        round_id="round-2",
        **review_commit_kwargs(review_boundary(base)),
    )

    assert result.review is not None
    ops = result.review.operations
    assert [o.json_pointer for o in ops] == ["/description"]
    assert read_review(store, "round-2").request_id == "request-2"
    state = read_state(store)
    assert state.accepted_generation == 0
    assert state.last_project_generation == 1


def test_user_edit_supersedes_pending_operation_in_coexisting_reviews(
    tmp_path,
) -> None:
    # Multiple pending reviews coexist; a direct user edit must supersede
    # the touched operation even in a review that active_round_id does not
    # currently point at.
    store, base = make_store(tmp_path)
    commit = ProjectCommitBoundary(store)
    first_candidate = base.project.model_dump(mode="json")
    first_candidate["description"] = "Reviewed description"
    first = commit.commit(
        base=base,
        candidate=first_candidate,
        round_id="round-1",
        **review_commit_kwargs(review_boundary(base)),
    )
    second_candidate = first.snapshot.project.model_dump(mode="json")
    second_candidate["name"] = "Reviewed name"
    second = commit.commit(
        base=first.snapshot,
        candidate=second_candidate,
        round_id="round-2",
        **review_commit_kwargs(review_boundary(base, seq=3)),
    )

    # Both candidates publish live; both reviews pend; the baseline holds.
    assert second.review is not None
    current = store.read("project-1")
    assert current.generation == 2
    assert current.project.name == "Reviewed name"
    assert current.project.description == "Reviewed description"
    state = read_state(store)
    assert state.active_round_id == "round-1"
    assert state.accepted_generation == 0
    for round_id in ("round-1", "round-2"):
        assert read_review(store, round_id).status is ReviewStatus.PENDING

    edited = second.snapshot.project.model_dump(mode="json")
    edited["description"] = "User rewrote the description by hand"
    commit.commit(
        base=second.snapshot,
        candidate=edited,
        origin="frontend_edit",
        round_id="round-user-edit",
    )

    first_review = read_review(store, "round-1")
    assert first_review.status is ReviewStatus.RESOLVED
    assert [operation.decision for operation in first_review.operations] == [
        ReviewOperationDecision.SUPERSEDED_BY_USER_EDIT,
    ]
    assert read_review(store, "round-2").status is ReviewStatus.PENDING
    state = read_state(store)
    # round-2 still pends: baseline holds; pointer moves to live review.
    assert state.active_round_id == "round-2"
    assert state.accepted_generation == 0


def test_runtime_writer_rebases_pending_operation_candidate(
    tmp_path,
) -> None:
    # An auto-fix touching a reviewed pointer must rebase the pending
    # operation's candidate hash, or Keep/Undo would fail CAS forever.
    store, base = make_store(tmp_path)
    commit = ProjectCommitBoundary(store)
    reviewed = base.project.model_dump(mode="json")
    reviewed["description"] = "Reviewed description"
    first = commit.commit(
        base=base,
        candidate=reviewed,
        round_id="round-1",
        **review_commit_kwargs(review_boundary(base)),
    )

    runtime_edit = first.snapshot.project.model_dump(mode="json")
    runtime_edit["description"] = "Runtime refined description"
    commit.commit(
        base=first.snapshot,
        candidate=runtime_edit,
        origin="runtime_task",
        round_id="round-runtime-fix",
    )

    review = read_review(store, "round-1")
    assert review.status is ReviewStatus.PENDING
    (operation,) = review.operations
    assert operation.decision is ReviewOperationDecision.PENDING
    assert operation.after == "Runtime refined description"

    # Rejecting after the rebase must restore the pre-review value.
    resolved = ProjectReviewService(store).decide(
        project_id="project-1",
        review_id=review.review_id,
        decision_token=review.decision_token,
        decisions=[
            ReviewDecisionItem(
                operation_id=operation.operation_id,
                decision="REJECT",
            ),
        ],
    )
    assert resolved.status is ReviewStatus.RESOLVED
    assert store.read("project-1").project.description == ""


def test_transaction_completion_checks_are_disabled() -> None:
    """validate_completion is intentionally fail-open for now: both a
    coherent snapshot and one violating the atomic checks must pass."""
    from dataclasses import replace

    from services.validators.base import ValidationIssue, ValidationReport
    from services.validators.transaction import (
        CompletionSnapshot,
        validate_completion,
    )

    valid = CompletionSnapshot(
        transaction_id="tx-1",
        status="ACTIVE",
        current_working_head="head-4",
        expected_working_head="head-4",
        last_consumed_message_seq=12,
        user_message_high_water=12,
        queued_user_message_count=0,
        pending_outbox_count=0,
        specialist_run_statuses=("SUCCEEDED", "BLOCKED"),
        task_statuses=("SUCCEEDED", "QUARANTINED"),
        unimported_result_count=0,
        active_lease_count=0,
        requested_consistency_report_id="report-1",
        current_consistency_report_id="report-1",
        consistency_report_working_head="head-4",
        consistency_report_marker="SUCCESS",
        domain_reports=(ValidationReport(),),
        journal_matches_tree=True,
        seal_cas_unchanged=True,
    )
    assert validate_completion(valid).valid

    broken = replace(
        valid,
        current_working_head="head-5",
        queued_user_message_count=1,
        pending_outbox_count=1,
        task_statuses=("RUNNING",),
        active_lease_count=1,
        domain_reports=(
            ValidationReport(
                (ValidationIssue("R2V_STORYBOARD_REQUIRED", "missing"),),
            ),
        ),
        journal_matches_tree=False,
        seal_cas_unchanged=False,
    )
    assert validate_completion(broken).valid


def _tools(tmp_path) -> AgentProjectTools:
    store = ProjectStore(tmp_path.resolve())
    store.create(Project.new(project_id="project-1", name="Initial"))
    tools = AgentProjectTools(
        store,
        context=AgentProjectToolContext(
            origin="runtime_task",
            caused_by_request_id="request-1",
            caused_by_message_seq=1,
        ),
    )
    tools.invoke("read_project", {"projectId": "project-1"})
    return tools


def _entity(entity_id: str) -> dict:
    return {
        "entity_id": entity_id,
        "kind": "character",
        "name": "Hero",
        "required_variant_ids": [],
    }


@pytest.mark.unit
def test_dotted_paths_are_a_lossless_alias():
    """Field trip 2026-08-05: the model wrote collection='visual.entities'
    (jq muscle memory) and burned the whole run on a formality. Dots with
    no slash convert to a pointer without losing information, so they must
    just work — for pointer ops and for upsert collections alike."""
    document = Project.new(project_id="p", name="n").model_dump(mode="json")

    apply_patch_ops(
        document,
        [
            {"op": "replace", "path": "description", "value": "点分也行"},
            {
                "op": "upsert_entity",
                "collection": "visual.entities",
                "id": "char:hero",
                "value": _entity("char:hero"),
            },
        ],
    )
    assert document["description"] == "点分也行"
    assert document["visual"]["entities"]["order"] == ["char:hero"]

    # Dotted aliases still cannot sidestep protected pointers.
    with pytest.raises(PatchOpError, match="保护字段"):
        apply_patch_ops(
            document,
            [{"op": "replace", "path": "generation", "value": 99}],
        )

    # Mixed shapes stay ambiguous and are refused with the field name.
    with pytest.raises(PatchOpError, match="RFC 6901"):
        apply_patch_ops(
            document,
            [
                {
                    "op": "replace",
                    "path": "visual/entities.items",
                    "value": {},
                },
            ],
        )


@pytest.mark.unit
def test_invoke_commits_flat_ops_end_to_end(tmp_path):
    tools = _tools(tmp_path)

    result = tools.invoke(
        "patch_project",
        {
            "projectId": "project-1",
            "ops": [
                {"op": "replace", "path": "/name", "value": "Patched"},
                {
                    "op": "upsert_entity",
                    "collection": "/visual/entities",
                    "id": "char:hero",
                    "value": _entity("char:hero"),
                },
            ],
        },
    )

    assert result["project"]["name"] == "Patched"
    entities = result["project"]["visual"]["entities"]
    assert entities["order"] == ["char:hero"]
    assert "/name" in result["changedPointers"]


@pytest.mark.unit
def test_invoke_accepts_double_encoded_ops_string(tmp_path):
    """Field trip 2026-08-05: the model sent ops as a JSON string on its
    first ever call. The string parses to the exact list, so decoding it
    is lossless and must not burn a retry turn."""
    tools = _tools(tmp_path)

    ops = [{"op": "replace", "path": "/name", "value": "双重编码"}]
    result = tools.invoke(
        "patch_project",
        {"projectId": "project-1", "ops": "\n" + json.dumps(ops) + "\n"},
    )

    assert result["project"]["name"] == "双重编码"

    # A string that is not a JSON list keeps the loud schema failure.
    with pytest.raises(AgentProjectToolError):
        tools.invoke(
            "patch_project",
            {"projectId": "project-1", "ops": '{"op": "replace"}'},
        )


@pytest.mark.unit
def test_invoke_repairs_stringified_ops_with_a_bracket_slip(tmp_path):
    """Second field trip 2026-08-05: the stringified array itself carried
    a missing comma, so strict decode bounced it with a misleading
    'must not be a string' and the model resent verbatim into the
    breaker. json_repair recovers structure-only slips."""
    tools = _tools(tmp_path)

    # Two ops with the separating comma dropped — the shape of the real
    # payload's defect.
    broken = (
        '[{"op": "replace", "path": "/name", "value": "修复后"}'
        ' {"op": "replace", "path": "/description", "value": "缺逗号"}]'
    )
    result = tools.invoke(
        "patch_project",
        {"projectId": "project-1", "ops": broken},
    )

    assert result["project"]["name"] == "修复后"
    assert result["project"]["description"] == "缺逗号"


@pytest.mark.unit
def test_invoke_reports_bad_op_without_committing(tmp_path):
    tools = _tools(tmp_path)

    with pytest.raises(AgentProjectToolError) as caught:
        tools.invoke(
            "patch_project",
            {
                "projectId": "project-1",
                "ops": [
                    {"op": "replace", "path": "/missing-field", "value": 1},
                ],
            },
        )
    assert caught.value.code == "PATCH_OPS_INVALID"
    assert "ops[0]" in str(caught.value)

    unchanged = tools.invoke("read_project", {"projectId": "project-1"})
    assert unchanged["project"]["name"] == "Initial"


@pytest.mark.unit
def test_invoke_translates_schema_failures(tmp_path):
    tools = _tools(tmp_path)

    with pytest.raises(AgentProjectToolError) as caught:
        tools.invoke(
            "patch_project",
            {
                "projectId": "project-1",
                "ops": [
                    {
                        "op": "upsert_entity",
                        "collection": "/visual/entities",
                        "id": "char:hero",
                        # Identity mismatch: schema validation must reject
                        # and the project stays unchanged.
                        "value": _entity("char:other"),
                    },
                ],
            },
        )
    assert caught.value.code == "PATCH_PROJECT_SCHEMA_INVALID"


def _r2v_element(element_id: str, narrative: str = "叙事") -> dict:
    return {
        "element_id": element_id,
        "label": element_id,
        "span": {"start_tick": 0, "duration_tick": 4000},
        "location": {},
        "creation": {
            "type": "r2v",
            "narrative": narrative,
            "storyboard_prompt": "分镜 prompt",
            "video_prompt": "视频 prompt",
        },
    }


@pytest.mark.unit
@pytest.mark.parametrize("tool_name", ["patch_project", "jq_project"])
def test_misplaced_video_prompt_reports_repair_without_committing(
    tmp_path,
    tool_name,
):
    tools = _tools(tmp_path)
    base = tools.store.read("project-1")
    path = "/timelines/items/timeline:main/elements_by_id/elem:1"
    element = _r2v_element("elem:1")
    element["video_prompt"] = element["creation"].pop("video_prompt")

    def write():
        arguments = (
            {"ops": [{"op": "add", "path": path, "value": element}]}
            if tool_name == "patch_project"
            else {
                "program": '.timelines.items["timeline:main"]'
                '.elements_by_id["elem:1"] = $element',
                "jsonArgs": {"element": element},
            }
        )
        return tools.invoke(tool_name, {"projectId": "project-1", **arguments})

    with pytest.raises(AgentProjectToolError) as caught:
        write()
    message = str(caught.value)
    assert path + "/creation/video_prompt" in message
    assert tool_name in message
    if tool_name == "patch_project":
        assert "请修正 ops" in message
        assert "program/jsonArgs" not in message
    assert tools.store.read("project-1").etag == base.etag

    element["creation"]["video_prompt"] = element.pop("video_prompt")
    saved = write()["project"]["timelines"]["items"]["timeline:main"]
    assert (
        saved["elements_by_id"]["elem:1"]["creation"]["video_prompt"]
        == "视频 prompt"
    )


@pytest.mark.unit
def test_frozen_snapshot_edits_fail_closed(tmp_path):
    """Element writes into an existing snapshot are rejected, not stranded.

    Snapshots never enter the work graph, so silently accepting such a
    commit leaves content that can never be produced — the incident shape
    behind agents spinning on "my edit had no effect"."""
    tools = _tools(tmp_path)
    main = "/timelines/items/timeline:main/elements_by_id/elem:1"
    tools.invoke(
        "patch_project",
        {
            "projectId": "project-1",
            "ops": [
                {"op": "add", "path": main, "value": _r2v_element("elem:1")},
            ],
        },
    )
    edited = tools.invoke(
        "patch_project",
        {
            "projectId": "project-1",
            "ops": [
                {
                    "op": "replace",
                    "path": f"{main}/creation/narrative",
                    "value": "第二版",
                },
            ],
        },
    )
    sid = "snapshot:timeline:main:1"
    assert sid in edited["project"]["timelines"]["items"]

    with pytest.raises(AgentProjectToolError) as caught:
        tools.invoke(
            "patch_project",
            {
                "projectId": "project-1",
                "ops": [
                    {
                        "op": "replace",
                        "path": (
                            f"/timelines/items/{sid}/elements_by_id"
                            f"/{sid}:elem:1/creation/narrative"
                        ),
                        "value": "误写进快照",
                    },
                ],
            },
        )
    assert caught.value.code == "SNAPSHOT_TIMELINE_FROZEN"
    assert sid in str(caught.value)

    unchanged = tools.invoke("read_project", {"projectId": "project-1"})
    frozen = unchanged["project"]["timelines"]["items"][sid]
    assert (
        frozen["elements_by_id"][f"{sid}:elem:1"]["creation"]["narrative"]
        == "叙事"
    )
