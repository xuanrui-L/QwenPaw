# -*- coding: utf-8 -*-
# Fixtures share names; unit tests exercise private admission boundaries.
# pylint: disable=redefined-outer-name,protected-access
"""Manual regeneration, frontend edit holds, and follow-up dispatch."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace
from types import SimpleNamespace

import pytest

from domain.enums import SpecialistRole, TaskKind
from domain.errors import ConflictError, ValidationError
from models import config as creator_config
from services.file_agent_runtime import manual_regeneration_hold as hold
from services.file_agent_runtime import work_scheduler
from services.file_agent_runtime.work_graph import (
    WorkGraph,
    WorkNode,
    WorkNodeStatus,
)
from services.file_agent_runtime.work_scheduler import WorkGraphScheduler
from services.media_files import r2v_execution
from services.media_files.image_execution import FileImageExecutionService
from services.project_files import frontend_edit_hold
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    ElementLocation,
    Project,
    R2VCreation,
    TimelineElement,
    TimelineSpan,
)
from services.project_files.review import ReviewDecisionItem
from services.runtime_files.execution_models import (
    SpecialistRunRecord,
    TaskRecord,
)

pytestmark = pytest.mark.unit
PROJECT_ID = "scheduler-project"


def test_s2v_preflight_failure_without_video_admission_rolls_back_hold(
    env,
    monkeypatch,
):
    from services.media_files import r2v_execution

    services, store = env
    node = replace(_nodes()[1], command="GENERATE_S2V_VIDEO")
    routes = _route_graph(monkeypatch, (_nodes()[0], node, _nodes()[2]))

    async def reject(*_args, **_kwargs):
        raise ValidationError("face not detected")

    monkeypatch.setattr(r2v_execution, "preflight_s2v_face_detect", reject)
    with pytest.raises(ValidationError, match="face not detected"):
        asyncio.run(
            routes.dispatch_work_graph_node(
                PROJECT_ID,
                node.node_id,
                services=services,
            ),
        )
    assert not store.read(PROJECT_ID).node_ids
    assert not hold.ProjectExecutionStore(services.root).list_tasks(PROJECT_ID)


def _nodes():
    return (
        WorkNode(
            node_id="storyboard:a",
            kind="storyboard",
            label="Storyboard",
            status=WorkNodeStatus.DONE,
            target_ref="element:a",
            command="GENERATE_STORYBOARD_IMAGE",
            timeline_id="main",
        ),
        WorkNode(
            node_id="video:a",
            kind="video",
            label="Video",
            status=WorkNodeStatus.STALE,
            deps=("storyboard:a",),
            target_ref="element:a",
            command="GENERATE_R2V_VIDEO",
            regeneration_of="old-video",
            timeline_id="main",
        ),
        WorkNode(
            node_id="compose:main",
            kind="compose",
            label="Compose",
            status=WorkNodeStatus.STALE,
            deps=("video:a",),
            target_ref="timeline:main",
            command="COMPOSE_FINAL_VIDEO",
        ),
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path))
    services = CreatorFileServices.create(tmp_path)
    services.projects.create(Project.new(project_id=PROJECT_ID, name="Hold"))
    return services, hold.ManualRegenerationHoldStore(tmp_path)


def _begin(store, index=0):
    return store.begin(PROJECT_ID, _nodes()[index], _nodes())


def _task(key="request", task_id="task-one"):
    return TaskRecord(
        task_id=task_id,
        project_id=PROJECT_ID,
        kind=TaskKind.IMAGE_GENERATION,
        request_fingerprint="fingerprint",
        idempotency_key=key,
        caused_by_request_id=key,
    )


def _run(key="request"):
    return SpecialistRunRecord(
        run_id="run-one",
        project_id=PROJECT_ID,
        round_id="round-one",
        role=SpecialistRole.VISUAL_DEVELOPMENT,
        input_generation=0,
        input_etag="etag",
        caused_by_request_id=key,
    )


def test_empty_and_initial_generation_do_not_pause(env):
    _, store = env
    assert store.read(PROJECT_ID).payload() == {"revision": 0, "nodeIds": []}
    assert (
        store.begin(
            PROJECT_ID,
            replace(_nodes()[0], status=WorkNodeStatus.READY),
            _nodes(),
        )
        is None
    )
    assert store.read(PROJECT_ID).revision == 0
    assert not (
        store.root / PROJECT_ID / "runtime/manual-regeneration-hold.json"
    ).exists()


def test_existing_output_is_regeneration_even_when_graph_is_ready(env):
    _, store = env
    node = replace(_nodes()[2], status=WorkNodeStatus.READY)
    operation = store.begin(PROJECT_ID, node, _nodes(), existing_output=True)
    assert store.is_held(PROJECT_ID, node.node_id)
    store.admitted(operation)
    assert not store.read(PROJECT_ID).node_ids


def test_untracked_model_admission_cannot_be_certified_as_rejected(env):
    services, store = env
    admission = hold.ManualAdmission(
        store,
        _begin(store),
        PROJECT_ID,
        "request",
    )
    with hold.manual_admission(admission):
        hold.mark_untracked_admission(services.root, PROJECT_ID)
    store.finish(admission, succeeded=False)
    assert store.read(PROJECT_ID).node_ids == {n.node_id for n in _nodes()}


def test_durable_restart_and_no_project_or_state_writes(env):
    services, store = env
    project_root = store.root / PROJECT_ID
    before = (project_root / "project.json").read_bytes()
    state_path = project_root / "runtime/state.json"
    state_before = state_path.read_bytes() if state_path.exists() else None
    op = _begin(store)
    restarted = hold.ManualRegenerationHoldStore(services.root)
    assert restarted.read(PROJECT_ID).node_ids == {n.node_id for n in _nodes()}
    restarted.admitted(op)
    assert restarted.read(PROJECT_ID).node_ids == {"video:a", "compose:main"}
    assert (project_root / "project.json").read_bytes() == before
    assert (
        state_path.read_bytes() if state_path.exists() else None
    ) == state_before


def test_overlapping_admission_releases_only_observed_target(env):
    _, store = env
    first = _begin(store)
    store.admitted(first)
    video = _begin(store, 1)
    assert store.is_held(PROJECT_ID, "video:a")
    later = _begin(store)
    store.admitted(video)
    assert store.is_held(PROJECT_ID, "video:a")
    store.rollback(later)
    assert store.read(PROJECT_ID).node_ids == {"compose:main"}


def test_rollback_preserves_preexisting_and_overlapping_contributions(env):
    _, store = env
    first = _begin(store)
    store.admitted(first)
    second = _begin(store, 1)
    third = _begin(store)
    store.rollback(second)
    assert store.is_held(PROJECT_ID, "video:a")
    store.rollback(third)
    assert store.read(PROJECT_ID).node_ids == {"video:a", "compose:main"}


def test_resume_compare_and_swap(env):
    _, store = env
    _begin(store)
    observed = store.read(PROJECT_ID).revision
    _begin(store)
    with pytest.raises(ConflictError):
        store.resume(PROJECT_ID, observed)
    latest = store.read(PROJECT_ID)
    store.resume(PROJECT_ID, latest.revision)
    assert not store.read(PROJECT_ID).node_ids
    with pytest.raises(ConflictError):
        store.resume(PROJECT_ID, latest.revision)


def test_old_operation_cannot_touch_recreated_project(env):
    services, store = env
    old = _begin(store)
    old_revision = store.read(PROJECT_ID).revision
    shutil.rmtree(services.root / PROJECT_ID)
    services.projects.create(
        Project.new(project_id=PROJECT_ID, name="Recreated"),
    )
    _begin(store)
    with pytest.raises(ConflictError):
        store.admitted(old)
    with pytest.raises(ConflictError):
        store.rollback(old)
    with pytest.raises(ConflictError):
        store.resume(PROJECT_ID, old_revision)
    assert store.is_held(PROJECT_ID, "video:a")


def test_relocated_project_keeps_hold_readable_and_resumable(
    env,
    tmp_path_factory,
):
    services, store = env
    store.admitted(_begin(store))
    expected = store.read(PROJECT_ID).node_ids
    assert expected
    relocated = tmp_path_factory.mktemp("relocated-root")
    shutil.copytree(services.root / PROJECT_ID, relocated / PROJECT_ID)
    moved = hold.ManualRegenerationHoldStore(relocated)
    assert moved.read(PROJECT_ID).node_ids == expected
    moved.resume(PROJECT_ID, moved.read(PROJECT_ID).revision)
    assert not moved.read(PROJECT_ID).node_ids


def test_corrupt_store_fails_closed(env):
    _, store = env
    _begin(store)
    (
        store.root / PROJECT_ID / "runtime/manual-regeneration-hold.json"
    ).write_text("{")
    with pytest.raises(Exception):
        store.is_held(PROJECT_ID, "video:a")
    with pytest.raises(Exception):
        store.resume(PROJECT_ID, 0)


@pytest.mark.parametrize("record_type", ["run", "task"])
def test_matching_durable_admission_never_rolls_back(env, record_type):
    services, store = env
    op = _begin(store)
    admission = hold.ManualAdmission(store, op, PROJECT_ID, "request")
    executions = hold.HoldAwareExecutionStore(services.root)
    with hold.manual_admission(admission):
        if record_type == "run":
            executions.create_run(_run())
        else:
            executions.create_task(_task())
    store.finish(admission, succeeded=False)
    assert store.read(PROJECT_ID).node_ids == {"video:a", "compose:main"}


def test_rejected_scope_blocks_late_thread_admission(env):
    services, store = env
    admission = hold.ManualAdmission(
        store,
        _begin(store),
        PROJECT_ID,
        "request",
    )
    with hold.manual_admission(admission):
        store.finish(admission, succeeded=False)
        with pytest.raises(ConflictError):
            hold.HoldAwareExecutionStore(services.root).create_task(_task())
    assert not store.read(PROJECT_ID).node_ids


def _route_graph(monkeypatch, nodes=None):
    from api import work_graph_routes as routes

    graph = WorkGraph(nodes=nodes or _nodes(), generation=0)
    monkeypatch.setattr(
        routes,
        "derive_work_graph",
        lambda *args, **kwargs: graph,
    )
    return routes


@pytest.mark.parametrize("durable", [False, True])
def test_api_failed_admission_preserves_prior_hold(env, monkeypatch, durable):
    services, store = env
    store.admitted(_begin(store))
    routes = _route_graph(monkeypatch)

    async def dispatch(self, _project_id, node, fingerprint, **_kwargs):
        assert store.is_held(PROJECT_ID, "video:a")
        if durable:
            key = f"dag-{node.node_id}-{self._dispatch_slot(fingerprint)}"
            await asyncio.to_thread(
                hold.HoldAwareExecutionStore(services.root).create_task,
                _task(key),
            )
        raise ValidationError("admission rejected")

    monkeypatch.setattr(WorkGraphScheduler, "dispatch_node", dispatch)
    with pytest.raises(ValidationError):
        asyncio.run(
            routes.dispatch_work_graph_node(
                PROJECT_ID,
                "video:a",
                services=services,
            ),
        )
    expected = {"compose:main"} if durable else {"video:a", "compose:main"}
    assert store.read(PROJECT_ID).node_ids == expected


@pytest.mark.parametrize("failure", ["cancel", "read"])
def test_api_uncertain_admission_stays_held(env, monkeypatch, failure):
    services, store = env
    routes = _route_graph(monkeypatch)

    async def dispatch(*_args, **_kwargs):
        if failure == "cancel":
            raise asyncio.CancelledError()
        monkeypatch.setattr(
            hold.ProjectExecutionStore,
            "list_specialist_runs",
            lambda *a: (_ for _ in ()).throw(OSError("unreadable")),
        )
        raise ValidationError("unknown admission")

    monkeypatch.setattr(WorkGraphScheduler, "dispatch_node", dispatch)
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else ValidationError,
    ):
        asyncio.run(
            routes.dispatch_work_graph_node(
                PROJECT_ID,
                "storyboard:a",
                services=services,
            ),
        )
    assert store.read(PROJECT_ID).node_ids == {n.node_id for n in _nodes()}


def test_api_initial_generation_and_resume_contract(env, monkeypatch):
    services, store = env
    routes = _route_graph(
        monkeypatch,
        (replace(_nodes()[0], status=WorkNodeStatus.READY), *_nodes()[1:]),
    )

    async def dispatch(*_args, **_kwargs):
        assert store.read(PROJECT_ID).revision == 0

    monkeypatch.setattr(WorkGraphScheduler, "dispatch_node", dispatch)
    assert asyncio.run(
        routes.dispatch_work_graph_node(
            PROJECT_ID,
            "storyboard:a",
            services=services,
        ),
    )["ok"]
    store.admitted(_begin(store))
    payload = routes._graph_payload(PROJECT_ID, services)
    assert payload["manualHold"] == store.read(PROJECT_ID).payload()
    assert [n["manuallyHeld"] for n in payload["nodes"]] == [False, True, True]
    wakes = []
    monkeypatch.setattr(
        "services.file_agent_runtime.registry.get_creator_agent_runtime",
        lambda: SimpleNamespace(
            services=services,
            work_scheduler=SimpleNamespace(wake=wakes.append),
        ),
    )
    body = routes.ResumeWorkGraphBody(
        revision=payload["manualHold"]["revision"],
    )
    assert asyncio.run(
        routes.resume_work_graph(PROJECT_ID, body, services),
    ) == {"ok": True}
    assert wakes == [PROJECT_ID]
    with pytest.raises(ConflictError):
        asyncio.run(routes.resume_work_graph(PROJECT_ID, body, services))


def _scheduler(env, monkeypatch, graph):
    services, _ = env
    monkeypatch.setattr(
        work_scheduler,
        "get_execution_authorization_mode",
        lambda: "allow_all",
    )
    monkeypatch.setattr(
        work_scheduler,
        "derive_work_graph",
        lambda *args, **kwargs: graph,
    )
    calls = []

    async def dispatch(*_args, **kwargs):
        calls.append(kwargs)

    scheduler = WorkGraphScheduler(
        services,
        image_dispatch=dispatch,
        r2v_dispatch=dispatch,
    )
    monkeypatch.setattr(scheduler, "wake", lambda *args: None)
    return scheduler, calls


def test_new_scheduler_observes_hold_then_explicit_resume(env, monkeypatch):
    _, store = env
    graph = WorkGraph(
        nodes=(
            _nodes()[0],
            replace(
                _nodes()[1],
                status=WorkNodeStatus.READY,
                regeneration_of=None,
            ),
        ),
        generation=0,
    )
    store.admitted(_begin(store))
    scheduler, calls = _scheduler(env, monkeypatch, graph)

    async def scenario():
        await scheduler.tick(PROJECT_ID)
        assert not calls
        store.resume(PROJECT_ID, store.read(PROJECT_ID).revision)
        await scheduler.tick(PROJECT_ID)
        await asyncio.gather(*scheduler._dispatch_tasks.get(PROJECT_ID, ()))
        await scheduler.shutdown()

    asyncio.run(scenario())
    assert len(calls) == 1


def test_automatic_initial_tick_records_no_hold(env, monkeypatch):
    _, store = env
    graph = WorkGraph(
        nodes=(replace(_nodes()[0], status=WorkNodeStatus.READY),),
        generation=0,
    )
    scheduler, calls = _scheduler(env, monkeypatch, graph)

    async def scenario():
        await scheduler.tick(PROJECT_ID)
        await asyncio.gather(*scheduler._dispatch_tasks.get(PROJECT_ID, ()))
        await scheduler.shutdown()

    asyncio.run(scenario())
    assert len(calls) == 1
    assert store.read(PROJECT_ID).revision == 0


@pytest.mark.parametrize("late", [False, True])
def test_preparation_checks_before_propose_and_accept(env, monkeypatch, late):
    _, store = env
    from services.prompt_sync_service import PromptSyncService

    graph = WorkGraph(
        nodes=(
            _nodes()[0],
            replace(
                _nodes()[1],
                status=WorkNodeStatus.GATED,
                prompt_sync_required=True,
            ),
        ),
        generation=0,
    )
    scheduler, _ = _scheduler(env, monkeypatch, graph)
    calls = []

    async def ready(*_args, **_kwargs):
        return None, None, graph, {"video:a": "GATED"}, {}

    async def propose(*_args, **_kwargs):
        calls.append("propose")
        store.admitted(_begin(store))
        return {"proposalId": "proposal"}

    async def accept(*_args, **_kwargs):
        calls.append("accept")

    monkeypatch.setattr(
        "services.file_agent_runtime.workgraph_execution."
        "ready_request_context",
        ready,
    )
    monkeypatch.setattr(
        PromptSyncService,
        "status",
        lambda *args: {"status": "needs_update", "baselineToken": "baseline"},
    )
    monkeypatch.setattr(PromptSyncService, "propose", propose)
    monkeypatch.setattr(PromptSyncService, "accept", accept)
    if not late:
        store.admitted(_begin(store))
    asyncio.run(scheduler._prepare_changed_prompts(PROJECT_ID, graph))
    assert calls == (["propose"] if late else [])


@pytest.mark.parametrize("record_type", ["run", "task"])
def test_late_hold_enforced_at_durable_automatic_admission(env, record_type):
    services, store = env
    executions = hold.HoldAwareExecutionStore(services.root)

    async def scenario():
        with hold.automatic_node(services.root, PROJECT_ID, "video:a"):
            await asyncio.to_thread(
                hold.check_automatic,
                services.root,
                PROJECT_ID,
            )
            await asyncio.to_thread(_begin, store)
            with pytest.raises(ConflictError):
                if record_type == "run":
                    await asyncio.to_thread(executions.create_run, _run())
                else:
                    await asyncio.to_thread(executions.create_task, _task())
        assert not executions.list_specialist_runs(PROJECT_ID)
        assert not executions.list_tasks(PROJECT_ID)
        executions.create_task(_task())

    asyncio.run(scenario())


def test_automatic_task_retains_scope_for_restart_claim(env):
    services, store = env
    with hold.automatic_node(services.root, PROJECT_ID, "video:a"):
        task = hold.HoldAwareExecutionStore(services.root).create_task(_task())
    _begin(store)
    assert task.metadata["automaticWorkNodeId"] == "video:a"
    with pytest.raises(ConflictError):
        with hold.admission_guard(
            services.root,
            PROJECT_ID,
            node_id=task.metadata["automaticWorkNodeId"],
        ):
            pytest.fail("paid claim must not execute")


def test_recovered_run_cannot_admit_held_automatic_task(env):
    services, store = env
    with hold.automatic_node(services.root, PROJECT_ID, "video:a"):
        run = hold.HoldAwareExecutionStore(services.root).create_run(_run())
    _begin(store)
    task = _task().model_copy(
        update={"run_id": run.run_id, "round_id": run.round_id},
    )
    with pytest.raises(ConflictError):
        hold.HoldAwareExecutionStore(services.root).create_task(task)


def test_image_paid_claim_checks_late_hold(env):
    from services.media_files.image_execution import (
        FileImageExecutionService,
        provider_claim_path,
    )

    services, store = env
    executor = FileImageExecutionService(services, provider=SimpleNamespace())
    with hold.automatic_node(services.root, PROJECT_ID, "storyboard:a"):
        task = executor.executions.create_task(_task())
    _begin(store)
    with pytest.raises(ConflictError):
        asyncio.run(executor._claim_provider(task))
    assert not provider_claim_path(
        services.root / PROJECT_ID,
        task.task_id,
    ).exists()
    store.resume(PROJECT_ID, store.read(PROJECT_ID).revision)
    assert asyncio.run(executor._claim_provider(task))


def test_r2v_unsubmitted_hold_ends_old_request_before_resume(env):
    from services.media_files.r2v_execution import (
        FileR2VExecutionService,
        R2VTaskState,
    )

    services, store = env
    executor = FileR2VExecutionService(services, provider=SimpleNamespace())
    with hold.automatic_node(services.root, PROJECT_ID, "video:a"):
        task = executor.executions.create_task(_task())
    state = R2VTaskState(
        project_id=PROJECT_ID,
        task_id=task.task_id,
        run_id="run-one",
        request_fingerprint=task.request_fingerprint,
        request={},
    )
    executor._state_store(PROJECT_ID, task.task_id).create(state)
    _begin(store)
    assert asyncio.run(executor._submit(task, state)) is False
    assert (
        executor._read_state_sync(PROJECT_ID, task.task_id).phase == "FAILED"
    )
    assert (
        executor.executions.get_task(PROJECT_ID, task.task_id).status.value
        == "FAILED"
    )
    store.resume(PROJECT_ID, store.read(PROJECT_ID).revision)
    assert asyncio.run(executor._submit(task, state)) is False
    assert (
        executor._read_state_sync(PROJECT_ID, task.task_id).phase == "FAILED"
    )


def test_automatic_context_cannot_inherit_manual_permission(env):
    services, store = env
    admission = hold.ManualAdmission(
        store,
        _begin(store),
        PROJECT_ID,
        "request",
    )
    with hold.manual_admission(admission):
        with hold.automatic_node(services.root, PROJECT_ID, "video:a"):
            with pytest.raises(hold.ManualHoldConflict):
                hold.check_automatic(services.root, PROJECT_ID)
        # The deliberate manual target is still allowed inside its own call.
        with hold.admission_guard(
            services.root,
            PROJECT_ID,
            node_id="storyboard:a",
        ):
            pass
        with pytest.raises(hold.ManualHoldConflict):
            with hold.admission_guard(
                services.root,
                PROJECT_ID,
                node_id="video:a",
            ):
                pytest.fail(
                    "a different node must not borrow manual permission",
                )


def test_commit_wake_does_not_borrow_manual_context(env, monkeypatch):
    services, store = env
    scheduler = WorkGraphScheduler(services)
    checked = []

    async def project_loop(project_id):
        with hold.automatic_node(services.root, project_id, "video:a"):
            with pytest.raises(hold.ManualHoldConflict):
                hold.check_automatic(services.root, project_id)
            checked.append(project_id)

    monkeypatch.setattr(scheduler, "_project_loop", project_loop)

    async def scenario():
        admission = hold.ManualAdmission(
            store,
            _begin(store),
            PROJECT_ID,
            "request",
        )
        with hold.manual_admission(admission):
            await asyncio.to_thread(
                asyncio.get_running_loop().call_soon_threadsafe,
                scheduler.wake,
                PROJECT_ID,
            )
        await asyncio.sleep(0)
        await scheduler._loops[PROJECT_ID]
        store.finish(admission, succeeded=True)
        assert admission.closed
        with hold.manual_admission(admission):
            with pytest.raises(hold.ManualHoldConflict):
                hold.check_automatic(services.root, PROJECT_ID)

    asyncio.run(scenario())
    assert checked == [PROJECT_ID]


@pytest.fixture
def _clean_holds():
    frontend_edit_hold.clear()
    yield
    frontend_edit_hold.clear()


@pytest.mark.usefixtures("_clean_holds")
def test_element_ids_from_pointers_extract_only_element_paths() -> None:
    ids = frontend_edit_hold.element_ids_from_pointers(
        [
            "/timelines/items/timeline:main/elements_by_id/elem:a/creation"
            "/storyboard_prompt",
            "/timelines/items/timeline:main/elements_by_id/elem:b/span"
            "/duration_tick",
            "/assets/artifact_slots_by_id/element:elem:a:video"
            "/selected_version_id",
            "/settings/aspect_ratio",
            "",
        ],
    )
    assert ids == {"elem:a", "elem:b"}


@pytest.mark.usefixtures("_clean_holds")
def test_hold_opens_and_expires() -> None:
    frontend_edit_hold.note_frontend_edit(PROJECT_ID, ["elem:a"], now=100.0)
    assert frontend_edit_hold.hold_remaining(
        PROJECT_ID,
        "elem:a",
        now=100.0,
    ) == pytest.approx(frontend_edit_hold.FRONTEND_EDIT_GRACE_SECONDS)
    assert (
        frontend_edit_hold.hold_remaining(PROJECT_ID, "elem:other", now=100.0)
        == 0.0
    )
    expired = 100.0 + frontend_edit_hold.FRONTEND_EDIT_GRACE_SECONDS + 1.0
    assert (
        frontend_edit_hold.hold_remaining(PROJECT_ID, "elem:a", now=expired)
        == 0.0
    )
    # An expired probe drops the entry entirely.
    assert (PROJECT_ID, "elem:a") not in frontend_edit_hold._holds


@pytest.mark.usefixtures("_clean_holds")
def test_clear_scopes_to_one_project() -> None:
    frontend_edit_hold.note_frontend_edit("p1", ["e1"])
    frontend_edit_hold.note_frontend_edit("p2", ["e1"])
    frontend_edit_hold.clear("p1")
    assert frontend_edit_hold.hold_remaining("p1", "e1") == 0.0
    assert frontend_edit_hold.hold_remaining("p2", "e1") > 0.0


def _element_graph() -> WorkGraph:
    return WorkGraph(
        nodes=(
            WorkNode(
                node_id="storyboard:elem:a",
                kind="storyboard",
                label="分镜",
                status=WorkNodeStatus.READY,
                target_ref="element:elem:a",
                command="GENERATE_STORYBOARD_IMAGE",
            ),
        ),
        generation=1,
    )


class _RecordingDispatch:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, services, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


def _scheduler_env(
    tmp_path,
    monkeypatch,
) -> tuple[WorkGraphScheduler, "_RecordingDispatch"]:
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    # Reuse the established scheduler harness: real services + a fabricated
    # element graph (the visual fixtures cannot produce element nodes).
    from services.project_files.facade import CreatorFileServices
    from services.project_files.models import Project

    services = CreatorFileServices.create(tmp_path.resolve())
    services.projects.create(
        Project.new(project_id=PROJECT_ID, name="Hold"),
    )
    monkeypatch.setattr(
        "services.file_agent_runtime.work_scheduler."
        "get_execution_authorization_mode",
        lambda: "allow_all",
    )
    monkeypatch.setattr(
        work_scheduler,
        "derive_work_graph",
        lambda *args, **kwargs: _element_graph(),
    )
    dispatch = _RecordingDispatch()
    scheduler = WorkGraphScheduler(services, image_dispatch=dispatch)
    return scheduler, dispatch


async def _drain() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


@pytest.mark.usefixtures("_clean_holds")
def test_tick_skips_frontend_held_elements_then_dispatches(
    tmp_path,
    monkeypatch,
):
    scheduler, dispatch = _scheduler_env(tmp_path, monkeypatch)

    async def scenario():
        frontend_edit_hold.note_frontend_edit(PROJECT_ID, ["elem:a"])
        await scheduler.tick(PROJECT_ID)
        await _drain()
        assert not dispatch.calls
        # The skip arms a one-shot recheck so the final state still runs.
        assert PROJECT_ID in scheduler._sync_gate_rechecks
        frontend_edit_hold.clear(PROJECT_ID)
        await scheduler.tick(PROJECT_ID)
        await _drain()
        await scheduler.shutdown()

    asyncio.run(scenario())

    assert len(dispatch.calls) == 1
    assert dispatch.calls[0]["target_ref"] == "element:elem:a"


_PNG = b"\x89PNG\r\n\x1a\n" + b"auto-continue" * 16

REVIEW_PROJECT_ID = "auto-continue-project"
ELEMENT_ID = "r2v-auto-1"


class _ImageProvider:
    async def generate(self, **_kwargs):
        return {"content": _PNG, "media_type": "image/png"}


def _services_with_pending_review(
    tmp_path,
    monkeypatch,
) -> CreatorFileServices:
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    services = CreatorFileServices.create(tmp_path.resolve())
    project = Project.new(project_id=REVIEW_PROJECT_ID, name="Auto Continue")
    project.timelines.items["timeline:main"].elements_by_id[
        ELEMENT_ID
    ] = TimelineElement(
        element_id=ELEMENT_ID,
        label="猫追老鼠",
        span=TimelineSpan(start_tick=0, duration_tick=4_000),
        location=ElementLocation(),
        creation=R2VCreation(
            narrative="猫发现老鼠后追逐",
            storyboard_prompt="动画分镜：猫发现并追逐老鼠",
        ),
    )
    services.projects.create(
        Project.model_validate(project.model_dump(mode="json")),
    )
    asyncio.run(
        FileImageExecutionService(services, provider=_ImageProvider()).execute(
            project_id=REVIEW_PROJECT_ID,
            command="GENERATE_STORYBOARD_IMAGE",
            target_ref=f"element:{ELEMENT_ID}",
            arguments={},
            idempotency_key="storyboard-1",
        ),
    )
    return services


def _accept_pending_review(services, decision_id: str) -> str:
    review = services.reviews.all_pending(REVIEW_PROJECT_ID)[0]
    services.reviews.decide(
        project_id=REVIEW_PROJECT_ID,
        review_id=review.review_id,
        decision_token=review.decision_token,
        decisions=[
            ReviewDecisionItem(
                operation_id=operation.operation_id,
                decision="ACCEPT",
            )
            for operation in review.operations
        ],
        decision_id=decision_id,
    )
    return review.review_id


def test_allow_all_auto_starts_video_for_approved_storyboard(
    tmp_path,
    monkeypatch,
):
    services = _services_with_pending_review(tmp_path, monkeypatch)
    review_id = _accept_pending_review(services, "decision-auto-1")
    monkeypatch.setattr(
        creator_config,
        "get_execution_authorization_mode",
        lambda: creator_config.EXECUTION_AUTHORIZATION_ALLOW_ALL,
    )
    dispatched: list[dict] = []

    async def fake_execute(_services, **kwargs):
        dispatched.append(kwargs)

    monkeypatch.setattr(
        r2v_execution,
        "execute_file_r2v_command",
        fake_execute,
    )

    asyncio.run(
        services.publish_review_followup(
            project_id=REVIEW_PROJECT_ID,
            review_id=review_id,
            decision_id="decision-auto-1",
        ),
    )

    assert len(dispatched) == 1
    assert dispatched[0]["target_ref"] == f"element:{ELEMENT_ID}"
    assert dispatched[0]["idempotency_key"].startswith(
        "review-auto-continue-",
    )


def test_required_authorization_mode_never_auto_starts(
    tmp_path,
    monkeypatch,
):
    services = _services_with_pending_review(tmp_path, monkeypatch)
    review_id = _accept_pending_review(services, "decision-auto-2")
    monkeypatch.setattr(
        creator_config,
        "get_execution_authorization_mode",
        lambda: creator_config.EXECUTION_AUTHORIZATION_REQUIRED,
    )
    dispatched: list[dict] = []

    async def fake_execute(_services, **kwargs):
        dispatched.append(kwargs)

    monkeypatch.setattr(
        r2v_execution,
        "execute_file_r2v_command",
        fake_execute,
    )

    asyncio.run(
        services.publish_review_followup(
            project_id=REVIEW_PROJECT_ID,
            review_id=review_id,
            decision_id="decision-auto-2",
        ),
    )

    assert not dispatched


def test_accepting_regenerated_storyboard_does_not_auto_start_held_video(
    tmp_path,
    monkeypatch,
):
    from services.file_agent_runtime.manual_regeneration_hold import (
        ManualRegenerationHoldStore,
    )
    from services.file_agent_runtime.work_graph import derive_work_graph

    services = _services_with_pending_review(tmp_path, monkeypatch)
    review_id = _accept_pending_review(services, "decision-held")
    graph = derive_work_graph(
        services.projects.read(REVIEW_PROJECT_ID).project
    )
    holds = ManualRegenerationHoldStore(services.root)
    node = graph.by_id[f"storyboard:{ELEMENT_ID}"]
    operation = holds.begin(
        REVIEW_PROJECT_ID,
        node,
        graph.nodes,
        existing_output=True,
    )
    holds.admitted(operation)
    monkeypatch.setattr(
        creator_config,
        "get_execution_authorization_mode",
        lambda: "allow_all",
    )
    dispatched = []

    async def execute(*_args, **kwargs):
        dispatched.append(kwargs)

    monkeypatch.setattr(r2v_execution, "execute_file_r2v_command", execute)
    asyncio.run(
        services.publish_review_followup(
            project_id=REVIEW_PROJECT_ID,
            review_id=review_id,
            decision_id="decision-held",
        ),
    )
    assert not dispatched
    assert holds.is_held(REVIEW_PROJECT_ID, f"video:{ELEMENT_ID}")
