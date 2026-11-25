# -*- coding: utf-8 -*-
"""Reclaiming stranded work and pausing unavailable providers."""

from __future__ import annotations

from test_workgraph_execution_requests import (
    _assert_approval_and_idempotent_admission,
)
from test_work_scheduler import _check_done_edge_milestone

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import pytest
from domain.enums import TaskKind, TaskStatus
from services.file_agent_runtime.work_graph import (
    WorkGraph,
    WorkNode,
    WorkNodeStatus,
)
from services.file_agent_runtime import work_scheduler
from services.file_agent_runtime.work_scheduler import (
    WorkGraphScheduler,
    _abandoned_queued_task_ids,
    _blocked_by_active_media_review,
)
from services.runtime_files.execution_models import TaskRecord
from services.runtime_files.path_safety import require_safe_runtime_segment
from test_work_scheduler import (
    PROJECT_ID,
    _RecordingBus,
    _RecordingDispatch,
    _drain,
    _enable_yolo,
    _services,
)

import json
from services.file_agent_runtime import (
    AgentModelTurn,
    AgentToolCall,
    CallbackAgentChatClient,
    FileCreatorAgentRuntime,
)
from test_workgraph_execution_requests import (
    approve,
    create,
    fake_dispatch,
    pin,
    wait_for,
)

pytestmark = pytest.mark.unit


def test_media_node_kinds_are_the_single_source_for_media_gates() -> None:
    """Approve/rebind, script gate and review fence must share one kind set.

    Regression: the approve/rebind whitelist hardcoded only visual/lineup/
    storyboard/video, so authorizing an interaction or compose node failed
    forever with "授权的 WorkGraph 目标已改变或尚未就绪".
    """

    from services.file_agent_runtime import workgraph_execution
    from services.file_agent_runtime.work_graph import MEDIA_NODE_KINDS

    assert {"interaction", "compose"} <= MEDIA_NODE_KINDS
    # request-side accepted kinds and the review fence read the same object.
    assert workgraph_execution.MEDIA_NODE_KINDS is MEDIA_NODE_KINDS
    assert work_scheduler._MEDIA_REVIEW_DEPENDENT_KINDS is MEDIA_NODE_KINDS


