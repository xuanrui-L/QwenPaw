# -*- coding: utf-8 -*-
"""Whole-piece cover poster inputs and helpers for interactive bundles.

The platform lists interactive works from a static cover image; unlike the
local player it never runs ``index.html``, so it cannot grab the entry
segment's first frame at runtime. The cover therefore ships inside the exported
ZIP as ``cover.jpg``.

Generation is a project-level task (see ``cover_execution``): it renders a 16:9
text-to-image poster from the story and persists it on the project. At export
we only read that stored poster; if it has not been produced yet we fall back
to a single frame pulled from the entry segment so the bundle essentially
always carries a cover.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

logger = logging.getLogger(__name__)

#: The poster is always landscape, whatever the video's own aspect ratio is.
COVER_ASPECT_RATIO = "16:9"
COVER_FILENAME = "cover.jpg"
#: Mirror the interaction base-frame cap; oversized posters are rejected.
COVER_LIMIT_BYTES = 8 * 1024 * 1024
#: Skip the very start of a clip, which is often a fade-from-black.
_COVER_SEEK_SECONDS = 1.0

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_FINGERPRINT_MARKER = "input_fingerprint="


def cover_input_fingerprint(project: Any) -> str:
    """Hash the rendered poster brief so staleness tracks the prompt.

    Deriving the fingerprint from the exact prompt (plus the fixed aspect
    ratio) means any input that changes the picture - story, style, or a
    character/scene the poster is anchored on - invalidates a stored cover,
    without a hand-maintained field list drifting from ``build_cover_prompt``.
    """

    raw = (
        f"{COVER_ASPECT_RATIO}\n{build_cover_prompt(project)}\n"
        f"refs={','.join(v for _, v in cover_reference_version_ids(project))}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def cover_is_current(project: Any) -> bool:
    """Whether a stored cover exists and still matches the current inputs."""

    presentation = project.interactive_presentation
    return bool(
        presentation.cover_file_id
        and presentation.cover_checksum
        and presentation.cover_fingerprint
        == _FINGERPRINT_MARKER + cover_input_fingerprint(project),
    )


def cover_design_notes(project: Any) -> str:
    return (
        "Agent-authored whole-piece cover\n"
        f"{_FINGERPRINT_MARKER}{cover_input_fingerprint(project)}"
    )


def _poster_subject_digest(visual: Any) -> tuple[str, str]:
    """Compact ``name（description）`` digests of characters and scenes.

    Entities are walked in ``entity_id`` order so the prompt - and therefore
    the fingerprint - stays stable across loads regardless of dict order.
    """

    entities = getattr(visual, "entities", None)
    items = getattr(entities, "items", None) or {}
    characters: list[str] = []
    scenes: list[str] = []
    for entity in sorted(
        items.values(),
        key=lambda item: getattr(item, "entity_id", "") or "",
    ):
        name = (getattr(entity, "name", "") or "").strip()
        if not name:
            continue
        description = (getattr(entity, "description", "") or "").strip()
        label = f"{name}（{description}）" if description else name
        kind = getattr(entity, "kind", "")
        if kind == "character":
            characters.append(label)
        elif kind == "scene":
            scenes.append(label)
    return "、".join(characters[:3]), "、".join(scenes[:3])


def _entity_selected_version(entity: Any) -> str | None:
    """An entity's chosen image artifact id.

    Mirrors the lineup identity resolution: canonical variant -> any variant
    with a selection -> entity-level selection. Returns ``None`` when the
    entity has no generated image yet.
    """

    variants = getattr(entity, "variants", None)
    items = getattr(variants, "items", None) or {}
    canonical = getattr(entity, "canonical_variant_id", None)
    variant = items.get(canonical) if canonical else None
    if variant is None or not getattr(
        variant,
        "selected_artifact_version_id",
        None,
    ):
        variant = next(
            (
                item
                for item in items.values()
                if getattr(item, "selected_artifact_version_id", None)
            ),
            variant,
        )
    return (
        (
            getattr(variant, "selected_artifact_version_id", None)
            if variant is not None
            else None
        )
        or getattr(entity, "selected_artifact_version_id", None)
        or None
    )


def cover_reference_version_ids(project: Any) -> list[tuple[str, str]]:
    """Ordered ``(label, artifact_version_id)`` inputs for an image-to-image
    poster: the first scene and first character (by declared entity order)
    that already have a selected image. Scene leads so 图一 is the setting
    and 图二 the protagonist, matching the poster brief. Empty when no
    reference image is ready yet, which keeps the cover text-to-image.
    """

    visual = getattr(project, "visual", None)
    entities = getattr(visual, "entities", None)
    items = getattr(entities, "items", None) or {}
    order = getattr(entities, "order", None) or list(items)
    scene: str | None = None
    protagonist: str | None = None
    for entity_id in order:
        entity = items.get(entity_id)
        if entity is None:
            continue
        kind = getattr(entity, "kind", "")
        if kind not in ("scene", "character"):
            continue
        version_id = _entity_selected_version(entity)
        if not version_id:
            continue
        if kind == "scene" and scene is None:
            scene = version_id
        elif kind == "character" and protagonist is None:
            protagonist = version_id
        if scene and protagonist:
            break
    refs: list[tuple[str, str]] = []
    if scene:
        refs.append(("关键场景", scene))
    if protagonist:
        refs.append(("主角", protagonist))
    return refs


def build_cover_prompt(
    project: Any,
    *,
    reference_labels: Sequence[str] | None = None,
) -> str:
    """A landscape key-art brief drawn from the story, not a template."""

    strategy = getattr(project, "strategy", None)
    visual = getattr(project, "visual", None)
    brief = (
        getattr(strategy, "creative_brief", "") or project.description or ""
    ).strip()
    direction = (getattr(strategy, "creative_direction", "") or "").strip()
    audience = (getattr(strategy, "audience", "") or "").strip()
    style = (getattr(visual, "style", "") or "").strip()
    bible = (getattr(visual, "visual_bible", "") or "").strip()
    characters, scenes = _poster_subject_digest(visual)

    lines = [
        f"为交互式互动短剧《{project.name}》设计一张横版宣传海报（key art），" "用作平台作品列表的封面。",
        "画幅横向 16:9，电影级布光与构图，主体突出、有戏剧张力与悬念氛围；"
        "画面预留干净的标题排版空间，但不要直接生成任何文字、字幕或水印。",
    ]
    if reference_labels:
        # 最多两个参考图（一个场景、一个主角），模型也只按这两张融合。
        roles = "，".join(
            f"图{numeral}是{label}"
            for numeral, label in zip(("一", "二"), reference_labels)
        )
        lines.insert(
            1,
            f"以提供的参考图为素材（{roles}），将主角自然地置入该场景，"
            "统一光影与美术风格后再合成海报；参考图只取其形象与氛围，不要照搬原图构图边界。",
        )
    if brief:
        lines.append(f"剧情方向：{brief}")
    if direction:
        lines.append(f"创作基调：{direction}")
    if characters:
        lines.append(f"主角：{characters}")
    if scenes:
        lines.append(f"关键场景：{scenes}")
    if bible:
        lines.append(f"视觉设定：{bible}")
    if style:
        lines.append(f"画面风格：{style}")
    if audience:
        lines.append(f"目标观众：{audience}")
    return "\n".join(lines)


async def render_cover_bytes(
    project: Any,
    *,
    references: Sequence[tuple[str, str]] = (),
) -> bytes:
    """Render a 16:9 poster with the configured image model and return bytes.

    ``references`` is an ordered ``(label, url)`` list of already-generated
    asset images (scene, protagonist); when present the poster is produced
    image-to-image from them, otherwise it falls back to text-to-image.

    Raises on any failure so the caller can record a durable task error; a
    missing cover never reaches the export path unannounced. Provider imports
    stay lazy so read-only paths never load the image backends.
    """

    from models.image import generate_image
    from utils.paths import media_path_from_url, media_task_scope

    labels = [label for label, _ in references]
    urls = [url for _, url in references]
    prompt = build_cover_prompt(project, reference_labels=labels or None)
    with media_task_scope(f"cover-{uuid4().hex[:16]}", project_id=None):
        result = await generate_image(
            prompt,
            aspect_ratio=COVER_ASPECT_RATIO,
            reference_image_urls=urls or None,
        )
    url = result["url"] if isinstance(result, dict) else result
    payload = media_path_from_url(url).read_bytes()
    if not payload or not _looks_like_image(payload):
        raise ValueError("封面生成结果不是有效图片")
    jpeg = _to_jpeg(payload, _suffix_for(payload))
    final = jpeg or payload
    if len(final) > COVER_LIMIT_BYTES:
        raise ValueError(
            f"封面超过 {COVER_LIMIT_BYTES} 字节上限",
        )
    return final


def poster_frame_from_video(video_bytes: bytes) -> bytes | None:
    """Extract one landscape frame from the entry cut as a cover fallback."""

    if not video_bytes:
        return None
    frame = _run_ffmpeg_frame(video_bytes, ".mp4", seek=_COVER_SEEK_SECONDS)
    if frame and len(frame) <= COVER_LIMIT_BYTES:
        return frame
    return None


def _looks_like_image(payload: bytes) -> bool:
    return payload.startswith(_JPEG_MAGIC) or payload.startswith(_PNG_MAGIC)


def _suffix_for(payload: bytes) -> str:
    return ".png" if payload.startswith(_PNG_MAGIC) else ".img"


def _to_jpeg(payload: bytes, suffix: str) -> bytes | None:
    """Re-encode a PNG poster so ``cover.jpg`` matches its name."""

    return _run_ffmpeg_frame(payload, suffix, seek=0.0)


def _run_ffmpeg_frame(
    payload: bytes,
    suffix: str,
    *,
    seek: float,
) -> bytes | None:
    from services.runtime_files.runtime_dependencies import resolve_ffmpeg

    executable = resolve_ffmpeg()
    if not executable:
        logger.info("未找到 ffmpeg，跳过封面转码/抽帧")
        return None
    with tempfile.TemporaryDirectory(prefix="creator-cover-") as tmp:
        source = Path(tmp) / f"source{suffix or '.img'}"
        output = Path(tmp) / "cover.jpg"
        source.write_bytes(payload)
        command = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
        ]
        if seek > 0:
            command += ["-ss", f"{seek}"]
        command += [
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output),
        ]
        try:
            subprocess.run(  # noqa: S603
                command,
                capture_output=True,
                timeout=30,
                check=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logger.info("ffmpeg 抽帧失败：%s", exc)
            return None
        if not output.is_file():
            return None
        return output.read_bytes()


__all__ = [
    "COVER_ASPECT_RATIO",
    "COVER_FILENAME",
    "COVER_LIMIT_BYTES",
    "build_cover_prompt",
    "cover_design_notes",
    "cover_input_fingerprint",
    "cover_is_current",
    "cover_reference_version_ids",
    "poster_frame_from_video",
    "render_cover_bytes",
]
