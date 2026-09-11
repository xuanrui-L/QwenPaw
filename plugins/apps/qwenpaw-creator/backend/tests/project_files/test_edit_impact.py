# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import copy
import pytest


from services.project_files.edit_impact import (
    apply_frontend_edit_impacts,
    summarize_committed_edit_impact,
)


def _project() -> dict:
    return {
        "timelines": {
            "items": {
                "timeline:main": {
                    "timeline_id": "timeline:main",
                    "elements_by_id": {
                        "r2v-1": {
                            "element_id": "r2v-1",
                            "label": "R2V",
                            "span": {
                                "start_tick": 0,
                                "duration_tick": 4_000,
                            },
                            "creation": {
                                "type": "r2v",
                                "video_prompt": "old",
                                "shots": {"items": {}, "order": []},
                            },
                            "outputs": {
                                "storyboard": {
                                    "slot_id": "element:r2v-1:storyboard",
                                },
                                "video": {
                                    "slot_id": "element:r2v-1:video",
                                },
                            },
                        },
                        "overlay-1": {
                            "element_id": "overlay-1",
                            "label": "OS",
                            "span": {
                                "start_tick": 0,
                                "duration_tick": 2_000,
                            },
                            "creation": {
                                "type": "overlay",
                                "text": "old copy",
                                "prompt": "",
                                "reference_version_ids": [],
                                "motion": {
                                    "format": "html_css",
                                    "html": (
                                        "<html><head><style>"
                                        ".copy{animation:pop 1s}"
                                        "</style></head><body>"
                                        "<div class='copy'>old<br>copy</div>"
                                        "</body></html>"
                                    ),
                                },
                            },
                            "outputs": {},
                        },
                    },
                },
            },
        },
        "assets": {
            "artifact_slots_by_id": {
                "element:r2v-1:storyboard": {
                    "slot_id": "element:r2v-1:storyboard",
                    "owner_ref": "element:r2v-1",
                    "selected_version_id": "storyboard-v1",
                },
                "element:r2v-1:video": {
                    "slot_id": "element:r2v-1:video",
                    "owner_ref": "element:r2v-1",
                    "selected_version_id": "video-v1",
                },
                "timeline:timeline:main:render": {
                    "slot_id": "timeline:timeline:main:render",
                    "kind": "final_video",
                    "owner_ref": "timeline:timeline:main",
                    "selected_version_id": "final-v1",
                },
            },
            "artifact_versions_by_id": {
                "storyboard-v1": {
                    "version_id": "storyboard-v1",
                    "owner_ref": "element:r2v-1",
                    "stale": False,
                    "stale_reason": None,
                },
                "video-v1": {
                    "version_id": "video-v1",
                    "owner_ref": "element:r2v-1",
                    "stale": False,
                    "stale_reason": None,
                },
                "final-v1": {
                    "version_id": "final-v1",
                    "owner_ref": "timeline:timeline:main",
                    "stale": False,
                    "stale_reason": None,
                },
            },
        },
    }


def _element_pointer(element_id: str, *suffix: str) -> str:
    return "/".join(
        (
            "",
            "timelines",
            "items",
            "timeline:main",
            "elements_by_id",
            element_id,
            *suffix,
        ),
    )


@pytest.mark.parametrize(
    "pointer",
    [
        _element_pointer("overlay-1", "creation", "text"),
        _element_pointer("r2v-1", "creation", "video_prompt"),
        "/assets/artifact_slots_by_id/element:r2v-1:video/selected_version_id",
        "/timelines/items/timeline:main/color_grade",
    ],
)
def test_production_edits_do_not_invalidate_upstream_script(pointer):
    project = _project()
    project["assets"]["artifact_slots_by_id"]["script:timeline:main"] = {
        "kind": "timeline_script",
        "owner_ref": "timeline:timeline:main",
        "selected_version_id": "script-v1",
    }
    project["assets"]["artifact_versions_by_id"]["script-v1"] = {
        "stale": False,
        "stale_reason": None,
    }
    updated, impact = apply_frontend_edit_impacts(project, [pointer])
    assert (
        updated["assets"]["artifact_versions_by_id"]["script-v1"]["stale"]
        is False
    )
    assert (
        updated["assets"]["artifact_versions_by_id"]["final-v1"]["stale"]
        is True
    )
    assert "script-v1" not in impact.invalidated_artifact_version_ids


