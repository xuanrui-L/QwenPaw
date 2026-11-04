# -*- coding: utf-8 -*-
"""Collapse legacy project echoes inside stored conversations, by itself.

``messages.jsonl`` used to append every ``read_project`` / ``jq_project``
result verbatim, so one imported Project carried 606 MB of conversation for
2952 messages -- 205 KB a record -- and every tick of its runtime re-read the
whole stream.  The read path stopped *showing* those bytes long ago:
``_elide_stale_snapshots`` collapses each superseded snapshot to a mutation
receipt before the model sees it, and new writes store that bounded view
directly.  This module recovers the surplus that is already invisible.

It runs the repair in a child process on purpose.  Collapsing means reading a
session stream whole, and doing that inside the server that is serving it is
the exact shape that ran the customer out of memory.  A supervised child keeps
the app upright -- an out-of-memory attempt costs one attempt, not the process
-- and it means nobody has to open a shell to reach a pod for the repair to
happen.  The script stays the single implementation, so its backup, its
grow-detection and its after-the-fact verification cannot drift from what the
product actually runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.runtime_files.atomic_store import AtomicJsonRecordStore

logger = logging.getLogger(__name__)

ELISION_FILE_NAME = ".storage-elision.json"
ELISION_SCRIPT = "scripts/elide_stored_project_snapshots.py"

# Project directories are named after the id, which carries this prefix; the
# script takes the id and re-adds it, so the two meet in the middle here.
PROJECT_DIR_PREFIX = "project-"

# Below this, a session file cannot pay for the process that would trim it.
MIN_ELISION_BYTES = 8 * 1024 * 1024

# Above this, the collapse is left alone until somebody with a bigger machine
# asks for it.  Measured on the field stream: 606 MB in, 2,314 MB of resident
# memory at the peak -- about four times the file.  A child shares its
# container's memory budget with the app, so an unbounded stream is a way for a
# cleanup to become the very incident it is trying to prevent.
MAX_AUTO_ELISION_BYTES_ENV = "CREATOR_MESSAGE_ELISION_MAX_BYTES"
MAX_AUTO_ELISION_DEFAULT = 64 * 1024 * 1024

# The child asks to be killed first.  Its peak belongs to the same cgroup as
# the server, and when the limit is reached the kernel picks by score: this
# is the difference between losing a cleanup pass and losing the app.
CHILD_OOM_SCORE_ADJ = "800"

# Generous, because the point is to survive a stream hundreds of times larger
# than anything that should have needed this at all.
ELISION_TIMEOUT_SECONDS = 900.0

_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})


def elision_enabled() -> bool:
    """Whether stored conversations may be collapsed; off only on request.

    Deleting nothing here -- every record is rewritten to text the runtime was
    already substituting, and the script copies each file to a backup root
    first -- so the switch exists for the case where a person wants their
    history byte-for-byte as it was written.
    """

    raw = os.environ.get("CREATOR_MESSAGE_ELISION_ENABLED", "")
    return raw.strip().casefold() not in _DISABLED_VALUES


@dataclass(frozen=True, slots=True)
class ElisionSummary:
    """What one background elision pass took on."""

    projects: int = 0
    files: int = 0
    saved_bytes: int = 0
    oversize_files: int = 0
    oversize_bytes: int = 0
    failures: list[str] = field(default_factory=list)


def max_auto_elision_bytes() -> int:
    """The largest stream this install lets the background collapse touch."""

    raw = os.environ.get(MAX_AUTO_ELISION_BYTES_ENV, "").strip()
    try:
        value = int(raw) if raw else MAX_AUTO_ELISION_DEFAULT
    except ValueError:
        return MAX_AUTO_ELISION_DEFAULT
    return value if value > 0 else MAX_AUTO_ELISION_DEFAULT


def _ledger(data_root: Path) -> AtomicJsonRecordStore[dict[str, Any]]:
    return AtomicJsonRecordStore(data_root / ELISION_FILE_NAME, None)


def _handled_bytes(
    stored: dict[str, Any],
    project_name: str,
    file_name: str,
) -> int:
    """Size one session file was last left at, or ``-1`` if never visited.

    Per file, not per Project: one Project can hold a 606 MB stream beside a
    9 MB one, and a shared floor would hide whichever of the two shrank.
    """

    entry = stored.get(project_name)
    if not isinstance(entry, dict):
        return -1
    files = entry.get("files")
    if not isinstance(files, dict):
        return -1
    value = files.get(file_name)
    return value if isinstance(value, int) else -1


def _candidates(
    data_root: Path,
    stored: dict[str, Any],
    ceiling: int,
) -> tuple[dict[str, list[Path]], int, int]:
    """Session files big enough to matter and not already left bounded.

    A file is retried only once it grows past the size the last attempt ended
    at, so a stream that cannot be collapsed -- or an attempt that died --
    costs one pass, not a full re-read of hundreds of megabytes every boot.

    Files above ``ceiling`` are counted and reported instead of touched: they
    are the ones whose collapse could take the container down with it, and an
    operator has to be able to see them to decide otherwise.
    """

    grouped: dict[str, list[Path]] = {}
    oversize_files = 0
    oversize_bytes = 0
    session_roots = sorted(
        data_root.glob(f"{PROJECT_DIR_PREFIX}*/runtime/sessions"),
    )
    for directory in session_roots:
        project_name = directory.parent.parent.name
        for path in sorted(
            directory.glob("*/messages.jsonl"),
        ):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < MIN_ELISION_BYTES:
                continue
            if size > ceiling:
                oversize_files += 1
                oversize_bytes += size
                continue
            if size <= _handled_bytes(stored, project_name, path.parent.name):
                continue
            grouped.setdefault(project_name, []).append(path)
    return grouped, oversize_files, oversize_bytes


def _remember(
    data_root: Path,
    project_name: str,
    paths: list[Path],
    moment: datetime,
) -> None:
    """Record where the pass left each file, so the next boot can skip it."""

    store = _ledger(data_root)
    try:
        stored = dict(store.read_or_none() or {})
    except OSError:
        logger.warning("elision ledger cannot be read: %s", store.path)
        return
    sizes = {}
    for path in paths:
        try:
            sizes[path.parent.name] = path.stat().st_size
        except OSError:
            continue
    if not sizes:
        return
    previous = stored.get(project_name)
    merged: dict[str, Any] = {}
    if isinstance(previous, dict) and isinstance(previous.get("files"), dict):
        merged.update(previous["files"])
    merged.update(sizes)
    stored[project_name] = {
        "at": moment.isoformat(),
        "files": merged,
    }
    try:
        store.write(stored)
    except OSError:
        logger.warning("elision ledger cannot be written: %s", store.path)


def _spawn_argv(
    script: Path,
    data_root: Path,
    project_scope: str,
) -> list[str]:
    """Command line for the child, asked to be the one the OOM killer picks.

    Only Linux exposes ``oom_score_adj``, and only raising it (never lowering
    it) is allowed without privileges; where either is unavailable the child
    simply runs as itself.
    """

    command = [
        sys.executable,
        str(script),
        "--data-root",
        str(data_root),
        "--project-id",
        project_scope,
    ]
    if sys.platform != "linux":
        return command
    return [
        "/bin/sh",
        "-c",
        (
            'echo "$1" > /proc/self/oom_score_adj 2>/dev/null; '
            'shift 1; exec "$@"'
        ),
        "creator-elision",
        CHILD_OOM_SCORE_ADJ,
        *command,
    ]


async def _run_one(
    data_root: Path,
    project_scope: str,
) -> tuple[bool, str]:
    """Collapse one Project's sessions in a child process; never raise.

    ``project_scope`` is the bare id the script expects -- it rebuilds the
    directory name by prepending ``project-`` itself -- so the caller decides
    what is addressed and a wrong scope shows up here rather than in a child
    that reports a clean run over an empty list.
    """

    script = Path(__file__).resolve().parents[2] / ELISION_SCRIPT
    env = dict(os.environ)
    env["CREATOR_DATA_ROOT"] = str(data_root)
    try:
        process = await asyncio.create_subprocess_exec(
            *_spawn_argv(script, data_root, project_scope),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except OSError as exc:
        return False, f"cannot start elision: {exc}"
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=ELISION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return False, "elision timed out"
    except asyncio.CancelledError:
        # Shutdown cancels the maintenance pass; a child left rewriting a
        # session file would outlive the app that owns the data root.
        process.kill()
        await process.wait()
        raise
    text = (output or b"").decode("utf-8", "replace").strip()
    if process.returncode != 0:
        return False, _tail(text)
    return True, text


def _tail(text: str) -> str:
    """The last few lines of a dead child, one line of which is the reason.

    A traceback puts the exception near the end and a pydantic report puts the
    useful sentence above it, so the last line alone is usually a doc link.
    """

    if not text:
        return "elision exited with a nonzero status"
    lines = [line for line in text.splitlines() if line.strip()][-4:]
    return " | ".join(line.strip() for line in lines)[:400]


async def elide_stored_conversations(services: Any) -> ElisionSummary:
    """Walk every Project with a collapsible conversation and trim it.

    Called off the startup path, once per boot: the sweeps it sits beside are
    cheap directory walks, while this one reads whole files, so it is the only
    maintenance action given its own process -- and the only one with a size
    ceiling, because that process pays for the stream several times over.
    """

    data_root = Path(services.root)
    if not elision_enabled() or not data_root.is_dir():
        return ElisionSummary()
    store = _ledger(data_root)
    try:
        stored = dict(store.read_or_none() or {})
    except OSError:
        stored = {}
    candidates, oversize_files, oversize_bytes = _candidates(
        data_root,
        stored,
        max_auto_elision_bytes(),
    )
    summary_projects = 0
    files = 0
    saved = 0
    failures: list[str] = []
    for project_name, paths in candidates.items():
        before = sum(path.stat().st_size for path in paths)
        moment = datetime.now(timezone.utc)
        ok, detail = await _run_one(
            data_root,
            project_name.removeprefix(PROJECT_DIR_PREFIX),
        )
        # Whatever the child left on disk is the new floor, failure included:
        # a partially collapsed file is still a file that should not be
        # re-read whole until it grows again.
        _remember(data_root, project_name, paths, moment)
        if not ok:
            failures.append(f"{project_name}: {detail}")
            continue
        after = sum(
            path.stat().st_size if path.exists() else 0 for path in paths
        )
        if after < before:
            summary_projects += 1
            files += len(paths)
            saved += before - after
    return ElisionSummary(
        projects=summary_projects,
        files=files,
        saved_bytes=saved,
        oversize_files=oversize_files,
        oversize_bytes=oversize_bytes,
        failures=failures,
    )
