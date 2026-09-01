# -*- coding: utf-8 -*-
# pylint: disable=protected-access,use-implicit-booleaness-not-comparison
"""Runtime notification bus: steer/inject delivery and idempotency."""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from services.file_agent_runtime import notifications as notifications_module
from services.file_agent_runtime.notifications import (
    EVENT_LEVELS,
    NOTIFICATION_SOURCE,
    NOTIFY_AUTONOMOUS_HARD_CAP,
    RuntimeEventKind,
    RuntimeNotificationBus,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project
from services.runtime_files.notification_store import NotificationOutboxStore

pytestmark = pytest.mark.unit

PROJECT_ID = "notify-project"
SESSION_ID = "notify-session"
CONVERSATION_ID = "notify-conversation"


def _services(tmp_path, monkeypatch) -> CreatorFileServices:
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    services = CreatorFileServices.create(tmp_path.resolve())

    def initialize(staged_root) -> None:
        services.sessions.initialize_staged_project(
            staged_root,
            PROJECT_ID,
            session_id=SESSION_ID,
            conversation_id=CONVERSATION_ID,
            initial_goal="build the video",
            goal_id="goal-notify",
            initial_message_id="message-initial",
            initial_client_message_id="client-initial",
        )

    services.projects.create(
        Project.new(project_id=PROJECT_ID, name="Notify"),
        initialize_staged_project=initialize,
    )
    return services


class _Wakes:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, project_id: str) -> None:
        self.calls.append(project_id)


def _bus(services) -> tuple[RuntimeNotificationBus, _Wakes]:
    wakes = _Wakes()
    return (
        RuntimeNotificationBus(services, wake_dispatcher=wakes),
        wakes,
    )


def _user_messages(services):
    return [
        item
        for item in services.sessions.list_messages(
            PROJECT_ID,
            SESSION_ID,
            after_seq=0,
            limit=None,
        )
        if item.role == "user"
    ]


def test_every_event_kind_declares_level() -> None:
    assert set(EVENT_LEVELS) == set(RuntimeEventKind)


def test_steer_appends_one_user_message_and_wakes(
    tmp_path,
    monkeypatch,
) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, wakes = _bus(services)

    asyncio.run(
        bus.notify(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_DETERMINISTIC_FAILURE,
            request_id="detfail-node-1-fp-CODE",
            text="节点 storyboard:e1 生成失败：参考图超出上限。",
            payload={"nodeId": "storyboard:e1"},
        ),
    )

    messages = _user_messages(services)
    assert messages[-1].source == NOTIFICATION_SOURCE
    assert "参考图超出上限" in messages[-1].content_parts[0].text
    assert messages[-1].metadata["notificationKind"] == (
        RuntimeEventKind.NODE_DETERMINISTIC_FAILURE.value
    )
    assert wakes.calls == [PROJECT_ID]


def test_steer_is_idempotent_by_request_id(tmp_path, monkeypatch) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)

    async def deliver_twice() -> None:
        for _ in range(2):
            await bus.steer(
                PROJECT_ID,
                kind=RuntimeEventKind.GRAPH_ALL_DONE,
                request_id="graphdone-g7",
                text="工作图全部节点已完成。",
            )

    asyncio.run(deliver_twice())

    notifications = [
        item
        for item in _user_messages(services)
        if item.source == NOTIFICATION_SOURCE
    ]
    assert len(notifications) == 1


def test_inject_dedupes_against_full_history(tmp_path, monkeypatch) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)

    async def scenario() -> None:
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_SUCCEEDED,
            request_id="node_succeeded-visual:a-fp1",
            text="生成完成：角色设计 A",
        )
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_SUCCEEDED,
            request_id="node_succeeded-visual:a-fp1",
            text="生成完成：角色设计 A",
        )
        # Drain everything, then replaying the same fact must stay dropped.
        await bus.drain_into_resume(PROJECT_ID, assigned_to="digest-run-1")
        await bus.settle_resume(PROJECT_ID, assigned_to="digest-run-1")
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_SUCCEEDED,
            request_id="node_succeeded-visual:a-fp1",
            text="生成完成：角色设计 A",
        )

    asyncio.run(scenario())

    store = bus.store
    assert store.pending_records(PROJECT_ID) == []
    assert len(store._current_versions(PROJECT_ID)) == 1  # noqa: SLF001


