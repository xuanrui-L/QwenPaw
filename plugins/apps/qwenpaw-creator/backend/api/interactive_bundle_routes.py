# -*- coding: utf-8 -*-
"""Interactive-bundle HTTP surface: export a branching project.

A branching project's deliverable is a self-hosted interactive zip
(player + manifest + per-branch segments), never a single mp4. Assembly
fails closed (409) until every reachable branch has its final cut — the
same gate the work graph exposes as the bundle node.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Response

from domain.errors import ConflictError, NotFoundError
from services.media_files.interactive_bundle import (
    InteractiveBundleError,
    assemble_interactive_bundle,
)
from services.project_files.assets import AssetFileStore
from services.project_files.facade import CreatorFileServices
from services.project_files.store import ProjectNotFound

from .dependencies import CreatorErrorRoute, project_file_services


router = APIRouter(
    prefix="/projects/{project_id}",
    tags=["interactive-bundle"],
    route_class=CreatorErrorRoute,
)


def _assemble(project_id: str, services: CreatorFileServices) -> bytes:
    try:
        snapshot = services.projects.read(project_id)
    except ProjectNotFound as exc:
        raise NotFoundError(str(exc)) from exc
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
            return stream.read()

    try:
        return assemble_interactive_bundle(
            project,
            read_artifact_file=read_artifact_file,
        )
    except InteractiveBundleError as exc:
        raise ConflictError(str(exc)) from exc


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
