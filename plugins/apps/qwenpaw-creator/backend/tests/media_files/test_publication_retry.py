# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from services.media_files.publication_retry import commit_with_lock_retry
from services.runtime_files.errors import LockTimeoutError

pytestmark = pytest.mark.unit


def test_publication_does_not_retry_non_lock_failures():
    calls = 0

    def broken_commit():
        nonlocal calls
        calls += 1
        raise ValueError("corrupt output")

    with pytest.raises(ValueError, match="corrupt output"):
        asyncio.run(
            commit_with_lock_retry(
                broken_commit,
                project_id="project",
                task_id="task",
            ),
        )
    assert calls == 1


def test_publication_backoff_can_be_cancelled():
    calls = 0

    async def scenario():
        attempted = asyncio.Event()
        loop = asyncio.get_running_loop()

        def busy_commit():
            nonlocal calls
            calls += 1
            loop.call_soon_threadsafe(attempted.set)
            raise LockTimeoutError(Path("project.lock"), 10.0)

        pending = asyncio.create_task(
            commit_with_lock_retry(
                busy_commit,
                project_id="project",
                task_id="task",
            ),
        )
        await attempted.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(scenario())
    assert calls == 1


def test_permanent_publication_contention_is_bounded():
    calls = 0

    def busy_commit():
        nonlocal calls
        calls += 1
        raise LockTimeoutError(Path("project.lock"), 10.0)

    with pytest.raises(LockTimeoutError):
        asyncio.run(
            commit_with_lock_retry(
                busy_commit,
                project_id="project",
                task_id="task",
            ),
        )
    assert calls == 5