def test_steer_folds_pending_quiet_prefix(tmp_path, monkeypatch) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)

    async def scenario() -> None:
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_DISPATCH_STARTED,
            request_id="node_dispatch_started-visual:a-fp1",
            text="已开始生成：角色设计 A",
        )
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_SUCCEEDED,
            request_id="node_succeeded-visual:a-fp1",
            text="生成完成：角色设计 A",
        )
        await bus.steer(
            PROJECT_ID,
            kind=RuntimeEventKind.COMPOSE_COMPLETED,
            request_id="compose-final-fp9",
            text="成片合成完成。",
        )

    asyncio.run(scenario())

    message = _user_messages(services)[-1]
    text = message.content_parts[0].text
    assert "已开始生成：角色设计 A" in text
    assert "生成完成：角色设计 A" in text
    assert "成片合成完成。" in text
    assert bus.store.pending_records(PROJECT_ID) == []


def test_steer_replay_after_partial_drain_converges(
    tmp_path,
    monkeypatch,
) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)
    store = bus.store

    async def scenario() -> str:
        await bus.steer(
            PROJECT_ID,
            kind=RuntimeEventKind.GRAPH_ALL_DONE,
            request_id="graphdone-g3",
            text="工作图全部节点已完成。",
        )
        # A later quiet event gets claimed by a crashed replay of the same
        # steer identity (crash between assign and append).
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_SUCCEEDED,
            request_id="node_succeeded-video:e1-fp2",
            text="生成完成：视频 e1",
        )
        stale_id = (
            "notif-graph_all_done-"
            + hashlib.sha256(b"graphdone-g3").hexdigest()[:24]
        )
        await asyncio.to_thread(store.assign, PROJECT_ID, stale_id)
        # Replay: rendered text now differs from the durable message. The
        # payload conflict must converge without data loss — the anchor is
        # provably inside the durable message, but the late-claimed quiet
        # record was never delivered and must return to PENDING.
        await bus.steer(
            PROJECT_ID,
            kind=RuntimeEventKind.GRAPH_ALL_DONE,
            request_id="graphdone-g3",
            text="工作图全部节点已完成。",
        )
        digest = await bus.drain_into_resume(
            PROJECT_ID,
            assigned_to="digest-after-replay",
        )
        await bus.settle_resume(PROJECT_ID, assigned_to="digest-after-replay")
        return digest

    digest = asyncio.run(scenario())

    notifications = [
        item
        for item in _user_messages(services)
        if item.source == NOTIFICATION_SOURCE
    ]
    assert len(notifications) == 1
    assert "生成完成：视频 e1" not in notifications[0].content_parts[0].text
    # The undelivered fact must reach a durable surface via the digest.
    assert "生成完成：视频 e1" in digest
    states = {
        record.state
        for record in store._current_versions(  # noqa: SLF001
            PROJECT_ID,
        ).values()
    }
    assert states == {"DRAINED"}


