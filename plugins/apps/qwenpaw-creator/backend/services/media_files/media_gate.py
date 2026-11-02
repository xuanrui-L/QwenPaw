# -*- coding: utf-8 -*-
"""One process-wide permit set for the local media subprocesses.

Every render path already caps *its own* fan-out --
``QWENPAW_SEGMENT_CONCURRENCY`` bounds the segments of one rough cut, the work
scheduler bounds the nodes it dispatches -- but the caps multiply instead of
adding up: N nodes in flight each spawn up to three ffmpeg processes, and an
OOMKill does not care how the peak was organized.  This module owns the layer
that was missing: a single gate every ffmpeg spawn crosses.

ffprobe calls are deliberately not gated.  They read a header or a duration,
exit in milliseconds and hold no encoder buffers; gating them would only add
queue latency to the hot paths that matter least.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from typing import Any

from services.observability import mem_probe

ENV_SLOTS = "QWENPAW_FFMPEG_CONCURRENCY"
#: Provisional per-process peak. ``CREATOR_MEM_PROBE`` data replaces it; until
#: then it only ever lowers the gate below :data:`MAX_SLOTS`.
DEFAULT_FFMPEG_PEAK_MB = 120.0
#: One node's own segment ceiling plus one neighbour: beyond this the gate
#: starts serializing work that previously ran at full speed.
MAX_SLOTS = 4
MIN_SLOTS = 1
_KILL_GRACE_SECONDS = 5.0
_BUDGET_NAME = "ffmpeg_processes"
# cgroup v1 reports "no limit" as a page-aligned sentinel near the top of the
# address space; anything that large is not a ceiling worth deriving from.
_UNLIMITED_BYTES = 1 << 50

# Windows has no process sessions; taskkill trees are handled by the callers
# that spawn a browser worker. Everywhere else a new session makes each ffmpeg
# its own group leader, so a stuck filter graph can be taken down as a group.
_GROUPS_SUPPORTED = (
    os.name != "nt"
    and hasattr(
        os,
        "killpg",
    )
    and hasattr(os, "getpgid")
)

_guard = threading.Lock()
_gate: threading.BoundedSemaphore | None = None
_gate_slots = 0
_in_flight = 0
_held = threading.local()


def container_memory_mb() -> float | None:
    """Memory ceiling of this container in MB, or ``None`` when uncapped."""

    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        try:
            with open(path, encoding="ascii") as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if raw == "max":
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value <= 0 or value >= _UNLIMITED_BYTES:
            continue
        return value / (1024 * 1024)
    return None


def slot_count() -> int:
    """How many media processes may run at once in this process."""

    raw = os.environ.get(ENV_SLOTS, "").strip()
    if raw:
        try:
            requested = int(raw)
        except ValueError:
            requested = 0
        if requested >= MIN_SLOTS:
            return requested
    limit_mb = container_memory_mb()
    if limit_mb is None:
        return MAX_SLOTS
    return max(
        MIN_SLOTS,
        min(MAX_SLOTS, int(limit_mb // DEFAULT_FFMPEG_PEAK_MB)),
    )


def _active_gate() -> tuple[threading.BoundedSemaphore, int]:
    """Return the shared gate, sized on first use.

    The size is read once: resizing a live semaphore would either lose permits
    or hand out extra ones, so an operator changes the value and restarts.
    """

    global _gate, _gate_slots

    with _guard:
        if _gate is None:
            _gate_slots = slot_count()
            _gate = threading.BoundedSemaphore(_gate_slots)
        return _gate, _gate_slots


def reset_gate() -> None:
    """Forget the built gate so the next acquire re-reads the environment.

    Test seam only: resetting while a permit is held loses that permit.
    """

    global _gate, _gate_slots

    with _guard:
        _gate = None
        _gate_slots = 0


def active_count() -> int:
    """Media processes currently inside the gate (including this thread's)."""

    with _guard:
        return _in_flight


@contextlib.contextmanager
def slot(where: str) -> Iterator[None]:
    """Hold one media permit while a subprocess runs.

    Not re-entrant: a caller that already holds a permit must not ask for a
    second one, or a saturated gate deadlocks the holder against its own
    neighbour.  Every gated call site wraps exactly one subprocess.
    """

    global _in_flight

    if getattr(_held, "depth", 0):
        nested_into = getattr(_held, "where", "?")
        raise RuntimeError(
            f"ffmpeg slot for {where!r} nests inside {nested_into!r}",
        )
    gate, slots = _active_gate()
    started = time.monotonic()
    gate.acquire()
    with _guard:
        _in_flight += 1
        active = _in_flight
    _held.depth = 1
    _held.where = where
    try:
        mem_probe.budget(
            _BUDGET_NAME,
            active,
            limit=slots,
            site=where,
            queued_seconds=round(time.monotonic() - started, 3),
        )
        yield
    finally:
        _held.depth = 0
        with _guard:
            _in_flight -= 1
        gate.release()


def popen_options(**overrides: Any) -> dict[str, Any]:
    """The ``Popen`` kwargs every gated spawn shares.

    ``start_new_session`` only exists on POSIX; a caller that builds its own
    ``Popen`` still gets the same signal fallback as :func:`run`.
    """

    options = dict(overrides)
    if _GROUPS_SUPPORTED:
        options.setdefault("start_new_session", True)
    return options


def kill_group(
    process: subprocess.Popen,
    grace_seconds: float = _KILL_GRACE_SECONDS,
) -> None:
    """Take a stuck media process down with everything it spawned.

    ``subprocess.run`` kills only its direct child on timeout, and the
    ``TimeoutExpired`` it raises carries no pid, so the group has to be reaped
    here -- while the handle still exists.
    """

    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(process.pid), signal_number)
        except OSError:
            # Already gone, or no group to reach: signal the child itself.
            try:
                process.send_signal(signal_number)
            except OSError:
                pass
        deadline = time.monotonic() + grace_seconds
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if process.poll() is not None:
            return


def run(
    command: Sequence[str | os.PathLike[str]],
    *,
    timeout: float | None = None,
    check: bool = False,
    **child_options: Any,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` behind the gate, with a group-kill on timeout.

    Drop-in for the media call sites: ``capture_output``, ``stdin``, ``text``
    and the rest are forwarded to ``Popen`` unchanged, and the same
    ``CompletedProcess`` / ``TimeoutExpired`` / ``CalledProcessError``
    contract comes back out.
    """

    if child_options.get("capture_output"):
        child_options.setdefault("stdout", subprocess.PIPE)
        child_options.setdefault("stderr", subprocess.PIPE)
        child_options.pop("capture_output")
    argv = [os.fspath(part) for part in command]

    with slot(os.path.basename(argv[0])):
        options = popen_options(**child_options)
        with subprocess.Popen(argv, **options) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_group(process)
                stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(
                    process.args,
                    timeout,
                    output=stdout,
                    stderr=stderr,
                ) from None
            returncode = process.returncode
    if check and returncode:
        raise subprocess.CalledProcessError(
            returncode,
            process.args,
            output=stdout,
            stderr=stderr,
        )
    return subprocess.CompletedProcess(
        process.args,
        returncode,
        stdout,
        stderr,
    )


__all__ = [
    "ENV_SLOTS",
    "MAX_SLOTS",
    "active_count",
    "container_memory_mb",
    "kill_group",
    "popen_options",
    "reset_gate",
    "run",
    "slot",
    "slot_count",
]