def test_synopsis_invalidates_script_but_adding_frozen_history_does_not():
    base = _project()
    base["timelines"]["order"] = ["timeline:main"]
    base["assets"]["artifact_slots_by_id"]["script:timeline:main"] = {
        "kind": "timeline_script",
        "owner_ref": "timeline:timeline:main",
        "selected_version_id": "script-v1",
    }
    base["assets"]["artifact_versions_by_id"]["script-v1"] = {"stale": False}
    candidate = copy.deepcopy(base)
    candidate["timelines"]["items"][
        "snapshot:timeline:main:1"
    ] = copy.deepcopy(
        candidate["timelines"]["items"]["timeline:main"],
    )
    candidate["timelines"]["order"].append("snapshot:timeline:main:1")
    updated, _ = apply_frontend_edit_impacts(
        candidate,
        ["/timelines/order"],
        base=base,
    )
    assert (
        updated["assets"]["artifact_versions_by_id"]["script-v1"]["stale"]
        is False
    )
    candidate["timelines"]["items"]["timeline:main"]["synopsis"] = "New story"
    updated, _ = apply_frontend_edit_impacts(
        candidate,
        ["/timelines/items/timeline:main/synopsis"],
        base=base,
    )
    assert (
        updated["assets"]["artifact_versions_by_id"]["script-v1"]["stale"]
        is True
    )


def test_overlay_copy_edit_invalidates_only_timeline_render() -> None:
    base = _project()
    candidate = _project()
    candidate["timelines"]["items"]["timeline:main"]["elements_by_id"][
        "overlay-1"
    ]["creation"]["text"] = "brand new copy"
    project, impact = apply_frontend_edit_impacts(
        candidate,
        [_element_pointer("overlay-1", "creation", "text")],
        base=base,
    )

    versions = project["assets"]["artifact_versions_by_id"]
    assert versions["final-v1"]["stale"] is True
    assert versions["video-v1"]["stale"] is False
    creation = project["timelines"]["items"]["timeline:main"][
        "elements_by_id"
    ]["overlay-1"]["creation"]
    assert creation["motion"] is not None
    html = creation["motion"]["html"]
    assert ".copy{animation:pop 1s}" in html
    assert "old" not in html
    assert "brand new copy" in re.sub(r"<[^>]+>", "", html)
    assert impact.regeneration_required is False
    assert impact.render_timeline_ids == {"timeline:main"}
    assert versions["final-v1"]["metadata"]["pendingAffectedElementIds"] == [
        "overlay-1",
    ]


@pytest.mark.parametrize("mode", ["r2v", "t2v", "i2v"])
@pytest.mark.parametrize("field", ["video_prompt", "generate_audio"])
def test_video_input_invalidates_video_and_final_but_not_storyboard(
    mode,
    field,
) -> None:
    document = _project()
    document["timelines"]["items"]["timeline:main"]["elements_by_id"]["r2v-1"][
        "creation"
    ]["type"] = mode
    project, impact = apply_frontend_edit_impacts(
        document,
        [_element_pointer("r2v-1", "creation", field)],
    )

    versions = project["assets"]["artifact_versions_by_id"]
    assert versions["storyboard-v1"]["stale"] is False
    assert versions["video-v1"]["stale"] is True
    assert versions["final-v1"]["stale"] is True
    assert impact.regeneration_required is True
    assert impact.invalidated_artifact_version_ids == {"video-v1", "final-v1"}


def test_committed_impact_can_be_reconstructed_for_idempotent_replay() -> None:
    project, _ = apply_frontend_edit_impacts(
        _project(),
        [_element_pointer("r2v-1", "creation", "video_prompt")],
    )
    impact = summarize_committed_edit_impact(
        project,
        [
            _element_pointer("r2v-1", "creation", "video_prompt"),
            "/assets/artifact_versions_by_id/video-v1/stale",
            "/assets/artifact_versions_by_id/final-v1/stale",
        ],
    )

    assert impact.affected_element_ids == {"r2v-1"}
    assert impact.render_timeline_ids == {"timeline:main"}
    assert impact.invalidated_artifact_version_ids == {"video-v1", "final-v1"}
    assert impact.regeneration_required is True
