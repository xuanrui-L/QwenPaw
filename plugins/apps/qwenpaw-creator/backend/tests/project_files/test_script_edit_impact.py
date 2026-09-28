# -*- coding: utf-8 -*-
"""Script invalidation follows the inputs of the affected narrative node."""

from __future__ import annotations

import copy

import pytest

from services.project_files.edit_impact import apply_frontend_edit_impacts

NODES = ("prologue", "ending-a", "ending-b", "hidden")


def _project():
    return {
        "name": "Branching story",
        "settings": {"target_duration_seconds": 60},
        "timelines": {
            "order": list(NODES),
            "items": {
                node: {
                    "title": node,
                    "synopsis": f"Synopsis of {node}",
                    "description": f"Saved script of {node}",
                    "planned_duration_seconds": 30,
                }
                for node in NODES
            },
        },
        "narrative_edges": [
            {
                "edge_id": "start",
                "source_timeline_id": "prologue",
                "target_timeline_id": "ending-a",
                "label": "Start",
                "prompt": "Choose a route",
                "tone": "safe",
            },
            {
                "edge_id": "secret",
                "source_timeline_id": "ending-b",
                "target_timeline_id": "hidden",
                "label": "Search again",
                "prompt": "Look inside the box",
                "tone": "risky",
            },
        ],
        "assets": {
            "artifact_slots_by_id": {
                f"script:{node}": {
                    "kind": "timeline_script",
                    "owner_ref": f"timeline:{node}",
                    "selected_version_id": f"script-v1:{node}",
                }
                for node in NODES
            },
            "artifact_versions_by_id": {
                f"script-v1:{node}": {"stale": False} for node in NODES
            },
        },
    }


def _invalidated(base, candidate, pointers):
    updated, impact = apply_frontend_edit_impacts(
        candidate,
        pointers,
        base=base,
    )
    expected_ids = {
        version_id
        for version_id, version in updated["assets"][
            "artifact_versions_by_id"
        ].items()
        if version["stale"]
    }
    assert impact.invalidated_artifact_version_ids == expected_ids
    assert candidate["assets"] == base["assets"]  # No in-place mutation.
    return {key.removeprefix("script-v1:") for key in expected_ids}


def test_rewiring_later_endings_does_not_expire_unchanged_prologue():
    # The production incident: initial Agent creation revises a remote ending
    # and replaces its outgoing branch while the prologue already has a draft.
    base = _project()
    candidate = copy.deepcopy(base)
    candidate["narrative_edges"][1] = {
        "edge_id": "take-evidence",
        "source_timeline_id": "ending-a",
        "target_timeline_id": "ending-b",
        "label": "Take the evidence",
        "prompt": "Keep the evidence for yourself",
    }
    candidate["timelines"]["items"]["ending-b"]["description"] += " Revised"

    assert _invalidated(
        base,
        candidate,
        ["/narrative_edges", "/timelines/items/ending-b/description"],
    ) == {"ending-a", "ending-b", "hidden"}


@pytest.mark.parametrize("field", ["label", "prompt"])
def test_choice_text_changes_invalidate_only_incident_scripts(field):
    base = _project()
    candidate = copy.deepcopy(base)
    candidate["narrative_edges"][1][field] = "A different choice"
    assert _invalidated(
        base,
        candidate,
        [f"/narrative_edges/1/{field}"],
    ) == {"ending-b", "hidden"}


def test_retargeting_edge_invalidates_both_old_and_new_endpoints():
    base = _project()
    candidate = copy.deepcopy(base)
    candidate["narrative_edges"][1]["target_timeline_id"] = "ending-a"
    assert _invalidated(base, candidate, ["/narrative_edges"]) == {
        "ending-a",
        "ending-b",
        "hidden",
    }


def test_choice_visual_tone_does_not_invalidate_any_script():
    base = _project()
    candidate = copy.deepcopy(base)
    candidate["narrative_edges"][0]["tone"] = "danger"
    assert not _invalidated(base, candidate, ["/narrative_edges/0/tone"])


@pytest.mark.parametrize("field", ["description", "planned_duration_seconds"])
def test_local_script_inputs_only_invalidate_their_owner(field):
    base = _project()
    candidate = copy.deepcopy(base)
    candidate["timelines"]["items"]["ending-b"][field] = (
        "Revised body" if field == "description" else 90
    )
    assert _invalidated(
        base,
        candidate,
        [f"/timelines/items/ending-b/{field}"],
    ) == {"ending-b"}


def test_duration_default_only_affects_scripts_without_an_override():
    base = _project()
    base["timelines"]["items"]["ending-b"]["planned_duration_seconds"] = None
    candidate = copy.deepcopy(base)
    candidate["settings"]["target_duration_seconds"] = 90
    assert _invalidated(
        base,
        candidate,
        ["/settings/target_duration_seconds"],
    ) == {"ending-b"}


@pytest.mark.parametrize("field", ["title", "synopsis"])
def test_shared_narrative_outline_changes_still_invalidate_scripts(field):
    # Every draft explicitly consumes the live outline for continuity.
    base = _project()
    candidate = copy.deepcopy(base)
    candidate["timelines"]["items"]["ending-b"][field] = "New story premise"
    assert _invalidated(
        base,
        candidate,
        [f"/timelines/items/ending-b/{field}"],
    ) == set(NODES)


def test_project_creative_brief_still_invalidates_scripts():
    base = _project()
    candidate = copy.deepcopy(base)
    candidate["strategy"] = {"creative_brief": "Change the story genre"}
    assert _invalidated(base, candidate, ["/strategy"]) == set(NODES)


def test_noop_and_frozen_history_do_not_invalidate_scripts():
    base = _project()
    candidate = copy.deepcopy(base)
    candidate["timelines"]["items"]["snapshot:prologue:1"] = copy.deepcopy(
        candidate["timelines"]["items"]["prologue"],
    )
    candidate["timelines"]["order"].append("snapshot:prologue:1")
    assert not _invalidated(
        base,
        candidate,
        ["/timelines", "/narrative_edges"],
    )
