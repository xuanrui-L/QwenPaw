# -*- coding: utf-8 -*-
# Pytest fixtures and contract probes retain exact types and private seams.
# pylint: disable=protected-access
# pylint: disable=use-implicit-booleaness-not-comparison
"""
Explicit WorkGraph admission uses real durable approvals; providers are mocked.
"""

import asyncio
from types import SimpleNamespace

import pytest

from services.file_agent_runtime import (
    AgentModelTurn,
    AgentToolCall,
    CallbackAgentChatClient,
    FileCreatorAgentRuntime,
)
from services.file_agent_runtime import driver as dm
from services.file_agent_runtime import work_scheduler as sm
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project, VisualEntity, VisualVariant
from services.runtime_files.execution_models import (
    TaskRecord,
    ExecutionAuthorizationStatus,
)
from domain.enums import TaskKind, TaskStatus


async def wait_for(predicate, seconds=12):
    limit = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > limit:
            raise AssertionError("probe wait timed out")
        await asyncio.sleep(0.01)


def create(temporary, count=1):
    services = CreatorFileServices.create(temporary.resolve())
    project = Project.new(project_id="probe-project", name="Independent probe")
    for index in range(count):
        entity_id = "hero" if index == 0 else f"hero-{index}"
        project.visual.entities.items[entity_id] = VisualEntity(
            entity_id=entity_id,
            kind="character",
            name=f"Hero {index}",
            required_variant_ids=["base"],
            variants={
                "items": {
                    "base": VisualVariant(
                        variant_id="base",
                        prompt=f"Adult {index} in a grey coat.",
                    ),
                },
                "order": ["base"],
            },
        )
        project.visual.entities.order.append(entity_id)

    def initialize(staged):
        services.sessions.initialize_staged_project(
            staged,
            "probe-project",
            session_id="probe-session",
            conversation_id="probe-conversation",
            initial_goal="Generate only this character.",
            goal_id="probe-goal",
            initial_message_id="probe-message",
            initial_client_message_id="probe-client",
        )

    snapshot = services.projects.create(
        project,
        initialize_staged_project=initialize,
    )
    services.poller.note_commit(snapshot)
    return services


def pin(monkeypatch):
    monkeypatch.setattr(
        dm,
        "get_execution_authorization_mode",
        lambda: "required",
    )
    monkeypatch.setattr(
        sm,
        "get_execution_authorization_mode",
        lambda: "required",
    )
    monkeypatch.setattr(dm, "get_creation_checkpoint_mode", lambda: "skip")
    monkeypatch.setattr(dm, "get_media_review_mode", lambda: "required")
    monkeypatch.setattr(
        dm,
        "_execution_provider_model",
        lambda *_args, **_kw: ("probe-provider", "probe-model"),
    )


