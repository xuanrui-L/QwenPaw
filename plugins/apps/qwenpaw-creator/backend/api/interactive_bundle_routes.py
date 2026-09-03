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
from services.media_files.rough_cut import (
    RoughCutError,
    collect_rough_cut_clips,
    render_rough_cut,
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


def _rough_cut(
    project_id: str,
    timeline_id: str,
    services: CreatorFileServices,
) -> bytes:
    try:
        snapshot = services.projects.read(project_id)
    except ProjectNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    project = snapshot.project
    timeline = project.timelines.items.get(timeline_id)
    if timeline is None:
        raise NotFoundError(f"Timeline {timeline_id!r} 不存在")
    project_root = services.projects.project_root(project_id)
    files_by_id = project.assets.files_by_id

    def resolve_file(file_id: str):
        indexed = files_by_id.get(file_id)
        if indexed is None:
            raise RoughCutError(f"粗剪素材文件 {file_id!r} 未在索引中")
        return project_root / indexed.relative_uri

    clips = collect_rough_cut_clips(
        project,
        timeline,
        resolve_file=resolve_file,
    )
    try:
        return render_rough_cut(clips)
    except RoughCutError as exc:
        raise ConflictError(str(exc)) from exc


@router.get("/timelines/{timeline_id}/rough-cut")
async def export_rough_cut(
    project_id: str,
    timeline_id: str,
    services: CreatorFileServices = Depends(project_file_services),
) -> Response:
    """Zero-model-cost draft: concat existing element videos / storyboard
    stills at 480p. Never persisted as an artifact — a draft can't be
    mistaken for the final cut."""

    payload = await asyncio.to_thread(
        _rough_cut,
        project_id,
        timeline_id,
        services,
    )
    return Response(
        content=payload,
        media_type="video/mp4",
        headers={
            "Content-Disposition": (
                f'inline; filename="{timeline_id}-rough-cut.mp4"'
            ),
        },
    )
