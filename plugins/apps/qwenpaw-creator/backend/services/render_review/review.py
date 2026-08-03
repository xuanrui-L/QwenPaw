# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Render self-review loop: frame evidence → VLM review → advisory feedback.

The loop is an adviser, never a gate: it runs detached after a final
composition is published, writes its reports under the Project's
``runtime/render-review/`` directory and, on a revise verdict, hands the
structured findings to the next AI editing director specialist run as a
turn user message (admitted through the durable session boundary). After
``MAX_REVIEW_ROUNDS`` the chain closes and delivery proceeds regardless of
the verdict, with the reports retained beside the render.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
from uuid import uuid4

from models.vlm_model import chat_completion, multimodal_media_part
from schemas.render_review import RenderReviewReport
from services.observability.tracing import trace_event
from services.render_review.frames import (
    RenderReviewError,
    extract_review_frames,
    probe_audio_profile,
)
from services.render_review.protocol import (
    MAX_REVIEW_ROUNDS,
    build_review_user_text,
    findings_feedback_payload,
    parse_review_report,
    review_system_prompt,
)
from services.runtime_files import (
    MessageChannel,
    MessageClassification,
    RuntimeSessionNotFound,
)
from services.runtime_files.media_probe import probe_media
from utils.exceptions import ModelError
from utils.logger import setup_logger

if TYPE_CHECKING:
    from services.project_files.facade import CreatorFileServices

logger = setup_logger("creator.render_review")

_TRACE_COMPONENT = "render_review"
_VLM_ATTEMPTS = 2
_UNSAFE_REF_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


def _reports_root(services: "CreatorFileServices", project_id: str) -> Path:
    return (
        services.projects.project_root(project_id)
        / "runtime"
        / "render-review"
    )