def test_required_approval_and_repeated_tool_only_one_real_admission(
    tmp_path,
    monkeypatch,
):
    pin(monkeypatch)

    async def scenario():
        services = create(tmp_path)
        turns = 0

        async def model(_messages, _tools):
            nonlocal turns
            turns += 1
            if turns <= 2:
                return AgentModelTurn(
                    tool_calls=(
                        AgentToolCall(
                            call_id=f"probe-call-{turns}",
                            name="request_workgraph_execution",
                            arguments={
                                "projectId": "probe-project",
                                "targetRefs": ["asset:hero"],
                                "kinds": ["visual"],
                            },
                        ),
                    ),
                )
            return AgentModelTurn(content="Complete.")

        runtime = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(model),
            poll_interval_seconds=0.01,
        )
        calls = []

        fake_dispatch(runtime, calls)
        try:
            await runtime.start()
            runtime.notify("probe-project")
            await wait_for(
                lambda: len(
                    runtime.executions.list_execution_authorizations(
                        "probe-project",
                    ),
                )
                == 1,
            )
            assert calls == [], "admission happened before approval"
            authorization = runtime.executions.list_execution_authorizations(
                "probe-project",
            )[0]
            approve(runtime, authorization)
            await wait_for(lambda: turns >= 3)
            await runtime.wait_until_idle("probe-project")
            assert len(calls) == 1
            assert (
                len(
                    runtime.executions.list_execution_authorizations(
                        "probe-project",
                    ),
                )
                == 1
            )
            assert len(runtime.executions.list_tasks("probe-project")) == 1
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_restart_needs_new_main_request_to_execute_old_approval(
    tmp_path,
    monkeypatch,
):
    pin(monkeypatch)

    async def scenario():
        services = create(tmp_path)

        async def request_model(_messages, _tools):
            return AgentModelTurn(
                tool_calls=(
                    AgentToolCall(
                        call_id="parked-call",
                        name="request_workgraph_execution",
                        arguments={
                            "projectId": "probe-project",
                            "targetRefs": ["asset:hero"],
                            "kinds": ["visual"],
                        },
                    ),
                ),
            )

        original = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(request_model),
            poll_interval_seconds=0.01,
        )
        calls = []

        async def forbidden_dispatch(*_args, **_kwargs):
            calls.append("called")
            raise AssertionError("No dispatch should occur during this probe")

        original.work_scheduler.dispatch_node = forbidden_dispatch
        await original.start()
        original.notify("probe-project")
        await wait_for(
            lambda: len(
                original.executions.list_execution_authorizations(
                    "probe-project",
                ),
            )
            == 1,
        )
        authorization = original.executions.list_execution_authorizations(
            "probe-project",
        )[0]
        await original.stop()
        approve(original, authorization)

        async def idle_model(_messages, _tools):
            return AgentModelTurn(content="No new media request.")

        restarted = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(idle_model),
            poll_interval_seconds=0.01,
        )
        restarted.work_scheduler.dispatch_node = forbidden_dispatch
        try:
            await restarted.start()
            await asyncio.sleep(0.1)
            assert calls == []
            assert restarted.executions.list_tasks("probe-project") == []
            assert (
                len(
                    restarted.executions.list_execution_authorizations(
                        "probe-project",
                    ),
                )
                == 1
            )
        finally:
            await restarted.stop()

    asyncio.run(scenario())


def approve(runtime, record, reject=False):
    runtime.executions.decide_execution_authorization(
        "probe-project",
        record.authorization_id,
        authorization_token=record.authorization_token,
        status=(
            ExecutionAuthorizationStatus.REJECTED
            if reject
            else ExecutionAuthorizationStatus.APPROVED
        ),
        decision={
            "provider": record.requested_provider,
            "model": record.requested_model,
            "maxCost": 0,
            "maxCandidates": 1,
        },
    )


def change_project(services, change):
    from datetime import datetime, timezone

    snapshot = services.projects.read("probe-project")
    project = snapshot.project.model_copy(deep=True)
    change(project)
    project.generation += 1
    project.updated_at = datetime.now(timezone.utc)
    return services.projects.replace("probe-project", project, snapshot.etag)


def direct_request(runtime, targets=None, kinds=("visual",)):
    from services.runtime_files.models import ReviewPolicy

    runtime._epochs.setdefault("probe-project", 0)
    return runtime._run_mainline_workgraph_execution(
        project_id="probe-project",
        session_id="probe-session",
        run_id="probe-main",
        epoch=0,
        request=runtime.services.sessions.list_messages(
            "probe-project",
            "probe-session",
        )[0],
        tools=SimpleNamespace(
            context=SimpleNamespace(
                round_id="probe-round",
                caused_by_request_id="probe-request",
                review_policy=ReviewPolicy.AUTO_FIX,
            ),
        ),
        call_id="one-main-call",
        arguments={
            "projectId": "probe-project",
            "targetRefs": targets or ["asset:hero"],
            **({"kinds": list(kinds)} if kinds is not None else {}),
        },
    )


def fake_dispatch(runtime, calls, publish=False):
    async def dispatch(project_id, node, fingerprint, **kwargs):
        snapshot = runtime.services.projects.read(project_id)
        assert kwargs["expected_object_versions"] == (
            f"project:{snapshot.etag}:work-graph",
        )
        calls.append(node.node_id)
        slot = runtime.work_scheduler._dispatch_slot(fingerprint)
        key = f"dag-{node.node_id}-{slot}"
        task = runtime.executions.create_task(
            TaskRecord(
                task_id=f"paid-task-{len(calls)}",
                project_id=project_id,
                kind=TaskKind.IMAGE_GENERATION,
                status=TaskStatus.QUEUED,
                request_fingerprint="probe-input",
                idempotency_key=key,
                caused_by_request_id=key,
                metadata={"targetRef": node.target_ref},
            ),
        )
        runtime.executions.transition_task(
            project_id,
            task.task_id,
            expected_status=TaskStatus.QUEUED,
            status=TaskStatus.RUNNING,
        )
        if publish:
            # Publish an unrelated sibling head while other approvals remain
            # pending.
            change_project(
                runtime.services,
                lambda p: setattr(
                    p,
                    "description",
                    f"Published sibling {len(calls)}",
                ),
            )
        runtime.executions.transition_task(
            project_id,
            task.task_id,
            expected_status=TaskStatus.RUNNING,
            status=TaskStatus.SUCCEEDED,
        )
        return SimpleNamespace(task_id=task.task_id)

    runtime.work_scheduler.dispatch_node = dispatch


