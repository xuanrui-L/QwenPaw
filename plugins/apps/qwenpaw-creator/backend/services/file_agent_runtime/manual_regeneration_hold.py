# -*- coding: utf-8 -*-
"""Process-local hold registry for manually regenerated work-graph nodes.

A person clicking 重新生成 on an upstream node (say a storyboard) gives an
explicit, node-scoped instruction: re-render THIS node from current
content. Under the unattended ladder (``execution_authorization=allow_all``)
the work scheduler would otherwise treat the freshly committed upstream
output as a changed input, mark every downstream node STALE and
auto-cascade a paid regeneration (storyboard -> video -> compose). That
cascade is correct for a hands-off 成片 run but wrong for a deliberate
manual re-roll the operator wants to inspect first.

This registry records the transitive downstream dependents of a manually
dispatched node so the scheduler skips them on its automatic tick. They
stay STALE — the workbench surfaces 待重新生成 — until the operator
regenerates each in turn by hand, which releases that node and holds its
own dependents. Only the automatic cascade is suppressed: a manual
dispatch goes straight through the work-graph API and never consults this
registry, so a human click always runs.

Scheduler-driven dispatch (the unattended 成片 pipeline) never records a
hold, so a fresh auto project still cascades end to end.

Single-process deployment is a hard premise (see ``frontend_edit_hold``),
so the registry is in-memory only: a restart clears it, which merely
re-enables automatic dispatch of already-persisted STALE state.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from .work_graph import WorkNode

_lock = threading.Lock()
# project_id -> node_ids held back from the automatic cascade.
_holds: dict[str, set[str]] = {}


def _transitive_dependents(
    root: str,
    nodes: Iterable["WorkNode"],
) -> set[str]:
    """Every node downstream of *root*, following ``deps`` in reverse."""
    dependents: dict[str, list[str]] = {}
    for node in nodes:
        for dependency in node.deps:
            dependents.setdefault(dependency, []).append(node.node_id)
    seen: set[str] = set()
    stack = [root]
    while stack:
        current = stack.pop()
        for child in dependents.get(current, ()):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def note_manual_regeneration(
    project_id: str,
    node_id: str,
    nodes: Iterable["WorkNode"],
) -> None:
    """Release *node_id* and hold its transitive downstream dependents.

    Called from the work-graph dispatch API right before a human-triggered
    node execution. Releasing the node itself keeps a repeat click (or a
    manual re-roll of an already-held descendant) dispatchable, while the
    dependents stay out of the scheduler's automatic cascade.
    """
    if not project_id or not node_id:
        return
    held = _transitive_dependents(node_id, nodes)
    with _lock:
        bucket = _holds.setdefault(project_id, set())
        bucket.discard(node_id)
        bucket |= held
        if not bucket:
            _holds.pop(project_id, None)


def is_held(project_id: str, node_id: str) -> bool:
    """True when *node_id* must be skipped by the automatic cascade."""
    with _lock:
        return node_id in _holds.get(project_id, ())


def held_nodes(project_id: str) -> frozenset[str]:
    """Snapshot of the held node ids for one project (inspection/tests)."""
    with _lock:
        return frozenset(_holds.get(project_id, ()))


def clear(project_id: str | None = None) -> None:
    """Drop holds for one project (delete/close) or all (tests)."""
    with _lock:
        if project_id is None:
            _holds.clear()
            return
        _holds.pop(project_id, None)


__all__ = [
    "clear",
    "held_nodes",
    "is_held",
    "note_manual_regeneration",
]
