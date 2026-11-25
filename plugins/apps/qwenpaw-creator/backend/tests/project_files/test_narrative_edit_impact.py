# -*- coding: utf-8 -*-
"""Effects of editing authored scripts and interactive branch inputs."""

from __future__ import annotations

import copy

import pytest

from services.project_files.edit_impact import apply_frontend_edit_impacts

from .test_edit_impact import _element_pointer, _project

import asyncio
import hashlib
from services.file_agent_runtime.work_graph import (
    WorkNodeStatus,
    derive_work_graph,
)
from services.file_agent_runtime import work_scheduler
from services.media_files import script_execution
from services.media_files.script_execution import (
    _build_script_prompt,
    execute_file_script_command,
)
from services.project_files.agent_tools import (
    AgentProjectToolContext,
    AgentProjectTools,
)
from services.project_files.models import NarrativeEdge, Timeline
from services.runtime_files.models import ChangeOrigin, ReviewPolicy
from media_files.test_script_execution import (
    DRAFT,
    PROJECT_ID,
    _draft,
    _mock_chat,
    _services,
)


def _script_project() -> dict:
    project = _project()
    project["timelines"]["order"] = [
        "timeline:main",
        "timeline:next",
        "timeline:end",
    ]
    for timeline_id in project["timelines"]["order"]:
        timeline = project["timelines"]["items"].setdefault(
            timeline_id,
            {"timeline_id": timeline_id, "elements_by_id": {}},
        )
        timeline.update(
            title=timeline_id,
            synopsis=f"Story for {timeline_id}",
            planned_duration_seconds=300,
        )
        project["assets"]["artifact_slots_by_id"][f"script:{timeline_id}"] = {
            "kind": "timeline_script",
            "owner_ref": f"timeline:{timeline_id}",
            "selected_version_id": f"script:{timeline_id}:v1",
        }
        project["assets"]["artifact_versions_by_id"][
            f"script:{timeline_id}:v1"
        ] = {"stale": False, "stale_reason": None}
    return project


@pytest.mark.parametrize(
    ("path", "value", "invalidates"),
    [
        (("strategy", "constraints"), "所有视频不要生成字幕", False),
        (
            (
                "timelines",
                "items",
                "timeline:main",
                "planned_duration_seconds",
            ),
            142,
            False,
        ),
        (("timelines", "items", "timeline:main", "synopsis"), "新的梗概", False),
        (
            ("timelines", "items", "timeline:main", "description"),
            "明确修改后的正文",
            True,
        ),
        (
            ("timelines", "items", "timeline:next", "description"),
            "其他集正文",
            False,
        ),
    ],
)
def test_authored_script_mirror_only_tracks_its_source_body(
    path,
    value,
    invalidates,
):
    base = _script_project()
    base["timelines"]["items"]["timeline:main"]["description"] = "已经确认的正文"
    version_id = "script:timeline:main:v1"
    base["assets"]["artifact_versions_by_id"][version_id]["metadata"] = {
        "scriptSource": "timeline",
    }
    candidate = copy.deepcopy(base)
    node = candidate
    for token in path[:-1]:
        node = node.setdefault(token, {})
    node[path[-1]] = value
    updated, impact = apply_frontend_edit_impacts(
        candidate,
        ["/" + "/".join(path)],
        base=base,
    )
    assert (
        updated["assets"]["artifact_versions_by_id"][version_id]["stale"]
        is invalidates
    )
    assert (
        version_id in impact.invalidated_artifact_version_ids
    ) is invalidates


