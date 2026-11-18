# -*- coding: utf-8 -*-
"""Per-project media task counts from durable execution records."""

from __future__ import annotations

from domain.enums import TaskKind
from services.project_files.facade import CreatorFileServices
from services.runtime_files.execution_store import ProjectExecutionStore


_BILLABLE_KINDS = frozenset(
    {TaskKind.IMAGE_GENERATION, TaskKind.R2V_GENERATION},
)


def media_call_count(services: CreatorFileServices, project_id: str) -> int:
    store = ProjectExecutionStore(services.root)
    return sum(
        1
        for task in store.list_tasks(project_id)
        if task.kind in _BILLABLE_KINDS
    )


__all__ = ["media_call_count"]
