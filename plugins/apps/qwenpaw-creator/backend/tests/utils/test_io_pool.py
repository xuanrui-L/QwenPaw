# -*- coding: utf-8 -*-
"""The shared ``to_thread`` pool is widened once, before the driver starts."""

import asyncio

import pytest

from utils import io_pool

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_pool():
    io_pool.reset_io_thread_pool_for_tests()
    yield
    io_pool.reset_io_thread_pool_for_tests()


def test_default_sizing_when_env_is_unset(monkeypatch) -> None:
    monkeypatch.delenv(io_pool.IO_THREADS_ENV, raising=False)
    assert io_pool.io_threads_from_env() == io_pool.DEFAULT_IO_THREADS


def test_env_override_zero_and_junk(monkeypatch) -> None:
    monkeypatch.setenv(io_pool.IO_THREADS_ENV, "12")
    assert io_pool.io_threads_from_env() == 12
    monkeypatch.setenv(io_pool.IO_THREADS_ENV, "0")
    assert io_pool.io_threads_from_env() == 0
    monkeypatch.setenv(io_pool.IO_THREADS_ENV, "not-a-number")
    assert io_pool.io_threads_from_env() == io_pool.DEFAULT_IO_THREADS


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


def test_opt_out_leaves_the_loop_default_alone(monkeypatch) -> None:
    monkeypatch.setenv(io_pool.IO_THREADS_ENV, "0")

    async def scenario():
        loop = asyncio.get_running_loop()
        io_pool.configure_io_thread_pool()
        # pylint: disable-next=protected-access
        return loop._default_executor

    assert asyncio.run(scenario()) is None