@pytest.mark.unit
@pytest.mark.parametrize("decision", ["approve", "reject", "skip"])
def test_workgraph_design_checkpoint_controls_media_admission(
    tmp_path,
    monkeypatch,
    decision,
):
    from media_files.conftest import make_r2v_element
    from models import config

    pin(monkeypatch)
    monkeypatch.setattr(
        dm,
        "get_execution_authorization_mode",
        lambda: "allow_all",
    )
    monkeypatch.setattr(
        sm,
        "get_execution_authorization_mode",
        lambda: "allow_all",
    )
    monkeypatch.setattr(
        dm,
        "get_creation_checkpoint_mode",
        lambda: "skip" if decision == "skip" else "required",
    )
    monkeypatch.setattr(config, "get_execution_mode", lambda: "co_creation")

    async def scenario():
        services = create(tmp_path, count=0)
        change_project(
            services,
            lambda p: p.timelines.items["timeline:main"].elements_by_id.update(
                {"shot-1": make_r2v_element("shot-1")},
            ),
        )
        runtime = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(lambda *_: None),
            poll_interval_seconds=0.01,
        )
        calls = []
        fake_dispatch(runtime, calls)
        pending = asyncio.create_task(
            direct_request(runtime, ["element:shot-1"], kinds=("storyboard",)),
        )
        try:
            if decision != "skip":
                await wait_for(
                    lambda: bool(
                        runtime.executions.list_execution_authorizations(
                            "probe-project",
                        ),
                    ),
                )
                (record,) = runtime.executions.list_execution_authorizations(
                    "probe-project",
                )
                assert record.operation == "creation_checkpoint_design"
                assert record.status is ExecutionAuthorizationStatus.PENDING
                assert "设计检查点" in record.summary
                assert not pending.done() and calls == []
                assert runtime.executions.list_tasks("probe-project") == []
                approve(runtime, record, reject=decision == "reject")
            if decision == "reject":
                with pytest.raises(dm.CreationCheckpointBlocked) as rejected:
                    await asyncio.wait_for(pending, 5)
                assert "创作检查点" in str(rejected.value)
                assert "不要重试生成" in rejected.value.recovery()
                assert calls == []
                assert runtime.executions.list_tasks("probe-project") == []
            else:
                assert (await asyncio.wait_for(pending, 5))[
                    "status"
                ] == "COMPLETED"
                assert calls == ["storyboard:shot-1"]
                (task,) = runtime.executions.list_tasks("probe-project")
                assert task.status is TaskStatus.SUCCEEDED
            if decision == "skip":
                assert (
                    runtime.executions.list_execution_authorizations(
                        "probe-project",
                    )
                    == []
                )
            assert (
                runtime.executions.list_specialist_runs("probe-project") == []
            )
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await runtime.stop()

    asyncio.run(scenario())