def test_media_review_fences_interaction_of_reviewed_element() -> None:
    """interaction was missing from the review fence; a pending review of its
    own element must now hold an interaction regeneration, while an unrelated
    review leaves it free (non-heavy: blocked only on its own owner)."""

    node = WorkNode(
        node_id="interaction:e1",
        kind="interaction",
        label="choice",
        status=WorkNodeStatus.READY,
        target_ref="element:e1",
    )
    assert _blocked_by_active_media_review(
        node,
        frozenset({"element:e1:storyboard"}),
        frozenset({"element:e1"}),
    )
    assert not _blocked_by_active_media_review(
        node,
        frozenset({"element:e2:storyboard"}),
        frozenset({"element:e2"}),
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_stale_script_dispatch_requires_automatic_execution(
    tmp_path,
    monkeypatch,
    enabled,
):
    services = _services(tmp_path, monkeypatch, ready_variants=0)
    node = WorkNode(
        node_id="script:timeline:main",
        kind="script",
        label="Script",
        status=WorkNodeStatus.STALE,
        timeline_id="timeline:main",
        command="GENERATE_TIMELINE_SCRIPT",
        target_ref="timeline:timeline:main",
        dispatch_fingerprint="script-inputs",
        regeneration_of="script-old",
    )
    graph = WorkGraph(nodes=(node,), generation=1)
    monkeypatch.setattr(
        work_scheduler,
        "derive_work_graph",
        lambda *a, **k: graph,
    )
    scheduler = WorkGraphScheduler(services)
    monkeypatch.setattr(scheduler, "enabled", lambda: enabled)
    monkeypatch.setattr(scheduler, "wake", lambda _project_id: None)
    dispatch = _RecordingDispatch()

    async def scenario():
        dispatched = asyncio.Event()

        async def script_dispatch(*args, **kwargs):
            result = await dispatch(*args, **kwargs)
            dispatched.set()
            return result

        monkeypatch.setattr(
            work_scheduler,
            "_default_script_dispatch",
            script_dispatch,
        )
        try:
            await scheduler.tick(PROJECT_ID)
            if enabled:
                await asyncio.wait_for(dispatched.wait(), timeout=2)
        finally:
            await scheduler.shutdown()

    asyncio.run(scenario())
    assert len(dispatch.calls) == int(enabled)
    if enabled:
        call = dispatch.calls[0]
        assert call["target_ref"] == "timeline:timeline:main"
        assert call["command"] == "GENERATE_TIMELINE_SCRIPT"
        assert "-regen-" in call["idempotency_key"]


def test_idempotency_key_is_a_safe_runtime_segment(tmp_path, monkeypatch):
    """Regression: the ledger fingerprint carries "|img:<model>|vid:<model>"
    and "|" is rejected by require_safe_runtime_segment. Media executors
    persist the dispatch key verbatim as Task idempotency_key /
    caused_by_request_id, so a raw fingerprint in the key failed every
    work-graph dispatch ("caused_by_request_id is not a safe path
    segment") and media generation never started."""
    services = _services(tmp_path, monkeypatch, ready_variants=1)
    _enable_yolo(monkeypatch)
    dispatch = _RecordingDispatch()
    scheduler = WorkGraphScheduler(services, image_dispatch=dispatch)

    async def scenario():
        await scheduler.tick(PROJECT_ID)
        await _drain()

    asyncio.run(scenario())

    key = dispatch.calls[0]["idempotency_key"]
    assert "|" not in key
    assert (
        require_safe_runtime_segment(key, label="caused_by_request_id") == key
    )


def _queued_task(target_ref, *, updated_at, last_attempt_seq=0):
    return SimpleNamespace(
        task_id="task-queued",
        kind="image_generation",
        status=TaskStatus.QUEUED,
        updated_at=updated_at,
        last_attempt_seq=last_attempt_seq,
        metadata={"targetRef": target_ref},
        input_refs=[target_ref],
        error=None,
        result=None,
    )


def test_stranded_variant_does_not_reopen_a_running_sibling(
    tmp_path, monkeypatch
):
    from api.work_graph_routes import dispatch_work_graph_node
    from services.file_agent_runtime.work_graph import derive_work_graph

    services = _services(tmp_path, monkeypatch, ready_variants=2)
    scheduler = WorkGraphScheduler(services)
    now = datetime.now(UTC)
    for variant in (0, 1):
        scheduler.executions.create_task(
            TaskRecord(
                task_id=f"task-variant-{variant}",
                project_id=PROJECT_ID,
                kind=TaskKind.IMAGE_GENERATION,
                request_fingerprint=f"hash-{variant}",
                metadata={
                    "targetRef": "asset:char:a",
                    "variantId": f"var:{variant}",
                },
                created_at=now - timedelta(hours=1),
                updated_at=now - timedelta(minutes=30),
            )
        )
    scheduler.executions.append_task_attempt(
        PROJECT_ID,
        "task-variant-1",
        event_id="provider-started",
        attempt_id="attempt-1",
        status="RUNNING",
        provider_task_id="existing-render",
    )
    tasks = scheduler.executions.list_tasks(PROJECT_ID)
    graph = derive_work_graph(
        services.projects.read(PROJECT_ID).project, tasks=tasks
    )
    candidates = scheduler._dispatch_candidates(PROJECT_ID, graph, tasks)
    assert {node.node_id for node in candidates if node.kind == "visual"} == {
        "visual:char:a:var:0",
    }
    result = asyncio.run(
        dispatch_work_graph_node(
            PROJECT_ID, "visual:char:a:var:1", services=services
        )
    )
    assert result["dispatched"] is False
    assert result["taskId"] == "task-variant-1"
    assert result["lastAttemptSeq"] == 1
    assert len(scheduler.executions.list_tasks(PROJECT_ID)) == 2


def test_abandoned_queued_task_ids_requires_no_attempt_and_age():
    """Only a never-claimed QUEUED task past the grace window counts, so a
    task with real progress or a fresh admission is never reopened."""

    now = datetime(2026, 11, 6, 12, tzinfo=UTC)
    stale = now - timedelta(minutes=16)
    assert _abandoned_queued_task_ids(
        [_queued_task("element:e1", updated_at=stale)],
        now=now,
    ) == {"task-queued"}
    assert (
        _abandoned_queued_task_ids(
            [_queued_task("element:e1", updated_at=now)],
            now=now,
        )
        == set()
    )
    assert (
        _abandoned_queued_task_ids(
            [_queued_task("element:e1", updated_at=stale, last_attempt_seq=1)],
            now=now,
        )
        == set()
    )


def _stranded_admission_case(tmp_path, monkeypatch, *, age: timedelta):
    """Dispatch once, then leave one QUEUED zero-attempt task of the given
    age and report how many extra dispatches the following ticks admit."""
    services = _services(tmp_path, monkeypatch, ready_variants=1)
    _enable_yolo(monkeypatch)
    dispatch = _RecordingDispatch()
    scheduler = WorkGraphScheduler(services, image_dispatch=dispatch)

    async def scenario():
        await scheduler.tick(PROJECT_ID)
        await _drain()
        target_ref = dispatch.calls[0]["target_ref"]
        admitted = len(dispatch.calls)
        monkeypatch.setattr(
            scheduler.executions,
            "list_tasks",
            lambda _project_id: [
                _queued_task(
                    target_ref,
                    updated_at=datetime.now(UTC) - age,
                ),
            ],
        )
        await scheduler.tick(PROJECT_ID)
        await _drain()
        reclaimed = len(dispatch.calls) - admitted
        await scheduler.shutdown()
        return reclaimed

    return asyncio.run(scenario())


def test_queued_task_without_attempt_reopens_dispatch(tmp_path, monkeypatch):
    """Field case 2026-11-06: the dispatch coroutine admitted the run and
    task, then died before recording a first attempt. The graph read the
    node as running, none of the re-entry paths recognised a QUEUED task,
    and the manual dispatch gate answers running with dispatched=false —
    the node sat unclaimed for 69 hours with compose gated behind it."""
    # The stub dispatch never records an attempt, so a still-stranded
    # admission re-enters on each wake up to the transient hard cap; what
    # is under test is that it re-enters at all.
    assert (
        _stranded_admission_case(
            tmp_path,
            monkeypatch,
            age=timedelta(minutes=16),
        )
        >= 1
    )


def test_fresh_queued_admission_is_not_requeued(tmp_path, monkeypatch):
    """A task admitted moments ago may still be starting; only the grace
    window separates it from a stranded one."""

    assert (
        _stranded_admission_case(
            tmp_path,
            monkeypatch,
            age=timedelta(minutes=1),
        )
        == 0
    )


def _admitted_failure_case(tmp_path, monkeypatch, *, last_attempt_seq):
    """Dispatch that admits a task record and then fails deterministically.

    Returns the dispatch count plus the scheduler's own warnings: the plugin
    logger does not propagate to the root, so caplog can never see it and the
    warning has to be recorded at its call site.
    """
    services = _services(tmp_path, monkeypatch, ready_variants=1)
    _enable_yolo(monkeypatch)
    notices: list[str] = []

    def record_warning(message, *args, **kwargs):
        notices.append(message % args if args else str(message))

    monkeypatch.setattr(work_scheduler.logger, "warning", record_warning)
    records: list = []
    dispatch = _RecordingDispatch(
        fail=True,
        error="reference budget exceeded",
        records=records,
    )
    scheduler = WorkGraphScheduler(services, image_dispatch=dispatch)
    # The stranded-node case is exactly the one where the paid-retry budget is
    # already spent: an unrecognised RuntimeError classifies as transient, so
    # only with no budget left does dispatch reach the zero-attempt reopen.
    monkeypatch.setattr(
        scheduler,
        "_transient_budget_available",
        lambda _key: False,
    )
    monkeypatch.setattr(
        scheduler.executions,
        "list_tasks",
        lambda _project_id: [
            SimpleNamespace(
                kind="image_generation",
                status=TaskStatus.FAILED,
                error={"code": "IMAGE_GENERATION_FAILED", "message": "x"},
                result=None,
                metadata=record.metadata,
                input_refs=record.input_refs,
                idempotency_key=record.idempotency_key,
                updated_at=record.updated_at,
                last_attempt_seq=last_attempt_seq,
            )
            for record in records
        ],
    )

    async def scenario():
        for _ in range(4):
            await scheduler.tick(PROJECT_ID)
            await _drain()
        await scheduler.shutdown()
        return len(dispatch.calls)

    dispatched = asyncio.run(scenario())
    reopened = any("left no attempt" in line for line in notices)
    return dispatched, reopened


def test_failure_without_any_attempt_reopens_the_ledger(
    tmp_path,
    monkeypatch,
):
    """Field case 2026-11-06: the executor never claimed the admitted task, so
    nothing was spent, yet the ledger mark stayed and the node stranded as
    "running" with zero attempts for 69 hours."""

    _, reopened = _admitted_failure_case(
        tmp_path,
        monkeypatch,
        last_attempt_seq=0,
    )
    assert reopened


def test_failure_after_an_attempt_keeps_the_ledger_mark(
    tmp_path,
    monkeypatch,
):
    """Once an attempt exists the provider may already have been paid, so the
    zero-attempt reopen must not fire; the existing recordless path is a
    separate decision and is not what this asserts about."""

    _, reopened = _admitted_failure_case(
        tmp_path,
        monkeypatch,
        last_attempt_seq=1,
    )
    assert not reopened


CREDITS_ERROR = (
    'Image generation failed with status 403: {"code": '
    '"ASP.BIZ.CREDITS_INSUFFICIENT", "message": "模型 Credits 不足，请先使用'
    '贡献值兑换", "retryable": false, "request_id": '
    '"bcc611fa-c5da-4190-8e3b-73aff40f4444"}. '
    "Check creator_image_model configuration."
)


UPSTREAM_ERROR = (
    'Video task submission failed with status 502: {"code": '
    '"ASP.UPSTREAM.ERROR", "message": "InvalidParameter", '
    '"retryable": true, "request_id": '
    '"d5507d1b-1059-9635-989e-e1f0c1b6b62e"}'
)


def test_credits_refusal_holds_the_project_and_reports_once(
    tmp_path,
    monkeypatch,
):
    """One exhausted balance must not become one failure per node.

    Every node in a fan-out would fail identically, so the breaker holds
    paid dispatch project-wide and a single event carries the provider's
    request id for handover.
    """
    from services.file_agent_runtime.notifications import RuntimeEventKind

    services = _services(tmp_path, monkeypatch, ready_variants=3)
    _enable_yolo(monkeypatch)
    monkeypatch.setattr(
        "services.file_agent_runtime.work_scheduler.get_media_parallelism",
        lambda: 1,
    )
    bus = _RecordingBus()
    records: list = []
    dispatch = _RecordingDispatch(
        fail=True,
        error=CREDITS_ERROR,
        records=records,
    )
    scheduler = WorkGraphScheduler(
        services,
        image_dispatch=dispatch,
        notifications=bus,
    )
    # Real executors admit a durable task before any provider spend, so the
    # refusal leaves a FAILED record; without one the record-less reopen path
    # would (correctly) treat the dispatch as never having happened.
    monkeypatch.setattr(
        scheduler.executions,
        "list_tasks",
        lambda _project_id: list(records),
    )

    async def scenario():
        for _ in range(4):
            await scheduler.tick(PROJECT_ID)
            await _drain()
        await scheduler.shutdown()

    asyncio.run(scenario())

    credits_events = [
        event
        for event in bus.events
        if event.kind is RuntimeEventKind.PROVIDER_CREDITS_EXHAUSTED
    ]
    # Serialised on purpose: the first refusal trips the breaker, so the
    # three remaining ticks dispatch nothing at all.
    assert len(dispatch.calls) == 1
    assert len(credits_events) == 1
    assert "provider_request_id=bcc611fa" in credits_events[0].text
    assert credits_events[0].payload["errorCode"] == (
        "ASP.BIZ.CREDITS_INSUFFICIENT"
    )
    # Not walled: the fix is a top-up and the prompt has nothing to do with
    # it, so the node stays dispatchable instead of going permanently quiet.
    assert scheduler._deterministic_failure_nodes == {}


def test_credits_breaker_is_tripped_once_and_cleared_by_cancel(
    tmp_path,
    monkeypatch,
) -> None:
    services = _services(tmp_path, monkeypatch, ready_variants=1)
    scheduler = WorkGraphScheduler(services)

    assert scheduler._trip_quota_breaker(
        PROJECT_ID,
        Exception(CREDITS_ERROR),
    )
    assert scheduler._quota_breaker_open(PROJECT_ID)
    # A second refusal inside the window must not emit a second event.
    assert not scheduler._trip_quota_breaker(
        PROJECT_ID,
        Exception(CREDITS_ERROR),
    )
    # Any other provider error leaves the breaker alone.
    assert not scheduler._trip_quota_breaker(
        PROJECT_ID,
        Exception(UPSTREAM_ERROR),
    )

    # The window is a TTL rather than a latch: a top-up resumes the run
    # without a restart.
    scheduler._quota_tripped[PROJECT_ID] -= (
        work_scheduler._QUOTA_BREAKER_SECONDS + 1
    )
    assert not scheduler._quota_breaker_open(PROJECT_ID)
    # And a refusal in the fresh window is reported again - the user paid
    # for more Credits and it still failed, which is new information.
    assert scheduler._trip_quota_breaker(
        PROJECT_ID,
        Exception(CREDITS_ERROR),
    )

    # Cancelling is the user's "start over", and the usual reason to cancel
    # a Credits-exhausted run is that they just paid for more.
    scheduler.cancel_project(PROJECT_ID)
    assert not scheduler._quota_breaker_open(PROJECT_ID)


RATE_LIMIT_ERROR = "Image generation failed: rate limited after all retries"


def test_rate_limit_breaker_holds_the_fan_out_and_expires(
    tmp_path,
    monkeypatch,
) -> None:
    services = _services(tmp_path, monkeypatch, ready_variants=1)
    scheduler = WorkGraphScheduler(services)

    assert scheduler._trip_rate_limit_breaker(
        PROJECT_ID,
        Exception(RATE_LIMIT_ERROR),
    )
    assert scheduler._rate_limit_breaker_open(PROJECT_ID)
    # One window produces one line: the rest of the fan-out would hear the same
    # refusal, so repeating it tells the agent nothing new.
    assert not scheduler._trip_rate_limit_breaker(
        PROJECT_ID,
        Exception(RATE_LIMIT_ERROR),
    )

    # A bare status says the same thing in the provider's own words.
    scheduler.cancel_project(PROJECT_ID)
    assert not scheduler._rate_limit_breaker_open(PROJECT_ID)
    assert scheduler._trip_rate_limit_breaker(
        PROJECT_ID,
        Exception(
            "Image generation failed with status 429: too many requests",
        ),
    )

    # Credits is the better diagnosis when a refusal looks like both: its
    # remedy is the user's and its window is far longer, so the short hold
    # would only delay the message that can actually be acted on.
    scheduler.cancel_project(PROJECT_ID)
    assert not scheduler._trip_rate_limit_breaker(
        PROJECT_ID,
        Exception(CREDITS_ERROR),
    )
    assert not scheduler._rate_limit_breaker_open(PROJECT_ID)

    # An unrelated transient fault must not wall the whole project - it says
    # nothing about the other nodes' inputs.
    assert not scheduler._trip_rate_limit_breaker(
        PROJECT_ID,
        Exception(UPSTREAM_ERROR),
    )
    assert not scheduler._rate_limit_breaker_open(PROJECT_ID)

    # The window is a TTL, so a throttle that clears resumes dispatch without
    # a restart; the first node through learns whether it really did.
    assert scheduler._trip_rate_limit_breaker(
        PROJECT_ID,
        Exception(RATE_LIMIT_ERROR),
    )
    scheduler._rate_limited_tripped[PROJECT_ID] -= (
        work_scheduler._RATE_LIMIT_BREAKER_SECONDS + 1
    )
    assert not scheduler._rate_limit_breaker_open(PROJECT_ID)


def test_credits_breaker_also_holds_prompt_preparation(
    tmp_path,
    monkeypatch,
) -> None:
    """Preparation is a paid text call, so the same refusal holds it."""
    services = _services(tmp_path, monkeypatch, ready_variants=0)
    constructed: list[str] = []

    class Sentinel:
        def __init__(self, *_args, **_kwargs) -> None:
            constructed.append("PromptSyncService")

    monkeypatch.setattr(
        "services.prompt_sync_service.PromptSyncService",
        Sentinel,
    )
    scheduler = WorkGraphScheduler(services)
    scheduler._trip_quota_breaker(PROJECT_ID, Exception(CREDITS_ERROR))
    graph = WorkGraph(
        nodes=(
            WorkNode(
                node_id="video:e1",
                kind="video",
                label="视频 e1",
                status=WorkNodeStatus.GATED,
                timeline_id="timeline:main",
                target_ref="element:e1",
                prompt_sync_required=True,
            ),
        ),
        generation=1,
    )

    asyncio.run(scheduler._prepare_changed_prompts(PROJECT_ID, graph))

    assert not constructed


def test_media_history_does_not_block_dispatch_but_gates_still_apply(
    tmp_path,
    monkeypatch,
):
    services = _services(tmp_path, monkeypatch, ready_variants=0)
    history = [
        TaskRecord(
            task_id=f"historical-media-{index}",
            project_id=PROJECT_ID,
            kind=TaskKind.R2V_GENERATION,
            status=TaskStatus.SUCCEEDED,
            request_fingerprint=f"historical-{index}",
        )
        for index in range(250)
    ]
    monkeypatch.setattr(
        work_scheduler.ProjectExecutionStore,
        "list_tasks",
        lambda _store, _project_id: history,
    )
    monkeypatch.setattr(work_scheduler, "get_media_parallelism", lambda: 1)
    monkeypatch.setattr(
        work_scheduler,
        "get_execution_authorization_mode",
        lambda: "required",
    )
    graph = WorkGraph(
        nodes=tuple(
            WorkNode(
                node_id=kind + ":test",
                kind=kind,
                label=kind,
                status=WorkNodeStatus.READY,
                command=command,
                target_ref="timeline:main"
                if kind == "compose"
                else "element:test",
            )
            for kind, command in [
                ("video", "GENERATE_R2V_VIDEO"),
                ("interaction", "GENERATE_INTERACTION_MOTION"),
                ("compose", "COMPOSE_FINAL_VIDEO"),
            ]
        ),
        generation=1,
    )
    completed = set()
    monkeypatch.setattr(
        work_scheduler,
        "derive_work_graph",
        lambda *_args, **_kwargs: WorkGraph(
            nodes=tuple(
                node for node in graph.nodes if node.kind not in completed
            ),
            generation=graph.generation,
        ),
    )
    scheduler = WorkGraphScheduler(services)
    monkeypatch.setattr(scheduler, "wake", lambda _project_id: None)
    calls = []
    release = asyncio.Event()

    async def dispatch(_project, node, *_args, **_kwargs):
        calls.append(node.kind)
        await release.wait()
        completed.add(node.kind)

    scheduler.dispatch_node = dispatch

    async def scenario():
        try:
            await scheduler.tick(PROJECT_ID)
            await _drain()
            assert not calls, "automatic dispatch still requires authorization"
            _enable_yolo(monkeypatch)
            for count in range(1, 4):
                release.clear()
                await scheduler.tick(PROJECT_ID)
                await _drain()
                assert len(calls) == count
                await scheduler.tick(PROJECT_ID)
                await _drain()
                assert (
                    len(calls) == count
                ), "inflight work still occupies capacity"
                release.set()
                await asyncio.gather(*scheduler._dispatch_tasks[PROJECT_ID])
        finally:
            await scheduler.shutdown()

    asyncio.run(scenario())
    assert sorted(calls) == ["compose", "interaction", "video"]


@pytest.mark.parametrize("manual", [True, False], ids=["click", "scheduler"])
@pytest.mark.parametrize(
    "original_first", [True, False], ids=["live", "stranded"]
)
@pytest.mark.parametrize("legacy", [False, True], ids=["current", "legacy"])
def test_queued_image_takeover_shares_original_provider_claim(
    tmp_path, monkeypatch, manual, original_first, legacy
):
    from api.work_graph_routes import dispatch_work_graph_node
    from services.runtime_files.execution_store import ProjectExecutionStore
    from services.file_agent_runtime.work_graph import derive_work_graph
    from services.media_files.image_execution import (
        ExistingImageProvider,
        FileImageExecutionService,
    )

    services = _services(tmp_path, monkeypatch, ready_variants=2)
    scheduler = WorkGraphScheduler(services)
    node_id = "visual:char:a:var:1"
    node = derive_work_graph(services.projects.read(PROJECT_ID).project).by_id[
        node_id
    ]
    worker = FileImageExecutionService(
        services, provider=ExistingImageProvider()
    )
    original_start = worker._start
    original_dispatch = WorkGraphScheduler.dispatch_node
    create_task = ProjectExecutionStore.create_task
    queued = asyncio.Event()
    start_original = asyncio.Event()
    provider_entered = asyncio.Event()
    finish_provider = asyncio.Event()
    provider_calls = []

    def backdate(store, record, **kwargs):
        past = record.created_at - timedelta(minutes=20)
        metadata = dict(record.metadata)
        if legacy:
            metadata.pop("commandArguments")
        return create_task(
            store,
            record.model_copy(
                update={
                    "created_at": past,
                    "updated_at": past,
                    "metadata": metadata,
                }
            ),
            **kwargs,
        )

    async def paused_start(**kwargs):
        queued.set()
        await start_original.wait()
        return await original_start(**kwargs)

    async def provider(_self, **kwargs):
        provider_calls.append(kwargs["prompt"])
        provider_entered.set()
        await finish_provider.wait()
        return {
            "content": b"\x89PNG\r\n\x1a\n" + b"image" * 16,
            "media_type": "image/png",
        }

    async def race_after_graph_read(self, *args, **kwargs):
        if original_first:
            start_original.set()
            await provider_entered.wait()
        return await original_dispatch(self, *args, **kwargs)

    monkeypatch.setattr(ProjectExecutionStore, "create_task", backdate)
    monkeypatch.setattr(worker, "_start", paused_start)
    monkeypatch.setattr(ExistingImageProvider, "generate", provider)
    monkeypatch.setattr(
        WorkGraphScheduler, "dispatch_node", race_after_graph_read
    )

    async def scenario():
        original = asyncio.create_task(
            worker.execute(
                project_id=PROJECT_ID,
                command=node.command,
                target_ref=node.target_ref,
                arguments={
                    **node.dispatch_arguments,
                    "prompt": "original specialist direction",
                },
                idempotency_key="specialist-admitted",
            )
        )
        await asyncio.wait_for(queued.wait(), timeout=3)
        tasks = scheduler.executions.list_tasks(PROJECT_ID)
        assert len(tasks) == 1
        assert tasks[0].last_attempt_seq == 0
        if not original_first:
            # Another variant can progress while the original worker is
            # absent. Its commit must not invalidate the stranded admission.
            snapshot = services.projects.read(PROJECT_ID)
            candidate = snapshot.project.model_copy(deep=True)
            candidate.visual.entities.items["char:a"].variants.items[
                "var:0"
            ].prompt = "sibling updated while the worker was down"
            candidate.generation += 1
            services.projects.replace(
                PROJECT_ID, candidate, expected_etag=snapshot.etag
            )
        graph = derive_work_graph(
            services.projects.read(PROJECT_ID).project, tasks=tasks
        )
        assert graph.by_id[node_id].task_id == tasks[0].task_id
        if manual:
            recovery = dispatch_work_graph_node(
                PROJECT_ID, node_id, services=services
            )
        else:
            (candidate,) = [
                n
                for n in scheduler._dispatch_candidates(
                    PROJECT_ID, graph, tasks
                )
                if n.node_id == node_id
            ]
            recovery = scheduler._dispatch(
                PROJECT_ID, candidate, scheduler._ledger_fingerprint(candidate)
            )
        retry = asyncio.create_task(recovery)
        try:
            await asyncio.wait_for(provider_entered.wait(), timeout=3)
            start_original.set()
            # The losing contender must return even while the provider is busy.
            loser = retry if original_first else original
            await asyncio.wait_for(
                asyncio.gather(loser, return_exceptions=True), timeout=3
            )
        finally:
            start_original.set()
            finish_provider.set()
            results = await asyncio.gather(
                original, retry, return_exceptions=True
            )
            await scheduler.shutdown()
        if manual and original_first:
            assert results[1]["reason"] == "already_running"
            assert results[1]["taskId"] == tasks[0].task_id
        assert len(provider_calls) == 1
        assert "original specialist direction" in provider_calls[0]
        (final,) = scheduler.executions.list_tasks(PROJECT_ID)
        assert final.task_id == tasks[0].task_id
        assert final.metadata["variantId"] == "var:1"
        assert final.status is TaskStatus.SUCCEEDED

    asyncio.run(scenario())


@pytest.mark.parametrize("raise_after_admission", [False, True])
def test_admitted_failure_is_reported_to_agent_with_task_identity(
    tmp_path,
    monkeypatch,
    raise_after_admission,
):
    pin(monkeypatch)

    async def scenario():
        services = create(tmp_path)
        observed = []

        async def model(messages, _tools):
            tool_messages = [m for m in messages if m.get("role") == "tool"]
            if tool_messages:
                observed.append(json.loads(tool_messages[-1]["content"]))
                return AgentModelTurn(
                    content="The generation failed and needs correction.",
                )
            return AgentModelTurn(
                tool_calls=(
                    AgentToolCall(
                        call_id="failure-call",
                        name="request_workgraph_execution",
                        arguments={
                            "projectId": "probe-project",
                            "targetRefs": ["asset:hero"],
                            "kinds": ["visual"],
                        },
                    ),
                ),
            )

        runtime = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(model),
            poll_interval_seconds=0.01,
        )
        calls = []
        fake_dispatch(
            runtime,
            calls,
            failure=True,
            raise_after_admission=raise_after_admission,
        )
        try:
            await runtime.start()
            runtime.notify("probe-project")
            await wait_for(
                lambda: runtime.executions.list_execution_authorizations(
                    "probe-project",
                ),
            )
            approve(
                runtime,
                runtime.executions.list_execution_authorizations(
                    "probe-project",
                )[0],
            )
            await wait_for(lambda: observed)
            await runtime.wait_until_idle("probe-project")
            item = observed[0]["items"][0]
            assert item["status"] == "FAILED"
            assert item["taskId"] == "paid-task-1"
            assert (
                item["error"]["message"]
                == "Output rejected: forbidden HTML tag"
            )
            assert len(calls) == 1
        finally:
            await runtime.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_first", [False, True])
def test_high_media_history_keeps_approval_and_idempotent_admission(
    tmp_path, monkeypatch, cancel_first
):
    _assert_approval_and_idempotent_admission(
        tmp_path,
        monkeypatch,
        cancel_first,
        historical_media_tasks=250,
    )


def test_bundle_done_edge_emits_one_milestone(tmp_path, monkeypatch):
    _check_done_edge_milestone(
        tmp_path,
        monkeypatch,
        "bundle",
        "bundle:project",
        "互动包准备",
        "node_succeeded",
    )
