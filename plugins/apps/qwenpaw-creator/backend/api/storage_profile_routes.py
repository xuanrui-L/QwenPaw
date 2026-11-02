# -*- coding: utf-8 -*-
"""Storage accounting, plus the on-demand sweep that frees settled history."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from domain.errors import BadRequestError, ConflictError, NotFoundError
from schemas.storage import (
    HistoryReclaimRequest,
    HistoryReclaimResultData,
    StorageProfileData,
)
from services.media_files.task_scratch import project_storage_profile
from services.project_files.facade import CreatorFileServices
from services.project_files.storage_reclaim import reclaim_project_history
from services.project_files.store import ProjectNotFound

from .dependencies import CreatorErrorRoute, project_file_services

router = APIRouter(
    prefix="/storage",
    tags=["creator-storage"],
    route_class=CreatorErrorRoute,
)


@router.get("/profile", response_model=StorageProfileData)
async def get_storage_profile(
    include_orphans: bool = Query(False, alias="includeOrphans"),
    services: CreatorFileServices = Depends(project_file_services),
) -> StorageProfileData:
    """Report every Project's footprint split at its release baseline.

    Scanning is a walk over the runtime tree, so it runs off the event loop;
    the orphan scan additionally reads the Asset index and is opt-in.
    """

    profile = await asyncio.to_thread(
        project_storage_profile,
        services,
        include_orphans=include_orphans,
    )
    return StorageProfileData.model_validate(profile)


@router.post(
    "/projects/{project_id}/reclaim-history",
    response_model=HistoryReclaimResultData,
)
async def reclaim_history(
    project_id: str,
    payload: HistoryReclaimRequest,
    services: CreatorFileServices = Depends(project_file_services),
) -> HistoryReclaimResultData:
    """Take one Project's settled history back now, not at the next restart.

    The same two sweeps run by themselves at boot -- terminal transactions past
    the retention window, and terminal Task scratch.  Reclaiming pre-baseline
    history is the default; a Project that explicitly chose to keep it is
    refused rather than silently doing nothing.  ``confirm`` is required
    because
    the caller is a person watching a button, not a cron job.
    """

    if not payload.confirm:
        raise BadRequestError("回收历史需要显式确认（confirm=true）")
    try:
        result = await asyncio.to_thread(
            reclaim_project_history,
            services,
            project_id,
        )
    except ProjectNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    if result.history_kept:
        raise ConflictError("该项目已选择保留历史数据，未执行回收")
    return HistoryReclaimResultData.model_validate(
        {
            "projectId": result.project_id,
            "historyKept": result.history_kept,
            "removedTransactions": result.removed_transactions,
            "transactionBytes": result.transaction_bytes,
            "releasedFiles": result.released_files,
            "releasedBytes": result.released_bytes,
        },
    )