@pytest.mark.parametrize(
    ("pointer", "value"),
    [
        (_element_pointer("overlay-1", "creation", "text"), "new copy"),
        (_element_pointer("r2v-1", "creation", "video_prompt"), "new shot"),
        (_element_pointer("r2v-1", "span", "start_tick"), 1_000),
        (_element_pointer("r2v-1", "span", "duration_tick"), 5_000),
        (
            (
                "/assets/artifact_slots_by_id/element:r2v-1:video/"
                "selected_version_id"
            ),
            None,
        ),
        ("/timelines/items/timeline:main/color_grade", {"exposure": 0.2}),
    ],
)
def test_production_edits_do_not_invalidate_upstream_script(pointer, value):
    base = _script_project()
    candidate = copy.deepcopy(base)
    tokens = pointer.lstrip("/").split("/")
    node = candidate
    for token in tokens[:-1]:
        node = node[token]
    node[tokens[-1]] = value
    assert candidate != base

    updated, impact = apply_frontend_edit_impacts(
        candidate,
        [pointer],
        base=base,
    )
    versions = updated["assets"]["artifact_versions_by_id"]
    for timeline_id in base["timelines"]["order"]:
        version_id = f"script:{timeline_id}:v1"
        assert versions[version_id]["stale"] is False
        assert version_id not in impact.invalidated_artifact_version_ids
    assert versions["final-v1"]["stale"] is True


@pytest.mark.parametrize("timeline_id", ["timeline:main", "timeline:next"])
def test_duration_change_invalidates_only_own_script(timeline_id):
    base = _script_project()
    candidate = copy.deepcopy(base)
    candidate["timelines"]["items"][timeline_id][
        "planned_duration_seconds"
    ] = 142
    element = copy.deepcopy(
        base["timelines"]["items"]["timeline:main"]["elements_by_id"]["r2v-1"],
    )
    element["element_id"] = "r2v-2"
    candidate["timelines"]["items"][timeline_id]["elements_by_id"][
        "r2v-2"
    ] = element
    updated, impact = apply_frontend_edit_impacts(
        candidate,
        [
            f"/timelines/items/{timeline_id}/planned_duration_seconds",
            f"/timelines/items/{timeline_id}/elements_by_id/r2v-2",
        ],
        base=base,
    )
    versions = updated["assets"]["artifact_versions_by_id"]
    for other_id in base["timelines"]["order"]:
        assert versions[f"script:{other_id}:v1"]["stale"] is (
            other_id == timeline_id
        )
    assert (
        f"script:{timeline_id}:v1" in impact.invalidated_artifact_version_ids
    )
    assert (
        base["assets"]["artifact_versions_by_id"][f"script:{timeline_id}:v1"][
            "stale"
        ]
        is False
    )


@pytest.mark.parametrize("timeline_id", ["timeline:main", "timeline:next"])
@pytest.mark.parametrize("field", ["title", "synopsis"])
def test_live_narrative_changes_invalidate_own_and_sibling_scripts(
    timeline_id,
    field,
):
    base = _script_project()
    candidate = copy.deepcopy(base)
    candidate["timelines"]["items"][timeline_id][field] = "New story"
    updated, impact = apply_frontend_edit_impacts(
        candidate,
        [f"/timelines/items/{timeline_id}/{field}"],
        base=base,
    )
    assert impact.invalidated_artifact_version_ids == {
        f"script:{tid}:v1" for tid in base["timelines"]["order"]
    }
    assert (
        updated["assets"]["artifact_versions_by_id"]["final-v1"]["stale"]
        is False
    )


@pytest.mark.parametrize(
    ("path", "value", "invalidates"),
    [
        (("name",), "New project", True),
        (("description",), "New description", True),
        (("scenario",), "video_edit", True),
        (("strategy", "creative_brief"), "New brief", True),
        (("strategy", "audience"), "New audience", True),
        (("strategy", "creative_direction"), "New direction", True),
        (("strategy", "constraints"), "The protagonist must survive", True),
        # Every timeline overrides the project default in this fixture.
        (("settings", "target_duration_seconds"), 600, False),
        (
            ("timelines", "order"),
            ["timeline:end", "timeline:next", "timeline:main"],
            True,
        ),
    ],
)
def test_shared_story_inputs_invalidate_only_consuming_scripts(
    path,
    value,
    invalidates,
):
    base = _script_project()
    candidate = copy.deepcopy(base)
    node = candidate
    for token in path[:-1]:
        node = node.setdefault(token, {})
    node[path[-1]] = value
    _, impact = apply_frontend_edit_impacts(
        candidate,
        ["/" + "/".join(path)],
        base=base,
    )
    expected = (
        {f"script:{tid}:v1" for tid in base["timelines"]["order"]}
        if invalidates
        else set()
    )
    assert impact.invalidated_artifact_version_ids == expected


