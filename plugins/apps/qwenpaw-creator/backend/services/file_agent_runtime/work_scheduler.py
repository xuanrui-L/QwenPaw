# -*- coding: utf-8 -*-
"""Event-driven scheduler that fans out READY media nodes in parallel.

The model plans; the Runtime executes. Media generation parameters are
deterministically assembled from project.json, so once the work graph
marks a media node READY there is nothing left that needs a model turn
— the scheduler dispatches it directly, several at a time, bounded by
``media_parallelism`` on top of the global model_slot semaphores.

Safety posture:
- Only runs for projects in the unattended ladder
  (execution_authorization=allow_all); otherwise the graph stays a
  read-only view and behavior is unchanged.
- A node is dispatched at most once per input fingerprint: FAILED nodes
  are *not* retried until a prompt or upstream selection actually
  changes (no paid retry loops). Manual dispatch via the API bypasses
  this ledger deliberately.
- Write-back safety comes from the existing commit boundary (field-level
  merge over disjoint pointers) and the durable idempotency slots.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from domain.enums import CreatorCommandType
from models.config import (
    EXECUTION_AUTHORIZATION_ALLOW_ALL,
    get_execution_authorization_mode,
    get_media_parallelism,
)
from services.file_agent_runtime.work_graph import (
    WorkGraph,
    WorkNode,
    derive_work_graph,
)
from services.project_files.facade import CreatorFileServices
from services.runtime_files.execution_store import ProjectExecutionStore
from utils.logger import setup_logger


logger = setup_logger("creator.work_scheduler")

# Loop exits after this long without a wake; any later wake restarts it.
_IDLE_EXIT_SECONDS = 300.0

_R2V_COMMANDS = {CreatorCommandType.GENERATE_R2V_VIDEO.value}

DispatchHook = Callable[[str, WorkGraph], Awaitable[None]]


class WorkGraphScheduler:
    """Per-project event loops dispatching READY media nodes."""

    def __init__(
        self,
        services: CreatorFileServices,
        *,
        image_dispatch: Callable[..., Awaitable[Any]] | None = None,
        r2v_dispatch: Callable[..., Awaitable[Any]] | None = None,
        on_tick: DispatchHook | None = None,
    ) -> None:
        self.services = services
        self.executions = ProjectExecutionStore(services.root)
        self._image_dispatch = image_dispatch
        self._r2v_dispatch = r2v_dispatch
        self._on_tick = on_tick
        self._wakes: dict[str, asyncio.Event] = {}
        self._loops: dict[str, asyncio.Task[None]] = {}
        # (project_id, node_id, fingerprint) -> already dispatched once.
        self._dispatched: set[tuple[str, str, str]] = set()
        self._inflight: dict[str, set[str]] = {}

    # -- lifecycle -----------------------------------------------------

    def wake(self, project_id: str) -> None:
        """Signal that durable state changed; start the loop if needed."""

        event = self._wakes.setdefault(project_id, asyncio.Event())
        event.set()
        task = self._loops.get(project_id)
        if task is None or task.done():
            self._loops[project_id] = asyncio.create_task(
                self._project_loop(project_id),
            )

    async def shutdown(self) -> None:
        for task in self._loops.values():
            task.cancel()
        for task in list(self._loops.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                pass
        self._loops.clear()

    # -- loop ----------------------------------------------------------

    async def _project_loop(self, project_id: str) -> None:
        event = self._wakes.setdefault(project_id, asyncio.Event())
        while True:
            try:
                await asyncio.wait_for(
                    event.wait(),
                    timeout=_IDLE_EXIT_SECONDS,
                )
            except asyncio.TimeoutError:
                if not self._inflight.get(project_id):
                    return
                continue
            event.clear()
            try:
                await self.tick(project_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    "work-graph tick failed for %s",
                    project_id,
                )

    # -- core ----------------------------------------------------------

    def enabled(self) -> bool:
        return (
            get_execution_authorization_mode()
            == EXECUTION_AUTHORIZATION_ALLOW_ALL
        )

    async def tick(self, project_id: str) -> WorkGraph | None:
        """Derive the graph once and dispatch what capacity allows."""

        if not self.enabled():
            return None
        try:
            snapshot = await asyncio.to_thread(
                self.services.projects.read,
                project_id,
            )
            tasks = await asyncio.to_thread(
                self.executions.list_tasks,
                project_id,
            )
        except Exception:  # pylint: disable=broad-except
            logger.exception("work-graph state read failed for %s", project_id)
            return None
        graph = derive_work_graph(snapshot.project, tasks=tasks)
        inflight = self._inflight.setdefault(project_id, set())
        running = sum(
            1
            for node in graph.nodes
            if node.status.value == "running"
        )
        capacity = get_media_parallelism() - running - len(inflight)
        for node in graph.ready_media_nodes():
            if capacity <= 0:
                break
            fingerprint = node.dispatch_fingerprint or node.node_id
            ledger_key = (project_id, node.node_id, fingerprint)
            if ledger_key in self._dispatched or node.node_id in inflight:
                continue
            self._dispatched.add(ledger_key)
            inflight.add(node.node_id)
            capacity -= 1
            asyncio.create_task(
                self._dispatch(project_id, node, fingerprint),
            )
        if self._on_tick is not None:
            await self._on_tick(project_id, graph)
        return graph

    async def _dispatch(
        self,
        project_id: str,
        node: WorkNode,
        fingerprint: str,
    ) -> None:
        logger.info(
            "work-graph dispatch project=%s node=%s command=%s",
            project_id,
            node.node_id,
            node.command,
        )
        try:
            await self.dispatch_node(project_id, node, fingerprint)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            # The task record carries the durable failure; the graph will
            # surface it as FAILED and the ledger prevents a paid retry
            # until the node's inputs change.
            logger.warning(
                "work-graph dispatch failed project=%s node=%s: %s",
                project_id,
                node.node_id,
                exc,
            )
        finally:
            self._inflight.get(project_id, set()).discard(node.node_id)
            self.wake(project_id)

    async def dispatch_node(
        self,
        project_id: str,
        node: WorkNode,
        fingerprint: str | None = None,
    ) -> Any:
        """Execute one node through the shared media executors."""

        if node.command is None or node.target_ref is None:
            raise ValueError(f"node {node.node_id} is not dispatchable")
        idempotency_key = (
            f"dag-{node.node_id}-{fingerprint or node.dispatch_fingerprint}"
        )
        if node.command in _R2V_COMMANDS:
            dispatch = self._r2v_dispatch or _default_r2v_dispatch
        else:
            dispatch = self._image_dispatch or _default_image_dispatch
        return await dispatch(
            self.services,
            project_id=project_id,
            command=node.command,
            target_ref=node.target_ref,
            arguments=dict(node.dispatch_arguments),
            idempotency_key=idempotency_key,
        )


async def _default_image_dispatch(
    services: CreatorFileServices,
    **kwargs: Any,
) -> Any:
    # Imported lazily: media executors pull heavy provider dependencies.
    from services.media_files.image_execution import (  # pylint: disable=import-outside-toplevel
        execute_file_image_command,
    )

    return await execute_file_image_command(services, **kwargs)


async def _default_r2v_dispatch(
    services: CreatorFileServices,
    **kwargs: Any,
) -> Any:
    from services.media_files.r2v_execution import (  # pylint: disable=import-outside-toplevel
        execute_file_r2v_command,
    )

    # The r2v entry point has a single command and takes no command kwarg.
    kwargs.pop("command", None)
    return await execute_file_r2v_command(services, **kwargs)


__all__ = ["WorkGraphScheduler"]
