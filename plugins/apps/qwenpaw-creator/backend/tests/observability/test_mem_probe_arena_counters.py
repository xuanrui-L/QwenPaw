# -*- coding: utf-8 -*-
# Test seams reach private probe helpers and ignore unused call args.
# pylint: disable=protected-access,unused-argument
"""Arena and fault counters that make the arena-cap trade measurable."""

from __future__ import annotations

import io
import tracemalloc

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


def _patch_open(monkeypatch, path: str, text: str | None) -> None:
    def fake_open(target, *args, **kwargs):
        if target != path or text is None:
            raise FileNotFoundError(target)
        return io.StringIO(text)

    monkeypatch.setattr("builtins.open", fake_open)


@pytest.mark.parametrize(("maps", "arenas"), [(MAPS, 2), (None, None)])
def test_arena_segments_counts_anonymous_64mib_mappings(
    monkeypatch, maps, arenas
):
    _patch_open(monkeypatch, "/proc/self/maps", maps)
    assert mem_probe._arena_segments() == arenas


def test_minor_faults_survive_spaces_in_comm(monkeypatch):
    monkeypatch.setattr(mem_probe, "_last_faults", None)
    monkeypatch.setattr(mem_probe, "_last_fault_at", None)
    _patch_open(monkeypatch, "/proc/self/stat", STAT)
    assert mem_probe._minor_fault_metrics() == (18707, None)

    _patch_open(monkeypatch, "/proc/self/stat", STAT.replace("18707", "18907"))
    faults, rate = mem_probe._minor_fault_metrics()
    assert faults == 18907
    assert rate is not None and rate >= 0


@pytest.mark.parametrize("trace_python", [False, True])
def test_python_tracing_is_opt_in(tmp_path, monkeypatch, trace_python):
    monkeypatch.setenv("CREATOR_MEM_PROBE", "1")
    if trace_python:
        monkeypatch.setenv("CREATOR_MEM_PROBE_PY", "1")
    else:
        monkeypatch.delenv("CREATOR_MEM_PROBE_PY", raising=False)
    for attr, value in (
        ("_enabled", False),
        ("_baseline", None),
        ("_output", None),
    ):
        monkeypatch.setattr(mem_probe, attr, value)

    assert mem_probe.start_probe(tmp_path)
    try:
        assert tracemalloc.is_tracing() is trace_python
        assert (mem_probe._baseline is not None) is trace_python
    finally:
        mem_probe.stop_probe()