@pytest.mark.parametrize(
    ("edge_index", "field", "value", "affected"),
    [
        (0, "edge_id", "new-edge", {"main", "next"}),
        (0, "label", "Stay", {"main", "next"}),
        (0, "prompt", "Which path?", {"main", "next"}),
        (0, "source_timeline_id", "timeline:end", {"main", "next", "end"}),
        (0, "target_timeline_id", "timeline:end", {"main", "next", "end"}),
        (1, "label", "Leave", {"next", "end"}),
        (0, "tone", "danger", set()),
    ],
)
def test_only_consumed_incident_edge_fields_invalidate_scripts(
    edge_index,
    field,
    value,
    affected,
):
    base = _script_project()
    base["narrative_edges"] = [
        {
            "edge_id": "main-next",
            "source_timeline_id": "timeline:main",
            "target_timeline_id": "timeline:next",
            "label": "Go",
            "prompt": "What next?",
        },
        {
            "edge_id": "next-end",
            "source_timeline_id": "timeline:next",
            "target_timeline_id": "timeline:end",
            "label": "Continue",
        },
    ]
    candidate = copy.deepcopy(base)
    candidate["narrative_edges"][edge_index][field] = value
    updated, impact = apply_frontend_edit_impacts(
        candidate,
        [f"/narrative_edges/{edge_index}/{field}"],
        base=base,
    )
    assert impact.invalidated_artifact_version_ids == {
        f"script:timeline:{name}:v1" for name in affected
    }
    for name in ("main", "next", "end"):
        assert updated["assets"]["artifact_versions_by_id"][
            f"script:timeline:{name}:v1"
        ]["stale"] is (name in affected)


@pytest.mark.parametrize("existing_snapshot", [False, True])
def test_snapshot_history_is_excluded_from_script_inputs(existing_snapshot):
    base = _script_project()
    snapshot_id = "snapshot:timeline:main:1"
    snapshot = copy.deepcopy(base["timelines"]["items"]["timeline:main"])
    snapshot["timeline_id"] = snapshot_id
    if existing_snapshot:
        base["timelines"]["items"][snapshot_id] = copy.deepcopy(snapshot)
        base["timelines"]["order"].append(snapshot_id)
    candidate = copy.deepcopy(base)
    snapshot.update(
        title="Archived",
        synopsis="Old story",
        planned_duration_seconds=10,
    )
    candidate["timelines"]["items"][snapshot_id] = snapshot
    candidate["timelines"]["order"] = [
        snapshot_id,
        *base["timelines"]["order"][:3],
    ]
    candidate["assets"]["artifact_slots_by_id"]["snapshot-script"] = {
        "kind": "timeline_script",
        "owner_ref": f"timeline:{snapshot_id}",
        "selected_version_id": "snapshot-v1",
    }
    candidate["assets"]["artifact_versions_by_id"]["snapshot-v1"] = {
        "stale": False,
    }
    updated, impact = apply_frontend_edit_impacts(
        candidate,
        ["/timelines"],
        base=base,
    )
    assert not impact.invalidated_artifact_version_ids
    versions = updated["assets"]["artifact_versions_by_id"]
    assert all(version["stale"] is False for version in versions.values())

    candidate["timelines"]["items"]["timeline:main"]["synopsis"] = "New story"
    updated, impact = apply_frontend_edit_impacts(
        candidate,
        ["/timelines/items/timeline:main/synopsis"],
        base=base,
    )
    assert impact.invalidated_artifact_version_ids == {
        f"script:{tid}:v1" for tid in base["timelines"]["order"][:3]
    }
    versions = updated["assets"]["artifact_versions_by_id"]
    assert versions["snapshot-v1"]["stale"] is False


