# -*- coding: utf-8 -*-
"""Objective-fact orchestrator: run every operator fail-open, render hints.

One synchronous entry point per artifact kind assembles the per-operator
facts. Every operator is wrapped individually: a crash records
``status="error"`` for that operator only, a missing dependency records
``status="skipped"`` with the reason — collecting facts must never sink
a review round.

The rendered block frames everything as HINTS: detections are factual
inputs for the reviewer's reasoning, not pass/fail judgements. "No
speech detected" on a plan that never asked for a voiceover is simply
context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import json

import numpy as np

from services.run_review.objective import (
    audio_facts as audio_ops,
)
from services.run_review.objective import (
    camera_motion as camera_ops,
)
from services.run_review.objective import (
    consistency as consistency_ops,
)
from services.run_review.objective import (
    light_metrics as light_ops,
)
from services.run_review.objective import (
    machine_params as machine_ops,
)
from services.run_review.objective import (
    ocr_check as ocr_ops,
)
from services.run_review.objective import video_index as index_ops
from services.run_review.objective.media_io import (
    PCM_SAMPLE_RATE,
    GraySamples,
    decode_pcm_mono,
    probe_info,
    sample_gray_frames,
    sample_rgb_frame,
)
from services.run_review.operator_registry import is_operator_enabled
from utils.logger import setup_logger

logger = setup_logger("creator.run_review.objective.facts")

_FACTS_PREAMBLE = (
    "以下为程序化客观检测结果，仅是事实提示、不是对错结论：检测到/未检测到"
    "某要素本身不构成缺陷（如无人声对纯环境音剪辑完全合法、静止镜头可能是"
    "刻意定机位、程序刀数与计划的差异可能来自叠化转场）。仅当计划明确声明了"
    "对应期望且与事实明显矛盾时才可作为发现，且必须结合画面证据确认。"
)


_DISABLED_BLOCK = {
    "status": "disabled",
    "reason": "已在自我审阅高级配置中关闭",
}


def _safe(
    facts: dict[str, Any],
    key: str,
    operator: Callable[[], Any],
    *,
    switch: str | None = None,
) -> None:
    """Run one operator fail-open; honour its advanced-config switch.

    ``switch`` names the registry key (defaults to ``key``): a disabled
    operator records a visible ``disabled`` block instead of silently
    vanishing from the facts, so review reports stay self-explaining.
    """
    if not is_operator_enabled(switch or key):
        facts[key] = dict(_DISABLED_BLOCK)
        return
    try:
        facts[key] = operator()
    except Exception as exc:  # noqa: BLE001 - fail-open per operator
        logger.warning("objective operator %s failed: %s", key, exc)
        facts[key] = {"status": "error", "error": str(exc)[:200]}


def _rgb_probe_frames(
    media_path: Path,
    timestamps_ms: Sequence[int],
    *,
    count: int = 3,
) -> list[tuple[int, np.ndarray]]:
    if not timestamps_ms:
        return []
    picks = np.linspace(0, len(timestamps_ms) - 1, num=count, dtype=int)
    frames: list[tuple[int, np.ndarray]] = []
    for index in dict.fromkeys(picks):
        timestamp = int(timestamps_ms[int(index)])
        try:
            frames.append(
                (
                    timestamp,
                    sample_rgb_frame(media_path, timestamp_ms=timestamp),
                ),
            )
        except Exception:  # noqa: BLE001 - single-frame failure tolerated
            continue
    return frames


def collect_video_facts(
    media_path: Path,
    *,
    expected_duration_seconds: float | None = None,
    expected_aspect: Any = None,
    expected_texts: Sequence[str] | None = None,
    planned_shot_count: int | None = None,
    transcript_sentences: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """All CPU objective facts for one video artifact (thread-safe)."""
    facts: dict[str, Any] = {}

    def machine_param_block() -> dict[str, Any]:
        return machine_ops.machine_param_facts(
            probe_info(media_path),
            expected_duration_seconds=expected_duration_seconds,
            expected_aspect=expected_aspect,
        )

    _safe(facts, "machine_params", machine_param_block)

    samples: GraySamples | None = None
    diffs: np.ndarray | None = None
    index: dict[str, Any] | None = None

    def index_block() -> dict[str, Any]:
        nonlocal samples, diffs, index
        samples = sample_gray_frames(media_path)
        diffs = index_ops.frame_diffs(samples)
        index = index_ops.build_video_index(samples, diffs=diffs)
        payload = dict(index)
        if planned_shot_count is not None:
            payload["planned_shot_count"] = planned_shot_count
        # Keep the prompt block bounded: scenes stay, raw curve does not.
        return payload

    _safe(facts, "video_index", index_block)

    def light_block() -> dict[str, Any]:
        if samples is None or diffs is None:
            return {"status": "skipped", "skip_reason": "解码失败"}
        rgb_frames = _rgb_probe_frames(media_path, samples.timestamps_ms)
        return {
            "sharpness": light_ops.sharpness_facts(
                light_ops.representative_gray_frames(samples),
            ),
            "stability": light_ops.stability_facts(diffs),
            "color": light_ops.color_facts(
                [frame for _, frame in rgb_frames],
            ),
        }

    _safe(facts, "light_metrics", light_block)

    transcript_rows = [dict(row) for row in (transcript_sentences or [])]
    transcript_text = " ".join(
        str(row.get("text") or "") for row in transcript_rows
    ).strip()

    def audio_block() -> dict[str, Any]:
        pcm = decode_pcm_mono(media_path)
        return audio_ops.audio_content_facts(
            pcm,
            PCM_SAMPLE_RATE,
            transcript_text=transcript_text,
        )

    _safe(facts, "audio_content", audio_block)

    def av_sync_block() -> dict[str, Any]:
        if not transcript_rows:
            return {
                "measured": False,
                "note": "无 ASR 转写（未配置或无人声），音画同步跳过",
            }
        if index is None:
            return {"status": "skipped", "skip_reason": "视频索引不可用"}
        return audio_ops.av_sync_facts(
            transcript_rows,
            index.get("cut_points_ms") or [],
        )

    _safe(facts, "av_sync", av_sync_block)

    def consistency_block() -> dict[str, Any]:
        if samples is None or index is None:
            return {"status": "skipped", "skip_reason": "视频索引不可用"}
        return consistency_ops.cross_shot_consistency_facts(
            samples,
            list(index.get("scenes") or []),
        )

    _safe(facts, "cross_shot_consistency", consistency_block)

    def camera_block() -> dict[str, Any]:
        if samples is None or index is None:
            return {"status": "skipped", "skip_reason": "视频索引不可用"}
        return camera_ops.camera_motion_facts(
            samples,
            dynamic_frame_ratio=float(
                index.get("dynamic_frame_ratio") or 0.0,
            ),
        )

    _safe(facts, "camera_motion", camera_block)

    def ocr_block() -> dict[str, Any]:
        expected = [text for text in (expected_texts or []) if text.strip()]
        if not expected:
            return {"measured": False, "note": "计划未声明需渲染的文字"}
        if not ocr_ops.ocr_available():
            return {
                "measured": False,
                "status": "skipped",
                "skip_reason": "easyocr 未安装：文字核验回退纯 VLM 路径",
            }
        if samples is None:
            return {"status": "skipped", "skip_reason": "解码失败"}
        stamped = _rgb_probe_frames(
            media_path,
            samples.timestamps_ms,
            count=6,
        )
        return ocr_ops.text_render_facts(stamped, expected)

    _safe(facts, "text_render", ocr_block, switch="ocr_text")
    return facts


def collect_image_facts(image_path: Path) -> dict[str, Any]:
    """Light objective facts for one still image (sharpness/color)."""
    facts: dict[str, Any] = {}

    def load() -> np.ndarray:
        from PIL import Image  # local import: PIL arrives via matplotlib

        with Image.open(image_path) as handle:
            return np.asarray(handle.convert("RGB"))

    def image_block() -> dict[str, Any]:
        rgb = load()
        gray = rgb.astype(np.float32).mean(axis=2).astype(np.uint8)
        return {
            "sharpness": light_ops.sharpness_facts([gray]),
            "color": light_ops.color_facts([rgb]),
        }

    _safe(facts, "light_metrics", image_block, switch="light_metrics")
    return facts


def render_facts_block(facts: Mapping[str, Any]) -> str:
    """Prompt-ready facts block with the hint framing attached."""
    return (
        "【客观事实提示（objective facts）】\n"
        + _FACTS_PREAMBLE
        + "\n"
        + json.dumps(dict(facts), ensure_ascii=False, default=str)
    )


__all__ = [
    "collect_image_facts",
    "collect_video_facts",
    "render_facts_block",
]
