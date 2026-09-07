# -*- coding: utf-8 -*-
"""Only a current baseline can absorb redundant own-storyboard references."""

from __future__ import annotations

from copy import deepcopy

import pytest

from services.project_files.prompt_sync import (
    derive_prompt_sync_changes,
    prompt_sync_status,
    sync_stamp,
)

pytestmark = pytest.mark.unit
TID, EID = "timeline:main", "shot:one"


def _documents():
    creation = {
        "type": "r2v",
        "shots": {
            "items": {"shot": {"description": "只抬左手一次", "dialogue": ""}},
            "order": ["shot"],
        },
        "storyboard_prompt": "[Image 1] 人物、[Image 2] 场景，九格动作分镜。",
        "video_prompt": "[Image 1] 动作分镜、[Image 2] 人物、[Image 3] 场景。",
        "video_reference_version_ids": ["old-board", "woman", "scene"],
    }
    before = {
        "project_id": "sync-storyboard-bookkeeping",
        "created_at": "2026-09-06T00:00:00Z",
        "settings": {"aspect_ratio": "9:16", "resolution": "720P"},
        "timelines": {
            "items": {
                TID: {
                    "ticks_per_second": 1000,
                    "elements_by_id": {
                        EID: {
                            "creation": creation,
                            "span": {"duration_tick": 6000},
                            "outputs": {
                                "storyboard": {"slot_id": "custom-own-slot"},
                            },
                        },
                    },
                },
            },
        },
        "assets": {
            "artifact_slots_by_id": {
                "custom-own-slot": {
                    "owner_ref": f"element:{EID}",
                    "kind": "r2v_storyboard_image",
                    "selected_version_id": "new-board",
                    "version_ids": ["old-board", "new-board"],
                },
            },
            "artifact_versions_by_id": {
                "old-board": {"slot_id": "custom-own-slot"},
                "new-board": {"slot_id": "custom-own-slot"},
                "other-board": {"slot_id": "another-element-slot"},
            },
        },
    }
    creation["prompt_sync"] = sync_stamp(before, TID, EID)
    after = deepcopy(before)
    _creation(after)["video_reference_version_ids"][0] = "new-board"
    return before, after


def _creation(document):
    return document["timelines"]["items"][TID]["elements_by_id"][EID][
        "creation"
    ]


@pytest.mark.parametrize(
    "refs",
    [
        ["new-board", "woman", "scene"],
        ["woman", "scene"],
        ["old-board", "woman", "new-board", "scene", "old-board"],
    ],
)
def test_current_baseline_only_redundant_own_refs_remains_current(refs):
    before, after = _documents()
    _creation(after)["video_reference_version_ids"] = refs
    unchanged_before = deepcopy(before)
    derive_prompt_sync_changes(before, after)
    assert prompt_sync_status(after, TID, EID)["status"] == "current"
    assert _creation(after)["prompt_sync"] == sync_stamp(after, TID, EID)
    assert before == unchanged_before


@pytest.mark.parametrize(
    "change",
    [
        "external_order",
        "external_duplicate",
        "external_removed",
        "external_added",
        "other_storyboard",
        "shot_description",
        "shot_dialogue",
        "storyboard_prompt",
        "video_prompt",
        "duration",
        "aspect_ratio",
        "selected_storyboard",
        "automatic_fallback",
    ],
)
def test_creative_or_effective_input_changes_are_not_reconfirmed(change):
    before, after = _documents()
    c = _creation(after)
    refs = c["video_reference_version_ids"]
    if change == "external_order":
        refs[1:] = ["scene", "woman"]
    elif change == "external_duplicate":
        refs.append("woman")
    elif change == "external_removed":
        refs.pop()
    elif change == "external_added":
        refs.append("key")
    elif change == "other_storyboard":
        refs.append("other-board")
    elif change == "shot_description":
        c["shots"]["items"]["shot"]["description"] = "抬右手"
    elif change == "shot_dialogue":
        c["shots"]["items"]["shot"]["dialogue"] = "一句新对白"
    elif change in {"storyboard_prompt", "video_prompt"}:
        c[change] += "改变动作"
    elif change == "duration":
        after["timelines"]["items"][TID]["elements_by_id"][EID]["span"][
            "duration_tick"
        ] = 8000
    elif change == "aspect_ratio":
        after["settings"]["aspect_ratio"] = "16:9"
    elif change == "selected_storyboard":
        after["assets"]["artifact_slots_by_id"]["custom-own-slot"][
            "selected_version_id"
        ] = "old-board"
    elif change == "automatic_fallback":
        refs.clear()
    derive_prompt_sync_changes(before, after)
    assert _creation(after)["prompt_sync"] == _creation(before)["prompt_sync"]
    assert prompt_sync_status(after, TID, EID)["status"] != "current"


@pytest.mark.parametrize(
    "previous",
    ["legacy", "needs_update", "needs_confirmation"],
)
def test_stale_or_untracked_baseline_cannot_be_washed_current(previous):
    before, after = _documents()
    if previous == "legacy":
        _creation(before)["prompt_sync"] = None
    elif previous == "needs_update":
        _creation(before)["shots"]["items"]["shot"]["description"] = "未审阅动作"
    else:
        _creation(before)["video_prompt"] += "未审阅提示词"
    # Give the candidate the same creative inputs as before; only own refs
    # change.
    after = deepcopy(before)
    _creation(after)["video_reference_version_ids"][0] = "new-board"
    _creation(after)["prompt_sync"] = sync_stamp(
        after,
        TID,
        EID,
    )  # Untrusted writer cannot forge it.
    derive_prompt_sync_changes(before, after)
    assert prompt_sync_status(after, TID, EID)["status"] != "current"


def test_unchanged_legacy_hash_formula_and_no_op_are_preserved():
    before, _ = _documents()
    after = deepcopy(before)
    stamp = deepcopy(_creation(before)["prompt_sync"])
    derive_prompt_sync_changes(before, after)
    assert _creation(after)["prompt_sync"] == stamp
    assert prompt_sync_status(after, TID, EID)["status"] == "current"
