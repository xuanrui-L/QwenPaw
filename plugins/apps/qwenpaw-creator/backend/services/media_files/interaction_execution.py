# -*- coding: utf-8 -*-
"""Interaction motion drafting over Project files（方案 2.7a interaction_draft）。

用文本模型为观众抉择点（InteractionCreation element）起草 html_css 可点击
动效：输入是问题文案、各选项对应分支边的 label/prompt（从
``project.narrative_edges`` join）与倒计时配置，输出一份**纯 HTML 文档**
（内联 CSS 动画、无 <script>、无外部资源、每个选项带 ``data-edge-ref``），
写回 ``element.creation.motion``（MotionGraphic html_css）。零媒体开销：
只消耗一次文本模型调用；``input_fingerprint`` 防重算（question + options +
edges 指纹嵌入 design_notes），同输入重复派发直接复放。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from domain.errors import ValidationError, ConflictError
from domain.enums import TaskKind, TaskStatus
from models import text_model
from services.runtime_files.execution_store import ProjectExecutionStore
from services.runtime_files.execution_models import TaskRecord
from services.runtime_files.errors import RecordNotFoundError
from services.project_files.interaction_html import validate_interaction_html
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    narrative_timeline_ids,
    InteractionCreation,
    InteractivePresentation,
    MotionGraphic,
    NarrativeEdge,
    Project,
    TimelineElement,
)
from services.project_files.presentation_html import validate_presentation_html
from services.runtime_files.models import (
    ChangeOrigin,
    ReviewPolicy,
    ReviewBoundary,
)
from utils.exceptions import ModelError
from utils.logger import setup_logger

from .interaction_fingerprint import (
    FINGERPRINT_MARKER as _FINGERPRINT_MARKER,
    interaction_request_fingerprint,
)
from .presentation_authoring import (
    PRESENTATION_TARGET,
    PRESENTATION_SYSTEM_PROMPT,
    presentation_fingerprint,
    presentation_prompt,
)


def _request_fingerprint(creation, edges, project):
    if isinstance(creation, InteractivePresentation):
        return presentation_fingerprint(project, creation)
    return interaction_request_fingerprint(creation, edges, project)


def _creation_for_target(project, element_id):
    if element_id == PRESENTATION_TARGET:
        return "", project.interactive_presentation
    timeline_id, element = _locate_interaction(project, element_id)
    return timeline_id, element.creation


logger = setup_logger("media_files.interaction")

# 不合格输出（缺 data-edge-ref 等）只重试一次：文本调用便宜但不免费，
# 连续两次结构性不合格说明 prompt/输入需要人工调整，报 ModelError。
_MAX_MODEL_ATTEMPTS = 2
_ACTIVE_TASKS: set[tuple[str, str, str]] = set()

# MotionGraphic.html 的模型约束（min_length=32 / max_length=200_000）。
_MIN_HTML_CHARS = 32
_MAX_HTML_CHARS = 200_000

_INTERACTION_SYSTEM_PROMPT = (
    "你是互动短剧的抉择动效设计师。输出一份完整的纯 HTML 文档"
    "（以 <!DOCTYPE html> 开头），作为观众抉择点的可点击动效层。硬性要求：\n"
    "- 全部样式与动画写在内联 <style> 中，只用 CSS 动画（@keyframes）；\n"
    "- 禁止出现 <script>，禁止引用任何外部资源（外链、外部字体、外部图片）；\n"
    '- 每个选项渲染为一个可点击的 button 元素，且必须带 data-edge-ref="<边id>" 属性，'
    "属性值逐字使用给定的边 id，每个选项恰好一个，不得多不得少；\n"
    "- 从零设计布局、色彩、排版、装饰与动画，根据故事和用户要求独立创作，不套模板。"
    "遵循画幅，响应式铺满容器；宿主不提供任何兜底视觉。\n"
    "- 若有倒计时，必须自行设计一个带 data-interaction-countdown 的文本节点；"
    "宿主只填入剩余秒数，不绘制外观。\n"
    "- 问句文字节点带 data-question，不要标在包裹按钮的容器上；"
    "每个 button 内用 span data-option-label 表达权威文案；\n"
    "- 不用 CSS 注释、转义、url()、@import；不使用事件属性、外部图片或链接；\n"
    "- 只输出 HTML 文档本身，不要任何解释，不要 markdown 代码围栏。"
)


@dataclass(frozen=True, slots=True)
class FileInteractionExecutionResult:
    timeline_id: str
    element_id: str
    input_fingerprint: str
    project_etag: str
    project_generation: int
    replayed: bool
    task_id: str | None = None


def _stable_id(prefix: str, project_id: str, idempotency_key: str) -> str:
    digest = uuid5(
        NAMESPACE_URL,
        f"qwenpaw-creator:file-interaction:{prefix}:{project_id}:"
        f"{idempotency_key}",
    ).hex
    return f"{prefix}-{digest}"


def _element_id_from_ref(target_ref: str) -> str:
    value = target_ref.strip().removeprefix("element:")
    if not value:
        raise ValidationError(f"invalid element ref: {target_ref!r}")
    return value


def _locate_interaction(
    project: Project,
    element_id: str,
) -> tuple[str, TimelineElement]:
    for timeline_id in narrative_timeline_ids(project):
        timeline = project.timelines.items[timeline_id]
        element = timeline.elements_by_id.get(element_id)
        if element is None:
            continue
        if not isinstance(element.creation, InteractionCreation):
            raise ValidationError(
                f"element 不是观众抉择交互: {element_id}",
            )
        return timeline_id, element
    raise ValidationError(f"element 不存在: {element_id}")


def _build_interaction_prompt(
    project: Project,
    timeline_id: str,
    creation: InteractionCreation,
    edges_by_id: Mapping[str, NarrativeEdge],
) -> str:
    timeline = project.timelines.items[timeline_id]
    option_lines: list[str] = []
    for index, option in enumerate(creation.options, start=1):
        edge = edges_by_id[option.edge_ref]
        target = project.timelines.items.get(edge.target_timeline_id)
        target_title = (
            (target.title or edge.target_timeline_id)
            if target is not None
            else edge.target_timeline_id
        )
        option_lines.append(
            f"{index}. 边id `{option.edge_ref}` · 选项文案「"
            f"{edge.label or option.edge_ref}」 · 走向《{target_title}》"
            + (f" · 抉择语「{edge.prompt}」" if edge.prompt else "")
            + (
                f" · 按钮外观要求「{option.design_prompt}」"
                if option.design_prompt
                else ""
            ),
        )
    countdown = (
        f"{creation.countdown_seconds:.0f} 秒后自动选择默认项"
        f"（default_edge_ref={creation.default_edge_ref}）"
        if creation.countdown_seconds and creation.default_edge_ref
        else (
            f"{creation.countdown_seconds:.0f} 秒倒计时"
            if creation.countdown_seconds
            else "无倒计时"
        )
    )
    sections = [
        f"项目：{project.name}（{project.description or '无描述'}）",
        f"源集（抉择点所在叙事节点）：{timeline.title or timeline_id}",
        f"画幅：{project.settings.aspect_ratio}；"
        f"视觉风格：{project.visual.style}；视觉基调：{project.visual.visual_bible}",
        f"设计与修改要求：{creation.design_prompt or '清晰可点击，与项目视觉一致'}",
        f"作品页面设计要求：{project.interactive_presentation.design_prompt}",
        f"背景帧引用：{creation.base_frame_ref or '播放器在抉择时暂停的画面'}；不嵌入图片资源",
        "热点（normalized_canvas，宿主应用精确位置）："
        f"{[o.model_dump(mode='json') for o in creation.options]}",
        f"抉择问题：{creation.question}",
        "选项（每个选项一个可点击元素，data-edge-ref 逐字用边id）：\n" + "\n".join(option_lines),
        f"倒计时：{countdown}",
        "请输出这份抉择动效的完整 HTML 文档。",
    ]
    return "\n\n".join(sections)


def _strip_code_fences(raw: str) -> str:
    """剥掉 markdown 代码围栏（```html ... ```），只留 HTML 文档本体。"""

    text = raw.strip()
    if text.startswith("```"):
        newline = text.find("\n")
        text = text[newline + 1 :] if newline >= 0 else ""
        stripped = text.rstrip()
        if stripped.endswith("```"):
            text = stripped[: -len("```")]
    return text.strip()


def _validate_motion_html(
    html: str,
    creation: InteractionCreation,
) -> list[str]:
    """结构性校验：不合格原因列表（空 = 合格）。"""

    return validate_interaction_html(
        html,
        [o.edge_ref for o in creation.options],
        require_countdown=bool(
            creation.countdown_seconds and creation.default_edge_ref,
        ),
    )


def _design_notes(
    creation: InteractionCreation,
    edges_by_id: Mapping[str, NarrativeEdge],
    fingerprint: str,
) -> str:
    """prompt 摘要 + 指纹标记（复放判定的持久化位置）。"""

    labels = "、".join(
        (
            edges_by_id[option.edge_ref].label or option.edge_ref
            for option in creation.options
        ),
    )
    countdown = (
        f"倒计时 {creation.countdown_seconds:.0f}s"
        if creation.countdown_seconds
        else "无倒计时"
    )
    return (
        f"抉择动效 · 问题「{creation.question}」 · 选项 {labels} · "
        f"{countdown}\n{_FINGERPRINT_MARKER}{fingerprint}"
    )


def _motion_is_drafted(motion: MotionGraphic | None) -> bool:
    return motion is not None and bool(motion.html or motion.html_file_id)


def _publish_interaction_motion(
    services: CreatorFileServices,
    *,
    project_id: str,
    timeline_id: str,
    element_id: str,
    html: str,
    design_notes: str,
    fingerprint: str,
    idempotency_key: str,
    input_fingerprint: str,
    design_prompt: str,
    task_id: str,
) -> FileInteractionExecutionResult:
    """写回 element.creation.motion，走既有提交边界（commit 时全量校验）。"""

    with services.projects.lifecycle_lock(project_id):
        base = services.projects.read(project_id)
        working = base.project.model_copy(deep=True)
        _, creation = _creation_for_target(working, element_id)
        current_edges = {
            edge.edge_id: edge for edge in working.narrative_edges
        }
        if (
            _request_fingerprint(creation, current_edges, working)
            != input_fingerprint
        ):
            raise ConflictError(
                "Interaction inputs changed during generation; "
                "stale result was discarded",
            )
        if services.reviews.all_pending(project_id):
            raise ConflictError(
                "Project has pending review; interaction result was discarded",
            )
        task = ProjectExecutionStore(services.root).get_task(
            project_id,
            task_id,
        )
        if task.status is not TaskStatus.RUNNING:
            raise ConflictError(
                "Interaction task was cancelled; result was discarded",
            )
        creation.design_prompt = design_prompt
        creation.motion = MotionGraphic(
            format="html_css",
            html=html,
            fps=24,
            loop=True,
            design_notes=design_notes,
        )
        commit = services.commits.commit(
            base=base,
            candidate=working.model_dump(mode="json"),
            origin=ChangeOrigin.RUNTIME_TASK,
            review_policy=ReviewPolicy.REQUIRE_REVIEW,
            review_boundary=ReviewBoundary(
                request_id=idempotency_key,
                accepted_generation=base.generation,
                accepted_etag=base.etag,
            ),
            caused_by_request_id=idempotency_key,
            round_id=_stable_id("round", project_id, idempotency_key),
            transaction_id=_stable_id(
                "transaction",
                project_id,
                idempotency_key,
            ),
            advance_accepted_baseline=False,
            _lifecycle_lock_held=True,
        )
        services.poller.note_commit(commit.snapshot)
    return FileInteractionExecutionResult(
        timeline_id=timeline_id,
        element_id=element_id,
        input_fingerprint=fingerprint,
        project_etag=commit.snapshot.etag,
        project_generation=commit.snapshot.generation,
        replayed=False,
        task_id=task_id,
    )


async def execute_file_interaction_command(
    services: CreatorFileServices,
    *,
    project_id: str,
    target_ref: str,
    arguments: Mapping[str, Any],
    idempotency_key: str,
    expected_object_versions: Sequence[str] = (),
) -> FileInteractionExecutionResult:
    """为一个抉择 element 起草 html_css 动效并写回 creation.motion。"""
    # Keep admission, generation, publication and durable failure in one flow.
    # pylint: disable=too-many-branches,too-many-statements

    is_presentation = target_ref == f"project:{project_id}"
    element_id = (
        PRESENTATION_TARGET
        if is_presentation
        else _element_id_from_ref(target_ref)
    )
    snapshot = await asyncio.to_thread(services.projects.read, project_id)
    if any(
        not value.startswith(f"project:{snapshot.etag}:")
        for value in expected_object_versions
    ):
        raise ConflictError(
            "Interaction command target changed before admission",
        )
    project = snapshot.project
    timeline_id, creation = _creation_for_target(project, element_id)

    edges_by_id = {edge.edge_id: edge for edge in project.narrative_edges}
    unknown = [
        option.edge_ref
        for option in getattr(creation, "options", [])
        if option.edge_ref not in edges_by_id
    ]
    if unknown:
        raise ValidationError(
            "交互选项引用未知分支边: " + "、".join(unknown),
        )

    input_fingerprint = _request_fingerprint(creation, edges_by_id, project)
    guidance = str(arguments.get("guidance") or "").strip()
    if guidance:
        creation = creation.model_copy(update={"design_prompt": guidance})
    fingerprint = _request_fingerprint(creation, edges_by_id, project)
    dispatch_key = idempotency_key
    # 与 script_execution 一致：stale 重派共享节点派发 key，但发布事务
    # 必须换新 id；用请求指纹为持久 id 定界，避免撞旧事务。
    idempotency_key = _stable_id(
        "interaction",
        project_id,
        f"{idempotency_key}:{fingerprint}",
    )
    motion = creation.motion
    if (
        _motion_is_drafted(motion)
        and f"{_FINGERPRINT_MARKER}{fingerprint}" in motion.design_notes
    ):
        logger.info(
            "interaction draft semantic replay: project=%s element=%s",
            project_id,
            element_id,
        )
        return FileInteractionExecutionResult(
            timeline_id=timeline_id,
            element_id=element_id,
            input_fingerprint=fingerprint,
            project_etag=snapshot.etag,
            project_generation=snapshot.generation,
            replayed=True,
        )

    execution = ProjectExecutionStore(services.root)
    task_id = _stable_id("task", project_id, idempotency_key)
    if services.reviews.all_pending(project_id):
        raise ConflictError(
            "Approve or reject pending project changes "
            "before generating interaction motion",
        )
    try:
        existing = execution.get_task(project_id, task_id)
    except RecordNotFoundError:
        existing = None
    if existing is not None:
        if existing.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
            raise ConflictError("Interaction generation already running")
        raise ModelError(
            "Interaction task already finished; "
            "edit the design or explicitly retry with a new key",
            retryable=False,
        )
    execution.create_task(
        TaskRecord(
            task_id=task_id,
            project_id=project_id,
            kind=TaskKind.INTERACTION_DRAFT,
            request_fingerprint=fingerprint,
            idempotency_key=dispatch_key,
            input_generation=snapshot.generation,
            input_etag=snapshot.etag,
            input_refs=[target_ref],
            metadata={
                "targetRef": target_ref,
                "timelineId": timeline_id,
                "elementId": element_id,
                "inputFingerprint": input_fingerprint,
            },
        ),
    )
    attempt_id = f"{task_id}-attempt-1"
    execution.append_task_attempt(
        project_id,
        task_id,
        event_id=f"{attempt_id}-start",
        attempt_id=attempt_id,
        status="RUNNING",
        input={"fingerprint": fingerprint},
    )
    prompt = (
        presentation_prompt(project, creation)
        if is_presentation
        else _build_interaction_prompt(
            project,
            timeline_id,
            creation,
            edges_by_id,
        )
    )
    active_key = (str(services.root), project_id, task_id)
    _ACTIVE_TASKS.add(active_key)
    try:
        html = None
        problems = []
        attempt_prompt = prompt
        for attempt in range(_MAX_MODEL_ATTEMPTS):
            raw = await text_model.chat_completion(
                attempt_prompt,
                system_prompt=(
                    PRESENTATION_SYSTEM_PROMPT
                    if is_presentation
                    else _INTERACTION_SYSTEM_PROMPT
                ),
                temperature=0.5,
                max_tokens=12000 if is_presentation else 6000,
                # A presentation contains four complete screens. Keep its
                # response bounded while allowing more time than one choice.
                timeout=300.0 if is_presentation else 180.0,
            )
            candidate = _strip_code_fences(raw)
            problems = (
                validate_presentation_html(
                    candidate,
                    narrative_timeline_ids(project),
                    {
                        key: value.model_dump(mode="json")
                        for key, value in creation.screens.items()
                    },
                )
                if is_presentation
                else _validate_motion_html(candidate, creation)
            )
            if not problems:
                html = candidate
                break
            attempt_prompt = (
                prompt
                + "\n\n上一次输出不合格（"
                + "；".join(problems)
                + "），请重新输出完整 HTML。"
            )
        if html is None:
            raise ModelError(
                "抉择动效生成结果不合格：" + "；".join(problems),
                retryable=False,
            )
        result = await asyncio.to_thread(
            _publish_interaction_motion,
            services,
            project_id=project_id,
            timeline_id=timeline_id,
            element_id=element_id,
            html=html,
            design_notes=(
                "Agent-authored project interface\n"
                f"{_FINGERPRINT_MARKER}{fingerprint}"
                if is_presentation
                else _design_notes(creation, edges_by_id, fingerprint)
            ),
            fingerprint=fingerprint,
            idempotency_key=idempotency_key,
            input_fingerprint=input_fingerprint,
            design_prompt=creation.design_prompt,
            task_id=task_id,
        )
        execution.append_task_attempt(
            project_id,
            task_id,
            event_id=f"{attempt_id}-end",
            attempt_id=attempt_id,
            status="SUCCEEDED",
            output_refs=[target_ref],
            output={
                "projectGeneration": result.project_generation,
                "reviewRequired": True,
                "modelAttempts": attempt + 1,
            },
        )
        return result
    except BaseException as exc:
        status = (
            TaskStatus.CANCELLED
            if isinstance(exc, asyncio.CancelledError)
            else (
                TaskStatus.QUARANTINED
                if isinstance(exc, ConflictError)
                else TaskStatus.FAILED
            )
        )
        current = execution.get_task(project_id, task_id)
        if current.status is TaskStatus.RUNNING:
            execution.append_task_attempt(
                project_id,
                task_id,
                event_id=f"{attempt_id}-end",
                attempt_id=attempt_id,
                status=status.value,
                error={"message": str(exc), "retryable": False},
            )
        raise
    finally:
        _ACTIVE_TASKS.discard(active_key)


def recover_interrupted_interaction_tasks(
    services: CreatorFileServices,
) -> int:
    """Converge a published result or park an interrupted one-shot text call.

    Creator supports one backend process. Active calls in that process are
    excluded; restarting must never resubmit a possibly billed model request.
    """
    execution = ProjectExecutionStore(services.root)
    recovered = 0
    for project_id in services.projects.discover_project_ids():
        for task in execution.list_tasks(project_id):
            if (
                task.kind is not TaskKind.INTERACTION_DRAFT
                or task.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}
            ):
                continue
            if (str(services.root), project_id, task.task_id) in _ACTIVE_TASKS:
                continue
            project = services.projects.read(project_id).project
            try:
                _, creation = _creation_for_target(
                    project,
                    str(task.metadata.get("elementId", "")),
                )
                published = bool(
                    creation.motion
                    and f"{_FINGERPRINT_MARKER}{task.request_fingerprint}"
                    in creation.motion.design_notes,
                )
            except ValidationError:
                published = False
            status = TaskStatus.SUCCEEDED if published else TaskStatus.FAILED
            updates = (
                {"result": {"reviewRequired": True}}
                if published
                else {
                    "error": {
                        "message": (
                            "Interaction generation interrupted; "
                            "edit inputs or explicitly retry"
                        ),
                        "retryable": False,
                    },
                }
            )
            attempts = execution.list_task_attempts(project_id, task.task_id)
            if attempts and attempts[-1].status.value == "RUNNING":
                execution.append_task_attempt(
                    project_id,
                    task.task_id,
                    event_id=f"{attempts[-1].attempt_id}-recovered",
                    attempt_id=attempts[-1].attempt_id,
                    status=status.value,
                    output=updates.get("result", {}),
                    error=updates.get("error"),
                )
            else:
                execution.transition_task(
                    project_id,
                    task.task_id,
                    expected_status=task.status,
                    status=status,
                    updates=updates,
                )
            recovered += 1
    return recovered


__all__ = [
    "FileInteractionExecutionResult",
    "execute_file_interaction_command",
]
