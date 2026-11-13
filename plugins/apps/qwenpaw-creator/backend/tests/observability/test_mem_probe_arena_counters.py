# -*- coding: utf-8 -*-
# Test seams reach private probe helpers and ignore unused call args.
# pylint: disable=protected-access,unused-argument
"""Arena and fault counters that make the arena-cap trade measurable."""

from __future__ import annotations

import io

import pytest

from services.observability import mem_probe

pytestmark = pytest.mark.unit

MAPS = """55a1c0000000-55a1c0041000 r-xp 00000000 08:01 1234 /usr/bin/python
7f0000000000-7f0004000000 rw-p 00000000 00:00 0
7f0008000000-7f000c000000 rw-p 00000000 00:00 0
7f0010000000-7f0011000000 rw-p 00000000 00:00 0
55a1c0100000-55a1c4100000 rw-p 00000000 00:00 0 [heap]
"""

STAT = (
    "102 (qwenpaw app worker) S 1 102 102 0 -1 "
    "4194304 18707 0 0 0 12345 678 0 0 20 0 74 0 99999 "
    "18446744073709551615 0 0 0 0 0 0 0 0 1 0 0 0 17 8 0 0 0 0 0"
)


def _patch_open(monkeypatch, path: str, text: str) -> None:
    def fake_open(target, *args, **kwargs):
        if target != path:
            raise FileNotFoundError(target)
        return io.StringIO(text)

    monkeypatch.setattr("builtins.open", fake_open)


def test_arena_segments_counts_only_anonymous_64mib_mappings(
    monkeypatch,
) -> None:
    """Two arena blocks, one 16 MiB anonymous mapping and a same-sized heap
    must not be counted, and a file-backed mapping neither."""

    _patch_open(monkeypatch, "/proc/self/maps", MAPS)
    assert mem_probe._arena_segments() == 2


def test_arena_segments_returns_none_without_proc(monkeypatch) -> None:
    def always_missing(target, *args, **kwargs):
        raise OSError(target)

    monkeypatch.setattr("builtins.open", always_missing)
    assert (
        mem_probe._arena_segments() is None
    )  # pylint: disable=protected-access


def test_minor_faults_survive_spaces_in_comm(monkeypatch) -> None:
    """Field indices count after comm, which may itself contain spaces and
    parentheses — the reason a plain split() on the whole line is wrong."""

    mem_probe._last_faults = None
    mem_probe._last_fault_at = None
    _patch_open(monkeypatch, "/proc/self/stat", STAT)
    (
        faults,
        rate,
    ) = mem_probe._minor_fault_metrics()  # pylint: disable=protected-access
    assert faults == 18707  # field 10 = minflt
    assert rate is None  # first sample has no window to divide by

    _patch_open(monkeypatch, "/proc/self/stat", STAT.replace("18707", "18907"))
    (
        faults,
        rate,
    ) = mem_probe._minor_fault_metrics()  # pylint: disable=protected-access
    assert faults == 18907
    assert rate is not None and rate >= 0


def test_python_tracing_stays_off_until_asked(tmp_path, monkeypatch) -> None:
    """The probe must not become the load it is measuring.

    tracemalloc.start() records a traceback for every allocation the
    interpreter makes: a live container with two Projects generating spent
    5.75 s of a 30 s GIL window inside take_snapshot and _group_by, while
    RSS -- the number that actually answers "will this OOM" -- costs one
    /proc read.
    """

    import tracemalloc

    monkeypatch.setenv("CREATOR_MEM_PROBE", "1")
    monkeypatch.delenv("CREATOR_MEM_PROBE_PY", raising=False)
    monkeypatch.setattr(mem_probe, "_enabled", False)
    monkeypatch.setattr(mem_probe, "_baseline", None)
    monkeypatch.setattr(mem_probe, "_output", None)

    assert mem_probe.start_probe(tmp_path) is True
    try:
        assert tracemalloc.is_tracing() is False
        assert mem_probe._baseline is None
    finally:
        mem_probe.stop_probe()


def test_python_tracing_can_be_turned_on_for_a_memory_hunt(
    tmp_path,
    monkeypatch,
) -> None:
    """The capability stays available, just not always paid for."""

    import tracemalloc

    monkeypatch.setenv("CREATOR_MEM_PROBE", "1")
    monkeypatch.setenv("CREATOR_MEM_PROBE_PY", "1")
    monkeypatch.setattr(mem_probe, "_enabled", False)
    monkeypatch.setattr(mem_probe, "_baseline", None)
    monkeypatch.setattr(mem_probe, "_output", None)

    assert mem_probe.start_probe(tmp_path) is True
    try:
        assert tracemalloc.is_tracing() is True
        assert mem_probe._baseline is not None
    finally:
        mem_probe.stop_probe()
