# -*- coding: utf-8 -*-
"""Opt-in memory probe for the generation pipeline.

Off unless ``CREATOR_MEM_PROBE`` is truthy, so the wiring can stay in the hot
paths permanently. A pod OOMKill never tells you which of three different
problems you have, and each one needs a different number:

* who holds the bytes - RSS of this process **plus its children**. Chromium
  (motion overlay, live operation) and ffmpeg run as subprocesses inside the
  same cgroup and are invisible to every Python-level profiler.
* where Python allocated them - ``tracemalloc`` top sites against a baseline.
* when it happened - a sampler thread writes a curve, so a spike can be lined
  up against the stage markers and the task log.

Output goes to the plugin log and to ``mem-probe-<pid>.jsonl`` under the data
root (about 90 bytes per sample: an hour at 5 s intervals is near 60 KB).
Diagnostics must never break generation, so every entry point swallows its own
failure after warning once.

Beyond the stage markers, four recorders exist because the four suspected
amplifiers are different shapes of problem:

* ``gauge``  - retained decoded frames and unbounded caches, by tracking one
  quantity plus the RSS delta between sightings;
* ``budget`` - declared concurrency limits, against ``peak_children``, which
  is how limits that never coordinate show up;
* ``watch`` / ``orphans`` - children that outlive a cancellation;
* ``held_bytes`` - explicit byte accounting for retained frame arrays.

Read ``py_current_mb`` and ``rss_*`` together rather than trusting either
alone: a freshly calloc'd array shows up in the Python heap immediately while
its untouched pages may never become resident, so the heap says "retained" and
only RSS says "the kernel actually had to give me this memory".
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Mapping

logger = logging.getLogger("qwenpaw").getChild(
    "plugin.qwenpaw_creator.mem_probe",
)

_ENV_FLAG = "CREATOR_MEM_PROBE"
_ENV_INTERVAL = "CREATOR_MEM_PROBE_INTERVAL"
_ENV_TOP = "CREATOR_MEM_PROBE_TOP"
_ENV_DIR = "CREATOR_MEM_PROBE_DIR"

_DEFAULT_INTERVAL_SECONDS = 5.0
_DEFAULT_TOP_SITES = 6
_PAGE_BYTES = 4096
_FALSE_VALUES = ("", "0", "false", "no", "off")

_state_lock = threading.RLock()
_enabled = False
_started_at = 0.0
_baseline: Any = None
_output: Path | None = None
_peak_tree_mb = 0.0
_stop = threading.Event()
_warned = False
_gauge_last: dict[str, float] = {}
_gauge_peak: dict[str, float] = {}
_gauge_rss: dict[str, float] = {}
_budget_peak: dict[str, float] = {}
_peak_children: dict[str, int] = {}
_watches: dict[int, dict[int, dict[str, Any]]] = {}
_watch_seq = 0


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() not in _FALSE_VALUES


def probe_enabled() -> bool:
    """True once ``start_probe`` accepted the env switch."""

    return _enabled


def _warn_once(exc: BaseException) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    logger.warning(
        "memory probe disabled after first failure: %s: %s",
        type(exc).__name__,
        exc,
    )


def _rss_self_mb() -> tuple[float, str]:
    """Resident size of this process, plus where the number came from."""

    try:
        import psutil  # type: ignore[import-not-found]

        return psutil.Process().memory_info().rss / 1048576.0, "psutil"
    except Exception:  # noqa: BLE001 - psutil is optional here
        pass
    try:
        with open("/proc/self/statm", encoding="utf-8") as handle:
            return int(handle.read().split()[1]) * _PAGE_BYTES / 1048576.0, (
                "procfs"
            )
    except (OSError, IndexError, ValueError):
        pass
    import resource

    scale = 1048576.0 if sys.platform == "darwin" else 1024.0
    return (
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale,
        "peak-only",
    )


def _census() -> tuple[float | None, list[dict[str, Any]]]:
    """RSS of this process plus every descendant, and the child rows."""

    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - subprocess sizes stay unknown
        return None, []
    root = psutil.Process()
    members = [root, *root.children(recursive=True)]
    rows: list[dict[str, Any]] = []
    total = 0.0
    for item in members:
        try:
            info = item.memory_info()
            started = item.create_time()
        except Exception:  # noqa: BLE001 - a child may exit mid-scan
            continue
        total += info.rss
        rows.append(
            {
                "pid": item.pid,
                "name": item.name(),
                "rss_mb": round(info.rss / 1048576.0, 1),
                "age_s": round(time.time() - started, 1),
            },
        )
    rows.sort(key=lambda row: row["rss_mb"], reverse=True)
    return round(total / 1048576.0, 1), rows


def _by_exe(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Per-executable process count and total RSS.

    This is what answers "how many ffmpeg are actually alive at once": the
    declared worker limits are per subsystem, so only the process table shows
    the sum across concurrent Tasks.
    """

    out: dict[str, dict[str, float]] = {}
    for row in rows[1:]:
        bucket = out.setdefault(row["name"], {"count": 0, "rss_mb": 0.0})
        bucket["count"] += 1
        bucket["rss_mb"] = round(bucket["rss_mb"] + row["rss_mb"], 1)
    return out


