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
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from domain.errors import ValidationError
from models import text_model
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    InteractionCreation,
    MotionGraphic,
    NarrativeEdge,
    Project,
    TimelineElement,
)
from services.runtime_files.models import ChangeOrigin, ReviewPolicy
from .interaction_fingerprint import (
    FINGERPRINT_MARKER as _FINGERPRINT_MARKER,
    interaction_request_fingerprint as _request_fingerprint,
)
from utils.exceptions import ModelError
from utils.logger import setup_logger

logger = setup_logger("media_files.interaction")

# 不合格输出（缺 data-edge-ref 等）只重试一次：文本调用便宜但不免费，
# 连续两次结构性不合格说明 prompt/输入需要人工调整，报 ModelError。
_MAX_MODEL_ATTEMPTS = 2

# MotionGraphic.html 的模型约束（min_length=32 / max_length=200_000）。
_MIN_HTML_CHARS = 32
_MAX_HTML_CHARS = 200_000

_EDGE_REF_ATTR = re.compile(r"data-edge-ref\s*=\s*[\"']([^\"']+)[\"']")

_INTERACTION_SYSTEM_PROMPT = (
    "你是互动短剧的抉择动效设计师。输出一份完整的纯 HTML 文档"
    "（以 <!DOCTYPE html> 开头），作为观众抉择点的可点击动效层。硬性要求：\n"
    "- 全部样式与动画写在内联 <style> 中，只用 CSS 动画（@keyframes）；\n"
    "- 禁止出现 <script>，禁止引用任何外部资源（外链、外部字体、外部图片）；\n"
    "- 每个选项渲染为一个可点击元素，且必须带 data-edge-ref=\"<边id>\" 属性，"
    "属性值逐字使用给定的边 id，每个选项恰好一个，不得多不得少；\n"
    "- 蓝图风格约束：深色影视氛围、竖屏 9:16 布局、问题文案醒目居中，"
    "选项按钮沿画面下部排布；若有倒计时则在画面角落预留倒计时视觉位；\n"
    "- 4-6 秒循环的呼吸/浮动动画，突出选项的可点击感；\n"
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
    for timeline_id in project.timelines.order:
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
            + (f" · 抉择语「{edge.prompt}」" if edge.prompt else ""),
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
        f"抉择问题：{creation.question}",
        "选项（每个选项一个可点击元素，data-edge-ref 逐字用边id）：\n"
        + "\n".join(option_lines),
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

    problems: list[str] = []
    lowered = html.lower()
    if "<script" in lowered:
        problems.append("包含 <script>，动效必须是纯 CSS 动画")
    refs = _EDGE_REF_ATTR.findall(html)
    expected = len(creation.options)
    if len(refs) != expected:
        problems.append(
            f"data-edge-ref 出现 {len(refs)} 次，应为选项数 {expected} 次",
        )
    missing = [
        option.edge_ref
        for option in creation.options
        if option.edge_ref not in refs
    ]
    if missing:
        problems.append("缺少选项 data-edge-ref：" + "、".join(missing))
    if len(html) < _MIN_HTML_CHARS:
        problems.append("HTML 文档过短")
    if len(html) > _MAX_HTML_CHARS:
        problems.append(f"HTML 文档超过 {_MAX_HTML_CHARS} 字符上限")
    return problems


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
) -> FileInteractionExecutionResult:
    """写回 element.creation.motion，走既有提交边界（commit 时全量校验）。"""

    with services.projects.lifecycle_lock(project_id):
        base = services.projects.read(project_id)
        working = base.project.model_copy(deep=True)
        timeline = working.timelines.items.get(timeline_id)
        element = (
            timeline.elements_by_id.get(element_id)
            if timeline is not None
            else None
        )
        if element is None or not isinstance(
            element.creation,
            InteractionCreation,
        ):
            raise ValidationError(
                f"抉择交互 element 已不存在: {element_id}",
            )
        element.creation.motion = MotionGraphic(
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
            review_policy=ReviewPolicy.AUTO_FIX,
            caused_by_request_id=idempotency_key,
            round_id=_stable_id("round", project_id, idempotency_key),
            transaction_id=_stable_id(
                "transaction",
                project_id,
                idempotency_key,
            ),
            advance_accepted_baseline=True,
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
    )


async def execute_file_interaction_command(
    services: CreatorFileServices,
    *,
    project_id: str,
    target_ref: str,
    arguments: Mapping[str, Any],
    idempotency_key: str,
) -> FileInteractionExecutionResult:
    """为一个抉择 element 起草 html_css 动效并写回 creation.motion。"""

    element_id = _element_id_from_ref(target_ref)
    snapshot = await asyncio.to_thread(services.projects.read, project_id)
    project = snapshot.project
    timeline_id, element = _locate_interaction(project, element_id)
    creation = element.creation
    assert isinstance(creation, InteractionCreation)

    edges_by_id = {edge.edge_id: edge for edge in project.narrative_edges}
    unknown = [
        option.edge_ref
        for option in creation.options
        if option.edge_ref not in edges_by_id
    ]
    if unknown:
        raise ValidationError(
            "交互选项引用未知分支边: " + "、".join(unknown),
        )

    fingerprint = _request_fingerprint(creation, edges_by_id)
    # 与 script_execution 一致：stale 重派共享节点派发 key，但发布事务
    # 必须换新 id；用请求指纹为持久 id 定界，避免撞旧事务。
    idempotency_key = f"{idempotency_key}:{fingerprint[:16]}"
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

    prompt = _build_interaction_prompt(
        project,
        timeline_id,
        creation,
        edges_by_id,
    )
    guidance = str(arguments.get("guidance") or "").strip()
    if guidance:
        prompt += f"\n\n额外修改意见（必须遵循）：{guidance}"

    html: str | None = None
    problems: list[str] = []
    attempt_prompt = prompt
    for _attempt in range(_MAX_MODEL_ATTEMPTS):
        raw = await text_model.chat_completion(
            attempt_prompt,
            system_prompt=_INTERACTION_SYSTEM_PROMPT,
            temperature=0.5,
        )
        candidate = _strip_code_fences(raw)
        problems = _validate_motion_html(candidate, creation)
        if not problems:
            html = candidate
            break
        logger.warning(
            "interaction draft invalid output for %s: %s",
            element_id,
            "；".join(problems),
        )
        attempt_prompt = (
            prompt
            + "\n\n上一次输出不合格（"
            + "；".join(problems)
            + "），请严格按硬性要求重新输出完整 HTML 文档。"
        )
    if html is None:
        raise ModelError(
            "抉择动效生成结果不合格：" + "；".join(problems),
            retryable=False,
        )

    return await asyncio.to_thread(
        _publish_interaction_motion,
        services,
        project_id=project_id,
        timeline_id=timeline_id,
        element_id=element_id,
        html=html,
        design_notes=_design_notes(creation, edges_by_id, fingerprint),
        fingerprint=fingerprint,
        idempotency_key=idempotency_key,
    )


__all__ = [
    "FileInteractionExecutionResult",
    "execute_file_interaction_command",
]
