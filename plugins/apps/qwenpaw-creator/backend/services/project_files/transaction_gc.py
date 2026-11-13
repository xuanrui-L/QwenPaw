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
* Pre-baseline history goes too, by default: a customer cannot press anything
  inside a pod, and space that only returns after a click never comes back.  A
  Project that chose to keep its history -- its own marker, or
  ``CREATOR_STORAGE_KEEP_HISTORY`` for a whole install -- is still untouched
  below the baseline, matching the scratch release rule.

One env var turns the whole sweep off without a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import os
from pathlib import Path
import shutil
import threading
from typing import Any
from uuid import uuid4

from services.media_files.task_scratch import (
    SweepBudget,
    auto_reclaim_max_bytes,
    history_reclaim_kept,
    read_reclaim_confirmation,
    read_scratch_baseline,
)
from services.runtime_files.atomic_store import AtomicJsonRecordStore
from services.runtime_files import idempotency_ledger
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
    stopped_early: bool = False
    skipped_projects: int = 0
    skipped_bytes: int = 0


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


def _eligible(
    entry: Path,
    *,
    baseline: datetime | None,
    kept: bool,
) -> bool:
    """Whether one directory is settled history this sweep may collect.

    A journal that cannot be read, or names a state the commit boundary has not
    reached, is never touched; a Project that asked to keep its history still
    gets the date gate.
    """

    if not entry.is_dir() or is_link_path(entry):
        return False
    if _journal_state(entry) not in TERMINAL_JOURNAL_STATES:
        return False
    if baseline is not None and kept and _mtime(entry) < baseline.timestamp():
        # Arrived with the Project, and this Project asked for its history to
        # be kept -- so the date gate still protects it.
        return False
    return True


def _eligible_candidates(
    transactions_root: Path,
    baseline: datetime | None,
    kept: bool,
) -> list[tuple[float, Path, int]]:
    """Settled transaction directories with their mtime and byte size."""

    candidates: list[tuple[float, Path, int]] = []
    for entry in transactions_root.iterdir():
        if not _eligible(entry, baseline=baseline, kept=kept):
            continue
        candidates.append((_mtime(entry), entry, _directory_bytes(entry)))
    return candidates


def _open_reservation_ids(transactions_root: Path) -> frozenset[str]:
    """Transactions an IN_PROGRESS idempotency reservation still needs.

    A PATCH that published its Project but failed to record the HTTP result
    replays that result from the transaction directory. Deleting it first
    turns the retry into a 409 and writes FAILED over work that really
    completed, so the sweep holds those back (review #243). The ledger is
    rebuilt from the records whenever it cannot be trusted.
    """

    return idempotency_ledger.open_transaction_ids(
        transactions_root.parent / "commands" / "idempotency",
    )


def _surplus_candidates(
    transactions_root: Path,
    baseline: datetime | None,
    kept: bool,
    retention: int,
    protected: frozenset[str] = frozenset(),
) -> list[tuple[float, Path, int]]:
    """Exactly what a sweep would remove: the newest ``retention`` stay.

    Both the storage profile and the collector must answer from this one
    ordering. The delete loop keeps the newest by mtime, so a profile that
    truncated the raw directory listing instead reported a different set: the
    home page then showed a pending size under the cap with no way to confirm
    it, while the real sweep stopped on a surplus above that cap.

    Protected transactions are dropped before the cut, so they neither get
    deleted nor consume one of the retained slots.
    """

    candidates = _eligible_candidates(transactions_root, baseline, kept)
    if protected:
        candidates = [
            item for item in candidates if item[1].name not in protected
        ]
    if len(candidates) <= retention:
        return []
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[retention:]


def surplus_transaction_bytes(
    project_root: Path,
    *,
    retention: int = TERMINAL_TRANSACTION_RETENTION,
) -> int:
    """What a sweep would reclaim below one Project, without touching it.

    The storage profile answers "how much is waiting for my confirmation" with
    this; computing it by deleting would be a poor way to ask.
    """

    transactions_root = Path(project_root) / "runtime" / TRANSACTIONS_DIR_NAME
    if not transactions_root.is_dir():
        return 0
    kept = history_reclaim_kept(project_root)
    baseline = read_scratch_baseline(Path(project_root))
    surplus = _surplus_candidates(
        transactions_root,
        baseline,
        kept,
        retention,
        _open_reservation_ids(transactions_root),
    )
    return sum(size for _mtime_value, _entry, size in surplus)


def _atomic_reclaim(entry: Path) -> bool:
    """Move one transaction directory to a hidden tombstone, then delete.

    The rename is atomic within the same filesystem, so a concurrent
    commit scan sees either the original directory intact or
    ``FileNotFoundError`` on ``lstat`` and never the intermediate
    ``shutil.rmtree`` state where ``journal.json`` has already been
    unlinked while the parent directory still exists. The tombstone
    name starts with a dot so ``list_transaction_ids`` and the surplus
    enumerator both skip it by their existing rule. A cross-device
    rename (OSError EXDEV) falls back to a direct rmtree, which keeps
    the older behaviour as a last resort.
    """

    tombstone = entry.with_name(f".deleted-{entry.name}-{uuid4().hex}")
    try:
        os.rename(entry, tombstone)
    except OSError as exc:
        logger.warning(
            "transaction directory could not be renamed to a tombstone "
            "(%s); falling back to in-place removal: %s",
            exc,
            entry,
        )
        shutil.rmtree(entry, ignore_errors=True)
        if entry.exists():
            logger.warning("transaction directory survived removal: %s", entry)
            return False
        return True
    shutil.rmtree(tombstone, ignore_errors=True)
    if tombstone.exists():
        # The rename already moved the tree out of the scan's view, so a
        # surviving tombstone is a disk cost, not a correctness one; the
        # next sweep picks it up again.
        logger.warning(
            "transaction tombstone cleanup deferred to next sweep: %s",
            tombstone,
        )
    return True