@pytest.mark.parametrize(
    ("path", "value", "invalidates"),
    [
        (("items", "s4", "current_intelligence_version_id"), "new", False),
        (("items", "s3", "current_intelligence_version_id"), "new", True),
        (("items", "s1", "current_intelligence_version_id"), None, True),
        (("items", "empty", "current_intelligence_version_id"), "new", True),
        (("order",), ["s1", "s2", "s3", "empty", "s4"], False),
        (("order",), ["empty", "s2", "s1", "s3", "s4"], True),
        (("order",), ["empty", "s1", "s2", "s3"], False),
    ],
)
def test_script_intelligence_uses_first_three_non_null_versions(
    path,
    value,
    invalidates,
):
    base = _script_project()
    source_ids = ["empty", "s1", "s2", "s3", "s4"]
    base["sources"] = {
        "sources": {
            "order": source_ids,
            "items": {
                sid: {
                    "current_intelligence_version_id": (
                        None if sid == "empty" else sid
                    ),
                }
                for sid in reversed(source_ids)
            },
        },
    }
    candidate = copy.deepcopy(base)
    node = candidate["sources"]["sources"]
    for token in path[:-1]:
        node = node[token]
    node[path[-1]] = value
    _, impact = apply_frontend_edit_impacts(
        candidate,
        ["/sources/sources/" + "/".join(path)],
        base=base,
    )
    expected = (
        {f"script:{tid}:v1" for tid in base["timelines"]["order"]}
        if invalidates
        else set()
    )
    assert impact.invalidated_artifact_version_ids == expected


def test_unchanged_script_inputs_preserve_existing_staleness():
    base = _script_project()
    version_id = "script:timeline:main:v1"
    base["assets"]["artifact_versions_by_id"][version_id].update(
        stale=True,
        stale_reason="Earlier story edit",
    )
    candidate = copy.deepcopy(base)
    candidate["timelines"]["items"]["timeline:next"][
        "planned_duration_seconds"
    ] = 142
    updated, impact = apply_frontend_edit_impacts(
        candidate,
        ["/timelines/items/timeline:next/planned_duration_seconds"],
        base=base,
    )
    assert updated["assets"]["artifact_versions_by_id"][version_id] == (
        base["assets"]["artifact_versions_by_id"][version_id]
    )
    assert impact.invalidated_artifact_version_ids == {
        "script:timeline:next:v1",
    }


def test_manual_rename_marks_the_title_as_user_supplied():
    before = _project()
    before.update(name="自动截取", name_source="auto")
    candidate = copy.deepcopy(before)
    candidate["name"] = "深夜来信"
    after, _ = apply_frontend_edit_impacts(candidate, ["/name"], base=before)
    assert after["name_source"] == "user"
    unchanged, _ = apply_frontend_edit_impacts(before, ["/description"])
    assert unchanged["name_source"] == "auto"


NODES = ("prologue", "ending-a", "ending-b", "hidden")


def _branch_project():
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
    base = _branch_project()
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
    base = _branch_project()
    candidate = copy.deepcopy(base)
    candidate["narrative_edges"][1][field] = "A different choice"
    assert _invalidated(
        base,
        candidate,
        [f"/narrative_edges/1/{field}"],
    ) == {"ending-b", "hidden"}


def test_retargeting_edge_invalidates_both_old_and_new_endpoints():
    base = _branch_project()
    candidate = copy.deepcopy(base)
    candidate["narrative_edges"][1]["target_timeline_id"] = "ending-a"
    assert _invalidated(base, candidate, ["/narrative_edges"]) == {
        "ending-a",
        "ending-b",
        "hidden",
    }


def test_choice_visual_tone_does_not_invalidate_any_script():
    base = _branch_project()
    candidate = copy.deepcopy(base)
    candidate["narrative_edges"][0]["tone"] = "danger"
    assert not _invalidated(base, candidate, ["/narrative_edges/0/tone"])