def test_three_approvals_survive_completed_sibling_project_commits(
    tmp_path,
    monkeypatch,
):
    pin(monkeypatch)
    monkeypatch.setattr(dm, "get_creation_checkpoint_mode", lambda: "required")
    from models import config

    monkeypatch.setattr(config, "get_execution_mode", lambda: "co_creation")

    async def scenario():
        services = create(tmp_path, count=3)
        runtime = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(lambda *_: None),
            poll_interval_seconds=0.01,
        )
        calls = []
        fake_dispatch(runtime, calls, publish=True)
        targets = ["asset:hero", "asset:hero-1", "asset:hero-2"]
        pending = asyncio.create_task(direct_request(runtime, targets))
        records = lambda: runtime.executions.list_execution_authorizations(
            "probe-project",
        )
        await wait_for(lambda: len(records()) == 3)
        assert not any(
            r.operation == "creation_checkpoint_plan" for r in records()
        )
        billing = [r for r in records() if r.operation == "image_generation"]
        assert len(billing) == 3 and all(
            r.status == ExecutionAuthorizationStatus.PENDING for r in billing
        )
        assert calls == []
        assert runtime.executions.list_specialist_runs("probe-project") == []
        initial = services.projects.read("probe-project").etag
        for index, record in enumerate(billing, start=1):
            approve(runtime, record)
            await wait_for(
                lambda expected=index: len(
                    [
                        t
                        for t in runtime.executions.list_tasks("probe-project")
                        if t.status == TaskStatus.SUCCEEDED
                    ],
                )
                == expected,
            )
        result = await asyncio.wait_for(pending, 5)
        assert result["status"] == "COMPLETED" and len(calls) == 3
        assert services.projects.read("probe-project").etag != initial
        required = [
            e
            for e in services.sessions.list_events(
                "probe-project",
                "probe-session",
            )
            if e.event_type == "execution.authorization_required"
        ]
        assert len(required) == 3
        assert {e.payload["toolCallId"] for e in required} == {"one-main-call"}
        assert len({e.payload["authorizationId"] for e in required}) == 3
        await direct_request(runtime, targets)
        assert len(records()) == 3 and len(calls) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    ["prompt", "ratio", "model", "review", "reject", "stop"],
)
def test_approved_request_cannot_cross_changed_terms_or_gates(
    tmp_path,
    monkeypatch,
    change,
):
    pin(monkeypatch)

    async def scenario():
        services = create(tmp_path)
        runtime = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(lambda *_: None),
            poll_interval_seconds=0.01,
        )
        calls = []
        fake_dispatch(runtime, calls)
        pending = asyncio.create_task(direct_request(runtime))
        await wait_for(
            lambda: len(
                runtime.executions.list_execution_authorizations(
                    "probe-project",
                ),
            )
            == 1,
        )
        record = runtime.executions.list_execution_authorizations(
            "probe-project",
        )[0]
        if change == "prompt":
            change_project(
                services,
                lambda p: setattr(
                    p.visual.entities.items["hero"].variants.items["base"],
                    "prompt",
                    "Changed scene",
                ),
            )
        elif change == "ratio":
            change_project(
                services,
                lambda p: setattr(p.settings, "aspect_ratio", "9:16"),
            )
        elif change == "model":
            monkeypatch.setattr(
                dm,
                "_execution_provider_model",
                lambda *_: ("probe-provider", "different-model"),
            )
        elif change == "review":
            monkeypatch.setattr(
                services.reviews,
                "all_pending",
                lambda *_: [object()],
            )
        elif change == "stop":
            runtime._epochs["probe-project"] = 1
        approve(runtime, record, reject=change == "reject")
        if change == "stop":
            with pytest.raises(dm.StaleAgentRun):
                await asyncio.wait_for(pending, 5)
        else:
            assert (await asyncio.wait_for(pending, 5))["status"] == "BLOCKED"
        assert (
            calls == []
            and runtime.executions.list_tasks("probe-project") == []
        )
        if change == "reject":
            assert (await direct_request(runtime))["status"] == "BLOCKED"
            assert (
                len(
                    runtime.executions.list_execution_authorizations(
                        "probe-project",
                    ),
                )
                == 1
            )

    asyncio.run(scenario())


def test_review_blocks_admission_and_allow_all_is_preserved(
    tmp_path,
    monkeypatch,
):
    pin(monkeypatch)
    monkeypatch.setattr(
        dm,
        "get_execution_authorization_mode",
        lambda: "allow_all",
    )

    async def scenario():
        services = create(tmp_path)
        runtime = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(lambda *_: None),
            poll_interval_seconds=0.01,
        )
        calls = []
        fake_dispatch(runtime, calls)
        original = services.reviews.all_pending
        monkeypatch.setattr(
            services.reviews,
            "all_pending",
            lambda *_: [object()],
        )
        assert (await direct_request(runtime))[
            "status"
        ] == "BLOCKED" and calls == []
        monkeypatch.setattr(services.reviews, "all_pending", original)
        assert (await direct_request(runtime))[
            "status"
        ] == "COMPLETED" and len(calls) == 1
        assert (
            runtime.executions.list_execution_authorizations("probe-project")
            == []
        )

    asyncio.run(scenario())