def collect_project_transactions(
    project_root: Path,
    *,
    retention: int = TERMINAL_TRANSACTION_RETENTION,
    budget: SweepBudget | None = None,
    enforce_cap: bool = True,
    report: dict | None = None,
) -> tuple[int, int]:
    """Remove settled transactions below one Project; return (count, bytes).

    ``report`` receives ``skipped_bytes`` when the Project's settled surplus is
    above the automatic cap and nobody confirmed reclaiming it yet.
    """

    if not transaction_gc_enabled():
        return (0, 0)
    transactions_root = Path(project_root) / "runtime" / TRANSACTIONS_DIR_NAME
    if not transactions_root.is_dir():
        return (0, 0)
    kept = history_reclaim_kept(project_root)
    baseline = read_scratch_baseline(Path(project_root))
    if baseline is None and kept:
        # A Project keeping its history needs proof that a transaction is newer
        # than its start, and an unrecorded start proves nothing; the scratch
        # sweep stamps it this boot, so the next one can collect.  A Project
        # whose history is not kept needs no such proof at all.
        return (0, 0)
    surplus = _surplus_candidates(
        transactions_root,
        baseline,
        kept,
        retention,
        _open_reservation_ids(transactions_root),
    )
    if not surplus:
        return (0, 0)
    # The cap protects the Project's history; whether a caller wants statistics
    # is a separate question. Gating it on ``report`` made the startup scan
    # wait for confirmation while the commit-triggered sweep -- which passes
    # no report -- deleted the same surplus unconfirmed.
    if enforce_cap:
        projected = sum(size for _mtime_value, _entry, size in surplus)
        if (
            projected > auto_reclaim_max_bytes()
            and read_reclaim_confirmation(project_root) is None
        ):
            if report is not None:
                report["skipped_bytes"] = projected
            return (0, 0)
    removed = 0
    removed_bytes = 0
    for _mtime_value, entry, size in surplus:
        if budget is not None and not budget.may_spend(size):
            break
        # ``shutil.rmtree`` is not atomic: the journal disappears first and
        # the directory is rmdired last, so a commit scan that already
        # captured this name in ``list_transaction_ids`` can see
        # "directory still here, journal already gone" and raise a false
        # ``PendingProjectRecoveryError`` on the intermediate state
        # (review #243 r4548690102 follow-up). Renaming to a
        # dot-prefixed tombstone is a single atomic operation, so the
        # scan sees either the fully intact transaction or nothing at
        # all -- ``list_transaction_ids`` skips leading-dot entries and
        # ``entry.lstat`` on the original name raises FileNotFoundError.
        # Then, and only then, do we rmtree the moved tree.
        if not _atomic_reclaim(entry):
            continue
        if budget is not None:
            budget.charge(size)
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
    project_ids: Any | None = None,
    budget: SweepBudget | None = None,
    enforce_cap: bool = True,
) -> TransactionGCSummary:
    """Sweep every discovered Project once; never raise, never gate startup.

    ``project_ids`` scopes the sweep, which is what the on-demand reclaim uses
    when a person asks for their space back before the next boot.  A shared
    ``budget`` bounds a scheduled pass; without one the sweep runs to the end
    of the tree.
    """

    if not transaction_gc_enabled():
        return TransactionGCSummary()
    wanted = None if project_ids is None else set(project_ids)
    projects = 0
    swept = 0
    removed = 0
    removed_bytes = 0
    skipped_projects = 0
    skipped_bytes = 0
    for project_id in services.projects.discover_project_ids():
        if wanted is not None and project_id not in wanted:
            continue
        if budget is not None and not budget.has_room():
            break
        projects += 1
        try:
            project_root = Path(services.projects.project_root(project_id))
            report: dict = {}
            count, size = collect_project_transactions(
                project_root,
                retention=retention,
                budget=budget,
                enforce_cap=enforce_cap,
                report=report,
            )
            if report.get("skipped_bytes"):
                skipped_projects += 1
                skipped_bytes += report["skipped_bytes"]
                logger.warning(
                    "transaction reclaim withheld pending confirmation: "
                    "project=%s bytes=%s cap=%s (confirm through "
                    "POST /storage/projects/{id}/reclaim-history)",
                    project_id,
                    report["skipped_bytes"],
                    auto_reclaim_max_bytes(),
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
        stopped_early=bool(budget is not None and budget.stopped_early),
        skipped_projects=skipped_projects,
        skipped_bytes=skipped_bytes,
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
