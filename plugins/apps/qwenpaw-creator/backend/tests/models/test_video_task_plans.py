# -*- coding: utf-8 -*-
"""Routing tests for the first-class video task planner.

Pure facts in, one VideoTaskPlan out. Pins the author-intent routing
matrix: an explicit Transition (cut OR blend), cut-flavoured continuity
text, a scene change or newly introduced elements keep the segment on
plain R2V; a same-scene continuous take (no Transition, continue/neutral
intent) routes to R2V with the promoted last-frame anchor
(``strong_continuity``); only explicit one-take text upgrades that to
VIDEO_EXTEND when the model and budgets allow.
"""

# pylint: disable=use-implicit-booleaness-not-comparison

from models.video_task_plans import (
    ContinuityFacts,
    VideoTaskKind,
    select_video_task_kind,
)


def _facts(**overrides) -> ContinuityFacts:
    base = {
        "creation_type": "r2v",
        "has_published_predecessor": True,
        "predecessor_version_id": "artifact-version-prev",
        "predecessor_duration_seconds": 8.0,
        "same_scene": True,
        "cross_episode": False,
        "new_elements": False,
        "explicit_first_frame": False,
        "segment_duration_seconds": 10,
        "ffmpeg_ready": True,
        "video_extension_supported": True,
        "transition_kind": "none",
        "continuity_intent": "neutral",
        "one_take": False,
    }
    base.update(overrides)
    return ContinuityFacts(**base)


def test_non_r2v_base_modes_pass_through() -> None:
    for base in ("t2v", "video_edit"):
        plan = select_video_task_kind(base, _facts())
        assert plan.kind is VideoTaskKind(base)
    i2v = select_video_task_kind("i2v", _facts())
    assert i2v.kind is VideoTaskKind.I2V
    assert i2v.auto_first_frame is True  # no explicit frame authored
    explicit = select_video_task_kind("i2v", _facts(explicit_first_frame=True))
    assert explicit.auto_first_frame is False


def test_new_episode_and_missing_predecessor_use_r2v() -> None:
    assert (
        select_video_task_kind(
            "r2v",
            _facts(cross_episode=True, has_published_predecessor=False),
        ).kind
        is VideoTaskKind.R2V
    )
    assert (
        select_video_task_kind(
            "r2v",
            _facts(has_published_predecessor=False),
        ).kind
        is VideoTaskKind.R2V
    )


def test_scene_change_uses_pure_r2v() -> None:
    plan = select_video_task_kind("r2v", _facts(same_scene=False))
    assert plan.kind is VideoTaskKind.R2V
    assert plan.strong_continuity is False


def test_new_elements_force_r2v_reference_stack() -> None:
    plan = select_video_task_kind("r2v", _facts(new_elements=True))
    assert plan.kind is VideoTaskKind.R2V
    assert plan.strong_continuity is False


def test_explicit_transition_pins_plain_r2v() -> None:
    # A cut is the author asking for the seam; a blend hides it by
    # construction. Neither may promote the anchor.
    for kind in ("cut", "blend"):
        plan = select_video_task_kind(
            "r2v",
            _facts(transition_kind=kind, continuity_intent="continue"),
        )
        assert plan.kind is VideoTaskKind.R2V
        assert plan.strong_continuity is False


def test_cut_flavoured_continuity_pins_plain_r2v() -> None:
    plan = select_video_task_kind(
        "r2v",
        _facts(continuity_intent="cut", one_take=True),
    )
    assert plan.kind is VideoTaskKind.R2V
    assert plan.strong_continuity is False


def test_same_scene_continuous_take_routes_to_strong_r2v() -> None:
    plan = select_video_task_kind(
        "r2v",
        _facts(continuity_intent="continue"),
    )
    assert plan.kind is VideoTaskKind.R2V
    assert plan.strong_continuity is True
    # Neutral (missing intent) takes the conservative plain route.
    neutral = select_video_task_kind(
        "r2v",
        _facts(continuity_intent="neutral"),
    )
    assert neutral.kind is VideoTaskKind.R2V
    assert neutral.strong_continuity is False
    assert plan.submit_duration_seconds == 10


def test_strong_r2v_needs_ffmpeg_for_the_anchor() -> None:
    plan = select_video_task_kind("r2v", _facts(ffmpeg_ready=False))
    assert plan.kind is VideoTaskKind.R2V
    assert plan.strong_continuity is False


def test_one_take_routes_to_independent_extend() -> None:
    plan = select_video_task_kind(
        "r2v",
        _facts(one_take=True, continuity_intent="continue"),
    )
    assert plan.kind is VideoTaskKind.VIDEO_EXTEND
    # The material is the predecessor's REAL published artifact.
    assert plan.predecessor_version_id == "artifact-version-prev"
    assert plan.material_duration_seconds == 8.0
    assert plan.ratio == "adaptive"
    # duration semantics: material + new (M0 measured).
    assert plan.submit_duration_seconds == 18
    assert plan.trim_offset_seconds == 8.0


def test_one_take_without_extension_capability_falls_back_to_strong_r2v() -> (
    None
):
    plan = select_video_task_kind(
        "r2v",
        _facts(
            one_take=True,
            continuity_intent="one_take",
            video_extension_supported=False,
        ),
    )
    assert plan.kind is VideoTaskKind.R2V
    assert plan.strong_continuity is True


def test_one_take_over_budget_falls_back_to_strong_r2v() -> None:
    long_prev = _facts(
        one_take=True,
        continuity_intent="one_take",
        predecessor_duration_seconds=20.0,
    )
    assert select_video_task_kind("r2v", long_prev).strong_continuity is True
    no_ffmpeg = _facts(
        one_take=True,
        continuity_intent="one_take",
        predecessor_duration_seconds=20.0,
        ffmpeg_ready=False,
    )
    plan = select_video_task_kind("r2v", no_ffmpeg)
    assert plan.kind is VideoTaskKind.R2V
    assert plan.strong_continuity is False


def test_extend_budget_boundary_is_inclusive() -> None:
    # 15s material + 13s segment = 28 = 30 - headroom: still extend.
    plan = select_video_task_kind(
        "r2v",
        _facts(
            one_take=True,
            continuity_intent="one_take",
            predecessor_duration_seconds=15.0,
            segment_duration_seconds=13,
        ),
    )
    assert plan.kind is VideoTaskKind.VIDEO_EXTEND
    # one second more and the total passes the budget: strong r2v.
    plan = select_video_task_kind(
        "r2v",
        _facts(
            one_take=True,
            continuity_intent="one_take",
            predecessor_duration_seconds=15.0,
            segment_duration_seconds=14,
        ),
    )
    assert plan.kind is VideoTaskKind.R2V
    assert plan.strong_continuity is True