def test_ordered_references_are_bound_to_approval(tmp_path):
    from services.file_agent_runtime.work_graph import derive_work_graph
    from services.file_agent_runtime.workgraph_execution import (
        requested_work_node,
    )

    services = create(tmp_path)
    project = services.projects.read("probe-project").project
    variant = project.visual.entities.items["hero"].variants.items["base"]
    variant.reference_artifact_version_ids = ["artifact-a", "artifact-b"]
    before_node = derive_work_graph(project).by_id["visual:hero:base"]
    before = requested_work_node(SimpleNamespace(project=project), before_node)
    variant.reference_artifact_version_ids.reverse()
    after_node = derive_work_graph(project).by_id["visual:hero:base"]
    after = requested_work_node(SimpleNamespace(project=project), after_node)
    assert before_node.dispatch_fingerprint == after_node.dispatch_fingerprint
    assert before.fingerprint != after.fingerprint
    assert "resolution" not in before.parameters


def test_real_media_publication_review_only_blocks_related_designs(
    tmp_path,
    monkeypatch,
):
    from services.media_files import image_execution
    from services.file_agent_runtime.workgraph_execution import (
        ready_request_context,
    )
    from services.runtime_files.models import ReviewPolicy

    pin(monkeypatch)
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    monkeypatch.setattr(
        image_execution,
        "media_review_policy",
        lambda: ReviewPolicy.REQUIRE_REVIEW,
    )
    monkeypatch.setattr(
        image_execution,
        "reserve_media_review",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        image_execution,
        "schedule_media_review",
        lambda *_args, **_kwargs: None,
    )

    class Provider:
        async def generate(self, **_kwargs):
            return {
                "content": b"\x89PNG\r\n\x1a\n" + b"publication-fixture" * 32,
                "media_type": "image/png",
            }

    async def scenario():
        services = create(tmp_path, count=3)
        worker = image_execution.FileImageExecutionService(
            services,
            provider=Provider(),
        )
        generated = await worker.execute(
            project_id="probe-project",
            command="GENERATE_ASSET",
            target_ref="asset:hero",
            arguments={"variantId": "base"},
            idempotency_key="fixture-publish-one-design",
        )
        reviews = services.reviews.all_pending("probe-project")
        assert len(reviews) == 1 and len(reviews[0].operations) >= 5
        assert any(
            op.json_pointer.endswith("/selected_artifact_version_id")
            for op in reviews[0].operations
        )
        runtime = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(lambda *_: None),
            poll_interval_seconds=0.01,
        )
        _, _, _, blocked = await ready_request_context(
            services,
            runtime.executions,
            "probe-project",
        )
        assert blocked["visual:hero:base"] == "WAITING_REVIEW"
        assert "visual:hero-1:base" not in blocked
        assert "visual:hero-2:base" not in blocked
        # Real pending published pixels may not be consumed as another design's
        # reference.
        change_project(
            services,
            lambda p: setattr(
                p.visual.entities.items["hero-1"].variants.items["base"],
                "reference_artifact_version_ids",
                [generated.artifact_version_id],
            ),
        )
        _, _, _, blocked = await ready_request_context(
            services,
            runtime.executions,
            "probe-project",
        )
        assert blocked["visual:hero-1:base"] == "WAITING_REVIEW"
        assert "visual:hero-2:base" not in blocked
        # An unrelated design can still receive and complete its own approval.
        calls = []
        fake_dispatch(runtime, calls)
        pending = asyncio.create_task(
            direct_request(runtime, ["asset:hero-2"]),
        )
        await wait_for(
            lambda: len(
                runtime.executions.list_execution_authorizations(
                    "probe-project",
                ),
            )
            == 1,
        )
        assert (
            calls == []
            and len(services.reviews.all_pending("probe-project")) == 1
        )
        approve(
            runtime,
            runtime.executions.list_execution_authorizations("probe-project")[
                0
            ],
        )
        assert (await pending)["status"] == "COMPLETED"
        assert calls == ["visual:hero-2:base"]
        # A mixed review is still a full creative gate, even when it contains
        # images.
        from services.runtime_files.models import (
            ReviewOperation,
            ProjectChangeKind,
        )
        from services.runtime_files.atomic_store import AtomicJsonRecordStore
        from services.runtime_files.models import ReviewRecord

        mixed = reviews[0].model_copy(deep=True)
        mixed.operations.append(
            ReviewOperation(
                operation_id="authored-prompt-change",
                kind=ProjectChangeKind.UPDATE,
                json_pointer=(
                    "/visual/entities/items/hero/variants/items/base/prompt"
                ),
                before_hash="before",
                after_hash="after",
                before="old prompt",
                after="new prompt",
            ),
        )
        store = AtomicJsonRecordStore(
            services.projects.project_root("probe-project")
            / "runtime/reviews"
            / mixed.review_id
            / "review.json",
            ReviewRecord,
        )
        store.write(mixed)
        _, _, _, blocked = await ready_request_context(
            services,
            runtime.executions,
            "probe-project",
        )
        assert blocked["visual:hero-2:base"] == "WAITING_REVIEW"

    asyncio.run(scenario())


