# -*- coding: utf-8 -*-
"""``GET /version`` reports the plugin identity straight from plugin.json."""

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.router import router

import os
import subprocess
import sys
import pytest

from utils import io_pool


def test_version_endpoint_reports_plugin_identity():
    manifest = json.loads(
        (Path(__file__).resolve().parents[3] / "plugin.json").read_text(
            encoding="utf-8",
        ),
    )
    app = FastAPI()
    app.include_router(router)

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.get("/version")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.json() == {
        "plugin_id": manifest["id"],
        "version": manifest["version"],
        "runtime": "creator-filesystem",
    }


BACKEND_DIR = Path(__file__).resolve().parents[2]


ENTRYPOINTS = (
    # The plugin's own entry, then the chains it pulls in the loader's order.
    "main",
    "api.router",
    "services.project_files.facade",
    "services.file_agent_runtime.driver",
)


@pytest.mark.unit
@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_backend_entrypoint_imports_without_a_cycle(entrypoint) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {entrypoint}"],
        cwd=BACKEND_DIR,
        env={**os.environ, "PYTHONPATH": str(BACKEND_DIR)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-4000:]


@pytest.fixture(autouse=True)
def _fresh_pool():
    io_pool.reset_io_thread_pool_for_tests()
    yield
    io_pool.reset_io_thread_pool_for_tests()


@pytest.mark.unit
def test_default_sizing_when_env_is_unset(monkeypatch) -> None:
    monkeypatch.delenv(io_pool.IO_THREADS_ENV, raising=False)
    assert io_pool.io_threads_from_env() == io_pool.DEFAULT_IO_THREADS


@pytest.mark.unit
def test_env_override_zero_and_junk(monkeypatch) -> None:
    monkeypatch.setenv(io_pool.IO_THREADS_ENV, "12")
    assert io_pool.io_threads_from_env() == 12
    monkeypatch.setenv(io_pool.IO_THREADS_ENV, "0")
    assert io_pool.io_threads_from_env() == 0
    monkeypatch.setenv(io_pool.IO_THREADS_ENV, "not-a-number")
    assert io_pool.io_threads_from_env() == io_pool.DEFAULT_IO_THREADS


@pytest.mark.unit
def test_configure_replaces_the_loop_executor_once(monkeypatch) -> None:
    monkeypatch.setenv(io_pool.IO_THREADS_ENV, "11")

    async def scenario():
        loop = asyncio.get_running_loop()
        # pylint: disable-next=protected-access
        assert loop._default_executor is None
        first = io_pool.configure_io_thread_pool()
        # pylint: disable-next=protected-access
        executor = loop._default_executor
        # pylint: disable-next=protected-access
        workers = executor._max_workers
        # A second startup call must keep the first pool rather than orphan
        # its threads behind a fresh executor.
        monkeypatch.setenv(io_pool.IO_THREADS_ENV, "20")
        second = io_pool.configure_io_thread_pool()
        # pylint: disable-next=protected-access
        return first, workers, second, loop._default_executor is executor

    first, workers, second, same_pool = asyncio.run(scenario())
    assert first == 11
    assert workers == 11
    assert second == 20
    assert same_pool


@pytest.mark.unit
def test_opt_out_leaves_the_loop_default_alone(monkeypatch) -> None:
    monkeypatch.setenv(io_pool.IO_THREADS_ENV, "0")

    async def scenario():
        loop = asyncio.get_running_loop()
        io_pool.configure_io_thread_pool()
        # pylint: disable-next=protected-access
        return loop._default_executor

    assert asyncio.run(scenario()) is None
