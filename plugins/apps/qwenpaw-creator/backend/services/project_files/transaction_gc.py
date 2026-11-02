# -*- coding: utf-8 -*-
"""Collect terminal Project transactions, at startup and as history grows.

Every commit leaves a transaction directory holding four full copies of the
Project (base, candidate, latest, final) plus its journal, changeset and round.
Nothing ever removed them, so a 500-commit project carried 500 directories of
history that no reader can use: recovery only needs transactions that never
finished, and the change record itself lives durably in the change rounds and
the Project generation.  Observed on one project: 238 directories, 3.1 GB.

The sweep runs at startup, next to the orphaned-scratch sweep, and -- because a
container that is never restarted never ran it -- again once one Project's
commit history outgrows :data:`TERMINAL_TRANSACTION_SWEEP_THRESHOLD`.  It is
deliberately conservative:

* A transaction is removed only when its own ``journal.json`` says it reached
  a terminal state.  A missing, unreadable or unknown-state journal is kept;
  so is every non-terminal transaction, whatever the index cache believes.
  The index is an optimization for the commit path, and deletion must not
  inherit its uncertainty.
* The newest :data:`TERMINAL_TRANSACTION_RETENTION` terminal transactions stay,
  so a rollback or an incident review still has recent receipts.
* Nothing older than the Project's reclaim baseline is touched, matching the
  scratch release rule: history a Project arrived with is treated as the
  user's, and a build that cannot read compressed snapshots must be able to
  roll back to a plain-document tree.

One env var turns the whole sweep off without a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import threading
from typing import Any

from services.media_files.task_scratch import read_scratch_baseline
from services.runtime_files.atomic_store import AtomicJsonRecordStore
from services.runtime_files.path_safety import is_link_path

from .transaction_index import drop_transaction_index

logger = logging.getLogger("qwenpaw.creator.project_files.transaction_gc")

# Terminal transactions kept after a sweep, newest first by directory mtime.
TERMINAL_TRANSACTION_RETENTION = 50

# Transaction directories one Project may accumulate before its next commit
# asks for a sweep of its own.  Above the retention on purpose: the startup
# sweep alone only ever ran on a build that was restarted.
TERMINAL_TRANSACTION_SWEEP_THRESHOLD = 100

TRANSACTIONS_DIR_NAME = "transactions"
JOURNAL_FILE_NAME = "journal.json"

# The only states this module will delete. Anything else -- including a state
# this build does not know yet -- is preserved.
TERMINAL_JOURNAL_STATES = frozenset({"RUNTIME_FINALIZED", "ABORTED"})

_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})


def transaction_gc_enabled() -> bool:
    """Whether terminal transactions may be collected; on unless switched off.

    Deleting commit history needs an emergency stop that does not require a
    release: one env var restores the previous keep-forever behaviour.
    """

    raw = os.environ.get("CREATOR_TRANSACTION_GC_ENABLED", "")
    return raw.strip().casefold() not in _DISABLED_VALUES


@dataclass(frozen=True, slots=True)
class TransactionGCSummary:
    """What one startup sweep removed, for the startup log."""

    projects: int = 0
    swept_projects: int = 0
    removed_transactions: int = 0
    removed_bytes: int = 0


def _journal_state(transaction_dir: Path) -> str | None:
    """The journal's ``state`` string, or None when it cannot be trusted."""

    try:
        stored = AtomicJsonRecordStore(
            transaction_dir / JOURNAL_FILE_NAME,
            None,
        ).read_or_none()
    except (
        Exception
    ):  # noqa: BLE001 - corrupt, unreadable, locked: all mean keep
        return None
    if not isinstance(stored, dict):
        return None
    state = stored.get("state")
    return state if isinstance(state, str) else None