@pytest.mark.parametrize("field", ["description", "planned_duration_seconds"])
def test_local_script_inputs_only_invalidate_their_owner(field):
    base = _branch_project()
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
    base = _branch_project()
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
    base = _branch_project()
    candidate = copy.deepcopy(base)
    candidate["timelines"]["items"]["ending-b"][field] = "New story premise"
    assert _invalidated(
        base,
        candidate,
        [f"/timelines/items/ending-b/{field}"],
    ) == set(NODES)


def test_project_creative_brief_still_invalidates_scripts():
    base = _branch_project()
    candidate = copy.deepcopy(base)
    candidate["strategy"] = {"creative_brief": "Change the story genre"}
    assert _invalidated(base, candidate, ["/strategy"]) == set(NODES)


def test_noop_and_frozen_history_do_not_invalidate_scripts():
    base = _branch_project()
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


pytestmark = pytest.mark.unit


def _branching_services(tmp_path):
    services = _services(tmp_path)
    base = services.projects.read(PROJECT_ID)
    project = base.project.model_copy(deep=True)
    project.timelines.items["timeline:main"].description = "Saved ending A"
    project.timelines.items["ending-b"] = Timeline(
        timeline_id="ending-b",
        title="Ending B",
        description="Saved ending B",
    )
    project.timelines.order.append("ending-b")
    project.narrative_edges = [
        NarrativeEdge(
            edge_id="ending-choice",
            source_timeline_id="timeline:main",
            target_timeline_id="ending-b",
            label="Look for more evidence",
        ),
    ]
    services.commits.commit(
        base=base,
        candidate=project.model_dump(mode="json"),
        origin=ChangeOrigin.INITIAL_CREATION,
        review_policy=ReviewPolicy.AUTO_FIX,
    )
    return services


@pytest.mark.parametrize("tool_name", ["jq_project", "patch_project"])
def test_initial_agent_ending_edit_keeps_script_and_replays_without_model(
    tmp_path,
    monkeypatch,
    tool_name,
):
    services = _branching_services(tmp_path)
    calls = _mock_chat(monkeypatch, [DRAFT])
    first = _draft(services, key="first")
    tools = AgentProjectTools(
        services.projects,
        context=AgentProjectToolContext(
            origin=ChangeOrigin.INITIAL_CREATION,
            review_policy=ReviewPolicy.AUTO_FIX,
            caused_by_message_seq=1,
        ),
    )
    tools.read_project(PROJECT_ID)
    base = services.projects.read(PROJECT_ID)
    # A concurrent production commit must survive the Agent's cached base.
    candidate = base.project.model_dump(mode="json")
    candidate["settings"]["aspect_ratio"] = "9:16"
    services.commits.commit(
        base=base,
        candidate=candidate,
        origin=ChangeOrigin.RUNTIME_TASK,
        review_policy=ReviewPolicy.AUTO_FIX,
    )
    edge = NarrativeEdge(
        edge_id="revised-ending-choice",
        source_timeline_id="ending-b",
        target_timeline_id="timeline:main",
        label="Keep the evidence",
    ).model_dump(mode="json")
    if tool_name == "jq_project":
        result = tools.jq_project(
            project_id=PROJECT_ID,
            program=".narrative_edges = $edges",
            json_args={"edges": [edge]},
        )
    else:
        result = tools.patch_project(
            project_id=PROJECT_ID,
            ops=[
                {"op": "replace", "path": "/narrative_edges", "value": [edge]},
            ],
        )
    project = result.project
    assert project.settings.aspect_ratio == "9:16"
    assert _build_script_prompt(
        base.project,
        base.project.timelines.items["timeline:ep2"],
        "",
    ) == _build_script_prompt(
        project,
        project.timelines.items["timeline:ep2"],
        "",
    )
    slot = project.assets.artifact_slots_by_id[first.slot_id]
    version = project.assets.artifact_versions_by_id[first.artifact_version_id]
    assert not version.stale
    assert slot.version_ids == [first.artifact_version_id]
    indexed = project.assets.files_by_id[first.file_id]
    payload = (
        services.projects.project_root(PROJECT_ID) / indexed.relative_uri
    ).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == version.checksum
    graph = derive_work_graph(project)
    assert graph.by_id["script:timeline:ep2"].status is WorkNodeStatus.DONE
    assert graph.by_id["script:timeline:ep2"].regeneration_of is None
    replay = _draft(services, key="retry-after-remote-edit")
    assert replay.replayed
    assert replay.artifact_version_id == first.artifact_version_id
    assert len(calls) == 1


