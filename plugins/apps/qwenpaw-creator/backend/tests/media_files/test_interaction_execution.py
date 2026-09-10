# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""interaction_draft 执行服务：文本模型起草抉择动效并写回 element.motion。"""

from __future__ import annotations

import asyncio

import pytest

from domain.errors import ValidationError
from services.media_files import interaction_execution
from services.media_files.interaction_execution import (
    execute_file_interaction_command,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    InteractionCreation,
    InteractionOption,
    NarrativeEdge,
    Project,
    Timeline,
    TimelineElement,
    TimelineSpan,
)
from utils.exceptions import ModelError

pytestmark = pytest.mark.unit

PROJECT_ID = "p-interaction-exec"
ELEMENT_ID = "el:choice"

GOOD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"/><style>
@keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.05)}}
.option{animation:pulse 4s ease-in-out infinite}
</style></head>
<body><span data-interaction-countdown></span>
<div class="question">是否当众揭发沈修？</div>
<button class="option" data-edge-ref="edge:a">选择A · 揭发真相</button>
<button class="option" data-edge-ref="edge:b">选择B · 保持沉默</button>
</body>
</html>"""

BAD_HTML_MISSING_REF = """<!DOCTYPE html>
<html><body>
<button data-edge-ref="edge:a">选择A · 揭发真相</button>
<button>选择B · 保持沉默</button>
</body></html>"""


def _services(tmp_path, *, with_runtime=False) -> CreatorFileServices:
    services = CreatorFileServices.create(tmp_path.resolve())
    project = Project.new(project_id=PROJECT_ID, name="Interaction Exec")
    project.timelines.items["timeline:main"].title = "第3集 · 双重身份"
    for timeline_id, title in (
        ("timeline:ep4a", "第4集A · 真相大白"),
        ("timeline:ep4b", "第4集B · 沉默代价"),
    ):
        project.timelines.items[timeline_id] = Timeline(
            timeline_id=timeline_id,
            title=title,
        )
        project.timelines.order.append(timeline_id)
    project.narrative_edges = [
        NarrativeEdge(
            edge_id="edge:a",
            source_timeline_id="timeline:main",
            target_timeline_id="timeline:ep4a",
            label="选择A · 揭发真相",
        ),
        NarrativeEdge(
            edge_id="edge:b",
            source_timeline_id="timeline:main",
            target_timeline_id="timeline:ep4b",
            label="选择B · 保持沉默",
        ),
    ]
    project.timelines.items["timeline:main"].elements_by_id[
        ELEMENT_ID
    ] = TimelineElement(
        element_id=ELEMENT_ID,
        label="观众抉择",
        span=TimelineSpan(start_tick=88_000, duration_tick=4_000),
        creation=InteractionCreation(
            type="interaction",
            question="是否当众揭发沈修？",
            options=[
                InteractionOption(edge_ref="edge:a"),
                InteractionOption(edge_ref="edge:b"),
            ],
            countdown_seconds=10,
            default_edge_ref="edge:a",
        ),
    )
    if with_runtime:
        for timeline in project.timelines.items.values():
            timeline.description = (
                "Published complete scene for interaction validation."
            )

    def initialize(staged):
        services.sessions.initialize_staged_project(
            staged,
            PROJECT_ID,
            session_id="interaction-session",
            conversation_id="interaction-conversation",
            initial_goal="Generate the existing interaction motion only.",
            goal_id="interaction-goal",
            initial_message_id="interaction-message",
            initial_client_message_id="interaction-client",
        )

    services.projects.create(
        Project.model_validate(project.model_dump(mode="json")),
        initialize_staged_project=initialize if with_runtime else None,
    )
    return services


def _mock_chat(monkeypatch, replies: list[str]):
    calls: list[dict] = []

    async def fake_chat_completion(prompt, *, system_prompt="", **_kwargs):
        calls.append({"prompt": prompt, "system": system_prompt})
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(
        interaction_execution.text_model,
        "chat_completion",
        fake_chat_completion,
    )
    return calls


def _execute(services, key: str = "dag-interaction-1"):
    return asyncio.run(
        execute_file_interaction_command(
            services,
            project_id=PROJECT_ID,
            target_ref=f"element:{ELEMENT_ID}",
            arguments={},
            idempotency_key=key,
        ),
    )


def test_interaction_command_writes_motion_back(tmp_path, monkeypatch):
    services = _services(tmp_path)
    # 模型输出裹了 markdown 代码围栏：必须被剥掉后再校验/写回。
    calls = _mock_chat(monkeypatch, [f"```html\n{GOOD_HTML}\n```"])

    result = _execute(services)

    assert not result.replayed
    assert result.timeline_id == "timeline:main"
    assert result.element_id == ELEMENT_ID
    snapshot = services.projects.read(PROJECT_ID)
    element = snapshot.project.timelines.items["timeline:main"].elements_by_id[
        ELEMENT_ID
    ]
    motion = element.creation.motion
    assert motion is not None
    assert motion.format == "html_css"
    assert motion.fps == 24
    assert motion.loop is True
    assert 'data-edge-ref="edge:a"' in motion.html
    assert 'data-edge-ref="edge:b"' in motion.html
    assert "```" not in motion.html
    # design_notes = prompt 摘要 + 指纹标记。
    assert "是否当众揭发沈修？" in motion.design_notes
    assert f"input_fingerprint={result.input_fingerprint}" in (
        motion.design_notes
    )
    # prompt 携带问题、边 label（join narrative_edges）与倒计时。
    assert "是否当众揭发沈修？" in calls[0]["prompt"]
    assert "选择A · 揭发真相" in calls[0]["prompt"]
    assert "选择B · 保持沉默" in calls[0]["prompt"]
    assert "10 秒" in calls[0]["prompt"]
    assert "data-edge-ref" in calls[0]["system"]


def test_same_inputs_replay_without_second_model_call(tmp_path, monkeypatch):
    services = _services(tmp_path)
    calls = _mock_chat(monkeypatch, [GOOD_HTML])

    first = _execute(services, key="dag-interaction-1")
    replay = _execute(services, key="dag-interaction-2")

    assert not first.replayed
    assert replay.replayed
    assert replay.input_fingerprint == first.input_fingerprint
    assert len(calls) == 1


def test_bad_output_retries_once_then_succeeds(tmp_path, monkeypatch):
    services = _services(tmp_path)
    calls = _mock_chat(monkeypatch, [BAD_HTML_MISSING_REF, GOOD_HTML])

    result = _execute(services)

    assert not result.replayed
    assert len(calls) == 2
    # 重试 prompt 点名了不合格原因。
    assert "不合格" in calls[1]["prompt"]


def test_persistently_bad_output_raises_model_error(tmp_path, monkeypatch):
    services = _services(tmp_path)
    calls = _mock_chat(
        monkeypatch,
        [BAD_HTML_MISSING_REF, BAD_HTML_MISSING_REF],
    )

    with pytest.raises(ModelError, match="data-edge-ref"):
        _execute(services)

    assert len(calls) == 2
    # 失败不写回：element.motion 保持为空。
    snapshot = services.projects.read(PROJECT_ID)
    element = snapshot.project.timelines.items["timeline:main"].elements_by_id[
        ELEMENT_ID
    ]
    assert element.creation.motion is None


def test_bad_inputs_are_rejected_fail_closed(tmp_path, monkeypatch):
    services = _services(tmp_path)
    # Model output smuggling a <script> is a deterministic model error.
    scripted = GOOD_HTML.replace(
        "</body>",
        "<script>alert(1)</script></body>",
    )
    calls = _mock_chat(monkeypatch, [scripted, scripted, GOOD_HTML])
    with pytest.raises(ModelError, match="script"):
        _execute(services)
    # An unknown target element never reaches the model.
    del calls[:]
    with pytest.raises(ValidationError, match="element 不存在"):
        asyncio.run(
            execute_file_interaction_command(
                services,
                project_id=PROJECT_ID,
                target_ref="element:el:ghost",
                arguments={},
                idempotency_key="dag-interaction-x",
            ),
        )
    assert not calls


def _decide_all(services, decision="ACCEPT"):
    from services.project_files.review import ReviewDecisionItem

    for review in services.reviews.all_pending(PROJECT_ID):
        services.reviews.decide(
            project_id=PROJECT_ID,
            review_id=review.review_id,
            decision_token=review.decision_token,
            decisions=[
                ReviewDecisionItem(
                    operation_id=o.operation_id,
                    decision=decision,
                )
                for o in review.operations
            ],
        )


def test_motion_is_reviewable_and_guidance_regenerates(tmp_path, monkeypatch):
    from services.runtime_files.execution_store import ProjectExecutionStore
    from services.file_agent_runtime.work_graph import (
        derive_work_graph,
        WorkNodeStatus,
    )
    from api.interactive_bundle_routes import _assemble
    from domain.errors import ConflictError

    services = _services(tmp_path)
    calls = _mock_chat(monkeypatch, [GOOD_HTML])
    result = _execute(services)
    reviews = services.reviews.all_pending(PROJECT_ID)
    assert reviews and any(
        "motion" in str(o.json_pointer) for r in reviews for o in r.operations
    )
    graph = derive_work_graph(
        services.projects.read(PROJECT_ID).project,
        pending_reviews=reviews,
    )
    assert (
        graph.by_id[f"interaction:{ELEMENT_ID}"].status
        is WorkNodeStatus.WAITING_REVIEW
    )
    with pytest.raises(ConflictError, match="pending"):
        _assemble(PROJECT_ID, services)
    _decide_all(services)
    revised = asyncio.run(
        execute_file_interaction_command(
            services,
            project_id=PROJECT_ID,
            target_ref=f"element:{ELEMENT_ID}",
            arguments={"guidance": "横屏，蓝色大按钮"},
            idempotency_key="revise-1",
        ),
    )
    assert (
        not revised.replayed
        and revised.input_fingerprint != result.input_fingerprint
    )
    assert len(calls) == 2 and "横屏，蓝色大按钮" in calls[-1]["prompt"]
    assert "16:9" in calls[-1]["prompt"]
    tasks = ProjectExecutionStore(services.root).list_tasks(PROJECT_ID)
    assert len(tasks) == 2 and all(
        t.status.value == "SUCCEEDED" for t in tasks
    )
    _decide_all(services, "REJECT")
    snapshot = services.projects.read(PROJECT_ID)
    assert (
        snapshot.project.timelines.items["timeline:main"]
        .elements_by_id[ELEMENT_ID]
        .creation.design_prompt
        == ""
    )


def test_inflight_input_change_discards_stale_result(tmp_path, monkeypatch):
    from domain.errors import ConflictError
    from services.runtime_files.models import ChangeOrigin
    from services.runtime_files.execution_store import ProjectExecutionStore
    from services.file_agent_runtime.work_graph import (
        derive_work_graph,
        WorkNodeStatus,
    )

    services = _services(tmp_path)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def chat(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return GOOD_HTML

        monkeypatch.setattr(
            interaction_execution.text_model,
            "chat_completion",
            chat,
        )
        task = asyncio.create_task(
            execute_file_interaction_command(
                services,
                project_id=PROJECT_ID,
                target_ref=f"element:{ELEMENT_ID}",
                arguments={},
                idempotency_key="inflight",
            ),
        )
        await entered.wait()
        execution = ProjectExecutionStore(services.root)
        graph = derive_work_graph(
            services.projects.read(PROJECT_ID).project,
            tasks=execution.list_tasks(PROJECT_ID),
        )
        assert (
            graph.by_id[f"interaction:{ELEMENT_ID}"].status
            is WorkNodeStatus.RUNNING
        )
        base = services.projects.read(PROJECT_ID)
        candidate = base.project.model_copy(deep=True)
        candidate.timelines.items["timeline:main"].elements_by_id[
            ELEMENT_ID
        ].creation.question = "已经修改的问题"
        services.commits.commit(
            base=base,
            candidate=candidate.model_dump(mode="json"),
            origin=ChangeOrigin.FRONTEND_EDIT,
        )
        release.set()
        with pytest.raises(ConflictError, match="changed"):
            await task
        assert (
            execution.list_tasks(PROJECT_ID)[0].status.value == "QUARANTINED"
        )
        assert not services.reviews.all_pending(PROJECT_ID)
        assert (
            services.projects.read(PROJECT_ID)
            .project.timelines.items["timeline:main"]
            .elements_by_id[ELEMENT_ID]
            .creation.motion
            is None
        )

    asyncio.run(scenario())


def test_failed_task_parks_scheduler_without_more_model_calls(
    tmp_path,
    monkeypatch,
):
    from services.file_agent_runtime.work_scheduler import WorkGraphScheduler
    from services.runtime_files.models import ChangeOrigin

    services = _services(tmp_path)
    base = services.projects.read(PROJECT_ID)
    candidate = base.project.model_copy(deep=True)
    for timeline in candidate.timelines.items.values():
        timeline.description = "已确认的剧本"
    services.commits.commit(
        base=base,
        candidate=candidate.model_dump(mode="json"),
        origin=ChangeOrigin.FRONTEND_EDIT,
    )
    calls = _mock_chat(monkeypatch, [BAD_HTML_MISSING_REF])

    async def scenario():
        scheduler = WorkGraphScheduler(services)
        monkeypatch.setattr(scheduler, "enabled", lambda: True)
        monkeypatch.setattr(scheduler, "wake", lambda *args, **kwargs: None)
        for _ in range(3):
            graph = await scheduler.tick(PROJECT_ID)
            await asyncio.gather(
                *list(scheduler._dispatch_tasks.get(PROJECT_ID, [])),
            )
        scheduler._closed = True
        assert len(calls) == 4
        assert (
            graph.by_id[f"interaction:{ELEMENT_ID}"].status.value == "failed"
        )
        assert len(scheduler.executions.list_tasks(PROJECT_ID)) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "html",
    [
        '<html><body><!-- <button data-edge-ref="edge:a">A</button>'
        '<button data-edge-ref="edge:b">B</button> --></body></html>',
        GOOD_HTML.replace("<body>", '<body onload="alert(1)">'),
        GOOD_HTML.replace(
            "</body>",
            '<img src="https://example.test/track"></body>',
        ),
        GOOD_HTML.replace(
            "</style>",
            '@import "https://example.test/a.css";</style>',
        ),
        GOOD_HTML.replace(
            "</body>",
            '<iframe srcdoc="unsafe"></iframe></body>',
        ),
    ],
)
def test_unsafe_or_noninteractive_html_is_rejected(html, tmp_path):
    services = _services(tmp_path)
    creation = (
        services.projects.read(PROJECT_ID)
        .project.timelines.items["timeline:main"]
        .elements_by_id[ELEMENT_ID]
        .creation
    )
    assert interaction_execution._validate_motion_html(html, creation)


def test_manual_http_dispatch_checks_versions_and_creates_review(
    tmp_path,
    monkeypatch,
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.work_graph_routes import router
    from api.dependencies import project_file_services
    from services.runtime_files.models import ChangeOrigin
    from domain.errors import ConflictError

    services = _services(tmp_path)
    base = services.projects.read(PROJECT_ID)
    candidate = base.project.model_copy(deep=True)
    for timeline in candidate.timelines.items.values():
        timeline.description = "已确认"
    services.commits.commit(
        base=base,
        candidate=candidate.model_dump(mode="json"),
        origin=ChangeOrigin.FRONTEND_EDIT,
    )
    calls = _mock_chat(monkeypatch, [GOOD_HTML])
    with pytest.raises(ConflictError, match="changed"):
        asyncio.run(
            execute_file_interaction_command(
                services,
                project_id=PROJECT_ID,
                target_ref=f"element:{ELEMENT_ID}",
                arguments={},
                idempotency_key="stale-request",
                expected_object_versions=["project:old:work-graph"],
            ),
        )
    assert not calls
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[project_file_services] = lambda: services
    with TestClient(app) as client:
        response = client.post(
            f"/projects/{PROJECT_ID}/work-graph/nodes/"
            f"interaction:{ELEMENT_ID}/dispatch",
        )
        assert response.status_code == 200, response.text
        assert response.json()["dispatched"]
        assert services.reviews.all_pending(PROJECT_ID)
        graph = client.get(f"/projects/{PROJECT_ID}/work-graph").json()
        node = next(
            n for n in graph["nodes"] if n["id"] == f"interaction:{ELEMENT_ID}"
        )
        assert node["status"] == "waiting_review"
        _decide_all(services, "REJECT")
        response = client.post(
            f"/projects/{PROJECT_ID}/work-graph/nodes/"
            f"interaction:{ELEMENT_ID}/dispatch",
        )
        assert response.status_code == 200 and response.json()["dispatched"]
    assert len(calls) == 2


def test_restart_parks_interrupted_text_task_without_rebilling(tmp_path):
    from services.runtime_files.execution_store import ProjectExecutionStore
    from services.runtime_files.execution_models import TaskRecord
    from services.media_files.interaction_execution import (
        recover_interrupted_interaction_tasks,
    )

    services = _services(tmp_path)
    store = ProjectExecutionStore(services.root)
    store.create_task(
        TaskRecord(
            task_id="interrupted",
            project_id=PROJECT_ID,
            kind="interaction_draft",
            request_fingerprint="sha256:unknown",
            metadata={
                "targetRef": f"element:{ELEMENT_ID}",
                "elementId": ELEMENT_ID,
            },
        ),
    )
    store.append_task_attempt(
        PROJECT_ID,
        "interrupted",
        event_id="attempt-start",
        attempt_id="attempt-1",
        status="RUNNING",
    )
    assert recover_interrupted_interaction_tasks(services) == 1
    assert store.get_task(PROJECT_ID, "interrupted").status.value == "FAILED"
    assert (
        store.list_task_attempts(PROJECT_ID, "interrupted")[-1].status.value
        == "FAILED"
    )
    assert recover_interrupted_interaction_tasks(services) == 0


@pytest.mark.parametrize("project_interface", [False, True])
def test_mainline_interaction_approval_review_and_idle_snapshot_stability(
    tmp_path,
    monkeypatch,
    project_interface,
):
    from services.file_agent_runtime import (
        AgentModelTurn,
        AgentToolCall,
        CallbackAgentChatClient,
        FileCreatorAgentRuntime,
    )
    from services.file_agent_runtime import driver as dm, work_scheduler as sm
    from services.runtime_files.execution_models import (
        ExecutionAuthorizationStatus,
    )
    from services.file_agent_runtime.workgraph_execution import (
        ready_request_context,
    )

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
    calls = _mock_chat(
        monkeypatch,
        [_presentation_html() if project_interface else GOOD_HTML],
    )
    target = (
        f"project:{PROJECT_ID}"
        if project_interface
        else f"element:{ELEMENT_ID}"
    )
    node_id = (
        "interaction:project"
        if project_interface
        else f"interaction:{ELEMENT_ID}"
    )

    async def scenario():
        services = _services(tmp_path, with_runtime=True)
        turns = 0

        async def model(_messages, tools):
            nonlocal turns
            turns += 1
            if turns <= 2:
                manifest = next(
                    t
                    for t in tools
                    if t["function"]["name"] == "request_workgraph_execution"
                )
                assert (
                    "interaction"
                    in manifest["function"]["parameters"]["properties"][
                        "kinds"
                    ]["items"]["enum"]
                )
                return AgentModelTurn(
                    tool_calls=(
                        AgentToolCall(
                            call_id=f"interaction-call-{turns}",
                            name="request_workgraph_execution",
                            arguments={
                                "projectId": PROJECT_ID,
                                "targetRefs": [target],
                                "kinds": ["interaction"],
                            },
                        ),
                    ),
                )
            return AgentModelTurn(content="Motion ready for review.")

        runtime = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(model),
            poll_interval_seconds=0.01,
        )
        try:
            await runtime.start()
            runtime.notify(PROJECT_ID)
            for _ in range(1000):
                auths = runtime.executions.list_execution_authorizations(
                    PROJECT_ID,
                )
                if auths:
                    break
                await asyncio.sleep(0.01)
            assert auths, "Mainline must admit an interaction request"
            assert not calls
            auth = auths[0]
            assert auth.operation == "interaction_draft"
            runtime.executions.decide_execution_authorization(
                PROJECT_ID,
                auth.authorization_id,
                authorization_token=auth.authorization_token,
                status=ExecutionAuthorizationStatus.APPROVED,
                decision={
                    "provider": auth.requested_provider,
                    "model": auth.requested_model,
                    "maxCost": 0,
                    "maxCandidates": 1,
                },
            )
            await runtime.wait_until_idle(PROJECT_ID, timeout_seconds=15)
            assert len(calls) == 1
            assert len(runtime.executions.list_tasks(PROJECT_ID)) == 1
            assert services.reviews.all_pending(PROJECT_ID)
            _, _, graph, blocked = await ready_request_context(
                services,
                runtime.executions,
                PROJECT_ID,
            )
            assert graph.by_id[node_id].status.value == "waiting_review"
            assert blocked[node_id] == "WAITING_REVIEW"
            _decide_all(services)
            baseline = services.projects.read(PROJECT_ID)
            for _ in range(20):
                await runtime.work_scheduler.tick(PROJECT_ID)
                fresh = services.projects.read(PROJECT_ID)
                assert (fresh.generation, fresh.etag) == (
                    baseline.generation,
                    baseline.etag,
                )
            assert len(calls) == 1
            assert not any(
                tid.startswith("snapshot:snapshot:")
                for tid in fresh.project.timelines.order
            )
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_repeated_live_edits_keep_one_frozen_baseline_and_publish_to_live(
    tmp_path,
    monkeypatch,
):
    from services.project_files.agent_tools import (
        AgentProjectTools,
        AgentProjectToolContext,
    )
    from services.runtime_files.models import ChangeOrigin
    from services.file_agent_runtime.work_graph import derive_work_graph
    from services.project_files import auto_snapshot
    from datetime import datetime, timedelta

    services = _services(tmp_path)
    calls = _mock_chat(monkeypatch, [GOOD_HTML])

    async def scenario():
        await execute_file_interaction_command(
            services,
            project_id=PROJECT_ID,
            target_ref=f"element:{ELEMENT_ID}",
            arguments={},
            idempotency_key="history-first",
        )
        _decide_all(services)
        agent = AgentProjectTools(
            services.projects,
            commits=services.commits,
            context=AgentProjectToolContext(
                origin=ChangeOrigin.AGENTDOCK_IDLE_GOAL,
            ),
        )
        agent.read_project(PROJECT_ID)
        for index in range(12):
            agent.patch_project(
                project_id=PROJECT_ID,
                ops=[
                    {
                        "op": "replace",
                        "path": (
                            "/timelines/items/timeline:main/elements_by_id/"
                            f"{ELEMENT_ID}/creation/design_prompt"
                        ),
                        "value": f"Live design revision {index}",
                    },
                ],
            )
            _decide_all(services)
        before = services.projects.read(PROJECT_ID)
        history = [
            tid
            for tid in before.project.timelines.order
            if tid.startswith("snapshot:")
        ]
        assert len(history) == 1
        first_frozen = before.project.timelines.items[history[0]].model_dump(
            mode="json",
        )

        class LaterClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(hours=1)

        monkeypatch.setattr(auto_snapshot, "datetime", LaterClock)
        agent.patch_project(
            project_id=PROJECT_ID,
            ops=[
                {
                    "op": "replace",
                    "path": (
                        "/timelines/items/timeline:main/elements_by_id/"
                        f"{ELEMENT_ID}/creation/design_prompt"
                    ),
                    "value": "Final live design",
                },
            ],
        )
        _decide_all(services)
        before = services.projects.read(PROJECT_ID)
        history = [
            tid
            for tid in before.project.timelines.order
            if tid.startswith("snapshot:")
        ]
        assert len(history) == 2
        frozen = {
            tid: before.project.timelines.items[tid].model_dump(mode="json")
            for tid in history
        }
        assert frozen[history[0]] == first_frozen
        frozen_element = next(iter(first_frozen["elements_by_id"]))
        with pytest.raises(ValidationError):
            await execute_file_interaction_command(
                services,
                project_id=PROJECT_ID,
                target_ref=f"element:{frozen_element}",
                arguments={},
                idempotency_key="frozen-forbidden",
            )
        await execute_file_interaction_command(
            services,
            project_id=PROJECT_ID,
            target_ref=f"element:{ELEMENT_ID}",
            arguments={},
            idempotency_key="history-final",
        )
        _decide_all(services)
        after = services.projects.read(PROJECT_ID)
        assert {
            tid: after.project.timelines.items[tid].model_dump(mode="json")
            for tid in history
        } == frozen
        assert (
            after.project.timelines.items["timeline:main"]
            .elements_by_id[ELEMENT_ID]
            .creation.design_prompt
            == "Final live design"
        )
        assert (
            len(
                [
                    tid
                    for tid in after.project.timelines.order
                    if tid.startswith("snapshot:")
                ],
            )
            == 2
        )
        graph = derive_work_graph(after.project)
        assert graph.by_id[f"interaction:{ELEMENT_ID}"].status.value == "done"
        assert not any("snapshot:" in node.node_id for node in graph.nodes)
        assert len(calls) == 2

    asyncio.run(scenario())


def _presentation_html(color="#f4efdf"):
    from pathlib import Path

    fixture = (
        Path(__file__).resolve().parents[3]
        / "player/tests/fixtures/authored-presentation.html"
    )
    return (
        fixture.read_text()
        .strip()
        .replace("#f4efdf", color)
        .replace(
            "__NODES__",
            "".join(
                '<button data-action="jump" '
                f'data-node-ref="{tid}">{tid}</button>'
                for tid in ("timeline:main", "timeline:ep4a", "timeline:ep4b")
            ),
        )
    )


def test_project_interface_review_revision_and_stability(
    tmp_path,
    monkeypatch,
):
    from services.file_agent_runtime.work_graph import derive_work_graph
    from services.media_files.presentation_authoring import (
        presentation_fingerprint,
    )
    from services.runtime_files.models import ChangeOrigin

    services = _services(tmp_path, with_runtime=True)
    calls = _mock_chat(
        monkeypatch,
        [_presentation_html(), _presentation_html("#fff2dc")],
    )

    def execute(key, guidance=""):
        return asyncio.run(
            execute_file_interaction_command(
                services,
                project_id=PROJECT_ID,
                target_ref=f"project:{PROJECT_ID}",
                arguments={"guidance": guidance},
                idempotency_key=key,
            ),
        )

    execute("page-1")
    snap = services.projects.read(PROJECT_ID)
    assert (
        snap.project.interactive_presentation.motion.html
        == _presentation_html()
    )
    graph = derive_work_graph(
        snap.project,
        pending_reviews=services.reviews.all_pending(PROJECT_ID),
    )
    assert graph.by_id["interaction:project"].status.value == "waiting_review"
    assert "interaction:project" in graph.by_id["bundle:project"].deps
    _decide_all(services)
    assert execute("page-1").replayed
    assert len(calls) == 1
    base = services.projects.read(PROJECT_ID)
    candidate = base.project.model_copy(deep=True)
    before_fp = presentation_fingerprint(candidate)
    candidate.timelines.items["snapshot:timeline:main:1"] = Timeline(
        timeline_id="snapshot:timeline:main:1",
        title="Historical",
    )
    candidate.timelines.order.append("snapshot:timeline:main:1")
    assert presentation_fingerprint(candidate) == before_fp
    candidate.interactive_presentation.design_prompt = "纸本地图布局，墨绿色标记"
    result = services.commits.commit(
        base=base,
        candidate=candidate.model_dump(mode="json"),
        origin=ChangeOrigin.FRONTEND_EDIT,
    )
    assert (
        derive_work_graph(result.snapshot.project)
        .by_id["interaction:project"]
        .status.value
        == "ready"
    )
    execute("page-2")
    assert len(calls) == 2
    _decide_all(services, "REJECT")
    assert (
        services.projects.read(
            PROJECT_ID,
        ).project.interactive_presentation.motion.html
        == _presentation_html()
    )
    assert (
        derive_work_graph(services.projects.read(PROJECT_ID).project)
        .by_id["interaction:project"]
        .status.value
        == "ready"
    )


@pytest.mark.parametrize(
    "bad",
    [
        lambda h: h.replace("<head>", "<head><script>alert(1)</script>"),
        lambda h: h.replace("<video ", '<video src="https://bad.example/" '),
        lambda h: h.replace(
            'data-node-ref="timeline:main"',
            'data-node-ref="missing"',
        ),
        lambda h: h.replace('data-action="start"', 'data-action="execute"'),
        lambda h: h.replace('<button data-action="replay">重新开始</button>', ""),
        lambda h: h.replace('data-action="map"', 'data-action="title"'),
        lambda h: h.replace(
            'data-bind="node.title"',
            'data-bind="project.title"',
        ),
        lambda h: h.replace('data-bind="node.synopsis"', ""),
        lambda h: h.replace(
            'data-screen="ending"',
            'data-screen="ending" data-bind="node.title"',
        ),
        lambda h: h.replace(
            "</style>",
            '@import "https://bad.example/x.css";</style>',
        ),
    ],
)
def test_authored_presentation_rejects_unsafe_or_broken_contract(bad):
    from services.project_files.presentation_html import (
        validate_presentation_html,
    )

    assert validate_presentation_html(
        bad(_presentation_html()),
        ["timeline:main", "timeline:ep4a", "timeline:ep4b"],
    )


def test_structured_controls_repair_label_and_revise_only_page(
    tmp_path,
    monkeypatch,
):
    import json

    from services.media_files.presentation_authoring import (
        presentation_fingerprint,
        presentation_is_current,
    )
    from services.project_files.models import InteractiveScreenDesign
    from services.runtime_files.models import ChangeOrigin

    services = _services(tmp_path)
    base = services.projects.read(PROJECT_ID)
    project = base.project.model_copy(deep=True)
    project.interactive_presentation.screens[
        "title"
    ] = InteractiveScreenDesign.model_validate(
        {
            "design_prompt": "海岸线构图",
            "controls": {
                "start": {
                    "label": "沿海出发",
                    "design_prompt": "朱红印章",
                },
            },
        },
    )
    services.commits.commit(
        base=base,
        candidate=project.model_dump(mode="json"),
        origin=ChangeOrigin.FRONTEND_EDIT,
    )
    html = _presentation_html().replace(">开始</button>", ">沿海出发</button>")
    calls = _mock_chat(monkeypatch, [_presentation_html(), html])
    asyncio.run(
        execute_file_interaction_command(
            services,
            project_id=PROJECT_ID,
            target_ref=f"project:{PROJECT_ID}",
            arguments={},
            idempotency_key="structured-page",
        ),
    )
    assert len(calls) == 2
    inputs = json.loads(calls[0]["prompt"])
    assert inputs["screens"]["title"]["controls"]["start"]["label"] == "沿海出发"
    assert "must honor its declared label" in calls[1]["prompt"]
    result = services.projects.read(PROJECT_ID).project
    assert result.interactive_presentation.motion.html == html
    assert presentation_is_current(result)
    assert result.timelines == project.timelines
    assert result.assets == project.assets
    _decide_all(services)
    old = presentation_fingerprint(result)
    result.interactive_presentation.screens["title"].controls[
        "start"
    ].design_prompt = "淡蓝色细线"
    assert presentation_fingerprint(result) != old
    assert not presentation_is_current(result)


def test_option_appearance_is_a_scoped_generation_input(tmp_path, monkeypatch):
    from services.media_files.interaction_fingerprint import (
        interaction_request_fingerprint,
    )
    from services.media_files.presentation_authoring import (
        presentation_fingerprint,
    )
    from services.runtime_files.models import ChangeOrigin

    services = _services(tmp_path)
    base = services.projects.read(PROJECT_ID)
    project = base.project.model_copy(deep=True)
    creation = (
        project.timelines.items["timeline:main"]
        .elements_by_id[ELEMENT_ID]
        .creation
    )
    edges = {edge.edge_id: edge for edge in project.narrative_edges}
    before = interaction_request_fingerprint(creation, edges, project)
    page_before = presentation_fingerprint(project)
    creation.options[0].design_prompt = "细线勾勒的三角形指路标记"
    assert interaction_request_fingerprint(creation, edges, project) != before
    assert presentation_fingerprint(project) == page_before
    services.commits.commit(
        base=base,
        candidate=project.model_dump(mode="json"),
        origin=ChangeOrigin.FRONTEND_EDIT,
    )
    calls = _mock_chat(monkeypatch, [GOOD_HTML])
    _execute(services)
    assert "细线勾勒的三角形指路标记" in calls[0]["prompt"]


@pytest.mark.parametrize(
    "screens",
    [
        {"unknown": {}},
        {"title": {"controls": {"delete": {}}}},
        {"map": {"controls": {"start": {}}}},
    ],
)
def test_presentation_design_rejects_unknown_screen_actions(screens):
    from pydantic import ValidationError as SchemaError
    from services.project_files.models import InteractivePresentation

    with pytest.raises(SchemaError):
        InteractivePresentation.model_validate({"screens": screens})
