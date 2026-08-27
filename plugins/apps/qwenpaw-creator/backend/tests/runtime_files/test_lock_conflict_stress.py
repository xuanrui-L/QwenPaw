# -*- coding: utf-8 -*-
"""Simulated lock-conflict stress: historical incident classes stay dead.

Each test reproduces one conflict shape from the production incidents that
motivated the in-process lock redesign: polling readers starving a writer,
lost updates on one record, lifecycle shared/exclusive interplay, and torn
JSONL tails racing lock-free readers.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from services.runtime_files.atomic_store import AtomicJsonRecordStore
from services.runtime_files.jsonl_store import DurableJsonlStore
from services.runtime_files.locking import CrossProcessFileLock


class EventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker: int
    value: int


def test_hammering_readers_never_starve_or_slow_a_writer(tmp_path):
    """The「停止按钮永远转圈」class: reads must not contend with writes at all."""

    stream = DurableJsonlStore(tmp_path / "events.jsonl", EventRecord)
    record = AtomicJsonRecordStore(tmp_path / "session.json")
    record.write({"status": "IDLE", "revision": 0})
    stop = threading.Event()
    reader_errors: list[BaseException] = []
    read_counts = [0] * 6

    def reader(index: int) -> None:
        while not stop.is_set():
            try:
                for envelope in stream.read_all():
                    EventRecord.model_validate(envelope.record)
                stream.read_records_after(0, limit=20)
                stream.last_seq()
                value = record.read_or_none()
                assert value is None or isinstance(value["revision"], int)
                read_counts[index] += 1
            except BaseException as error:  # pragma: no cover - asserted
                reader_errors.append(error)
                return

    threads = [
        threading.Thread(target=reader, args=(index,)) for index in range(6)
    ]
    for thread in threads:
        thread.start()
    try:
        started = time.monotonic()
        for value in range(1, 201):
            stream.append(EventRecord(worker=0, value=value))
            record.update(
                lambda current: {
                    "status": "RUNNING",
                    "revision": current["revision"] + 1,
                },
            )
        writer_elapsed = time.monotonic() - started
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)
    assert not reader_errors
    assert all(not thread.is_alive() for thread in threads)
    # 400 durable fsync writes under full-speed hammering from 6 readers.
    # Before the redesign every read also took the write lock and a single
    # writer routinely hit the 10s timeout; lock-free reads make writer
    # latency independent of read pressure.
    assert writer_elapsed < 20.0
    assert [e.seq for e in stream.read_all()] == list(range(1, 201))
    assert record.read()["revision"] == 200
    assert all(count > 0 for count in read_counts)


def test_concurrent_updates_to_one_record_lose_nothing(tmp_path):
    """Mutual exclusion of the in-process per-path lock: no lost updates."""

    record = AtomicJsonRecordStore(tmp_path / "counter.json")
    record.write({"value": 0})
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(40):
                record.update(
                    lambda current: {"value": current["value"] + 1},
                )
        except BaseException as error:  # pragma: no cover - asserted
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert record.read()["value"] == 200


def test_lifecycle_shared_holders_overlap_and_exclude_the_exclusive_side(
    tmp_path,
):
    """Runtime writers share the lifecycle lock; delete/commit waits."""

    path = tmp_path / "project-lifecycle.lock"
    first_in = threading.Event()
    second_in = threading.Event()
    release_shared = threading.Event()
    order: list[str] = []

    def shared_holder(name: str, entered: threading.Event) -> None:
        with CrossProcessFileLock(path, shared=True, timeout_seconds=5):
            entered.set()
            release_shared.wait(timeout=5)
            order.append(f"{name}-released")

    def exclusive_writer() -> None:
        with CrossProcessFileLock(path, timeout_seconds=5):
            order.append("exclusive-acquired")

    holders = [
        threading.Thread(target=shared_holder, args=("a", first_in)),
        threading.Thread(target=shared_holder, args=("b", second_in)),
    ]
    for thread in holders:
        thread.start()
    # Both shared holders must be inside simultaneously (no serialization).
    assert first_in.wait(timeout=2)
    assert second_in.wait(timeout=2)
    writer = threading.Thread(target=exclusive_writer)
    writer.start()
    time.sleep(0.05)
    assert "exclusive-acquired" not in order
    release_shared.set()
    writer.join(timeout=5)
    for thread in holders:
        thread.join(timeout=5)
    assert order[-1] == "exclusive-acquired"


def test_torn_crash_tail_never_breaks_lockfree_readers(tmp_path):
    """A crash fragment is invisible to readers and repaired by append."""

    path = tmp_path / "events.jsonl"
    stream = DurableJsonlStore(path, EventRecord)
    for value in range(1, 4):
        stream.append(EventRecord(worker=1, value=value))
    with path.open("ab") as handle:
        handle.write(b'{"seq":4,"record":{"worker":1')

    errors: list[BaseException] = []

    def reader() -> None:
        try:
            for _ in range(50):
                assert [e.seq for e in stream.read_all()] == [1, 2, 3]
                assert stream.last_seq() == 3
        except BaseException as error:  # pragma: no cover - asserted
            errors.append(error)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    # Reads never truncated the fragment; only the next append repairs it.
    assert not path.read_bytes().endswith(b"\n")
    assert stream.append(EventRecord(worker=1, value=4)).seq == 4
    assert [e.seq for e in stream.read_all()] == [1, 2, 3, 4]


def test_writers_on_different_paths_never_contend(tmp_path):
    """Domain sharding: unrelated records must not serialize each other."""

    first = AtomicJsonRecordStore(tmp_path / "a" / "record.json")
    second = AtomicJsonRecordStore(tmp_path / "b" / "record.json")
    first.write({"value": 0})
    second.write({"value": 0})
    barrier = threading.Barrier(2)
    elapsed: dict[str, float] = {}
    errors: list[BaseException] = []

    def worker(name: str, store: AtomicJsonRecordStore) -> None:
        try:
            barrier.wait(timeout=5)
            started = time.monotonic()
            for _ in range(100):
                store.update(
                    lambda current: {"value": current["value"] + 1},
                )
            elapsed[name] = time.monotonic() - started
        except BaseException as error:  # pragma: no cover - asserted
            errors.append(error)

    threads = [
        threading.Thread(target=worker, args=("first", first)),
        threading.Thread(target=worker, args=("second", second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert first.read()["value"] == 100
    assert second.read()["value"] == 100
    assert elapsed["first"] < 15.0 and elapsed["second"] < 15.0


def test_no_lock_files_appear_under_stress(tmp_path):
    """The zero-lock-file invariant holds under concurrent load."""

    stream = DurableJsonlStore(tmp_path / "events.jsonl", EventRecord)

    def worker(index: int) -> None:
        for value in range(20):
            stream.append(EventRecord(worker=index, value=value))

    threads = [
        threading.Thread(target=worker, args=(index,)) for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    leftovers = [
        entry
        for entry in Path(tmp_path).rglob("*")
        if ".lock" in entry.name or entry.name.endswith(".readers")
    ]
    assert leftovers == []
    assert len(stream.read_all()) == 80
