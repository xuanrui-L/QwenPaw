# -*- coding: utf-8 -*-
"""Interactive-bundle HTTP surface: export a branching project.

A branching project's deliverable is a self-hosted interactive zip
(player + manifest + per-branch segments), never a single mp4. Assembly
fails closed (409) until every reachable branch has its final cut — the
same gate the work graph exposes as the bundle node.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

from fastapi import APIRouter, Depends, Response

from domain.errors import ConflictError, NotFoundError
from domain.enums import TaskStatus
from services.media_files.interactive_bundle import (
    InteractiveBundleError,
    assemble_interactive_bundle,
)
from services.project_files.assets import AssetFileStore
from services.project_files.facade import CreatorFileServices
from services.project_files.store import ProjectNotFound
from services.runtime_files.execution_store import ProjectExecutionStore
from services.run_review.media_review import active_media_review_slots

from .dependencies import CreatorErrorRoute, project_file_services

router = APIRouter(
    prefix="/projects/{project_id}",
    tags=["interactive-bundle"],
    route_class=CreatorErrorRoute,
)


def _assemble_locked(project_id: str, services: CreatorFileServices) -> bytes:
    # File verification and ffprobe gates share the locked export snapshot.
    # pylint: disable=too-many-nested-blocks
    try:
        snapshot = services.projects.read(project_id)
    except ProjectNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    if services.reviews.all_pending(project_id):
        raise ConflictError(
            "Approve or reject pending changes before exporting "
            "the interactive bundle",
        )
    if any(
        task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}
        for task in ProjectExecutionStore(services.root).list_tasks(project_id)
    ) or active_media_review_slots(project_id):
        raise ConflictError(
            "Wait for generation and media review to finish "
            "before exporting the interactive bundle",
        )
    project = snapshot.project
    store = AssetFileStore(services.projects.project_root(project_id))
    files_by_id = project.assets.files_by_id

    def read_artifact_file(file_id: str) -> bytes:
        indexed = files_by_id.get(file_id)
        if indexed is None:
            raise InteractiveBundleError(
                f"segment file {file_id!r} is not indexed",
            )
        with store.open_verified(indexed) as stream:
            payload = stream.read()
        # Probe the exported bytes; metadata is editable.
        if indexed.media_type == "video/mp4":
            try:
                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration:stream=codec_type,width,height",
                        "-of",
                        "json",
                        "pipe:0",
                    ],
                    input=payload,
                    capture_output=True,
                    timeout=30,
                    check=True,
                )
                info = json.loads(probe.stdout)
                duration = float(info.get("format", {}).get("duration", 0))
                if duration <= 0 or not any(
                    v.get("codec_type") == "video"
                    and v.get("width", 0) > 0
                    and v.get("height", 0) > 0
                    for v in info.get("streams", [])
                ):
                    raise ValueError("no playable video stream")
                for timeline in project.timelines.items.values():
                    slot = project.assets.artifact_slots_by_id.get(
                        f"timeline:{timeline.timeline_id}:render",
                    )
                    version = (
                        project.assets.artifact_versions_by_id.get(
                            slot.selected_version_id or "",
                        )
                        if slot
                        else None
                    )
                    if version and version.file_id == file_id:
                        for element in timeline.elements_by_id.values():
                            if (
                                element.enabled
                                and element.creation.type == "interaction"
                                and element.span.start_tick
                                / timeline.ticks_per_second
                                > duration + 0.05
                            ):
                                raise ValueError(
                                    f"{timeline.timeline_id}: interaction "
                                    "time exceeds actual video duration "
                                    f"{duration:.3f}s",
                                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise InteractiveBundleError(
                    f"Invalid segment {file_id}: {exc}",
                ) from exc
        return payload

    try:
        return assemble_interactive_bundle(
            project,
            read_artifact_file=read_artifact_file,
        )
    except InteractiveBundleError as exc:
        raise ConflictError(str(exc)) from exc


def _assemble(project_id: str, services: CreatorFileServices) -> bytes:
    # Check reviews, selected versions and files under one snapshot lock.
    with services.projects.lifecycle_lock(project_id):
        return _assemble_locked(project_id, services)


@router.get("/interactive-bundle")
async def export_interactive_bundle(
    project_id: str,
    services: CreatorFileServices = Depends(project_file_services),
) -> Response:
    payload = await asyncio.to_thread(_assemble, project_id, services)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{project_id}-interactive.zip"'
            ),
        },
    )