def test_remote_branch_edit_during_model_call_keeps_valid_result(
    tmp_path,
    monkeypatch,
):
    services = _branching_services(tmp_path)
    calls = []

    async def finish_ending_then_return(prompt, **_kwargs):
        calls.append(prompt)
        tools = AgentProjectTools(
            services.projects,
            context=AgentProjectToolContext(
                origin=ChangeOrigin.INITIAL_CREATION,
            ),
        )
        tools.read_project(PROJECT_ID)
        tools.patch_project(
            project_id=PROJECT_ID,
            ops=[
                {"op": "remove", "path": "/narrative_edges/0"},
            ],
        )
        return DRAFT

    monkeypatch.setattr(
        script_execution.text_model,
        "chat_completion",
        finish_ending_then_return,
    )
    first = _draft(services, key="in-flight")
    assert not first.replayed
    replay = _draft(services, key="retry")
    assert replay.replayed
    assert replay.artifact_version_id == first.artifact_version_id
    assert len(calls) == 1


def test_unused_default_duration_does_not_redraft_script(
    tmp_path,
    monkeypatch,
):
    services = _services(tmp_path)
    calls = _mock_chat(monkeypatch, [DRAFT])
    first = _draft(services, key="first")
    tools = AgentProjectTools(
        services.projects,
        context=AgentProjectToolContext(origin=ChangeOrigin.INITIAL_CREATION),
    )
    tools.read_project(PROJECT_ID)
    result = tools.patch_project(
        project_id=PROJECT_ID,
        ops=[
            {
                "op": "replace",
                "path": "/settings/target_duration_seconds",
                "value": 90,
            },
        ],
    )
    assert not result.project.assets.artifact_versions_by_id[
        first.artifact_version_id
    ].stale
    replay = _draft(services, key="retry")
    assert replay.replayed
    assert replay.artifact_version_id == first.artifact_version_id
    assert len(calls) == 1


@pytest.mark.parametrize("automatic", [False, True])
def test_scheduler_regenerates_stale_script_once_and_keeps_saved_body(
    tmp_path,
    monkeypatch,
    automatic,
):
    services = _services(tmp_path)
    calls = _mock_chat(monkeypatch, [DRAFT, DRAFT + "\n**管家**：请保管钥匙。\n"])
    monkeypatch.setattr(
        work_scheduler,
        "get_execution_authorization_mode",
        lambda: "allow_all" if automatic else "required",
    )

    async def scenario():
        first = await execute_file_script_command(
            services,
            project_id=PROJECT_ID,
            target_ref="timeline:timeline:ep2",
            arguments={},
            idempotency_key="first-script",
        )
        base = services.projects.read(PROJECT_ID)
        candidate = base.project.model_dump(mode="json")
        candidate["timelines"]["items"]["timeline:main"][
            "description"
        ] = "已发布第一集正文"
        episode = candidate["timelines"]["items"]["timeline:ep2"]
        episode["synopsis"] = "林晚决定留下钥匙，进入旧宅。"
        episode["description"] = "林晚把钥匙收进口袋，然后走进旧宅。必须保留这个动作。"
        candidate, _ = apply_frontend_edit_impacts(
            candidate,
            [
                "/timelines/items/timeline:ep2/synopsis",
                "/timelines/items/timeline:ep2/description",
            ],
            base=base.project.model_dump(mode="json"),
        )
        services.commits.commit(
            base=base,
            candidate=candidate,
            origin=ChangeOrigin.FRONTEND_EDIT,
            review_policy=ReviewPolicy.AUTO_FIX,
        )
        scheduler = work_scheduler.WorkGraphScheduler(services)
        try:
            await scheduler.tick(PROJECT_ID)
            if automatic:
                deadline = asyncio.get_running_loop().time() + 5
                while (
                    services.projects.read(PROJECT_ID)
                    .project.assets.artifact_slots_by_id["script:timeline:ep2"]
                    .selected_version_id
                    == first.artifact_version_id
                ):
                    assert asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.01)
            for _ in range(3):
                await scheduler.tick(PROJECT_ID)
                await asyncio.sleep(0.01)
            project = services.projects.read(PROJECT_ID).project
            assert project.assets.artifact_versions_by_id[
                first.artifact_version_id
            ].stale
            selected = project.assets.artifact_slots_by_id[
                "script:timeline:ep2"
            ].selected_version_id
            if automatic:
                assert selected != first.artifact_version_id
                assert not project.assets.artifact_versions_by_id[
                    selected
                ].stale
                assert len(calls) == 1
                version = project.assets.artifact_versions_by_id[selected]
                file = project.assets.files_by_id[version.file_id]
                assert (
                    services.projects.project_root(PROJECT_ID)
                    / file.relative_uri
                ).read_text(encoding="utf-8") == episode["description"]
            else:
                assert selected == first.artifact_version_id
                assert len(calls) == 1
        finally:
            await scheduler.shutdown()

    asyncio.run(scenario())