def test_steer_survives_transient_append_failure(
    tmp_path,
    monkeypatch,
) -> None:
    """A transient Session write failure must not lose the NEXT_STEP event:
    it stays durable in the outbox and a retry delivers it exactly once."""

    services = _services(tmp_path, monkeypatch)
    bus, wakes = _bus(services)
    original_append = services.sessions.append_message
    failures = {"remaining": 1}

    def flaky_append(*args, **kwargs):
        if failures["remaining"] > 0:
            failures["remaining"] -= 1
            raise OSError("transient session write failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(services.sessions, "append_message", flaky_append)

    delivered_first = asyncio.run(
        bus.steer(
            PROJECT_ID,
            kind=RuntimeEventKind.SUBAGENT_TERMINAL,
            request_id="specialist-run-transient-1",
            text="Specialist 终态 [FAILED]：测试瞬时失败。",
        ),
    )
    assert delivered_first is False
    assert wakes.calls == []
    staged = bus.store.undelivered_records(PROJECT_ID)
    assert [record.request_id for record in staged] == [
        "specialist-run-transient-1",
    ], "the event must stay durable in the outbox after the failed append"

    delivered_retry = asyncio.run(
        bus.steer(
            PROJECT_ID,
            kind=RuntimeEventKind.SUBAGENT_TERMINAL,
            request_id="specialist-run-transient-1",
            text="Specialist 终态 [FAILED]：测试瞬时失败。",
        ),
    )
    assert delivered_retry is True
    notifications = [
        item
        for item in _user_messages(services)
        if item.source == NOTIFICATION_SOURCE
    ]
    assert len(notifications) == 1
    assert "测试瞬时失败" in notifications[0].content_parts[0].text
    assert bus.store.undelivered_records(PROJECT_ID) == []


def test_mark_injected_returns_each_record_once(tmp_path, monkeypatch) -> None:
    """Same-run re-claims must not duplicate the digest on every turn."""

    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)
    asyncio.run(
        bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_SUCCEEDED,
            request_id="node_succeeded-video:e7-fp1",
            text="生成完成：视频 e7",
        ),
    )

    first = bus.store.mark_injected(PROJECT_ID, "agent-run-turns")
    second = bus.store.mark_injected(PROJECT_ID, "agent-run-turns")

    assert [record.request_id for record in first] == [
        "node_succeeded-video:e7-fp1",
    ]
    assert second == []


def test_outbox_survives_store_reopen(tmp_path, monkeypatch) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)

    asyncio.run(
        bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_GATED,
            request_id="node_gated-video:e2-fp3",
            text="节点待条件满足：视频 e2",
        ),
    )

    reopened = NotificationOutboxStore(services.root)
    pending = reopened.pending_records(PROJECT_ID)
    assert [record.request_id for record in pending] == [
        "node_gated-video:e2-fp3",
    ]


def test_autonomous_hard_cap_downgrades_steer_to_inject(
    tmp_path,
    monkeypatch,
) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, wakes = _bus(services)
    for index in range(NOTIFY_AUTONOMOUS_HARD_CAP):
        services.sessions.append_message(
            PROJECT_ID,
            SESSION_ID,
            CONVERSATION_ID,
            role="user",
            content_parts=[{"type": "text", "text": f"自动续跑 {index}"}],
            source="yolo_auto_resume",
        )

    delivered = asyncio.run(
        bus.steer(
            PROJECT_ID,
            kind=RuntimeEventKind.GRAPH_ALL_DONE,
            request_id="graphdone-g5",
            text="工作图全部节点已完成。",
        ),
    )

    assert delivered is False
    assert wakes.calls == []
    assert not [
        item
        for item in _user_messages(services)
        if item.source == NOTIFICATION_SOURCE
    ]
    assert [
        record.request_id for record in bus.store.pending_records(PROJECT_ID)
    ] == ["graphdone-g5"]


def test_human_message_resets_autonomous_streak(
    tmp_path,
    monkeypatch,
) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)
    for index in range(NOTIFY_AUTONOMOUS_HARD_CAP):
        services.sessions.append_message(
            PROJECT_ID,
            SESSION_ID,
            CONVERSATION_ID,
            role="user",
            content_parts=[{"type": "text", "text": f"自动续跑 {index}"}],
            source="yolo_auto_resume",
        )
    services.sessions.append_message(
        PROJECT_ID,
        SESSION_ID,
        CONVERSATION_ID,
        role="user",
        content_parts=[{"type": "text", "text": "请继续"}],
        source="user",
    )

    delivered = asyncio.run(
        bus.steer(
            PROJECT_ID,
            kind=RuntimeEventKind.GRAPH_ALL_DONE,
            request_id="graphdone-g6",
            text="工作图全部节点已完成。",
        ),
    )

    assert delivered is True
    assert _user_messages(services)[-1].source == NOTIFICATION_SOURCE


