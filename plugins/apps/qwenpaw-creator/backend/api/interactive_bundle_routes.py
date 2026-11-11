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
import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Callable
from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from domain.errors import ConflictError, NotFoundError
from domain.enums import TaskStatus
from services.media_files.interactive_bundle import (
    InteractiveBundleError,
    InteractiveBundlePlan,
    plan_interactive_bundle,
    write_interactive_bundle,
)
from services.project_files.assets import AssetFileError, AssetFileStore
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project
from services.project_files.store import ProjectNotFound
from services.runtime_files.execution_store import ProjectExecutionStore
from services.run_review.media_review import active_media_review_slots

from .dependencies import CreatorErrorRoute, project_file_services

router = APIRouter(
    prefix="/projects/{project_id}",
    tags=["interactive-bundle"],
    route_class=CreatorErrorRoute,
)

# A package holds every reachable branch, so its peak is the whole set of
# segments. Serialized by design: two exports in flight would double it.
_EXPORT_GATE = threading.BoundedSemaphore(1)
_COPY_CHUNK_BYTES = 1024 * 1024


def _artifact_readers(
    project: Project,
    store: AssetFileStore,
) -> tuple[Callable[[str], bytes], Callable[[str, BinaryIO], None]]:
    """Build the two ways export reads an Asset: whole, or straight to a sink.

    Both verify size plus sha256 through ``open_verified``, and both run the
    ffprobe gate on the stored bytes -- metadata is editable, the file is not.
    """

    files_by_id = project.assets.files_by_id

    def _indexed(file_id: str):
        indexed = files_by_id.get(file_id)
        if indexed is None:
            raise InteractiveBundleError(
                f"segment file {file_id!r} is not indexed",
            )
        return indexed

    def _probe(stream: BinaryIO, file_id: str) -> None:
        # Probe the exported bytes; metadata is editable.
        # pylint: disable=too-many-nested-blocks
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
                stdin=stream,
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
        finally:
            # The child read through the shared descriptor; rewind for us.
            stream.seek(0)

    def read_artifact_file(file_id: str) -> bytes:
        indexed = _indexed(file_id)
        with store.open_verified(indexed) as stream:
            if indexed.media_type == "video/mp4":
                _probe(stream, file_id)
            return stream.read()

    def copy_artifact_file(file_id: str, sink: BinaryIO) -> None:
        indexed = _indexed(file_id)
        with store.open_verified(indexed) as stream:
            if indexed.media_type == "video/mp4":
                _probe(stream, file_id)
            shutil.copyfileobj(stream, sink, _COPY_CHUNK_BYTES)

    return read_artifact_file, copy_artifact_file


def _plan_locked(
    project_id: str,
    services: CreatorFileServices,
) -> tuple[Project, InteractiveBundlePlan]:
    """Decide what ships, from one stable snapshot.

    Only this part needs the lifecycle lock: it reads Project state and the
    small authored documents. The media itself is streamed later, and each
    file is re-verified against its recorded checksum, so a concurrent edit
    cannot silently change what ships.
    """

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
    read_artifact_file, _ = _artifact_readers(
        project,
        AssetFileStore(services.projects.project_root(project_id)),
    )
    try:
        plan = plan_interactive_bundle(
            project,
            read_artifact_file=read_artifact_file,
        )
    except InteractiveBundleError as exc:
        raise ConflictError(str(exc)) from exc
    return project, plan


def _assemble(
    project_id: str,
    services: CreatorFileServices,
) -> tuple[int, Iterator[bytes]]:
    """Plan under the lock, then stream the package onto the data root.

    Returns the archive size plus a chunk iterator, the same contract as
    ``ProjectStore.export``: only one segment is ever held in memory, and the
    exact size lets the download UI show real progress. The file lands in
    ``exports/`` rather than in the system temp directory because the startup
    disk GC reaps stale files there if the process dies mid-download.
    """

    with _EXPORT_GATE:
        with services.projects.lifecycle_lock(project_id):
            project, plan = _plan_locked(project_id, services)
        read_artifact_file, copy_artifact_file = _artifact_readers(
            project,
            AssetFileStore(services.projects.project_root(project_id)),
        )
        export_root = services.projects.root / "exports"
        export_root.mkdir(parents=True, exist_ok=True)
        archive_path = (
            export_root / f"{project_id}-interactive-{uuid4().hex}.zip"
        )
        written = False
        try:
            with archive_path.open("wb") as handle:
                write_interactive_bundle(
                    plan,
                    handle,
                    read_artifact_file=read_artifact_file,
                    copy_artifact_file=copy_artifact_file,
                )
            written = True
        except (InteractiveBundleError, AssetFileError) as exc:
            raise ConflictError(str(exc)) from exc
        finally:
            if not written:
                archive_path.unlink(missing_ok=True)
        return archive_path.stat().st_size, _stream_package(archive_path)


def _stream_package(archive_path: Path) -> Iterator[bytes]:
    """Yield the finished package, then drop it: the file is scratch."""

    try:
        with archive_path.open("rb") as handle:
            while chunk := handle.read(_COPY_CHUNK_BYTES):
                yield chunk
    finally:
        archive_path.unlink(missing_ok=True)


@router.get("/interactive-bundle")
async def export_interactive_bundle(
    project_id: str,
    services: CreatorFileServices = Depends(project_file_services),
) -> StreamingResponse:
    archive_size, archive_chunks = await asyncio.to_thread(
        _assemble,
        project_id,
        services,
    )
    return StreamingResponse(
        content=archive_chunks,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{project_id}-interactive.zip"'
            ),
            "Content-Length": str(archive_size),
        },
    )