def _chain_path(reports_root: Path, target_ref: str) -> Path:
    safe_ref = _UNSAFE_REF_CHARS.sub("-", target_ref).strip("-") or "target"
    return reports_root / f"chain-{safe_ref}.json"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f"{path.name}.tmp-{uuid4().hex[:8]}")
    staging.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(staging, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _admit_round(
    reports_root: Path,
    *,
    target_ref: str,
    video_id: str,
) -> tuple[int, str] | None:
    """Return ``(round_number, chain_id)`` or ``None`` when the chain is spent."""
    state = _read_json(_chain_path(reports_root, target_ref))
    if state is not None and state.get("status") == "open":
        rounds_completed = int(state.get("rounds_completed") or 0)
        if str(state.get("last_video_id") or "") == video_id:
            return None
        if rounds_completed >= MAX_REVIEW_ROUNDS:
            # Defensive: an open chain never exceeds the cap in practice.
            return None
        return rounds_completed + 1, str(state.get("chain_id") or "")
    return 1, f"chain-{uuid4().hex[:12]}"


def _complete_round(
    reports_root: Path,
    *,
    target_ref: str,
    chain_id: str,
    round_number: int,
    video_id: str,
    verdict: str,
) -> None:
    keep_open = verdict == "revise" and round_number < MAX_REVIEW_ROUNDS
    _write_json(
        _chain_path(reports_root, target_ref),
        {
            "chain_id": chain_id,
            "target_ref": target_ref,
            "rounds_completed": round_number,
            "status": "open" if keep_open else "closed",
            "last_video_id": video_id,
            "last_verdict": verdict,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def _plan_context(
    services: "CreatorFileServices",
    project_id: str,
    target_ref: str,
) -> dict[str, Any]:
    context: dict[str, Any] = {"timeline_ref": target_ref}
    try:
        snapshot = services.projects.read(project_id)
    except Exception:
        return context
    settings = getattr(snapshot.project, "settings", None)
    if settings is not None:
        context["content_type"] = getattr(settings, "content_type", None)
        context["target_duration_seconds"] = getattr(
            settings,
            "target_duration_seconds",
            None,
        )
    return context


async def review_render(
    services: "CreatorFileServices",
    *,
    project_id: str,
    video_path: Path,
    video_id: str,
    round_number: int = 1,
    plan_context: Mapping[str, Any] | None = None,
) -> RenderReviewReport:
    """Run one review round and persist the report beside the render."""
    reports_root = _reports_root(services, project_id)
    video_dir = reports_root / video_id
    frames = await asyncio.to_thread(
        extract_review_frames,
        video_path,
        output_dir=video_dir / f"frames-round-{round_number}",
    )
    audio_profile = await asyncio.to_thread(probe_audio_profile, video_path)
    probe = await asyncio.to_thread(probe_media, str(video_path))
    context = dict(plan_context or {})
    user_text = build_review_user_text(
        frames=frames,
        audio_profile=audio_profile,
        video_duration_seconds=probe.duration_seconds,
        plan_context=context,
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for frame in frames:
        content.append(
            multimodal_media_part(Path(frame.image_path).as_uri(), "image"),
        )
    video_ref = f"artifact-version:{video_id}"
    report: RenderReviewReport | None = None
    last_error: Exception | None = None
    for attempt in range(_VLM_ATTEMPTS):
        try:
            response_text = await chat_completion(
                content,
                system_prompt=review_system_prompt(),
                temperature=0.2,
                max_tokens=2400,
            )
        except ModelError as exc:
            # Transient provider/network failures get one more attempt;
            # the loop stays advisory either way.
            last_error = exc
            logger.warning(
                "render review VLM call failed (attempt %d): %s",
                attempt + 1,
                exc,
            )
            await asyncio.sleep(2 * (attempt + 1))
            continue
        try:
            report = parse_review_report(
                response_text,
                video_ref=video_ref,
                round_number=round_number,
            )
            break
        except ValueError as exc:
            last_error = exc
            logger.warning(
                "render review response unparsable (attempt %d): %s",
                attempt + 1,
                exc,
            )
    if report is None:
        raise RenderReviewError(
            f"review response invalid after {_VLM_ATTEMPTS} attempts: {last_error}",
        )
    await asyncio.to_thread(
        _write_json,
        video_dir / f"round-{round_number}.json",
        report.model_dump(mode="json"),
    )
    trace_event(
        "render_review.report",
        component=_TRACE_COMPONENT,
        attributes={
            "videoRef": video_ref,
            "round": round_number,
            "verdict": report.verdict,
            "failedDimensions": [
                item.dimension.value for item in report.failed_findings()
            ],
            "frameCount": len(frames),
            "hasAudio": audio_profile.has_audio,
        },
        projectId=project_id,
    )
    return report


def _feedback_message_text(
    report: RenderReviewReport,
    *,
    target_ref: str,
) -> str:
    payload = findings_feedback_payload(report)
    return (
        f"【成片自我审阅反馈 · 第 {report.round}/{MAX_REVIEW_ROUNDS} 轮】\n"
        f"成片 {report.video_ref} 未通过自我审阅。请委派 ai_editing_director "
        f"修订 {target_ref}：仅修复下列结构化审阅发现中列出的问题，不要扩大改动"
        "范围，修订完成后重新合成成片。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _admit_feedback(
    services: "CreatorFileServices",
    *,
    project_id: str,
    report: RenderReviewReport,
    target_ref: str,
    chain_id: str,
) -> bool:
    """Admit the findings as a durable turn message for the next editing run."""
    try:
        session = services.sessions.get_project_session_snapshot(project_id)
    except RuntimeSessionNotFound:
        logger.info(
            "render review feedback skipped: project %s has no runtime session",
            project_id,
        )
        return False
    conversations = services.sessions.list_conversations(
        project_id,
        session.session_id,
    )
    default = next(
        (item for item in conversations if item.is_default),
        conversations[0] if conversations else None,
    )
    if default is None:
        return False
    request_id = f"render-review-{chain_id}-round-{report.round}"
    services.sessions.admit_user_request(
        project_id,
        session.session_id,
        default.conversation_id,
        request_id=request_id,
        client_message_id=request_id,
        content_parts=[
            {
                "type": "text",
                "text": _feedback_message_text(report, target_ref=target_ref),
            },
        ],
        source="render_review_feedback",
        channel=MessageChannel.RUNTIME,
        classification=MessageClassification.MUTATION_INSTRUCTION,
        metadata={"renderReview": findings_feedback_payload(report)},
    )
    return True


async def run_review_loop(
    services: "CreatorFileServices",
    *,
    project_id: str,
    video_path: Path,
    video_id: str,
    target_ref: str,
) -> RenderReviewReport | None:
    """Run one advisory review round for a freshly published final render."""
    try:
        reports_root = _reports_root(services, project_id)
        admitted = await asyncio.to_thread(
            _admit_round,
            reports_root,
            target_ref=target_ref,
            video_id=video_id,
        )
        if admitted is None:
            trace_event(
                "render_review.skipped",
                component=_TRACE_COMPONENT,
                attributes={
                    "videoRef": f"artifact-version:{video_id}",
                    "reason": "chain_spent_or_duplicate",
                },
                projectId=project_id,
            )
            return None
        round_number, chain_id = admitted
        plan_context = await asyncio.to_thread(
            _plan_context,
            services,
            project_id,
            target_ref,
        )
        report = await review_render(
            services,
            project_id=project_id,
            video_path=video_path,
            video_id=video_id,
            round_number=round_number,
            plan_context=plan_context,
        )
        feedback_sent = False
        if report.verdict == "revise" and round_number < MAX_REVIEW_ROUNDS:
            feedback_sent = await asyncio.to_thread(
                _admit_feedback,
                services,
                project_id=project_id,
                report=report,
                target_ref=target_ref,
                chain_id=chain_id,
            )
            if feedback_sent:
                # Lazy import: the runtime registry pulls in the driver and
                # would create an import cycle at module load time.
                from services.file_agent_runtime.registry import (
                    notify_creator_agent_runtime,
                )

                notify_creator_agent_runtime(project_id)
        await asyncio.to_thread(
            _complete_round,
            reports_root,
            target_ref=target_ref,
            chain_id=chain_id,
            round_number=round_number,
            video_id=video_id,
            verdict=report.verdict,
        )
        trace_event(
            "render_review.round_completed",
            component=_TRACE_COMPONENT,
            attributes={
                "videoRef": report.video_ref,
                "round": round_number,
                "verdict": report.verdict,
                "feedbackSent": feedback_sent,
            },
            projectId=project_id,
        )
        return report
    except Exception as exc:
        # Advisory only: a review failure must never disturb delivery.
        logger.exception("render review loop failed for %s", video_id)
        trace_event(
            "render_review.failed",
            component=_TRACE_COMPONENT,
            status="error",
            attributes={
                "videoRef": f"artifact-version:{video_id}",
                "errorType": type(exc).__name__,
                "error": str(exc)[:500],
            },
            projectId=project_id,
        )
        return None


def schedule_render_review(
    services: "CreatorFileServices",
    *,
    project_id: str,
    published_result: Mapping[str, Any],
) -> None:
    """Detach a review round for a published COMPOSE_FINAL_VIDEO result."""
    try:
        indexed = published_result.get("indexedFile")
        artifact = published_result.get("artifactVersion")
        if not isinstance(indexed, Mapping) or not isinstance(
            artifact,
            Mapping,
        ):
            return
        relative_uri = str(indexed.get("relative_uri") or "")
        video_id = str(artifact.get("version_id") or "")
        target_ref = str(published_result.get("targetRef") or "")
        if not relative_uri or not video_id or not target_ref:
            return
        video_path = services.projects.project_root(project_id) / relative_uri
        task = asyncio.create_task(
            run_review_loop(
                services,
                project_id=project_id,
                video_path=video_path,
                video_id=video_id,
                target_ref=target_ref,
            ),
        )

        def _log_outcome(done: asyncio.Task[Any]) -> None:
            if not done.cancelled() and done.exception() is not None:
                logger.error(
                    "render review task crashed: %s",
                    done.exception(),
                )

        task.add_done_callback(_log_outcome)
    except Exception:
        # Never let advisory scheduling break the compose result path.
        logger.exception(
            "failed to schedule render review for project %s",
            project_id,
        )


__all__ = [
    "review_render",
    "run_review_loop",
    "schedule_render_review",
]
