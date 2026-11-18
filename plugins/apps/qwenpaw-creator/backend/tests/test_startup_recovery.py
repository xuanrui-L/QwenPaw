# -*- coding: utf-8 -*-
"""Startup cancellation must release the prepared root before a retry."""

import asyncio
import threading

import pytest

import dev_main
from services.project_files.facade import creator_file_services


def test_cancelled_standalone_startup_recovers_the_same_root_on_retry(
    tmp_path,
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(
        dev_main, "require_creator_data_root", lambda: tmp_path
    )
    monkeypatch.setattr(
        dev_main, "ensure_creator_runtime_dependencies", lambda: {}
    )
    other_root = tmp_path / "unrelated"
    other = creator_file_services(other_root)

    def wait_in_recovery(_services):
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(
        dev_main, "recover_interrupted_source_analysis", wait_in_recovery
    )

    async def start():
        async with dev_main.lifespan(dev_main.app):
            pytest.fail("cancelled startup accepted traffic")

    async def cancel_startup():
        task = asyncio.create_task(start())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(cancel_startup())
        tombstone = tmp_path / ".deleted-on-retry"
        tombstone.mkdir()
        creator_file_services(tmp_path)
        assert not tombstone.exists()
        assert creator_file_services(other_root) is other
    finally:
        dev_main.clear_creator_file_service_registry()
