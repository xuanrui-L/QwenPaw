# -*- coding: utf-8 -*-
"""Send Creator feedback with diagnostics using the host's Platform account."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Header, Request, UploadFile
from pydantic import BaseModel, Field, HttpUrl

from domain.errors import ConflictError, ValidationError
from services.observability import read_trace_records

from .dependencies import CreatorErrorRoute

router = APIRouter(
    prefix="/feedbacks",
    tags=["creator-feedback"],
    route_class=CreatorErrorRoute,
)

PROJECT_ID_LIMIT = 128
FEEDBACK_LIMIT = 65_536
SCREENSHOT_BYTE_LIMIT = 5 * 1024 * 1024
_IMAGE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_PLATFORM_FEEDBACK = "/api/v1/feedback"


# How far back to scan for a stage signal. The newest failure is frequently a
# generic api/CONFLICT, so a single record too often reads as "unknown".
STAGE_SCAN = 20

# Creator's generation ladder, so the platform can group complaints by step.
# Which step is read back off the cited trace rather than asked of the person:
# the wording is a heuristic, and "unknown" is an honest answer when the
# record says nothing about where the run stood.
STAGE_BY_TOKEN = (
    ("IMAGE_GENERATION", "media_generation"),
    ("VIDEO", "media_generation"),
    ("MEDIA", "media_generation"),
    ("TTS", "media_generation"),
    ("ASR", "media_generation"),
    ("EXPORT", "exported"),
    ("BUNDLE", "exported"),
    ("REVIEW", "composition"),
    ("COMPOSE", "composition"),
    ("SCRIPT", "script"),
    ("BLUEPRINT", "blueprint"),
    ("INTERACTION", "design"),
)


def _stage_of(record: dict[str, Any] | None) -> str:
    if not record:
        return "unknown"
    haystack = " ".join(
        [
            str(record.get("name") or ""),
            str(record.get("component") or ""),
            str((record.get("attributes") or {}).get("errorCode") or ""),
        ],
    ).upper()
    for token, stage in STAGE_BY_TOKEN:
        if token in haystack:
            return stage
    return "unknown"


def _pointer(record: dict[str, Any] | None) -> dict[str, Any]:
    """Map one trace record onto the platform's optional pointer fields."""

    if not record:
        return {}
    pointer: dict[str, Any] = {}
    for source, target in (
        ("traceId", "trace_id"),
        ("traceFile", "trace_file"),
        ("timestamp", "trace_ts"),
    ):
        value = record.get(source)
        if isinstance(value, str) and value.strip():
            pointer[target] = value.strip()
    line = record.get("traceLine")
    if isinstance(line, int) and line > 0:
        pointer["trace_line"] = line
    return pointer


def _records(
    project_id: str,
    status: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """The newest ``limit`` trace records for a project, ascending."""

    filters: dict[str, str] = {"projectId": project_id}
    if status:
        filters["status"] = status
    try:
        return list(read_trace_records(filters=filters, limit=limit))
    except Exception:  # noqa: BLE001 - diagnostics must never block feedback
        return []


def _stage_of_records(records: list[dict[str, Any]]) -> str:
    """The stage named by the newest record that names one.

    The single most recent error is often a generic ``api``/CONFLICT that says
    nothing about which step broke, so walk back until a record does rather
    than reporting "unknown" over a real signal a few lines earlier.
    """

    for record in reversed(records):  # ascending, so tail is newest
        stage = _stage_of(record)
        if stage != "unknown":
            return stage
    return "unknown"


@router.post("/draft")
async def build_feedback_draft(
    data: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Complete a feedback body with the context only Creator can supply."""
    project_id = str(data.get("project_id") or "").strip()
    stage = str(data.get("stage") or "").strip()
    feedback = str(data.get("feedback") or "").strip()
    if not project_id:
        raise ValidationError("缺少 project_id，无法提交反馈")
    if len(project_id) > PROJECT_ID_LIMIT:
        raise ValidationError(f"project_id 超过 {PROJECT_ID_LIMIT} 字符")
    if not feedback:
        raise ValidationError("反馈内容为空")
    if len(feedback) > FEEDBACK_LIMIT:
        raise ValidationError(f"反馈内容超过 {FEEDBACK_LIMIT} 字符")

    # A complaint usually follows a failure, so cite the newest error and
    # derive the stage from the newest error that actually names a stage.
    errors = _records(project_id, "error", STAGE_SCAN)
    recent = errors or _records(project_id, None, STAGE_SCAN)
    record = recent[-1] if recent else None
    stage = stage or _stage_of_records(recent)
    return {
        "project_id": project_id,
        "stage": stage or "unknown",
        "feedback": feedback,
        **_pointer(record),
    }


class _AccountRequired(ConflictError):
    code = "PLATFORM_ACCOUNT_REQUIRED"


def _platform_connection(request: Request):
    # The QwenPaw host owns login, refresh and account-change checks.
    service = getattr(request.app.state, "community_connection_service", None)
    if service is None:
        raise _AccountRequired("请在 QwenPaw 设置中连接 Platform 账号")
    return service


@router.get("/platform/account")
async def feedback_account(request: Request) -> dict[str, str]:
    status = await _platform_connection(request).status(local=False)
    account = status.get("account") or {}
    if not account.get("id"):
        raise _AccountRequired("请在 QwenPaw 设置中连接 Platform 账号")
    return {"id": str(account["id"])}


@router.post("/platform/screenshots")
async def upload_platform_screenshot(
    request: Request,
    file: UploadFile,
    x_platform_account: str = Header(min_length=1, max_length=128),
) -> dict:
    extension = _IMAGE_EXTENSIONS.get(file.content_type or "")
    if not extension:
        raise ValidationError("截图格式无效")
    content = await file.read(SCREENSHOT_BYTE_LIMIT + 1)
    if not content or len(content) > SCREENSHOT_BYTE_LIMIT:
        raise ValidationError("截图必须为 5 MB 以内的图片")
    return await _platform_connection(request).community_request(
        "POST",
        _PLATFORM_FEEDBACK + "/screenshots",
        account_id=x_platform_account,
        files={
            "file": (f"screenshot.{extension}", content, file.content_type)
        },
        timeout=60,
    )


class PlatformFeedback(BaseModel):
    description: str = Field(min_length=1, max_length=FEEDBACK_LIMIT)
    screenshot_urls: list[HttpUrl] = Field(default_factory=list, max_length=8)


@router.post("/platform")
async def submit_platform_feedback(
    body: PlatformFeedback,
    request: Request,
    x_platform_account: str = Header(min_length=1, max_length=128),
) -> dict:
    if not body.description.strip():
        raise ValidationError("反馈内容为空")
    payload = {
        "modules": ["qwenpaw_usage"],
        "qwenpaw_version_tag": "qwenpaw_creator",
        "description": body.description,
    }
    if body.screenshot_urls:
        payload["screenshot_urls"] = [str(url) for url in body.screenshot_urls]
    await _platform_connection(request).community_request(
        "POST",
        _PLATFORM_FEEDBACK,
        account_id=x_platform_account,
        json=payload,
    )
    return {"submitted": True}


__all__ = ["router"]