def _cgroup_mb() -> tuple[float | None, float | None]:
    """Used and limit of this container's memory cgroup, if any."""

    for used_path, limit_path in (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        (
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        ),
    ):
        try:
            used = int(Path(used_path).read_text(encoding="utf-8")) / 1048576.0
            raw = Path(limit_path).read_text(encoding="utf-8").strip()
            if raw == "max":
                return round(used, 1), None
            limit = int(raw) / 1048576.0
            if limit > 1e12:  # cgroup v1 sentinel for "no limit"
                return round(used, 1), None
            return round(used, 1), round(limit, 1)
        except (OSError, ValueError):
            continue
    return None, None


def _avail_mb() -> float | None:
    try:
        import psutil  # type: ignore[import-not-found]

        return round(psutil.virtual_memory().available / 1048576.0, 1)
    except Exception:  # noqa: BLE001
        return None


def _python_mb() -> tuple[float | None, float | None]:
    try:
        import tracemalloc

        if not tracemalloc.is_tracing():
            return None, None
        current, peak = tracemalloc.get_traced_memory()
        return round(current / 1048576.0, 1), round(peak / 1048576.0, 1)
    except Exception:  # noqa: BLE001
        return None, None


def _top_sites(limit: int) -> list[str]:
    try:
        import tracemalloc

        if _baseline is None or not tracemalloc.is_tracing():
            return []
        snapshot = tracemalloc.take_snapshot()
        return [
            f"{site.traceback[0].filename.split('/')[-1]}"
            f":{site.traceback[0].lineno} +{site.size_diff / 1024:.0f}KiB"
            for site in snapshot.compare_to(_baseline, "lineno")[:limit]
            if site.size_diff > 0
        ]
    except Exception:  # noqa: BLE001
        return []


def _measure() -> dict[str, Any]:
    global _peak_tree_mb
    self_mb, source = _rss_self_mb()
    tree_mb, rows = _census()
    if tree_mb is not None and tree_mb > _peak_tree_mb:
        _peak_tree_mb = round(tree_mb, 1)
    current_mb, peak_mb = _python_mb()
    used_mb, limit_mb = _cgroup_mb()
    by_exe = _by_exe(rows)
    for name, bucket in by_exe.items():
        _peak_children[name] = max(
            _peak_children.get(name, 0),
            int(bucket["count"]),
        )
    return {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "elapsed_s": round(time.monotonic() - _started_at, 3),
        "rss_self_mb": round(self_mb, 1),
        "rss_tree_mb": tree_mb if tree_mb is not None else round(self_mb, 1),
        "rss_source": source,
        "peak_tree_mb": _peak_tree_mb,
        "py_current_mb": current_mb,
        "py_peak_mb": peak_mb,
        "avail_mb": _avail_mb(),
        "cgroup_used_mb": used_mb,
        "cgroup_limit_mb": limit_mb,
        "children": rows[:5],
        "by_exe": by_exe,
        "peak_children": dict(_peak_children),
    }