def test_drain_into_resume_never_steals_a_live_claim(
    tmp_path,
    monkeypatch,
) -> None:
    """A record ASSIGNED to another delivery may be mid-append: the digest
    must leave it alone; the startup sweep is the recovery boundary."""

    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)
    store = bus.store

    async def scenario() -> tuple[str, str]:
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_DISPATCH_STARTED,
            request_id="node_dispatch_started-video:e1-fp1",
            text="已开始生成：视频 e1",
        )
        # Another delivery holds this record between its claim and its
        # append (or a crash left it behind).
        await asyncio.to_thread(
            store.assign,
            PROJECT_ID,
            "digest-crashed",
        )
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_SUCCEEDED,
            request_id="node_succeeded-video:e1-fp1",
            text="生成完成：视频 e1",
        )
        live_digest = await bus.drain_into_resume(
            PROJECT_ID,
            assigned_to="digest-run-9",
        )
        await bus.settle_resume(PROJECT_ID, assigned_to="digest-run-9")
        # Process restart: the sweep reopens the stranded claim, and the
        # next digest delivers it.
        await bus.reopen_all_injected(PROJECT_ID)
        recovered_digest = await bus.drain_into_resume(
            PROJECT_ID,
            assigned_to="digest-run-10",
        )
        await bus.settle_resume(PROJECT_ID, assigned_to="digest-run-10")
        return live_digest, recovered_digest

    live_digest, recovered_digest = asyncio.run(scenario())

    assert (
        "已开始生成：视频 e1" not in live_digest
    ), "a live claim must never be stolen by a concurrent digest"
    assert "生成完成：视频 e1" in live_digest
    assert "已开始生成：视频 e1" in recovered_digest
    states = {
        record.state
        for record in store._current_versions(  # noqa: SLF001
            PROJECT_ID,
        ).values()
    }
    assert states == {"DRAINED"}


def test_drain_into_resume_returns_empty_when_nothing_pends(
    tmp_path,
    monkeypatch,
) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)

    digest = asyncio.run(
        bus.drain_into_resume(PROJECT_ID, assigned_to="digest-run-1"),
    )

    assert digest == ""


def test_cancel_pending_clears_outbox(tmp_path, monkeypatch) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)

    async def scenario() -> str:
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_DISPATCH_STARTED,
            request_id="node_dispatch_started-video:e1-fp1",
            text="已开始生成：视频 e1",
        )
        await bus.cancel_pending(PROJECT_ID)
        return await bus.drain_into_resume(
            PROJECT_ID,
            assigned_to="digest-after-stop",
        )

    digest = asyncio.run(scenario())

    assert digest == ""
    states = {
        record.state
        for record in bus.store._current_versions(  # noqa: SLF001
            PROJECT_ID,
        ).values()
    }
    assert states == {"CANCELLED"}


def test_notify_rejects_undeclared_level(tmp_path, monkeypatch) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)
    monkeypatch.delitem(
        notifications_module.EVENT_LEVELS,
        RuntimeEventKind.NODE_GATED,
    )

    with pytest.raises(ValueError):
        asyncio.run(
            bus.notify(
                PROJECT_ID,
                kind=RuntimeEventKind.NODE_GATED,
                request_id="node_gated-x",
                text="x",
            ),
        )


def _exhaust_hard_cap(services) -> None:
    for index in range(NOTIFY_AUTONOMOUS_HARD_CAP):
        services.sessions.append_message(
            PROJECT_ID,
            SESSION_ID,
            CONVERSATION_ID,
            role="user",
            content_parts=[{"type": "text", "text": f"自动续跑 {index}"}],
            source="yolo_auto_resume",
        )