def test_stale_script_sync_preserves_authored_body_without_model_call(
    tmp_path,
    monkeypatch,
):
    services = _services(tmp_path)
    calls = _mock_chat(monkeypatch, [DRAFT])
    first = _draft(services, key="initial")
    authored = "\n  原稿的空格与台词都要保留。\n\n陈默：还有七天以后，他们全部倒下。\n"
    base = services.projects.read(PROJECT_ID)
    candidate = base.project.model_dump(mode="json")
    candidate["timelines"]["items"]["timeline:ep2"]["description"] = authored
    candidate["assets"]["artifact_versions_by_id"][first.artifact_version_id][
        "stale"
    ] = True
    services.commits.commit(
        base=base,
        candidate=candidate,
        origin=ChangeOrigin.FRONTEND_EDIT,
    )

    def sync(key):
        return asyncio.run(
            execute_file_script_command(
                services,
                project_id=PROJECT_ID,
                target_ref="timeline:timeline:ep2",
                arguments={"source": "timeline"},
                idempotency_key=key,
            ),
        )

    result = sync("sync-body")
    current = services.projects.read(PROJECT_ID)
    version = current.project.assets.artifact_versions_by_id[
        result.artifact_version_id
    ]
    file = current.project.assets.files_by_id[version.file_id]
    assert (
        services.projects.project_root(PROJECT_ID) / file.relative_uri
    ).read_bytes() == authored.encode("utf-8")
    assert (
        current.project.timelines.items["timeline:ep2"].description == authored
    )
    assert version.metadata["scriptSource"] == "timeline"
    assert not version.stale
    assert current.project.assets.artifact_versions_by_id[
        first.artifact_version_id
    ].stale
    assert sync("sync-again").replayed
    assert len(calls) == 1


def test_explicit_script_revision_includes_existing_authored_body(
    tmp_path,
    monkeypatch,
):
    services = _services(tmp_path)
    calls = _mock_chat(monkeypatch, [DRAFT])
    base = services.projects.read(PROJECT_ID)
    candidate = base.project.model_dump(mode="json")
    candidate["timelines"]["items"]["timeline:ep2"]["description"] = DRAFT
    services.commits.commit(
        base=base,
        candidate=candidate,
        origin=ChangeOrigin.FRONTEND_EDIT,
    )
    _draft(services, key="revise", guidance="只调整开场动作")
    assert DRAFT in calls[0]["prompt"]
    assert "只调整开场动作" in calls[0]["prompt"]