def _emit(record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if _output is not None:
        try:
            with _output.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            _warn_once(exc)
    summary = " ".join(
        f"{key}={record[key]}"
        for key in (
            "label",
            "rss_self_mb",
            "rss_tree_mb",
            "peak_tree_mb",
            "py_current_mb",
            "py_peak_mb",
        )
        if key in record
    )
    logger.info("mem_probe %s", summary or line[:300])


def _sample_loop(interval_seconds: float) -> None:
    while not _stop.wait(interval_seconds):
        try:
            with _state_lock:
                record = _measure()
            record["label"] = "sample"
            _emit(record)
        except Exception as exc:  # noqa: BLE001
            _warn_once(exc)
            return


def start_probe(data_root: Path | None = None) -> bool:
    """Turn the probe on from the env switch; idempotent.

    Returns whether it is live, so callers can log the fact without repeating
    the env parsing.
    """

    global _baseline, _enabled, _output, _started_at
    if _enabled:
        return True
    if not _truthy(os.environ.get(_ENV_FLAG)):
        return False
    import tracemalloc

    with _state_lock:
        _started_at = time.monotonic()
        tracemalloc.start()
        _baseline = tracemalloc.take_snapshot()
        directory = (
            Path(os.environ[_ENV_DIR]).expanduser()
            if os.environ.get(_ENV_DIR)
            else (data_root or Path(os.getcwd()))
        )
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _output = directory / f"mem-probe-{os.getpid()}.jsonl"
        except OSError as exc:
            _output = None
            _warn_once(exc)
        _enabled = True
        interval = float(
            os.environ.get(_ENV_INTERVAL) or _DEFAULT_INTERVAL_SECONDS,
        )
        threading.Thread(
            target=_sample_loop,
            args=(max(1.0, interval),),
            name="creator-mem-probe",
            daemon=True,
        ).start()
    logger.info(
        "memory probe ON | interval=%ss output=%s (set %s=0 to silence)",
        interval,
        _output,
        _ENV_FLAG,
    )
    return True


def stage(label: str, **fields: Any) -> None:
    """Mark one pipeline step; no-op unless the probe is live."""

    if not _enabled:
        return
    try:
        with _state_lock:
            record = _measure()
        record["label"] = label
        if fields:
            record.update(
                {
                    key: value
                    for key, value in fields.items()
                    if isinstance(value, (int, float, str, bool))
                    or value is None
                },
            )
        top = int(os.environ.get(_ENV_TOP) or _DEFAULT_TOP_SITES)
        sites = _top_sites(top)
        if sites:
            record["top_allocs"] = sites
        _emit(record)
    except Exception as exc:  # noqa: BLE001
        _warn_once(exc)


def _scalars(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in fields.items()
        if isinstance(value, (int, float, str, bool)) or value is None
    }


def held_bytes(values: Any) -> int:
    """Total buffer bytes of an iterable of arrays or byte strings.

    Sizes are counted rather than inferred from the memory stats, because a
    retained array can be invisible in RSS (untouched copy-on-write pages) and
    because the interesting number for a batch is "how many MiB of frames does
    this list hold", which no allocator reports per container.
    """

    total = 0
    for value in values:
        nbytes = getattr(value, "nbytes", None)
        if isinstance(nbytes, (int, float)):
            total += int(nbytes)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            total += len(value)
    return total


def gauge(label: str, key: str, value: float, **fields: Any) -> None:
    """Track one quantity across sightings, with the RSS change beside it.

    A quantity that only ever grows (cache entries, retained arrays per batch)
    plus a positive ``rss_delta_mb`` is the signature of retention: the peak
    column tells you the worst sighting of the run, and ``py_current_mb``
    separates "held by Python" from "held by a buffer RSS has not committed".
    """

    if not _enabled:
        return
    try:
        with _state_lock:
            record = _measure()
            tracked = f"{label}:{key}"
            previous = _gauge_last.get(tracked)
            previous_rss = _gauge_rss.get(label)
            _gauge_last[tracked] = value
            _gauge_rss[label] = record["rss_tree_mb"]
            _gauge_peak[label] = max(_gauge_peak.get(label, value), value)
        record["label"] = f"gauge:{label}"
        record[key] = value
        record["peak_value"] = _gauge_peak[label]
        if previous is not None:
            record[f"{key}_delta"] = round(value - previous, 2)
        if previous_rss is not None:
            record["rss_delta_mb"] = round(
                record["rss_tree_mb"] - previous_rss,
                1,
            )
        record.update(_scalars(fields))
        _emit(record)
    except Exception as exc:  # noqa: BLE001
        _warn_once(exc)


def budget(
    name: str,
    active: float,
    limit: float | None = None,
    **fields: Any,
) -> None:
    """Record a concurrency budget and the largest value seen for it.

    Each subsystem caps itself, so the caps never add up to anything: compare
    ``peak_children`` in the same record with the sum of the budgets to see the
    real number of local decoders running at once.
    """

    if not _enabled:
        return
    try:
        with _state_lock:
            record = _measure()
            _budget_peak[name] = max(_budget_peak.get(name, active), active)
        record["label"] = f"budget:{name}"
        record["active"] = active
        record["peak_active"] = _budget_peak[name]
        if limit is not None:
            record["limit"] = limit
        record["ffmpeg_alive"] = (
            (record.get("by_exe") or {})
            .get(
                "ffmpeg",
                {},
            )
            .get("count")
        )
        record.update(_scalars(fields))
        _emit(record)
    except Exception as exc:  # noqa: BLE001
        _warn_once(exc)


def watch(label: str = "") -> int:
    """Snapshot live children; hand the token to ``orphans`` later."""

    global _watch_seq
    if not _enabled:
        return 0
    try:
        with _state_lock:
            _watch_seq += 1
            token = _watch_seq
            _watches[token] = {
                row["pid"]: dict(row) for row in _census()[1][1:]
            }
        if label:
            logger.info(
                "mem_probe watch=%s token=%s children=%s",
                label,
                token,
                len(_watches[token]),
            )
        return token
    except Exception as exc:  # noqa: BLE001
        _warn_once(exc)
        return 0


def orphans(token: int, label: str = "") -> list[dict[str, Any]]:
    """Live children compared against a ``watch`` snapshot.

    Two readings come out of one record. ``kept`` are watched children still
    running, which matters when the snapshot was taken around a spawn. ``new``
    are children that appeared after it and are still alive - the shape a
    cancelled render leaves behind, because unwinding the coroutine stops
    neither the worker thread nor the ffmpeg it launched.
    """

    if not _enabled or not token:
        return []
    try:
        watched = _watches.pop(token, None)
        if not watched:
            return []
        alive = {row["pid"]: row for row in _census()[1][1:]}
        rows = [
            {
                "pid": pid,
                "name": row["name"],
                "age_s": row["age_s"],
                "rss_mb": row["rss_mb"],
                "in_watch": pid in watched,
            }
            for pid, row in alive.items()
        ]
        kept = [row for row in rows if row["in_watch"]]
        fresh = [row for row in rows if not row["in_watch"]]
        if kept or fresh:
            record = _measure()
            record["label"] = f"orphans:{label or token}"
            record["watched"] = len(watched)
            record["kept"] = len(kept)
            record["new"] = len(fresh)
            record["new_mb"] = round(
                sum(float(row["rss_mb"] or 0.0) for row in fresh),
                1,
            )
            record["new_pids"] = [
                f"{row['name']}:{row['pid']}@{row['age_s']}s"
                for row in fresh[:12]
            ]
            _emit(record)
            if fresh:
                logger.warning(
                    "mem_probe %s | watched=%s still_running=%s "
                    "spawned_after=%s (%s MB)",
                    label or f"token {token}",
                    len(watched),
                    len(kept),
                    len(fresh),
                    record["new_mb"],
                )
        return rows
    except Exception as exc:  # noqa: BLE001
        _warn_once(exc)
        return []


def stop_probe() -> dict[str, Any] | None:
    """Close the sampler and report the run's summary."""

    global _baseline, _enabled
    if not _enabled:
        return None
    _stop.set()
    with _state_lock:
        summary = _measure()
    summary["label"] = "summary"
    summary["top_allocs"] = _top_sites(
        int(os.environ.get(_ENV_TOP) or _DEFAULT_TOP_SITES),
    )
    _emit(summary)
    logger.warning(
        "memory probe summary | peak_process_tree=%s MB rss_self=%s MB "
        "python_peak=%s MB",
        summary.get("peak_tree_mb"),
        summary.get("rss_self_mb"),
        summary.get("py_peak_mb"),
    )
    _enabled = False
    _baseline = None
    import tracemalloc

    tracemalloc.stop()
    return summary