def test_idle_flush_delivers_parked_next_step_after_cooldown(
    tmp_path,
    monkeypatch,
) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, wakes = _bus(services)
    _exhaust_hard_cap(services)
    asyncio.run(
        bus.steer(
            PROJECT_ID,
            kind=RuntimeEventKind.SUBAGENT_TERMINAL,
            request_id="specialist-run-blocked-1",
            text="Specialist 终态 [BLOCKED]：TTS 服务不可用。",
            payload={
                "specialistRunId": "specialist-run-blocked-1",
                "originSource": "run_review_feedback",
                "originMessageId": "message-review-9",
            },
        ),
    )
    assert bus.store.pending_records(PROJECT_ID) != []
    monkeypatch.setattr(
        notifications_module,
        "NOTIFY_IDLE_FLUSH_COOLDOWN_SECONDS",
        0.0,
    )

    flushed = asyncio.run(bus.flush_pending_on_idle(PROJECT_ID))

    assert flushed is True
    message = _user_messages(services)[-1]
    assert message.source == NOTIFICATION_SOURCE
    assert message.metadata["idleFlush"] is True
    # The event keeps its own identity: kind and payload survive the
    # flush, so delegation-origin restoration and repair budgets still
    # key on the original review feedback.
    assert message.metadata["notificationKind"] == "subagent_terminal"
    assert message.metadata["specialistRunId"] == "specialist-run-blocked-1"
    assert message.metadata["originSource"] == "run_review_feedback"
    assert message.metadata["originMessageId"] == "message-review-9"
    assert "TTS 服务不可用" in message.content_parts[0].text
    assert wakes.calls == [PROJECT_ID]
    assert bus.store.undelivered_records(PROJECT_ID) == []


def test_idle_flush_waits_out_cooldown(tmp_path, monkeypatch) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, wakes = _bus(services)
    asyncio.run(
        bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.SUBAGENT_TERMINAL,
            request_id="specialist-run-blocked-2",
            text="Specialist 终态 [BLOCKED]。",
        ),
    )

    flushed = asyncio.run(bus.flush_pending_on_idle(PROJECT_ID))

    assert flushed is False
    assert asyncio.run(bus.has_flush_candidates(PROJECT_ID)) is False
    assert wakes.calls == []
    assert len(bus.store.pending_records(PROJECT_ID)) == 1


def test_idle_flush_requires_a_next_step_event(tmp_path, monkeypatch) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)
    monkeypatch.setattr(
        notifications_module,
        "NOTIFY_IDLE_FLUSH_COOLDOWN_SECONDS",
        0.0,
    )
    asyncio.run(
        bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_GATED,
            request_id="node_gated-video:e2-fp3",
            text="节点待条件满足：视频 e2",
        ),
    )

    flushed = asyncio.run(bus.flush_pending_on_idle(PROJECT_ID))

    assert flushed is False
    assert len(bus.store.pending_records(PROJECT_ID)) == 1


def test_idle_flush_budget_exhausts_and_resets_on_human(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)
    monkeypatch.setattr(
        notifications_module,
        "NOTIFY_IDLE_FLUSH_COOLDOWN_SECONDS",
        0.0,
    )
    for index in range(notifications_module.NOTIFY_IDLE_FLUSH_BUDGET):
        services.sessions.append_message(
            PROJECT_ID,
            SESSION_ID,
            CONVERSATION_ID,
            role="user",
            content_parts=[{"type": "text", "text": f"兜底投递 {index}"}],
            source=NOTIFICATION_SOURCE,
            metadata={"idleFlush": True},
        )
    asyncio.run(
        bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.SUBAGENT_TERMINAL,
            request_id="specialist-run-blocked-3",
            text="Specialist 终态 [BLOCKED]。",
        ),
    )

    with caplog.at_level("INFO", logger="creator.notifications"):
        # The dispatcher probes every poll tick; only the first blocked
        # attempt may scan and log — later ticks short-circuit silently.
        for _ in range(3):
            assert asyncio.run(bus.flush_pending_on_idle(PROJECT_ID)) is False
    assert (
        sum(
            "idle flush skipped" in record.message for record in caplog.records
        )
        == 1
    )
    assert len(bus.store.pending_records(PROJECT_ID)) == 1

    services.sessions.append_message(
        PROJECT_ID,
        SESSION_ID,
        CONVERSATION_ID,
        role="user",
        content_parts=[{"type": "text", "text": "请继续"}],
        source="user",
    )

    assert asyncio.run(bus.flush_pending_on_idle(PROJECT_ID)) is True
    assert bus.store.undelivered_records(PROJECT_ID) == []


