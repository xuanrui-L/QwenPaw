# -*- coding: utf-8 -*-
"""Sizing of the thread pool that every ``asyncio.to_thread`` call shares."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

IO_THREADS_ENV = "CREATOR_IO_THREADS"
DEFAULT_IO_THREADS = 32

_configured = False


def io_threads_from_env() -> int:
    """Parsed ``CREATOR_IO_THREADS``; ``0`` or below keeps Python's sizing."""

    raw = os.environ.get(IO_THREADS_ENV, "").strip()
    try:
        return int(raw) if raw else DEFAULT_IO_THREADS
    except ValueError:
        return DEFAULT_IO_THREADS


def configure_io_thread_pool() -> int:
    """Give the shared ``to_thread`` pool room to hold background bookkeeping.

    ``asyncio.to_thread`` sizes its default executor at ``cpu + 4`` -- eight
    threads on a four-core container -- while roughly 730 call sites across the
    API and the runtime driver share it. The driver alone wakes once per second
    per Project, so HTTP readers queued behind bookkeeping: measured on eight
    Projects, ``/projects`` took 18-22 s with one browser tab open, 2.3-3.2 s
    with the tab closed, and 6.7-7.1 s whenever the driver's reconcile ceiling
    aged out for every Project at once. Those calls are blocking file and
    provider waits, not computation, so the pool is sized for waiting rather
    than for cores.

    One-shot: a second call leaves the first executor in place instead of
    orphaning its threads. ``CREATOR_IO_THREADS=0`` opts out entirely.
    """

    global _configured  # noqa: PLW0603  one-shot by design
    workers = io_threads_from_env()
    if workers <= 0 or _configured:
        return workers
    _configured = True
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="creator-io",
        ),
    )
    return workers


def reset_io_thread_pool_for_tests() -> None:
    """Drop the one-shot flag so a test can exercise the setter again."""

    global _configured  # noqa: PLW0603  test seam
    _configured = False
