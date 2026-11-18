# -*- coding: utf-8 -*-
"""Work-graph HTTP surface: read the production DAG, dispatch one node.

The graph is derived on demand from durable facts (never persisted), so
GET is cheap and always current. Manual dispatch is the human override:
it bypasses the scheduler's once-per-fingerprint ledger deliberately —
a person clicking retry is an explicit instruction.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Sequence

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from domain.enums import TERMINAL_TASK_STATUSES
from domain.errors import (
    ImageTaskAlreadyClaimed,
    NotFoundError,
    ValidationError,
)
from models.config import (
    get_image_model_name,
    get_video_model_name,
)
from services.file_agent_runtime import manual_regeneration_hold
from services.file_agent_runtime.work_graph import (
    WorkGraph,
    WorkNode,
    derive_work_graph,
)
from services.file_agent_runtime.work_scheduler import (
    WorkGraphScheduler,
    _abandoned_queued_task_ids,
)
from services.media_files.call_budget import media_call_count
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project
from services.project_files.store import ProjectNotFound
from services.run_review import admission
from services.runtime_files.execution_store import ProjectExecutionStore

from .dependencies import CreatorErrorRoute, project_file_services

router = APIRouter(
    prefix="/projects/{project_id}",
    tags=["work-graph"],
    route_class=CreatorErrorRoute,
)

# Work-node kind -> the artifact slot kind it produces. A node's target_ref
# is the slot's owner_ref, so the pair identifies the artifact version whose
# automatic review state this node represents.
_NODE_SLOT_KINDS = {
    "visual": "visual_asset_image",
    "lineup": "cast_lineup_image",
    "storyboard": "r2v_storyboard_image",
    "video": "element_video",
    "compose": "final_video",
}


def _review_skip_notes(
    project: Project,
    reports_root: Any,
) -> dict[tuple[str, str], dict[str, str]]:
    """Skip notes for currently selected artifact versions.

    Keyed by ``(owner_ref, slot_kind)`` so a node can pick its own artifact
    without depending on opaque slot ids. A note only applies while the
    version it names is still the slot's selected output.
    """

    notes: dict[tuple[str, str], dict[str, str]] = {}
    for slot in project.assets.artifact_slots_by_id.values():
        if not slot.selected_version_id:
            continue
        note = admission.media_review_skip_note(
            reports_root,
            slot_id=slot.slot_id,
        )
        if note is None or note["version_id"] != slot.selected_version_id:
            continue
        notes[(slot.owner_ref, slot.kind)] = {
            "reason": note["reason"],
            "hint": note["hint"],
        }
    return notes


def _node_review_skip(
    node: WorkNode,
    notes: dict[tuple[str, str], dict[str, str]],
) -> dict[str, str] | None:
    slot_kind = _NODE_SLOT_KINDS.get(node.kind)
    if slot_kind is None or not node.target_ref:
        return None
    return notes.get((node.target_ref, slot_kind))


class DispatchWorkGraphRequest(BaseModel):
    regenerate: bool = False


def read_project_work_graph(
    project_id: str,
    services: CreatorFileServices,
    *,
    snapshot: Any | None = None,
) -> WorkGraph:
    snapshot = snapshot or _read_project(project_id, services)
    tasks = ProjectExecutionStore(services.root).list_tasks(project_id)
    return derive_work_graph(
        snapshot.project,
        tasks=tasks,
        pending_reviews=services.reviews.all_pending(project_id),
        media_models=(get_image_model_name(), get_video_model_name()),
    )


def _graph_payload(project_id: str, services: CreatorFileServices) -> dict:
    snapshot = _read_project(project_id, services)
    graph = read_project_work_graph(
        project_id,
        services,
        snapshot=snapshot,
    )
    hold = manual_regeneration_hold.ManualRegenerationHoldStore(
        services.root,
    ).read(project_id)
    review_skips = _review_skip_notes(
        snapshot.project,
        services.projects.project_root(project_id) / "runtime" / "run-review",
    )
    return {
        "projectId": project_id,
        "manualHold": hold.payload(),
        "generation": graph.generation,
        "counts": graph.counts(),
        "mediaCalls": media_call_count(services, project_id),
        "nodes": [
            {
                "id": node.node_id,
                "manuallyHeld": node.node_id in hold.node_ids,
                "kind": node.kind,
                "label": node.label,
                "status": node.status.value,
                "deps": list(node.deps),
                "lane": node.lane,
                "timelineId": node.timeline_id,
                "taskId": node.task_id,
                "progress": node.progress,
                "error": node.error,
                "missing": list(node.missing),
                "locator": node.locator,
                "dispatchable": node.command is not None,
                "promptSyncRequired": node.prompt_sync_required,
                "preparationState": (
                    "waiting" if node.prompt_sync_required else None
                ),
                "reviewSkip": _node_review_skip(node, review_skips),
            }
            for node in graph.nodes
        ],
    }


def _read_project(project_id: str, services: CreatorFileServices):
    try:
        return services.projects.read(project_id)
    except ProjectNotFound as exc:
        raise NotFoundError(str(exc)) from exc


@router.get("/work-graph")
async def get_work_graph(
    project_id: str,
    response: Response,
    services: CreatorFileServices = Depends(project_file_services),
) -> dict[str, Any]:
    payload = await asyncio.to_thread(_graph_payload, project_id, services)
    # Read process activity on the event loop that owns the scheduler. A
    # saved synchronization gap alone is never evidence of user action.
    from services.file_agent_runtime.registry import get_creator_agent_runtime

    runtime = get_creator_agent_runtime()
    if runtime is not None and runtime.services.root == services.root:
        for node in payload["nodes"]:
            if node["promptSyncRequired"]:
                preparation = runtime.work_scheduler.prompt_preparation_status(
                    project_id,
                    node["id"],
                )
                node["preparationState"] = preparation["state"]
                if preparation.get("error"):
                    node["error"] = preparation["error"]
    response.headers["Cache-Control"] = "no-store"
    return payload


class ResumeWorkGraphBody(BaseModel):
    revision: int = Field(ge=0, strict=True)


@router.post("/work-graph/resume")
async def resume_work_graph(
    project_id: str,
    body: ResumeWorkGraphBody,
    services: CreatorFileServices = Depends(project_file_services),
) -> dict[str, Any]:
    await asyncio.to_thread(_read_project, project_id, services)
    await asyncio.to_thread(
        manual_regeneration_hold.ManualRegenerationHoldStore(
            services.root,
        ).resume,
        project_id,
        body.revision,
    )
    from services.file_agent_runtime.registry import get_creator_agent_runtime

    runtime = get_creator_agent_runtime()
    if runtime is not None and runtime.services.root == services.root:
        runtime.work_scheduler.wake(project_id)
    return {"ok": True}


def _task_admission_detail(
    tasks: Sequence[Any],
    task_id: str | None,
) -> dict[str, Any]:
    """Describe the specific task selected by the work graph."""
    if not task_id:
        return {}
    for task in tasks:
        if task.task_id != task_id:
            continue
        updated = getattr(task, "updated_at", None)
        age = None
        if isinstance(updated, datetime):
            age = round(
                (datetime.now(UTC) - updated).total_seconds(),
                1,
            )
        return {
            "taskId": getattr(task, "task_id", None),
            "taskStatus": getattr(
                getattr(task, "status", None),
                "value",
                None,
            ),
            "lastAttemptSeq": int(
                getattr(task, "last_attempt_seq", 0) or 0,
            ),
            "ageSeconds": age,
        }
    return {}


@router.post("/work-graph/nodes/{node_id:path}/dispatch")
async def dispatch_work_graph_node(
    project_id: str,
    node_id: str,
    request: DispatchWorkGraphRequest | None = None,
    services: CreatorFileServices = Depends(project_file_services),
) -> dict[str, Any]:
    snapshot = await asyncio.to_thread(_read_project, project_id, services)
    holds = manual_regeneration_hold.ManualRegenerationHoldStore(services.root)
    hold_state = await asyncio.to_thread(holds.read, project_id)
    if not hold_state.project_identity.endswith(
        ":" + snapshot.project.model_dump(mode="json")["created_at"],
    ):
        raise manual_regeneration_hold.ManualHoldConflict(
            "Project lifetime changed",
        )
    tasks = await asyncio.to_thread(
        ProjectExecutionStore(services.root).list_tasks,
        project_id,
    )
    graph = derive_work_graph(
        snapshot.project,
        tasks=tasks,
        media_models=(get_image_model_name(), get_video_model_name()),
    )
    node = graph.by_id.get(node_id)
    if node is None:
        raise NotFoundError(f"work-graph 节点不存在: {node_id}")
    if node.command is None:
        raise ValidationError(f"节点 {node_id} 不支持直接派发")
    regenerate = bool(request and request.regenerate)
    if regenerate and node.kind != "interaction":
        raise ValidationError("Explicit regeneration is only for interaction")
    # A node whose task was admitted but never claimed reads as "running" and
    # used to be answered with a silent dispatched=false, which left the user
    # staring at a queue label while nothing was queued (field case
    # 2026-11-06: 69 hours). That one is allowed through to the real dispatch
    # path; a genuinely running render is still refused, now with a reason.
    unclaimed = (
        node.status.value == "running"
        and node.task_id in _abandoned_queued_task_ids(tasks)
    )
    if (node.status.value == "running" and not unclaimed) or (
        node.kind == "interaction"
        and node.status.value == "done"
        and not regenerate
    ):
        # Existing media controls request a reroll without a request body.
        # Interaction controls explicitly opt in to bypass semantic reuse.
        return {
            "ok": True,
            "nodeId": node_id,
            "status": node.status.value,
            "dispatched": False,
            "reason": (
                "already_done"
                if node.status.value == "done"
                else "already_running"
            ),
            **_task_admission_detail(tasks, node.task_id),
        }
    if node.missing:
        raise ValidationError(
            f"节点 {node_id} 的依赖未就绪：" + "、".join(node.missing[:5]),
        )
    scheduler = WorkGraphScheduler(services)
    if regenerate:
        # Concurrent clicks at this Project revision share a durable slot.
        # Automatic scheduler ticks never acquire this manual capability.
        node = replace(
            node,
            dispatch_fingerprint=(
                f"{node.dispatch_fingerprint}-manual-{snapshot.etag}"
            ),
            dispatch_arguments={**node.dispatch_arguments, "regenerate": True},
        )
    operation = await asyncio.to_thread(
        holds.begin,
        project_id,
        node,
        graph.nodes,
        expected_identity=hold_state.project_identity,
        existing_output=bool(
            node.kind == "compose"
            and (
                slot := snapshot.project.assets.artifact_slots_by_id.get(
                    f"{node.target_ref}:render",
                )
            )
            and slot.selected_version_id,
        ),
    )
    fingerprint = scheduler.manual_retry_fingerprint(node, tasks)
    admission = manual_regeneration_hold.ManualAdmission(
        holds,
        operation,
        project_id,
        # Admission must use the scheduler's exact durable request key.
        # pylint: disable-next=protected-access
        f"dag-{node.node_id}-{scheduler._dispatch_slot(fingerprint)}",
        node_id=node.node_id,
    )
    with manual_regeneration_hold.manual_admission(admission):
        try:
            await scheduler.dispatch_node(
                project_id,
                node,
                fingerprint,
                expected_object_versions=(
                    f"project:{snapshot.etag}:work-graph",
                ),
            )
        except ImageTaskAlreadyClaimed as exc:
            await asyncio.to_thread(holds.finish, admission, succeeded=False)
            current = await asyncio.to_thread(
                ProjectExecutionStore(services.root).get_task,
                project_id,
                exc.details["taskId"],
            )
            settled = current.status in TERMINAL_TASK_STATUSES
            if settled:
                current_graph = await asyncio.to_thread(
                    read_project_work_graph, project_id, services
                )
                node = current_graph.by_id.get(node_id, node)
            return {
                "ok": True,
                "nodeId": node_id,
                "status": node.status.value,
                "dispatched": False,
                "reason": "already_settled" if settled else "already_running",
                **_task_admission_detail([current], current.task_id),
            }
        except Exception:
            try:
                await asyncio.to_thread(
                    holds.finish,
                    admission,
                    succeeded=False,
                )
            except Exception:
                # An unreadable admission ledger is not proof of rejection.
                pass
            raise
        await asyncio.to_thread(holds.finish, admission, succeeded=True)
    return {
        "ok": True,
        "nodeId": node_id,
        "status": "dispatched",
        "dispatched": True,
    }


__all__ = ["router"]
