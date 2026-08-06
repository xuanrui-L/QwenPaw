# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long
"""Feedback API routes for Creator plugin."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Path, Query

from schemas.common import StrictModel
from services.feedback import (
    get_feedback,
    get_feedback_reasons,
    submit_feedback,
)

router = APIRouter(prefix="/projects/{project_id}/feedback", tags=["feedback"])


class FeedbackSubmission(StrictModel):
    """Request body for feedback submission."""

    conversation_id: str
    assistant_message_id: str
    score_label: str
    feedback_reason: str = ""
    feedback_comment: str = ""


class FeedbackResponse(StrictModel):
    """Response for feedback operations."""

    ok: bool
    record: dict[str, Any] | None = None


class FeedbackReasonsResponse(StrictModel):
    """Response for feedback reasons list."""

    reasons: list[str]


@router.post("/", response_model=FeedbackResponse)
async def post_feedback(
    project_id: str = Path(..., description="Project ID"),
    body: FeedbackSubmission = Body(...),
) -> FeedbackResponse:
    """Submit feedback for an assistant message."""
    try:
        record = submit_feedback(
            project_id=project_id,
            conversation_id=body.conversation_id,
            assistant_message_id=body.assistant_message_id,
            score_label=body.score_label,
            feedback_reason=body.feedback_reason,
            feedback_comment=body.feedback_comment,
        )
        return FeedbackResponse(ok=True, record=record)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/", response_model=FeedbackResponse)
async def get_feedback_for_message(
    project_id: str = Path(..., description="Project ID"),
    message_id: str = Query(..., description="Assistant message ID"),
) -> FeedbackResponse:
    """Get feedback for a specific assistant message."""
    if not message_id:
        raise HTTPException(status_code=400, detail="message_id is required")

    record = get_feedback(project_id, message_id)
    return FeedbackResponse(ok=True, record=record)


@router.get("/reasons", response_model=FeedbackReasonsResponse)
async def get_feedback_reasons() -> FeedbackReasonsResponse:
    """Get the list of predefined feedback reasons for 'bad' score."""
    return FeedbackReasonsResponse(reasons=get_feedback_reasons())
