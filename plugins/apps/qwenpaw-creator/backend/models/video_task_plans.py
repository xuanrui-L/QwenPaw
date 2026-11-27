# -*- coding: utf-8 -*-
"""First-class video task kinds and the continuity planning layer.

Wan3.0 (and the other All-in-One families) expose five peer task types
that the provider routes on ``input.media`` types plus prompt intent:
text-to-video, image-to-video (first / first+last frame), omni
reference generation, video editing and video extension. They are
*peers*: extension and editing are not decorations on top of the
reference stack, they are their own request shapes.

Two contract facts (official docs, M0-verified against the live API on
2026-11-19) shape the planner:

* ``first_frame``/``last_frame`` are mutually exclusive with every
  ``reference_*`` type, so a continuation that needs pixel-level
  anchoring cannot also carry the cast reference stack.
* An extension response CONTAINS the material: submitting an 8s clip
  with ``duration=13`` returns a 13s video whose first 8s are the
  material (output head frame SSIM 0.81 vs the material's first frame).
  Callers must submit ``duration = material + new`` and trim the
  material prefix before publishing, or the segment duplicates its
  predecessor on the timeline.

The planning model selects the request shape and dependencies. Helpers here
validate the declared dependency and describe the prepared provider task.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from dataclasses import dataclass
from enum import StrEnum
from domain.errors import ValidationError

if TYPE_CHECKING:
    from services.project_files.models import Timeline, TimelineElement

# Wan3.0 reference_video quota: at most 5 clips totalling <=15s, and
# input + output share the 30s ceiling. The headroom absorbs provider
# rounding so a plan never submits right at the documented edge.
WAN_30_MAX_REFERENCE_VIDEO_SECONDS = 15.0
WAN_30_MAX_INPUT_PLUS_OUTPUT_SECONDS = 30.0
WAN_30_EXTEND_HEADROOM_SECONDS = 2.0


class VideoTaskKind(StrEnum):
    """The five peer provider task types a submission materializes as.

    Values match the wire ``mode`` strings consumed by
    ``video_model.submit_video_task`` so a plan's kind can be handed
    straight to the provider layer.
    """

    T2V = "t2v"
    I2V = "i2v"
    R2V = "r2v"
    VIDEO_EDIT = "video_edit"
    # Continuation: media carries ONLY the predecessor video as
    # ``reference_video`` plus an extend-intent prompt. No reference
    # stack, no synthetic ids, no last-frame image — the predecessor's
    # own footage is the continuity, and the published artifact is the
    # material prefix trimmed off.
    VIDEO_EXTEND = "video_extend"


@dataclass(frozen=True, slots=True)
class VideoTaskPlan:
    """The routed task kind plus everything the submit path must honour.

    ``predecessor_version_id``/``material_duration_seconds``/
    ``trim_offset_seconds`` are only meaningful for VIDEO_EXTEND (the
    material is the predecessor's whole published video). The material
    duration is used for the provider budget, and the video track's end
    determines the trim offset. ``ratio``
    overrides the project ratio when the kind must follow the material
    (extension) instead of the plan. ``submit_duration_seconds`` is the
    provider duration (extension semantics: material + new); the
    timeline keeps the Element's own duration separately.
    ``auto_first_frame`` marks an I2V whose first frame is adopted from
    the predecessor's last frame rather than authored.
    ``strong_continuity`` marks an R2V whose reference stack the
    executor must lead with the predecessor's last frame (promoted to
    图1, authored [Image N] markers shifted +1) so a same-scene
    continuous take opens on the predecessor's final picture; A/B
    measurement showed the buried-anchor stack (storyboard-led) seams at
    SSIM 0.39-0.53 while a single promoted anchor reaches ~0.79.
    """

    kind: VideoTaskKind
    predecessor_version_id: str = ""
    material_duration_seconds: float = 0.0
    ratio: str = ""
    submit_duration_seconds: int = 0
    trim_offset_seconds: float | None = None
    strong_continuity: bool = False


def planned_predecessor_id(element: TimelineElement) -> str | None:
    creation = element.creation
    if creation.type == "r2v":
        plan = creation.generation_plan
        return plan.predecessor_element_id if plan is not None else None
    if creation.type == "i2v":
        return creation.predecessor_element_id
    return None


def planned_predecessor(
    timeline: Timeline,
    element: TimelineElement,
) -> TimelineElement | None:
    predecessor_id = planned_predecessor_id(element)
    if predecessor_id is None:
        return None
    candidate = timeline.elements_by_id.get(predecessor_id)
    if (
        candidate is None
        or not candidate.enabled
        or candidate.creation.type not in {"r2v", "i2v", "t2v", "s2v"}
        or candidate.span.start_tick >= element.span.start_tick
        or candidate.span.start_tick + candidate.span.duration_tick
        > element.span.start_tick
    ):
        raise ValidationError(
            f"指定的前段 {predecessor_id} 必须是同一轨道内已启用、先于本段结束的视频镜头",
        )
    return candidate


def uses_storyboard(creation: Any) -> bool:
    plan = getattr(creation, "generation_plan", None)
    return plan is None or plan.use_storyboard


__all__ = [
    "VideoTaskKind",
    "VideoTaskPlan",
    "planned_predecessor",
    "planned_predecessor_id",
    "uses_storyboard",
]
