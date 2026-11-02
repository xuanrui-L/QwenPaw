# -*- coding: utf-8 -*-
"""Run the settled-history sweeps for one Project, right now.

The sweeps already run at boot: terminal transactions past the retention window
and terminal Task scratch.  By default they no longer stop at a Project's
reclaim baseline, because a customer inside a pod cannot press anything and
space that waits for a click never comes back.

This module is the on-demand door for the same code -- "give me my space back
now, not at the next restart".  It deletes nothing itself, so every rule the
sweeps already enforce still applies: only a transaction whose own
``journal.json`` names a terminal state, the newest receipts inside the
retention window, ``QUARANTINED`` work kept as evidence, a quiet period before
a terminal Task's scratch is touched, and both environment switches.  A Project
that explicitly chose to keep its history is reported as kept and left alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.media_files.task_scratch import (
    history_reclaim_kept,
    release_orphaned_scratch,
)

from .store import ProjectNotFound
from .transaction_gc import collect_terminal_transactions


@dataclass(frozen=True, slots=True)
class HistoryReclaimResult:
    """What one on-demand sweep actually freed."""

    project_id: str
    history_kept: bool
    removed_transactions: int
    transaction_bytes: int
    released_files: int
    released_bytes: int


def reclaim_project_history(
    services: Any,
    project_id: str,
    *,
    now: datetime | None = None,
) -> HistoryReclaimResult:
    """Sweep one Project's settled history now; report the real delta.

    The Project is addressed on disk before anything is deleted, so a malformed
    or unknown id fails without touching a byte.  Existence is the only
    assertion: a Project whose ``project.json`` no longer validates is
    precisely the case where freeing its settled history still has to work.
    """

    moment = now or datetime.now(timezone.utc)
    project_root = Path(services.projects.project_root(project_id))
    if not services.projects.project_path(project_id).is_file():
        raise ProjectNotFound(f"Project does not exist: {project_id}")
    if history_reclaim_kept(project_root):
        return HistoryReclaimResult(
            project_id=project_id,
            history_kept=True,
            removed_transactions=0,
            transaction_bytes=0,
            released_files=0,
            released_bytes=0,
        )
    transactions = collect_terminal_transactions(
        services,
        project_ids=(project_id,),
    )
    scratch = release_orphaned_scratch(
        services,
        now=moment,
        project_ids=(project_id,),
    )
    return HistoryReclaimResult(
        project_id=project_id,
        history_kept=False,
        removed_transactions=transactions.removed_transactions,
        transaction_bytes=transactions.removed_bytes,
        released_files=scratch.removed_files,
        released_bytes=scratch.removed_bytes,
    )