def compose_fixture(tmp_path):
    import hashlib
    from datetime import datetime, timezone
    from services.project_files.models import (
        IndexedFile,
        SourceAssetVersion,
        TimelineElement,
    )

    services = create(tmp_path, count=0)
    content = b"local-source-fixture"
    relative = "assets/sources/clip/source.mp4"
    path = services.projects.project_root("probe-project") / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()
    now = datetime.now(timezone.utc)

    def install(project):
        project.assets.files_by_id["source-file"] = IndexedFile(
            file_id="source-file",
            kind="source_original",
            relative_uri=relative,
            sha256=checksum,
            size_bytes=len(content),
            media_type="video/mp4",
            created_at=now,
        )
        project.assets.source_versions_by_id[
            "source-version"
        ] = SourceAssetVersion(
            version_id="source-version",
            logical_asset_id="source",
            name="clip.mp4",
            file_id="source-file",
            checksum=checksum,
            media_kind="video",
            media_type="video/mp4",
            duration_seconds=1,
            created_at=now,
        )
        project.timelines.items["timeline:main"].elements_by_id[
            "edit"
        ] = TimelineElement.model_validate(
            {
                "element_id": "edit",
                "location": {},
                "span": {"start_tick": 0, "duration_tick": 1000},
                "creation": {
                    "type": "edit",
                    "intent": "A local cut",
                    "reason": "Use the chosen clip",
                    "original_sound": "preserve",
                },
                "render_source": {
                    "type": "source_asset_version",
                    "version_id": "source-version",
                    "source_in_tick": 0,
                    "source_out_tick": 1000,
                },
            },
        )

    change_project(services, install)
    return services


