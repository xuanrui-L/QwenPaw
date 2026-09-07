# -*- coding: utf-8 -*-
"""A complete Shot description can explicitly direct intentional silence."""

import pytest

from services.file_agent_runtime.work_graph import (
    _element_dialogue_density_gap,
    _video_prompt_dialogue_gaps,
)
from services.project_files.models import R2VCreation, Shot

pytestmark = pytest.mark.unit


def _creation(rows, **kwargs):
    shots = [
        Shot(
            shot_id=f"shot-{index}",
            description=description,
            dialogue=dialogue,
            duration_seconds=3,
            camera="⊙ 静止",
            framing="中景",
        )
        for index, (description, dialogue) in enumerate(rows)
    ]
    return R2VCreation(
        character_refs=["char:woman"],
        shots={
            "order": [shot.shot_id for shot in shots],
            "items": {shot.shot_id: shot for shot in shots},
        },
        **kwargs,
    )


def test_complete_description_silence_needs_no_extra_dialogue_ui():
    creation = _creation(
        [("女子停在门口。有意静默，无对白、无旁白；仅保留钥匙轻响。", "")],
    )
    assert _element_dialogue_density_gap(creation, "short_drama") is None


def test_one_silent_shot_does_not_exempt_other_shots():
    creation = _creation(
        [("女子等待，有意静默。", ""), ("女子回头，继续寻找。", "")],
    )
    gap = _element_dialogue_density_gap(creation, "short_drama")
    assert gap is not None and "0/1" in gap


def test_deliberate_silence_is_excluded_from_the_density_denominator():
    creation = _creation(
        [
            ("女子等待，有意静默。", ""),
            ("女子轻声说：原来在这儿。", "原来在这儿。"),
        ],
        min_dialogue_ratio=1,
    )
    assert _element_dialogue_density_gap(creation, "short_drama") is None


@pytest.mark.parametrize(
    "description",
    [
        "无对白，但有画外旁白。",
        "只描写钥匙音效。",
        "女子没有找到钥匙。",
    ],
)
def test_no_generic_silence_or_audio_role_inference(description):
    creation = _creation([(description, "")])
    assert _element_dialogue_density_gap(creation, "short_drama") is not None


def test_silence_note_does_not_bypass_existing_verbatim_speech_contract():
    creation = _creation(
        [("女子等待，有意静默。", "旧台词仍在。")],
        video_prompt="女子静静等待。",
    )
    assert _video_prompt_dialogue_gaps(creation)


def test_existing_element_silence_and_explicit_zero_ratio_still_work():
    creation = _creation([("女子停在门口。", "")], narrative="整段有意静默")
    assert _element_dialogue_density_gap(creation, "short_drama") is None
    creation.narrative = ""
    creation.min_dialogue_ratio = 0
    assert _element_dialogue_density_gap(creation, "short_drama") is None


def test_other_scenarios_keep_the_existing_exemption():
    creation = _creation([("女子停在门口。", "")])
    assert _element_dialogue_density_gap(creation, "general") is None
