# -*- coding: utf-8 -*-
"""The「正在停止所有 Agent」incident regression: stop must win under polling.

Production wedge (twice): steady UI polling took shared Runtime locks, the
stop's exclusive writes lost the lock race on every poll tick, and the dock
showed "stopping" forever.  Reads are lock-free now, so an interrupt must
complete promptly no matter how hard the session is being polled.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from services.file_agent_runtime import (
    AgentModelTurn,
    CallbackAgentChatClient,
    FileCreatorAgentRuntime,
)
from services.file_agent_runtime.run_store import CreatorAgentRunStore
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project

pytestmark = pytest.mark.unit

PROJECT_ID = "project-1"
SESSION_ID = "session-1"
CONVERSATION_ID = "conversation-1"
GOAL_ID = "goal-1"


def _create_project(tmp_path):
    services = CreatorFileServices.create(tmp_path.resolve())

    def initialize(staged_root) -> None:
        services.sessions.initialize_staged_project(
            staged_root,
            PROJECT_ID,
            session_id=SESSION_ID,
            conversation_id=CONVERSATION_ID,
            initial_goal="生成一段视频",
            goal_id=GOAL_ID,
            initial_message_id="message-initial",
            initial_client_message_id="client-initial",
        )

    snapshot = services.projects.create(
        Project.new(project_id=PROJECT_ID, name="Stress"),
        initialize_staged_project=initialize,
    )
    services.poller.note_commit(snapshot)
    return services


async def _wait_for(predicate, *, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise TimeoutError("condition was not reached")
        await asyncio.sleep(0.01)


def test_stop_completes_promptly_under_snapshot_hammering(
    tmp_path,
    caplog,
) -> None:
    async def callback(_messages, _tools) -> AgentModelTurn:
        # A hung provider turn: the stop must never wait for it.
        await asyncio.sleep(30)
        return AgentModelTurn(content="不应到达")

    async def scenario():
        services = _create_project(tmp_path)
        driver = FileCreatorAgentRuntime(
            services,
            model_client=CallbackAgentChatClient(callback),
            poll_interval_seconds=0.01,
        )
        runs = CreatorAgentRunStore(services.root)
        await driver.start()
        driver.notify(PROJECT_ID)
        await _wait_for(
            lambda: any(
                run.status.value == "RUNNING" for run in runs.list(PROJECT_ID)
            ),
        )

        stop_polling = threading.Event()
        poll_errors: list[BaseException] = []
        poll_counts = [0] * 4

        def poller(index: int) -> None:
            while not stop_polling.is_set():
                try:
                    session = services.sessions.get_project_session_snapshot(
                        PROJECT_ID,
                    )
                    services.sessions.list_events(
                        PROJECT_ID,
                        session.session_id,
                        after_seq=0,
                        limit=50,
                    )
                    runs.list(PROJECT_ID)
                    poll_counts[index] += 1
                except BaseException as error:  # pragma: no cover
                    poll_errors.append(error)
                    return

        threads = [
            threading.Thread(target=poller, args=(index,))
            for index in range(4)
        ]
        for thread in threads:
            thread.start()
        try:
            await asyncio.sleep(0.2)
            started = time.monotonic()
            assert await driver.interrupt(
                PROJECT_ID,
                reason="user_interrupt",
            )
            await _wait_for(
                lambda: (
                    services.sessions.get_project_session_snapshot(
                        PROJECT_ID,
                    ).active_run_id
                    is None
                ),
                timeout=5.0,
            )
            stop_elapsed = time.monotonic() - started
        finally:
            stop_polling.set()
            for thread in threads:
                thread.join(timeout=5)
        await driver.stop()
        return stop_elapsed, poll_errors, poll_counts

    with caplog.at_level("WARNING"):
        stop_elapsed, poll_errors, poll_counts = asyncio.run(scenario())

    assert not poll_errors
    assert all(count > 0 for count in poll_counts)
    # The incident shape was an unbounded stall (>10s lock timeouts on every
    # attempt); the stop must now finish in seconds regardless of polling.
    assert stop_elapsed < 5.0
    assert "timed out" not in caplog.text
    assert "LockTimeout" not in caplog.text