@pytest.mark.parametrize(
    "gate",
    ["none", "motion", "review", "stale_etag", "render_model"],
)
def test_explicit_local_compose_uses_real_task_without_model_calls(
    tmp_path,
    monkeypatch,
    gate,
):
    from models import config
    from services.media_files import (
        local_execution,
        call_budget,
        motion_design,
    )
    from services.file_agent_runtime import workgraph_execution as bridge

    pin(monkeypatch)
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    monkeypatch.setattr(
        config,
        "is_self_review_enabled",
        lambda: gate == "render_model",
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "Local-only compose must not call media budgets or model design",
        )

    monkeypatch.setattr(bridge, "ensure_media_call_budget", forbidden)
    monkeypatch.setattr(call_budget, "ensure_media_call_budget", forbidden)
    monkeypatch.setattr(motion_design, "design_motion_overlays", forbidden)

    async def scenario():
        services = compose_fixture(tmp_path)
        runtime = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(lambda *_: None),
            poll_interval_seconds=0.01,
        )
        runtime.work_scheduler.dispatch_node = forbidden
        calls = []

        class Runner:
            async def render(self, spec):
                calls.append(spec.target_ref)
                spec.output_path.write_bytes(
                    b"\x00\x00\x00\x18ftypmp42" + b"timeline-video" * 64,
                )
                return {
                    "media_type": "video/mp4",
                    "duration_seconds": spec.expected_duration_seconds,
                }

        original = local_execution.execute_file_local_media_command

        async def execute(*args, **kwargs):
            assert kwargs["command"] == "COMPOSE_FINAL_VIDEO"
            assert kwargs["target_ref"] == "timeline:timeline:main"
            assert kwargs["expected_object_versions"] == (
                f"project:{services.projects.read('probe-project').etag}"
                ":work-graph",
            )
            if gate == "stale_etag":
                change_project(
                    services,
                    lambda p: setattr(p, "description", "Concurrent edit"),
                )
            # Forward the production call unchanged through this test spy.
            # pylint: disable-next=missing-kwoa
            return await original(*args, **kwargs, runner=Runner())

        monkeypatch.setattr(
            local_execution,
            "execute_file_local_media_command",
            execute,
        )
        if gate == "motion":
            from services.project_files.models import TimelineElement

            change_project(
                services,
                lambda p: p.timelines.items[
                    "timeline:main"
                ].elements_by_id.update(
                    {
                        "caption": TimelineElement.model_validate(
                            {
                                "element_id": "caption",
                                "location": {},
                                "span": {
                                    "start_tick": 0,
                                    "duration_tick": 1000,
                                },
                                "creation": {
                                    "type": "overlay",
                                    "text": "Unstyled caption",
                                },
                            },
                        ),
                    },
                ),
            )
        elif gate == "review":
            monkeypatch.setattr(
                services.reviews,
                "all_pending",
                lambda *_: [object()],
            )
        result = await direct_request(
            runtime,
            ["timeline:timeline:main"],
            kinds=None,
        )
        assert (
            runtime.executions.list_execution_authorizations("probe-project")
            == []
        )
        if gate != "none":
            assert result["status"] == "BLOCKED" and calls == []
            assert runtime.executions.list_tasks("probe-project") == []
        else:
            assert result["status"] == "COMPLETED"
            tasks = runtime.executions.list_tasks("probe-project")
            assert (
                len(tasks) == 1
                and tasks[0].kind == TaskKind.COMPOSE
                and tasks[0].status == TaskStatus.SUCCEEDED
            )
            assert len(tasks[0].output_refs) == 1
            assert calls == ["timeline:timeline:main"]
            assert (
                await direct_request(
                    runtime,
                    ["timeline:timeline:main"],
                    kinds=("compose",),
                )
            )["status"] == "COMPLETED"
            assert calls == ["timeline:timeline:main"]
            assert len(runtime.executions.list_tasks("probe-project")) == 1

    asyncio.run(scenario())


def test_local_compose_joins_existing_running_task(tmp_path, monkeypatch):
    from models import config
    from services.media_files import local_execution

    pin(monkeypatch)
    monkeypatch.setattr(config, "is_self_review_enabled", lambda: False)

    async def scenario():
        services = compose_fixture(tmp_path)
        runtime = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(lambda *_: None),
            poll_interval_seconds=0.01,
        )
        release = asyncio.Event()
        started = asyncio.Event()
        calls = []

        class Runner:
            async def render(self, spec):
                calls.append(spec.target_ref)
                started.set()
                await release.wait()
                spec.output_path.write_bytes(
                    b"\x00\x00\x00\x18ftypmp42" + b"timeline-video" * 64,
                )
                return {
                    "media_type": "video/mp4",
                    "duration_seconds": spec.expected_duration_seconds,
                }

        original = local_execution.execute_file_local_media_command

        async def execute(*args, **kwargs):
            # Forward the production call unchanged through this test spy.
            # pylint: disable-next=missing-kwoa
            return await original(*args, **kwargs, runner=Runner())

        monkeypatch.setattr(
            local_execution,
            "execute_file_local_media_command",
            execute,
        )
        first = asyncio.create_task(
            direct_request(
                runtime,
                ["timeline:timeline:main"],
                kinds=("compose",),
            ),
        )
        await asyncio.wait_for(started.wait(), 3)
        second = asyncio.create_task(
            direct_request(
                runtime,
                ["timeline:timeline:main"],
                kinds=("compose",),
            ),
        )
        await asyncio.sleep(0.03)
        assert not second.done()
        assert len(runtime.executions.list_tasks("probe-project")) == 1
        release.set()
        results = await asyncio.gather(first, second)
        assert [result["status"] for result in results] == [
            "COMPLETED",
            "COMPLETED",
        ]
        assert (
            results[0]["items"][0]["taskId"]
            == results[1]["items"][0]["taskId"]
        )
        assert len(calls) == 1

    asyncio.run(scenario())
