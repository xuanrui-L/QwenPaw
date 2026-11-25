# -*- coding: utf-8 -*-
"""Automatic continuation, bounded retries, and runtime reconciliation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import pytest
from domain.enums import TaskKind
from services.file_agent_runtime import (
    AgentModelError,
    AgentModelTurn,
    FileCreatorAgentRuntime,
)
from services.file_agent_runtime import driver as driver_module
from services.file_agent_runtime.work_graph import (
    WorkGraph,
    WorkNode,
    WorkNodeStatus,
)
from services.runtime_files.models import MessageChannel, MessageClassification
from test_driver import (
    CONVERSATION_ID,
    GOAL_ID,
    PROJECT_ID,
    SESSION_ID,
    _create_project,
    _driver,
    _run_to_idle,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("detail", "expected_code", "expected_retryable"),
    [
        (
            "Creator AgentScope model request failed: Error code: 403 - "
            "{'error': {'code': 'ASP.BIZ.CREDITS_INSUFFICIENT', "
            "'message': '模型 Credits 不足，请先使用贡献值兑换', "
            "'retryable': False, 'type': 'BUSINESS'}, 'request_id': "
            "'70491bf2-9f12-4e43-82c1-bab4a321f647'}",
            "MODEL_QUOTA_EXCEEDED",
            False,
        ),
        (
            "Creator AgentScope model request failed: Error code: 503 - "
            "upstream connect error",
            "MODEL_REQUEST_FAILED",
            True,
        ),
    ],
)
def test_model_request_failure_defers_to_the_provider_retry_verdict(
    tmp_path,
    monkeypatch,
    detail: str,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    """A refused request is not automatically a retriable one.

    Measured on a project that died on its last shot: every turn answered 403
    ``ASP.BIZ.CREDITS_INSUFFICIENT`` with ``retryable: false``, but this branch
    hardcoded ``retryable=True``, so nine identical failures landed inside two
    seconds and the only on-screen words were that the feedback had gone back
    to the Agent. Nothing the Agent can send fixes an empty balance, so the
    run has to stop and say what is missing.
    """

    async def callback(_messages, _tools):
        raise AgentModelError(detail)

    async def scenario():
        services, _snapshot = _create_project(
            tmp_path,
            initial_goal="请生成结果",
        )
        driver = _driver(services, callback)
        resumes: list[bool] = []

        async def spy(**kwargs) -> None:
            resumes.append(bool(kwargs.get("after_failure")))

        monkeypatch.setattr(driver, "_queue_yolo_completion_resume", spy)
        await _run_to_idle(driver, services, error=True)
        session = services.sessions.get_project_session(PROJECT_ID)
        await driver.stop()
        return session, resumes

    session, resumes = asyncio.run(scenario())

    assert session.error["code"] == expected_code
    assert session.error["retryable"] is expected_retryable
    assert session.error["message"] == detail
    # The flag is not only carried to the UI: it is the sole gate on handing an
    # unattended project back to the Agent, so a quota refusal must not queue.
    assert resumes == ([True] if expected_retryable else [])


def test_finishing_handle_cannot_cache_an_unprocessed_user_message(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    services, _ = _create_project(tmp_path, initial_goal=None)
    calls = []

    async def model(*_args, **_kwargs):
        calls.append(True)
        return AgentModelTurn(content="done")

    async def scenario():
        driver = _driver(services, model)
        old = SimpleNamespace(
            task=asyncio.get_running_loop().create_future(), run_id="old-run"
        )
        driver._active[PROJECT_ID] = old
        settle = driver._reconcile_admission_state

        async def finish_while_reading(project_id, session, handle):
            snapshot = await settle(project_id, session, handle)
            old.task.set_result(None)
            driver._active.pop(project_id)
            _append_human_message(services, "Please generate that image now.")
            driver.notify(project_id)
            return snapshot

        with monkeypatch.context() as local:
            local.setattr(
                driver, "_reconcile_admission_state", finish_while_reading
            )
            await driver._reconcile_project(PROJECT_ID)
        await driver._reconcile_project(PROJECT_ID)
        assert PROJECT_ID in driver._active
        await driver.wait_until_idle(PROJECT_ID)
        assert len(calls) == 1

    asyncio.run(scenario())


def _queue_completion_resume(
    driver,
    *,
    run_id="run-completion",
    after_failure=False,
):
    asyncio.run(
        driver._queue_yolo_completion_resume(
            project_id=PROJECT_ID,
            session_id=SESSION_ID,
            conversation_id=CONVERSATION_ID,
            run_id=run_id,
            after_failure=after_failure,
        ),
    )
    return driver.sessions.list_messages(PROJECT_ID, SESSION_ID, limit=None)


@pytest.mark.parametrize("resume_count", [2, 6, 10])
def test_yolo_new_done_allows_same_generation_and_sixth_resume(
    completion_resume_runtime,
    resume_count,
) -> None:
    _services, driver, state = completion_resume_runtime
    for index in range(resume_count):
        state["nodes"].append(
            WorkNode(
                f"visual:{index}",
                "visual",
                f"角色 {index}",
                WorkNodeStatus.DONE,
            ),
        )
        messages = _queue_completion_resume(driver, run_id=f"run-{index}")
        checkpoint = messages[-1].metadata["completionCheckpoint"]
        assert checkpoint["version"] == 1
        assert checkpoint["completedNodeIds"] == [
            f"visual:{i}" for i in range(index + 1)
        ]
        assert checkpoint["unproductiveResumes"] == 0
        assert (
            messages[-1].metadata["projectGeneration"] == state["generation"]
        )
    assert (
        sum(item.source == driver.YOLO_RESUME_SOURCE for item in messages)
        == resume_count
    )
    assert not any(
        item.source == "creator_execution_notice" for item in messages
    )


@pytest.mark.parametrize("generation_churn", [False, True])
def test_yolo_repeated_state_pauses_visibly_once_across_restart(
    completion_resume_runtime,
    monkeypatch,
    generation_churn,
) -> None:
    services, driver, state = completion_resume_runtime
    before = _queue_completion_resume(driver)
    state["generation"] += int(generation_churn)
    node = state["nodes"][0]
    state["nodes"][0] = replace(
        node,
        label="模型声称已经完成",
        missing=tuple(reversed(node.missing)),
        task_id="new-task-id",
        progress=0.9,
        locator={"updatedAt": "later", "modelClaim": "completed"},
    )
    driver._wake.clear()
    messages = _queue_completion_resume(driver, run_id="different-run")
    pause = messages[-1]
    assert pause.role == "assistant"
    assert pause.source == "creator_execution_notice"
    assert (
        pause.metadata["executionPause"]["reason"] == "no_committed_progress"
    )
    assert "未检测到新的节点完成进展" in pause.content_parts[0].text
    assert "video_prompt 缺失" in pause.content_parts[0].text
    assert (
        "检查" in pause.content_parts[0].text
        and "继续" in pause.content_parts[0].text
    )
    assert [item for item in messages if item.role == "user"] == [
        item for item in before if item.role == "user"
    ]
    assert not driver._wake.is_set()
    restarted = _driver(services, lambda _messages, _tools: AgentModelTurn())
    monkeypatch.setattr(restarted.work_scheduler, "enabled", lambda: True)
    monkeypatch.setattr(
        restarted.work_scheduler,
        "wake",
        state["wakes"].append,
    )
    state["generation"] += 1
    assert (
        _queue_completion_resume(restarted, run_id="restart-run") == messages
    )
    events = services.sessions.list_events(PROJECT_ID, SESSION_ID)
    completed = [
        event for event in events if event.event_type == "message.completed"
    ]
    assert len(completed) == 1
    assert completed[0].payload["messageId"] == pause.message_id
    assert (
        services.sessions.get_project_session(
            PROJECT_ID,
        ).last_consumed_message_seq
        == 0
    )
    assert driver.runs.list(PROJECT_ID) == []
    assert driver.executions.list_tasks(PROJECT_ID) == []


def _resume_once(driver, *, run_id, **kwargs):
    asyncio.run(
        driver._queue_yolo_completion_resume(
            project_id=PROJECT_ID,
            session_id=SESSION_ID,
            conversation_id=CONVERSATION_ID,
            run_id=run_id,
            **kwargs,
        ),
    )


def _spy_message_reads(monkeypatch, driver):
    calls = []
    original = driver.sessions.list_messages

    def spy(*args, **kwargs):
        calls.append(args[1:])
        return original(*args, **kwargs)

    monkeypatch.setattr(driver.sessions, "list_messages", spy)
    return calls


def test_fused_pause_memo_skips_the_history_reread_next_tick(
    completion_resume_runtime,
    monkeypatch,
) -> None:
    services, driver, _state = completion_resume_runtime
    _queue_completion_resume(driver, run_id="run-first")
    fused = _queue_completion_resume(driver, run_id="run-fuse")
    notices = [
        item for item in fused if item.source == "creator_execution_notice"
    ]
    assert len(notices) == 1
    calls = _spy_message_reads(monkeypatch, driver)

    _resume_once(driver, run_id="run-second-tick")

    # Nothing moved, so the fused verdict is replayed from the memo: the tick
    # must not rebuild a history whose answer it already has.
    assert calls == []
    assert [
        item
        for item in services.sessions.list_messages(
            PROJECT_ID,
            SESSION_ID,
            limit=None,
        )
        if item.source == "creator_execution_notice"
    ] == notices


def test_fused_pause_memo_releases_when_a_human_message_lands(
    completion_resume_runtime,
    monkeypatch,
) -> None:
    services, driver, _state = completion_resume_runtime
    _queue_completion_resume(driver, run_id="run-first")
    _queue_completion_resume(driver, run_id="run-fuse")
    _append_human_message(services, "把第一场的台词补齐")
    calls = _spy_message_reads(monkeypatch, driver)

    _resume_once(driver, run_id="run-after-human")

    # A new message moved the head, so the fuse has to be re-decided against
    # the real history -- the memo may not swallow a human re-arm.
    assert calls


def test_fused_pause_memo_releases_when_a_task_finishes_without_a_write(
    completion_resume_runtime,
    monkeypatch,
) -> None:
    _services, driver, state = completion_resume_runtime
    _queue_completion_resume(driver, run_id="run-first")
    _queue_completion_resume(driver, run_id="run-fuse")
    # A media task completing is durable progress the Project generation never
    # sees. The fuse reads it as a new DONE node, so the memo key must cover
    # node statuses or a finished shot would stay paused forever.
    state["nodes"].append(
        WorkNode("visual:new", "visual", "新角色", WorkNodeStatus.DONE),
    )
    calls = _spy_message_reads(monkeypatch, driver)

    _resume_once(driver, run_id="run-after-task")

    assert calls


def test_completion_resume_reuses_the_projection_it_was_given(
    completion_resume_runtime,
    monkeypatch,
) -> None:
    services, driver, state = completion_resume_runtime
    snapshot = services.projects.read(PROJECT_ID)
    graph = WorkGraph(
        nodes=tuple(state["nodes"]),
        generation=state["generation"],
    )
    reads = []
    derives = []
    monkeypatch.setattr(
        services.projects,
        "read",
        lambda _project_id: reads.append(_project_id) or snapshot,
    )
    monkeypatch.setattr(
        driver_module,
        "derive_work_graph",
        lambda *_args, **_kwargs: derives.append(1) or graph,
    )

    _resume_once(
        driver,
        run_id="run-tick",
        snapshot=snapshot,
        task_records=[],
        graph=graph,
    )

    assert reads == []
    assert derives == []
    messages = services.sessions.list_messages(
        PROJECT_ID,
        SESSION_ID,
        limit=None,
    )
    assert any(item.source == driver.YOLO_RESUME_SOURCE for item in messages)


def _append_human_message(services, text: str) -> int:
    return services.sessions.append_message(
        PROJECT_ID,
        SESSION_ID,
        CONVERSATION_ID,
        role="user",
        content_parts=[{"type": "text", "text": text}],
        source="initial_creation",
        channel=MessageChannel.COMPOSER,
    ).message.message_seq


def test_prompt_contract_resume_is_not_rearmed_by_an_unrelated_human_turn(
    prompt_contract_runtime,
) -> None:
    services, driver, _state = prompt_contract_runtime
    messages = _queue_completion_resume(driver, run_id="run-1")
    resume = messages[-1]
    assert resume.source == driver.PROMPT_CONTRACT_RESUME_SOURCE
    assert "分镜内容与提示词待同步" in resume.content_parts[0].text
    assert "confirm_prompt_sync" in resume.content_parts[0].text
    assert resume.metadata["actionableFingerprint"]
    # The same gap inside one uninterrupted streak still trips the fuse.
    paused = _queue_completion_resume(driver, run_id="run-2")
    assert paused[-1].source == "creator_execution_notice"
    assert (
        paused[-1].metadata["executionPause"]["reason"]
        == "no_committed_progress"
    )
    # A chat turn that wrote nothing must not re-arm the fuse: the identical
    # instruction was already delivered and is still unanswered.
    human_seq = _append_human_message(services, "你好")
    settled = _queue_completion_resume(driver, run_id="run-3")
    assert settled[-1].message_seq == human_seq
    assert not any(
        item.source == driver.PROMPT_CONTRACT_RESUME_SOURCE
        and item.message_seq > human_seq
        for item in settled
    )


def test_prompt_contract_resume_returns_after_any_committed_write(
    prompt_contract_runtime,
) -> None:
    services, driver, state = prompt_contract_runtime
    resume = _queue_completion_resume(driver, run_id="run-1")[-1]
    _queue_completion_resume(driver, run_id="run-2")
    human_seq = _append_human_message(services, "改一下结尾")
    state["generation"] += 1
    resumed = _queue_completion_resume(driver, run_id="run-3")
    assert resumed[-1].message_seq > human_seq
    assert resumed[-1].source == driver.PROMPT_CONTRACT_RESUME_SOURCE
    assert (
        resumed[-1].metadata["actionableFingerprint"]
        == resume.metadata["actionableFingerprint"]
    )


def test_prompt_contract_resume_still_retries_a_transient_failure(
    prompt_contract_runtime,
) -> None:
    services, driver, _state = prompt_contract_runtime
    _queue_completion_resume(driver, run_id="run-1")
    _queue_completion_resume(driver, run_id="run-2")
    human_seq = _append_human_message(services, "你好")
    retried = _queue_completion_resume(
        driver,
        run_id="run-3",
        after_failure=True,
    )
    assert retried[-1].message_seq > human_seq
    assert retried[-1].source == driver.PROMPT_CONTRACT_RESUME_SOURCE
    assert "瞬态故障" in retried[-1].content_parts[0].text


@pytest.mark.parametrize(
    "change",
    [
        {"missing": ("新的结构缺口",)},
        {"status": WorkNodeStatus.STALE},
        {"error": "prompt preparation failed"},
        {"dispatch_fingerprint": "changed-input"},
        {"regeneration_of": "artifact-version-old"},
    ],
)
def test_yolo_actionable_changes_consume_budget_without_progress_credit(
    completion_resume_runtime,
    change,
) -> None:
    _services, driver, state = completion_resume_runtime
    before = _queue_completion_resume(driver)[-1].metadata[
        "completionCheckpoint"
    ]
    state["generation"] += 1
    state["nodes"][0] = replace(state["nodes"][0], **change)
    after = _queue_completion_resume(driver)[-1].metadata[
        "completionCheckpoint"
    ]
    assert after["actionableFingerprint"] != before["actionableFingerprint"]
    assert after["unproductiveResumes"] == 1


@pytest.mark.parametrize("generation_churn", [False, True])
def test_yolo_five_changed_unproductive_attempts_exhaust_budget(
    completion_resume_runtime,
    generation_churn,
) -> None:
    _services, driver, state = completion_resume_runtime
    for index in range(6):
        state["generation"] += int(generation_churn)
        state["nodes"][0] = replace(
            state["nodes"][0],
            dispatch_fingerprint=f"input-{index}",
        )
        messages = _queue_completion_resume(driver, run_id=f"run-{index}")
        if index < 5:
            assert (
                messages[-1].metadata["completionCheckpoint"][
                    "unproductiveResumes"
                ]
                == index
            )
    assert (
        sum(item.source == driver.YOLO_RESUME_SOURCE for item in messages) == 5
    )
    assert (
        messages[-1].metadata["executionPause"]["reason"]
        == "consecutive_resume_limit"
    )
    assert "预算已耗尽" in messages[-1].content_parts[0].text
    state["generation"] += 1
    state["nodes"][0] = replace(
        state["nodes"][0],
        dispatch_fingerprint="another-input",
    )
    assert (
        _queue_completion_resume(driver, run_id="repeat-budget-check")
        == messages
    )
    state["nodes"].append(
        WorkNode("visual:new", "visual", "新角色", WorkNodeStatus.DONE),
    )
    messages = _queue_completion_resume(driver, run_id="productive-sixth")
    assert (
        messages[-1].metadata["completionCheckpoint"]["unproductiveResumes"]
        == 0
    )
    assert (
        sum(item.source == driver.YOLO_RESUME_SOURCE for item in messages) == 6
    )


def test_yolo_done_union_prevents_toggling_from_earning_progress(
    completion_resume_runtime,
) -> None:
    _services, driver, state = completion_resume_runtime
    done = WorkNode("visual:hero", "visual", "角色", WorkNodeStatus.DONE)
    state["nodes"].append(done)
    _queue_completion_resume(driver)
    state["nodes"][1] = replace(done, status=WorkNodeStatus.READY)
    state["nodes"][0] = replace(
        state["nodes"][0],
        dispatch_fingerprint="input-1",
    )
    checkpoint = _queue_completion_resume(driver)[-1].metadata[
        "completionCheckpoint"
    ]
    assert checkpoint["completedNodeIds"] == [done.node_id]
    assert checkpoint["unproductiveResumes"] == 1
    state["nodes"][1] = done
    state["nodes"][0] = replace(
        state["nodes"][0],
        dispatch_fingerprint="input-2",
    )
    checkpoint = _queue_completion_resume(driver)[-1].metadata[
        "completionCheckpoint"
    ]
    assert checkpoint["unproductiveResumes"] == 2
    state["nodes"].append(
        WorkNode("visual:new", "visual", "新角色", WorkNodeStatus.DONE),
    )
    checkpoint = _queue_completion_resume(driver)[-1].metadata[
        "completionCheckpoint"
    ]
    assert checkpoint["completedNodeIds"] == ["visual:hero", "visual:new"]
    assert checkpoint["unproductiveResumes"] == 0


def test_yolo_absolute_ceiling_survives_notifications_until_human_input(
    completion_resume_runtime,
) -> None:
    services, driver, state = completion_resume_runtime
    for index in range(11):
        state["nodes"].append(
            WorkNode(f"visual:{index}", "visual", "角色", WorkNodeStatus.DONE),
        )
        services.sessions.append_message(
            PROJECT_ID,
            SESSION_ID,
            CONVERSATION_ID,
            role="user",
            content_parts=[{"type": "text", "text": "进度"}],
            source=driver_module.NOTIFICATION_SOURCE,
        )
        messages = _queue_completion_resume(driver, run_id=f"run-{index}")
    assert (
        sum(item.source == driver.YOLO_RESUME_SOURCE for item in messages)
        == 10
    )
    assert (
        messages[-1].metadata["executionPause"]["reason"]
        == "unattended_budget"
    )
    assert "10 次自动续跑上限" in messages[-1].content_parts[0].text
    services.sessions.append_message(
        PROJECT_ID,
        SESSION_ID,
        CONVERSATION_ID,
        role="user",
        content_parts=[{"type": "text", "text": "检查后继续"}],
        source="user",
    )
    assert (
        _queue_completion_resume(driver)[-1].source
        == driver.YOLO_RESUME_SOURCE
    )


@pytest.mark.parametrize("mode", ["manual", "auto_approve"])
@pytest.mark.parametrize("count", [5, 10])
def test_completion_resume_mode_separation_keeps_absolute_ceiling(
    completion_resume_runtime,
    monkeypatch,
    mode,
    count,
) -> None:
    services, driver, state = completion_resume_runtime
    monkeypatch.setattr(driver_module, "get_media_review_mode", lambda: mode)
    other_source = (
        driver.YOLO_RESUME_SOURCE
        if mode == "manual"
        else driver.PROMPT_CONTRACT_RESUME_SOURCE
    )
    for index in range(count):
        services.sessions.append_message(
            PROJECT_ID,
            SESSION_ID,
            CONVERSATION_ID,
            role="user",
            content_parts=[{"type": "text", "text": f"续跑 {index}"}],
            source=other_source,
            metadata={"projectGeneration": state["generation"]},
        )
    feedback = _queue_completion_resume(driver)[-1]
    if count == 10:
        assert (
            feedback.metadata["executionPause"]["reason"]
            == "unattended_budget"
        )
    else:
        expected_source = (
            driver.PROMPT_CONTRACT_RESUME_SOURCE
            if mode == "manual"
            else driver.YOLO_RESUME_SOURCE
        )
        assert feedback.source == expected_source
    if mode == "manual":
        assert state["wakes"] == []


@pytest.mark.parametrize(
    "streak,same_generation",
    [(1, True), (5, False), (4, False)],
)
def test_yolo_legacy_metadata_preserves_generation_and_streak_guards(
    completion_resume_runtime,
    streak,
    same_generation,
) -> None:
    services, driver, state = completion_resume_runtime
    for index in range(streak):
        services.sessions.append_message(
            PROJECT_ID,
            SESSION_ID,
            CONVERSATION_ID,
            role="user",
            content_parts=[{"type": "text", "text": f"旧续跑 {index}"}],
            source=driver.YOLO_RESUME_SOURCE,
            metadata={
                "projectGeneration": state["generation"]
                - int(not same_generation),
            },
        )
    state["nodes"].append(
        WorkNode("visual:done", "visual", "角色", WorkNodeStatus.DONE),
    )
    messages = _queue_completion_resume(driver)
    if streak == 4:
        assert (
            messages[-1].metadata["completionCheckpoint"][
                "unproductiveResumes"
            ]
            == 4
        )
        state["nodes"][0] = replace(
            state["nodes"][0],
            dispatch_fingerprint="new-input",
        )
        messages = _queue_completion_resume(driver)
    assert messages[-1].source == "creator_execution_notice"
    assert messages[-1].metadata["executionPause"]["reason"] == (
        "no_committed_progress"
        if same_generation
        else "consecutive_resume_limit"
    )


@pytest.mark.parametrize("after_failure", [False, True])
def test_yolo_deterministic_errors_precede_generic_feedback(
    completion_resume_runtime,
    after_failure,
) -> None:
    _services, driver, state = completion_resume_runtime
    state["nodes"] = [
        WorkNode(f"script:{i}", "script", f"剧本 {i}", WorkNodeStatus.STALE)
        for i in range(9)
    ] + [
        WorkNode(
            "video:prep",
            "video",
            "生成准备",
            WorkNodeStatus.GATED,
            prompt_sync_required=True,
        ),
        WorkNode("video:ready", "video", "视频派发", WorkNodeStatus.READY),
        WorkNode(
            "visual:failed",
            "visual",
            "角色",
            WorkNodeStatus.FAILED,
            error="invalid reference",
        ),
    ]
    state["failures"] = {
        "video:prep": "PROMPT_PREPARATION_FAILED",
        "video:ready": "reference budget exceeded",
    }
    feedback = _queue_completion_resume(driver, after_failure=after_failure)[
        -1
    ]
    text = feedback.content_parts[0].text
    assert "PROMPT_PREPARATION_FAILED" in text
    assert "reference budget exceeded" in text
    assert "invalid reference" in text
    assert text.index("PROMPT_PREPARATION_FAILED") < text.index("剧本 0")
    assert text.index("invalid reference") < text.index("剧本 0")
    assert "剧本 5" not in text
    assert feedback.metadata["modelRequiredNodes"][:3] == [
        "video:prep",
        "video:ready",
        "visual:failed",
    ]


@pytest.mark.parametrize(
    "status",
    [WorkNodeStatus.READY, WorkNodeStatus.GATED],
)
def test_yolo_scheduler_failure_alone_is_actionable(
    completion_resume_runtime,
    status,
) -> None:
    _services, driver, state = completion_resume_runtime
    state["nodes"][0] = replace(
        state["nodes"][0],
        status=status,
        missing=(),
        authored_text_gap=False,
        prompt_sync_required=True,
    )
    state["failures"] = {"video:ep1": "PROMPT_PREPARATION_FAILED"}
    feedback = _queue_completion_resume(driver)[-1]
    assert feedback.source == driver.YOLO_RESUME_SOURCE
    assert "PROMPT_PREPARATION_FAILED" in feedback.content_parts[0].text
    state["failures"]["video:ep1"] = "another deterministic error"
    feedback = _queue_completion_resume(driver)[-1]
    assert (
        feedback.metadata["completionCheckpoint"]["unproductiveResumes"] == 1
    )
    assert "another deterministic error" in feedback.content_parts[0].text
    assert (
        _queue_completion_resume(driver)[-1].metadata["executionPause"][
            "reason"
        ]
        == "no_committed_progress"
    )


@pytest.mark.parametrize(
    "status",
    [WorkNodeStatus.READY, WorkNodeStatus.RUNNING],
)
def test_yolo_machine_only_work_stays_scheduler_owned(
    completion_resume_runtime,
    status,
) -> None:
    services, driver, state = completion_resume_runtime
    state["nodes"] = [
        WorkNode(
            "video:ep1",
            "video",
            "视频",
            status,
            command="r2v_generation",
        ),
    ]
    before = services.sessions.list_messages(
        PROJECT_ID,
        SESSION_ID,
        limit=None,
    )
    assert _queue_completion_resume(driver) == before
    assert state["wakes"] == [PROJECT_ID]
    assert not driver._wake.is_set()


def test_yolo_transient_failure_without_nodes_still_gets_bounded_resume(
    completion_resume_runtime,
) -> None:
    _services, driver, state = completion_resume_runtime
    state["nodes"] = []
    messages = _queue_completion_resume(driver, after_failure=True)
    assert messages[-1].source == driver.YOLO_RESUME_SOURCE
    assert "瞬态故障" in messages[-1].content_parts[0].text
    messages = _queue_completion_resume(driver, after_failure=True)
    assert (
        messages[-1].metadata["executionPause"]["reason"]
        == "no_committed_progress"
    )


@pytest.mark.parametrize("completion_resume_runtime", [250], indirect=True)
def test_yolo_resumes_after_many_historical_media_tasks(
    completion_resume_runtime,
):
    _services, driver, state = completion_resume_runtime
    history = driver.executions.list_tasks(PROJECT_ID)
    assert len(history) == 250
    assert all(task.kind is TaskKind.IMAGE_GENERATION for task in history)

    messages = _queue_completion_resume(driver)

    assert state["wakes"] == [PROJECT_ID]
    assert messages[-1].source == driver.YOLO_RESUME_SOURCE
    assert messages[-1].metadata["modelRequiredNodes"] == ["video:ep1"]
    assert not any(
        item.source == "creator_execution_notice" for item in messages
    )
    assert driver._wake.is_set()


def test_yolo_no_progress_pause_is_durable_not_a_new_model_request(
    tmp_path,
    monkeypatch,
):
    services, snapshot = _create_project(tmp_path, initial_goal="完成短剧")
    driver = _driver(services, lambda *_args: AgentModelTurn())
    node = WorkNode(
        node_id="visual:hero",
        kind="visual",
        label="角色状态图",
        status=WorkNodeStatus.FAILED,
        error="请修改输入文本",
    )
    monkeypatch.setattr(
        driver_module,
        "get_media_review_mode",
        lambda: "auto_approve",
    )
    monkeypatch.setattr(
        driver_module,
        "derive_work_graph",
        lambda *_a, **_kw: WorkGraph(
            nodes=(node,),
            generation=snapshot.generation,
        ),
    )
    monkeypatch.setattr(
        driver.work_scheduler,
        "wake",
        lambda _project_id: None,
    )
    services.sessions.append_message(
        PROJECT_ID,
        SESSION_ID,
        CONVERSATION_ID,
        role="user",
        source=driver.YOLO_RESUME_SOURCE,
        content_parts=[{"type": "text", "text": "修正输入文本"}],
        metadata={"projectGeneration": snapshot.generation},
    )
    before = services.sessions.list_messages(PROJECT_ID, SESSION_ID)
    asyncio.run(
        driver._queue_yolo_completion_resume(
            project_id=PROJECT_ID,
            session_id=SESSION_ID,
            conversation_id=CONVERSATION_ID,
            run_id="paused-run",
        ),
    )
    after = services.sessions.list_messages(PROJECT_ID, SESSION_ID)
    assert [m.message_id for m in before if m.role == "user"] == [
        m.message_id for m in after if m.role == "user"
    ]
    assert after[-1].source == "creator_execution_notice"
    assert (
        after[-1].metadata["executionPause"]["reason"]
        == "no_committed_progress"
    )
    assert not driver._wake.is_set()


def _quiet_tick_driver(tmp_path):
    """One idle Project whose every message is already consumed."""

    services, _snapshot = _create_project(tmp_path, initial_goal="完成短剧")
    session = services.sessions.get_project_session(PROJECT_ID)
    services.sessions.mark_messages_consumed(
        PROJECT_ID,
        SESSION_ID,
        through_seq=session.last_message_seq,
        goal_id=GOAL_ID,
    )
    driver = _driver(services, lambda _messages, _tools: AgentModelTurn())
    counts = {"messages": 0, "project_read": 0}
    list_messages = driver.sessions.list_messages

    def spy_messages(*args, **kwargs):
        counts["messages"] += 1
        return list_messages(*args, **kwargs)

    driver.sessions.list_messages = spy_messages
    read_project = services.projects.read

    def spy_read(project_id):
        counts["project_read"] += 1
        return read_project(project_id)

    services.projects.read = spy_read
    return services, driver, counts


def test_reconcile_skips_the_chain_while_nothing_durable_moved(
    tmp_path,
) -> None:
    services, driver, counts = _quiet_tick_driver(tmp_path)

    asyncio.run(driver._reconcile_project(PROJECT_ID))
    after_first = dict(counts)
    assert after_first["messages"] > 0  # the chain really ran

    asyncio.run(driver._reconcile_project(PROJECT_ID))
    assert counts == after_first  # quiet tick: no scan, no read, no derivation

    # A new human request must be picked up on the very next tick, not after
    # the quiet ceiling: the pointer moved, so the guard cannot swallow it.
    services.sessions.admit_user_request(
        PROJECT_ID,
        SESSION_ID,
        CONVERSATION_ID,
        request_id="next",
        client_message_id="next",
        content_parts=[{"type": "text", "text": "再加一个镜头"}],
        source="user",
        channel=MessageChannel.COMPOSER,
        classification=MessageClassification.MUTATION_INSTRUCTION,
    )
    asyncio.run(driver._reconcile_project(PROJECT_ID))
    assert counts["messages"] > after_first["messages"]


def test_reconcile_skips_a_deliberately_parked_head(tmp_path) -> None:
    """A parked head is a verdict, not a reason to re-scan every tick.

    Every real Project here holds 3-21 unconsumed automated messages that
    reconcile intentionally does not launch; re-deriving that "no" each tick
    was the whole idle cost.
    """

    services, driver, counts = _quiet_tick_driver(tmp_path)
    admitted = services.sessions.admit_user_request(
        PROJECT_ID,
        SESSION_ID,
        CONVERSATION_ID,
        request_id="parked",
        client_message_id="parked",
        content_parts=[{"type": "text", "text": "先别动这条"}],
        source="user",
        channel=MessageChannel.COMPOSER,
        classification=MessageClassification.MUTATION_INSTRUCTION,
    )
    driver._blocked_heads[PROJECT_ID] = admitted.message.message_seq

    asyncio.run(driver._reconcile_project(PROJECT_ID))
    settled = dict(counts)
    asyncio.run(driver._reconcile_project(PROJECT_ID))
    assert counts == settled  # same head, same decision, no re-scan

    # A second request moves the head, and the next tick must see it at once.
    services.sessions.admit_user_request(
        PROJECT_ID,
        SESSION_ID,
        CONVERSATION_ID,
        request_id="newer",
        client_message_id="newer",
        content_parts=[{"type": "text", "text": "现在改这个"}],
        source="user",
        channel=MessageChannel.COMPOSER,
        classification=MessageClassification.MUTATION_INSTRUCTION,
    )
    asyncio.run(driver._reconcile_project(PROJECT_ID))
    assert counts["messages"] > settled["messages"]


def test_reconcile_quiet_window_expires(tmp_path) -> None:
    _services, driver, counts = _quiet_tick_driver(tmp_path)
    asyncio.run(driver._reconcile_project(PROJECT_ID))
    settled = dict(counts)
    asyncio.run(driver._reconcile_project(PROJECT_ID))
    assert counts == settled

    driver._RECONCILE_QUIET_CEILING_SECONDS = -1.0

    asyncio.run(driver._reconcile_project(PROJECT_ID))
    assert counts["messages"] > settled["messages"]


@pytest.mark.parametrize("status", ["RUNNING", "RESUMING"])
def test_reconcile_never_quiet_while_the_session_is_settling(
    tmp_path,
    status,
) -> None:
    from services.runtime_files.models import CreatorSessionStatus

    services, driver, counts = _quiet_tick_driver(tmp_path)
    asyncio.run(driver._reconcile_project(PROJECT_ID))
    settled = dict(counts)
    services.sessions.set_session_status(
        PROJECT_ID,
        SESSION_ID,
        getattr(CreatorSessionStatus, status),
    )

    asyncio.run(driver._reconcile_project(PROJECT_ID))

    assert counts["messages"] > settled["messages"]


def test_idle_tick_skips_even_the_session_read(tmp_path) -> None:
    """An idle Project must not touch disk beyond a stat pass.

    The quiet guard could only prove the chain was redundant after loading the
    Session, so every tick still paid one shared-lock read of ``session.json``
    per Project -- eight Projects' worth queued against every HTTP reader in
    the same thread pool. The remembered signature leads with a stat
    fingerprint that covers ``session.json`` by mtime and size, so an unchanged
    fingerprint means there is nothing new to read.
    """

    from services.runtime_files.models import CreatorSessionStatus

    services, driver, _counts = _quiet_tick_driver(tmp_path)
    reads: list[str] = []
    real_snapshot = driver.sessions.get_project_session_snapshot

    def spy_snapshot(project_id):
        reads.append(project_id)
        return real_snapshot(project_id)

    driver.sessions.get_project_session_snapshot = spy_snapshot

    asyncio.run(driver._reconcile_project(PROJECT_ID))
    assert reads == [PROJECT_ID]  # nothing remembered: the read is required

    asyncio.run(driver._reconcile_project(PROJECT_ID))
    assert reads == [PROJECT_ID]  # idle tick: no Session read at all

    # A peer moving session.json is seen on the very next tick, not after the
    # quiet ceiling expires: the fingerprint covers that exact file.
    services.sessions.set_session_status(
        PROJECT_ID,
        SESSION_ID,
        CreatorSessionStatus.RUNNING,
    )
    asyncio.run(driver._reconcile_project(PROJECT_ID))
    assert reads.count(PROJECT_ID) == 2


def test_startup_sweep_discovers_ids_without_reading_documents(
    tmp_path,
) -> None:
    """The boot sweep needs identities, not every validated document.

    It used to call ``projects.list()``, which reads, migrates and validates
    every Project before the dispatcher exists -- a full load whose only
    consumer was ``summary.project_id``. Measured on eight Projects that cost
    34 MB and roughly a second of GIL held before the first request could
    land, on top of the sweep's own per-Project work.
    """

    services, _snapshot = _create_project(tmp_path, initial_goal="生成角色图")
    calls: list[str] = []
    real_list = services.projects.list

    def spy_list(*args, **kwargs):
        calls.append("list")
        return real_list(*args, **kwargs)

    services.projects.list = spy_list
    driver = _driver(services, lambda _messages, _tools: AgentModelTurn())

    async def scenario():
        await driver.start()
        await driver.stop()

    asyncio.run(scenario())
    assert calls == []


def test_transient_run_failures_are_classified_by_type_not_wording() -> None:
    """Lock contention must count as retryable, and wording must not decide.

    Field case: an export died on LockTimeoutError inside the catch-all
    branch, which hardcoded retryable=False, so YOLO never retried a failure
    that had changed nothing at all -- the Project sat at 本次执行未完成 with
    nothing willing to try again.

    The other half matters equally: a provider refusal or a validation fault
    must not be swept into this bucket, because those wait for a human by
    design.
    """

    from domain.errors import ValidationError
    from services.runtime_files.errors import LockTimeoutError

    transient = FileCreatorAgentRuntime.TRANSIENT_RUN_FAILURES
    assert LockTimeoutError in transient
    assert TimeoutError in transient
    assert ConnectionError in transient
    # One entry covers the lock family, so no message matching is needed.
    assert issubclass(LockTimeoutError, TimeoutError)
    assert not issubclass(ValidationError, transient)


@pytest.fixture(name="completion_resume_runtime")
def _completion_resume_runtime(tmp_path, monkeypatch, request):
    services, snapshot = _create_project(
        tmp_path,
        initial_goal="完成短剧",
        historical_media_tasks=getattr(request, "param", 0),
    )
    driver = _driver(services, lambda _messages, _tools: AgentModelTurn())
    state = {
        "generation": snapshot.generation,
        "nodes": [
            WorkNode(
                node_id="video:ep1",
                kind="video",
                label="第一场 · 视频",
                status=WorkNodeStatus.GATED,
                missing=("video_prompt 缺失", "dialogue 缺失"),
                authored_text_gap=True,
                dispatch_fingerprint="input-0",
            ),
        ],
        "failures": {},
        "wakes": [],
    }
    monkeypatch.setattr(
        driver_module,
        "get_media_review_mode",
        lambda: "auto_approve",
    )
    monkeypatch.setattr(
        services.projects,
        "read",
        lambda _project_id: replace(snapshot, generation=state["generation"]),
    )
    monkeypatch.setattr(
        driver_module,
        "derive_work_graph",
        lambda *_args, **_kwargs: WorkGraph(
            nodes=tuple(state["nodes"]),
            generation=state["generation"],
        ),
    )
    monkeypatch.setattr(driver.work_scheduler, "enabled", lambda: True)
    monkeypatch.setattr(driver.work_scheduler, "wake", state["wakes"].append)
    monkeypatch.setattr(
        driver.work_scheduler,
        "deterministic_failure_nodes_for_project",
        lambda _project_id: state["failures"],
    )
    return services, driver, state


@pytest.fixture(name="prompt_contract_runtime")
def _prompt_contract_runtime(tmp_path, monkeypatch):
    services, snapshot = _create_project(tmp_path, initial_goal="完成短剧")
    driver = _driver(services, lambda _messages, _tools: AgentModelTurn())
    state = {
        "generation": snapshot.generation,
        "nodes": [
            WorkNode(
                node_id="storyboard:ep1",
                kind="storyboard",
                label="第一场 · 分镜",
                status=WorkNodeStatus.GATED,
                missing=("分镜内容与提示词待同步",),
                authored_text_gap=True,
                prompt_sync_required=True,
                dispatch_fingerprint="input-0",
            ),
        ],
    }
    monkeypatch.setattr(
        driver_module,
        "get_media_review_mode",
        lambda: "manual",
    )
    monkeypatch.setattr(
        services.projects,
        "read",
        lambda _project_id: replace(
            snapshot,
            generation=state["generation"],
        ),
    )
    monkeypatch.setattr(
        driver_module,
        "derive_work_graph",
        lambda *_args, **_kwargs: WorkGraph(
            nodes=tuple(state["nodes"]),
            generation=state["generation"],
        ),
    )
    return services, driver, state