def test_idle_flush_folds_quiet_but_never_steals_live_claims(
    tmp_path,
    monkeypatch,
) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)
    store = bus.store
    monkeypatch.setattr(
        notifications_module,
        "NOTIFY_IDLE_FLUSH_COOLDOWN_SECONDS",
        0.0,
    )

    async def scenario() -> bool:
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_GATED,
            request_id="node_gated-video:e2-fp9",
            text="待条件满足：视频 e2",
        )
        # Another delivery holds this quiet record mid-append: the flush
        # must leave it alone.
        await asyncio.to_thread(store.assign, PROJECT_ID, "digest-live")
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.SUBAGENT_TERMINAL,
            request_id="specialist-run-blocked-4",
            text="Specialist 终态 [BLOCKED]。",
        )
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.NODE_SUCCEEDED,
            request_id="node_succeeded-video:e1-fp1",
            text="生成完成：视频 e1",
        )
        return await bus.flush_pending_on_idle(PROJECT_ID)

    assert asyncio.run(scenario()) is True
    message = _user_messages(services)[-1]
    text = message.content_parts[0].text
    assert "Specialist 终态 [BLOCKED]。" in text
    assert "生成完成：视频 e1" in text
    assert (
        "待条件满足：视频 e2" not in text
    ), "a live foreign claim must never be stolen by the flush"
    assert message.metadata["notificationKind"] == "subagent_terminal"
    states = {
        record.request_id: record.state
        for record in store._current_versions(  # noqa: SLF001
            PROJECT_ID,
        ).values()
    }
    assert states["specialist-run-blocked-4"] == "DRAINED"
    assert states["node_succeeded-video:e1-fp1"] == "DRAINED"
    assert states["node_gated-video:e2-fp9"] == "ASSIGNED"


def test_idle_flush_wave_delivers_each_terminal_under_its_identity(
    tmp_path,
    monkeypatch,
) -> None:
    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)
    monkeypatch.setattr(
        notifications_module,
        "NOTIFY_IDLE_FLUSH_COOLDOWN_SECONDS",
        0.0,
    )

    async def scenario() -> bool:
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.SUBAGENT_TERMINAL,
            request_id="specialist-run-parked-a",
            text="Specialist 终态 [BLOCKED]：A。",
            payload={"specialistRunId": "specialist-run-parked-a"},
        )
        await bus.inject(
            PROJECT_ID,
            kind=RuntimeEventKind.SUBAGENT_TERMINAL,
            request_id="specialist-run-parked-b",
            text="Specialist 终态 [FAILED]：B。",
            payload={"specialistRunId": "specialist-run-parked-b"},
        )
        return await bus.flush_pending_on_idle(PROJECT_ID)

    assert asyncio.run(scenario()) is True
    flushes = [
        item
        for item in _user_messages(services)
        if item.metadata.get("idleFlush")
    ]
    assert [item.metadata["specialistRunId"] for item in flushes] == [
        "specialist-run-parked-a",
        "specialist-run-parked-b",
    ]
    assert bus.store.undelivered_records(PROJECT_ID) == []


def test_outbox_write_after_delete_cannot_recreate_project(
    tmp_path,
    monkeypatch,
) -> None:
    """The lifecycle-guarded write re-checks the Project and must never
    recreate a ghost project directory through the JSONL append."""

    import shutil

    from services.runtime_files.errors import RecordNotFoundError

    services = _services(tmp_path, monkeypatch)
    bus, _wakes = _bus(services)
    project_root = services.root / PROJECT_ID
    shutil.rmtree(project_root)

    with pytest.raises(RecordNotFoundError):
        bus.store.append_pending(
            PROJECT_ID,
            kind="node_gated",
            level="quiet",
            request_id="node_gated-after-delete",
            text="不应写入",
        )

    assert (
        not project_root.exists()
    ), "a write racing DELETE must not recreate the project directory"