def _directory_bytes(transaction_dir: Path) -> int:
    total = 0
    for path in transaction_dir.rglob("*"):
        try:
            if path.is_file() and not is_link_path(path):
                total += path.stat().st_size
        except OSError:  # pragma: no cover - a vanishing file is not a loss
            continue
    return total


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def collect_project_transactions(
    project_root: Path,
    *,
    retention: int = TERMINAL_TRANSACTION_RETENTION,
) -> tuple[int, int]:
    """Remove settled transactions below one Project; return (count, bytes)."""

    if not transaction_gc_enabled():
        return (0, 0)
    transactions_root = Path(project_root) / "runtime" / TRANSACTIONS_DIR_NAME
    if not transactions_root.is_dir():
        return (0, 0)
    baseline = read_scratch_baseline(Path(project_root))
    if baseline is None:
        # Nothing is provably newer than an unrecorded start; the scratch sweep
        # stamps it this boot, so the next one can collect.
        return (0, 0)
    candidates: list[tuple[float, Path, int]] = []
    for entry in transactions_root.iterdir():
        if not entry.is_dir() or is_link_path(entry):
            continue
        if _journal_state(entry) not in TERMINAL_JOURNAL_STATES:
            continue
        if _mtime(entry) < baseline.timestamp():
            # Arrived with the Project: pre-upgrade history, not this build's
            # to reclaim.
            continue
        candidates.append((_mtime(entry), entry, _directory_bytes(entry)))
    if len(candidates) <= retention:
        return (0, 0)
    candidates.sort(key=lambda item: item[0], reverse=True)
    removed = 0
    removed_bytes = 0
    for _mtime_value, entry, size in candidates[retention:]:
        shutil.rmtree(entry, ignore_errors=True)
        if entry.exists():
            logger.warning("transaction directory survived removal: %s", entry)
            continue
        removed += 1
        removed_bytes += size
    if removed:
        # The listing the commit path cached no longer matches the directory.
        drop_transaction_index(transactions_root)
    return (removed, removed_bytes)


def collect_terminal_transactions(
    services: Any,
    *,
    retention: int = TERMINAL_TRANSACTION_RETENTION,
) -> TransactionGCSummary:
    """Sweep every discovered Project once; never raise, never gate startup."""

    if not transaction_gc_enabled():
        return TransactionGCSummary()
    projects = 0
    swept = 0
    removed = 0
    removed_bytes = 0
    for project_id in services.projects.discover_project_ids():
        projects += 1
        try:
            project_root = Path(services.projects.project_root(project_id))
            count, size = collect_project_transactions(
                project_root,
                retention=retention,
            )
        except Exception:  # noqa: BLE001 - a sweep never breaks startup
            logger.warning(
                "transaction sweep skipped: project=%s",
                project_id,
                exc_info=True,
            )
            continue
        if count:
            swept += 1
            removed += count
            removed_bytes += size
    return TransactionGCSummary(
        projects=projects,
        swept_projects=swept,
        removed_transactions=removed,
        removed_bytes=removed_bytes,
    )


_sweep_guard = threading.Lock()
_sweeps_in_flight: set[str] = set()
_sweep_counts: dict[str, int] = {}


def request_project_sweep(
    project_root: Path,
    *,
    directory_count: int,
    threshold: int = TERMINAL_TRANSACTION_SWEEP_THRESHOLD,
    retention: int = TERMINAL_TRANSACTION_RETENTION,
) -> bool:
    """Sweep one Project in the background once its history outgrows itself.

    ``directory_count`` comes from the listing the commit path already keeps,
    so a normal commit pays nothing here. It counts every transaction directory
    rather than only settled ones: a sweep may still find nothing to remove,
    but it never misses one that is due.

    Housekeeping must not block or fail a commit: the sweep runs on a daemon
    thread, swallows everything it raises, and one Project never runs two at a
    time. A sweep that removed nothing is not retried until the count has grown
    by another threshold's worth, so pre-existing history this build will not
    touch does not cost a scan per commit.
    """

    if not transaction_gc_enabled():
        return False
    key = str(project_root)
    with _sweep_guard:
        if directory_count <= threshold:
            return False
        if key in _sweeps_in_flight:
            return False
        if directory_count < _sweep_counts.get(key, 0) + threshold:
            return False
        _sweeps_in_flight.add(key)
        _sweep_counts[key] = directory_count

    def sweep() -> None:
        try:
            removed, removed_bytes = collect_project_transactions(
                Path(project_root),
                retention=retention,
            )
            if removed:
                logger.info(
                    "terminal transactions released during runtime: "
                    "project_root=%s transactions=%s bytes=%s",
                    project_root,
                    removed,
                    removed_bytes,
                )
        except Exception:  # noqa: BLE001 - a sweep never reaches the committer
            logger.warning(
                "transaction sweep failed: project_root=%s",
                project_root,
                exc_info=True,
            )
        finally:
            with _sweep_guard:
                _sweeps_in_flight.discard(key)

    threading.Thread(
        target=sweep,
        name=f"creator-transaction-sweep:{Path(project_root).name}",
        daemon=True,
    ).start()
    return True


__all__ = [
    "TERMINAL_TRANSACTION_RETENTION",
    "TERMINAL_TRANSACTION_SWEEP_THRESHOLD",
    "TransactionGCSummary",
    "collect_project_transactions",
    "collect_terminal_transactions",
    "request_project_sweep",
    "transaction_gc_enabled",
]
