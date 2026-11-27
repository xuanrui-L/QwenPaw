# -*- coding: utf-8 -*-
# pylint: disable=protected-access
from __future__ import annotations

import hashlib
import inspect
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from services.project_files import agent_tools as agent_tools_module
from services.project_files.agent_tools import (
    _CUT_ADVISORY_SEEN,
    _PLAN_ADVISORY_SEEN,
    AGENT_PROJECT_TOOL_SCHEMAS,
    AgentProjectToolContext,
    AgentProjectToolError,
    AgentProjectTools,
    UnknownAgentProjectTool,
    agent_project_tool_manifest,
)
from services.project_files.commit import ProjectCommitBoundary
from services.project_files.models import (
    EditPlan,
    EditPlanDesignFloor,
    Project,
)
from services.project_files.schema_prompt import build_project_schema_prompt
from services.project_files.store import ProjectStore

from .conftest import (
    commit_indexed_file,
    edit_element,
    make_store,
    read_state,
    review_boundary,
    spoken_edit_element,
    timeline_project_with,
)

import shutil
import subprocess
from services.project_files import jq_transform as jq_transform_module
from services.project_files.jq_transform import (
    JqLimits,
    JqProjectTransformer,
    JqTransformError,
)
from services.project_files.json_pointer import JsonCasConflict
from services.project_files.json_pointer import (
    canonical_json_bytes as cas_json_bytes,
)
from services.project_files.json_pointer import merge_candidate
from services.project_files.locator_map import derive_ui_locator
from services.project_files.serialization import (
    canonical_json_bytes as project_json_bytes,
)

pytestmark = pytest.mark.unit


def _context(**overrides):
    kwargs = {
        "origin": "runtime_task",
        "caused_by_request_id": "request-1",
        "caused_by_message_seq": 1,
    }
    kwargs.update(overrides)
    return AgentProjectToolContext(**kwargs)


def _tools(store, *, context=None):
    return AgentProjectTools(store, context=context or _context())


def _external_commit(store, base, **updates):
    candidate = base.project.model_dump(mode="json")
    candidate.update(updates)
    return (
        ProjectCommitBoundary(store)
        .commit(base=base, candidate=candidate, origin="frontend_edit")
        .snapshot
    )


def test_read_project_returns_and_privately_caches_the_real_base(tmp_path):
    store, base = make_store(tmp_path)
    tools = _tools(store)

    result = tools.read_project("project-1")
    assert result.project.project_id == "project-1"
    assert result.etag == base.etag

    # The returned model is a copy. Mutating a caller-owned result cannot alter
    # the private three-way-merge base held by the Runtime.
    result.project.name = "caller mutation"
    committed = tools.jq_project(
        project_id="project-1",
        program=".description = $description",
        string_args={"description": "Agent description"},
    )
    assert committed.project.name == "Initial"
    assert committed.project.description == "Agent description"
    assert committed.changed_pointers == ["/description"]


def test_cached_base_enables_base_candidate_latest_three_way_merge(tmp_path):
    store, base = make_store(tmp_path)
    tools = _tools(store)
    tools.read_project("project-1")
    _external_commit(store, base, description="User changed this concurrently")

    result = tools.jq_project(
        project_id="project-1",
        program=".name = $name",
        string_args={"name": "Agent name"},
    )

    assert result.generation == 2
    assert result.project.name == "Agent name"
    assert result.project.description == "User changed this concurrently"
    assert store.read("project-1").etag == result.etag


def test_jq_rejects_nested_object_with_protected_pointer_details(tmp_path):
    store, base = make_store(tmp_path)
    tools = _tools(store)
    tools.read_project("project-1")

    with pytest.raises(
        AgentProjectToolError,
        match="完整 Project 根对象",
    ) as caught:
        tools.jq_project(
            project_id="project-1",
            program='.timelines.items["timeline:main"]',
        )

    assert "/project_id" in str(caught.value)
    assert caught.value.code == "JQ_RESULT_NOT_PROJECT_ROOT"
    assert "/project_id" in caught.value.details["changedProtectedPointers"]
    assert store.read("project-1").etag == base.etag


def test_runtime_context_not_model_arguments_controls_review(tmp_path):
    store, base = make_store(tmp_path)
    context = _context(
        origin="agentdock_interrupt",
        review_policy="require_review",
        review_boundary=review_boundary(base),
        caused_by_request_id="request-2",
        caused_by_message_seq=2,
        round_id="agent-tool-round-2",
    )
    tools = _tools(store, context=context)
    observed = tools.read_project("project-1")
    assert observed.etag == base.etag

    result = tools.jq_project(
        project_id="project-1",
        program='.description = "review me"',
    )

    assert result.review_id == "review-agent-tool-round-2"
    state = read_state(store)
    assert state.accepted_generation == 0
    assert state.last_project_generation == 1


def test_manifest_and_invoke_expose_only_model_owned_arguments(tmp_path):
    schemas = AGENT_PROJECT_TOOL_SCHEMAS
    assert tuple(schemas)[:5] == (
        "read_project",
        "read_project_file",
        "jq_project",
        "patch_project",
        "elements_at",
    )
    jq_parameters = schemas["jq_project"]["parameters"]
    assert set(jq_parameters["properties"]) == {
        "projectId",
        "program",
        "stringArgs",
        "jsonArgs",
    }
    assert jq_parameters["required"] == ["projectId", "program"]
    assert jq_parameters["additionalProperties"] is False
    assert "完整 Project 根对象" in schemas["jq_project"]["description"]
    assert [
        item["function"]["name"] for item in agent_project_tool_manifest()
    ] == list(schemas)
    signature = inspect.signature(AgentProjectTools.jq_project)
    assert "origin" not in signature.parameters

    store, _base = make_store(tmp_path)
    tools = _tools(store)
    read = tools.invoke("read_project", {"projectId": "project-1"})
    written = tools.invoke(
        "jq_project",
        {
            "projectId": "project-1",
            # A deprecated baseEtag is tolerated and ignored; the Runtime
            # selects the base itself.
            "baseEtag": read["etag"],
            "program": ".name = $name | .settings.platform = $platform",
            "stringArgs": {"name": "Invoked", "platform": "web"},
        },
    )
    assert written["project"]["name"] == "Invoked"
    shared_pointers = [
        pointer
        for pointer in written["changedPointers"]
        if pointer != "/name_source"
    ]
    assert shared_pointers == ["/name", "/settings/platform"]

    with pytest.raises(ValidationError, match="origin"):
        tools.invoke(
            "jq_project",
            {"projectId": "project-1", "program": ".", "origin": "task"},
        )
    with pytest.raises(UnknownAgentProjectTool):
        tools.invoke("write_project", {})


def test_invoke_translates_project_schema_errors_with_paths(tmp_path):
    store, base = make_store(tmp_path)
    tools = _tools(store)
    tools.invoke("read_project", {"projectId": "project-1"})

    with pytest.raises(AgentProjectToolError) as caught:
        tools.invoke(
            "jq_project",
            {
                "projectId": "project-1",
                "program": ('.visual.variants = {"items": {}, "order": []}'),
            },
        )
    message = str(caught.value)
    assert caught.value.code == "JQ_PROJECT_SCHEMA_INVALID"
    assert "visual.variants" in message
    assert "visual.entities.items" in message
    assert "项目未被修改" in message
    assert store.read("project-1").etag == base.etag


def test_project_pages_supply_replayable_utf8_cursor_and_reject_stale_pages(
    tmp_path,
):
    store, _ = make_store(tmp_path)
    tools = _tools(store)
    text = "对白片段：花儿本是心里的话。"
    tools.jq_project(
        project_id="project-1",
        program=f".description = {json.dumps(text)}",
    )
    request = {
        "projectId": "project-1",
        "fields": ["description", "name"],
        "maxBytes": 11,
    }
    first = tools.invoke("read_project", request)
    page = first
    chunks = []
    while True:
        assert len(page["content"].encode()) <= 11
        chunks.append(page["content"])
        if page["eof"]:
            assert page["nextPage"] is None
            break
        assert isinstance(page["nextPage"]["pointer"], str)
        page = tools.invoke("read_project", page["nextPage"])
    assert json.loads("".join(chunks)) == {
        "description": text,
        "name": store.read("project-1").project.name,
    }
    with pytest.raises(AgentProjectToolError, match="Fields do not exist"):
        tools.invoke(
            "read_project",
            {"projectId": "project-1", "fields": ["missing"]},
        )
    missing_etag = dict(first["nextPage"])
    missing_etag.pop("expectedEtag")
    with pytest.raises(AgentProjectToolError, match="expectedEtag"):
        tools.invoke("read_project", missing_etag)
    tools.jq_project(
        project_id="project-1",
        program='.description = "new text"',
    )
    with pytest.raises(AgentProjectToolError, match="changed between pages"):
        tools.invoke("read_project", first["nextPage"])


def test_read_project_file_pages_only_verified_indexed_utf8_text(tmp_path):
    store, base = make_store(tmp_path)
    content = "第一页：你好，Creator。\n第二页：文件索引。".encode()
    commit_indexed_file(
        store,
        base,
        file_id="file-story",
        kind="large_text",
        content=content,
        media_type="text/plain; charset=utf-8",
        relative_uri="assets/text/story.txt",
        staging_id="agent-read",
    )
    tools = _tools(store)

    # maxBytes=5 deliberately splits Chinese UTF-8 code points.
    args = {"projectId": "project-1", "fileId": "file-story", "maxBytes": 5}
    chunks: list[str] = []
    offset = 0
    while True:
        page = tools.invoke("read_project_file", {**args, "offset": offset})
        assert page["offset"] == offset
        assert page["sha256"] == hashlib.sha256(content).hexdigest()
        assert page["sizeBytes"] == len(content)
        assert len(page["content"].encode()) <= 5
        chunks.append(page["content"])
        offset = page["nextOffset"]
        if page["eof"]:
            break

    assert "".join(chunks).encode() == content
    assert offset == len(content)


def test_read_project_file_rejects_unknown_and_binary_files(tmp_path):
    store, base = make_store(tmp_path)
    commit_indexed_file(
        store,
        base,
        file_id="file-image",
        kind="artifact_payload",
        content=b"\x89PNG\r\n\x1a\n",
        media_type="image/png",
        relative_uri="assets/images/reference.png",
        staging_id="binary",
    )
    tools = _tools(store)

    with pytest.raises(AgentProjectToolError, match="does not exist"):
        tools.invoke(
            "read_project_file",
            {"projectId": "project-1", "fileId": "file-missing"},
        )
    with pytest.raises(AgentProjectToolError, match="not readable UTF-8 text"):
        tools.invoke(
            "read_project_file",
            {"projectId": "project-1", "fileId": "file-image"},
        )


# ── edit_plan / cut-boundary advisories (softened nudges) ───────────────────


def _advisory_tools(store):
    _PLAN_ADVISORY_SEEN.clear()
    _CUT_ADVISORY_SEEN.clear()
    return _tools(store, context=_context(round_id="agent-round-1"))


def _result(project: Project):
    return SimpleNamespace(project=project)


def test_plan_advisory_fires_until_plan_is_complete(tmp_path):
    store, base = make_store(tmp_path)
    tools = _advisory_tools(store)
    after = timeline_project_with(edit_element())

    advisory = tools._edit_plan_advisory(base.project, _result(after))

    assert advisory is not None
    assert advisory["kind"] == "edit_plan"
    hint = advisory["hints"][0]
    assert hint["timelineId"] == "timeline:main"
    assert hint["addedElementIds"] == ["el-edit-1"]
    assert "concept" in hint["missing"]
    assert "design_floor.opening" in hint["missing"]

    plan = EditPlan(
        concept="猫的越狱日记",
        pacing="hook 1.2s，动静交替",
        signature_device="爪印转场",
        design_floor=EditPlanDesignFloor(
            opening="1.5s 标题卡",
            transitions="硬切 + 爪印族",
            body="每场景一个设计节拍",
            ending="定格硬停",
        ),
    )
    planned = timeline_project_with(edit_element(), edit_plan=plan)
    tools = _advisory_tools(store)  # fresh dedup state
    assert tools._edit_plan_advisory(base.project, _result(planned)) is None


def test_cut_advisory_hints_off_boundary_endpoints(tmp_path, monkeypatch):
    store, base = make_store(tmp_path)
    tools = _advisory_tools(store)
    monkeypatch.setattr(
        agent_tools_module,
        "_transcript_boundaries_ms",
        lambda project, root, intelligence_id: (0, 5_000, 9_000),
    )
    # in=600ms is 600ms past the 0ms boundary (hint); out=5_100ms is
    # only 100ms from 5_000ms (inside tolerance, no hint).
    after = timeline_project_with(
        spoken_edit_element(in_tick=600, out_tick=5_100),
    )

    advisory = tools._cut_boundary_advisory(base.project, _result(after))

    assert advisory is not None
    assert advisory["kind"] == "cut_boundary"
    assert advisory["toleranceMs"] == 300
    assert len(advisory["hints"]) == 1
    hint = advisory["hints"][0]
    assert hint["endpoint"] == "in"
    assert hint["cutMs"] == 600
    assert hint["nearestSentenceBoundaryMs"] == 0
    assert "不阻断" in advisory["message"]

    # Identical findings are deduplicated on the next commit.
    again = tools._cut_boundary_advisory(base.project, _result(after))
    assert again is None

    # Human/front-end commits (no round_id) never receive the nudge.
    human = _tools(store)
    assert human._cut_boundary_advisory(base.project, _result(after)) is None


def test_schema_prompt_is_static_deterministic_and_cached_across_turns(
    tmp_path,
) -> None:
    build_project_schema_prompt.cache_clear()
    first = build_project_schema_prompt()
    assert build_project_schema_prompt() is first
    build_project_schema_prompt.cache_clear()
    regenerated = build_project_schema_prompt()

    assert regenerated.sha256 == first.sha256
    assert regenerated.sha256 == (
        "sha256:"
        + hashlib.sha256(regenerated.text.encode("utf-8")).hexdigest()
    )

    store = ProjectStore(tmp_path.resolve())
    store.create(Project.new(project_id="project-1", name="One"))
    first_runtime = AgentProjectTools(
        store,
        context=AgentProjectToolContext(origin="initial_creation"),
    )
    second_runtime = AgentProjectTools(
        store,
        context=AgentProjectToolContext(origin="runtime_task"),
    )
    assert first_runtime.schema_prompt is second_runtime.schema_prompt


def test_disjoint_object_changes_merge_without_losing_latest() -> None:
    base = {"story": {"title": "old", "outline": "v1"}, "generation": 1}
    candidate = {"story": {"title": "agent", "outline": "v1"}, "generation": 1}
    latest = {"story": {"title": "old", "outline": "user"}, "generation": 2}

    merged, changes = merge_candidate(
        base=base,
        candidate=candidate,
        latest=latest,
    )

    assert merged["story"] == {"title": "agent", "outline": "user"}
    assert [item.pointer for item in changes] == ["/story/title"]


def test_cas_normalizes_float_but_project_etag_preserves_type() -> None:
    assert cas_json_bytes({"value": 1}) == cas_json_bytes({"value": 1.0})
    assert project_json_bytes({"value": 1}) != project_json_bytes(
        {"value": 1.0},
    )


def test_same_field_change_is_a_deterministic_conflict() -> None:
    base = {"story": {"title": "old"}}
    candidate = {"story": {"title": "agent"}}
    latest = {"story": {"title": "user"}}

    with pytest.raises(JsonCasConflict) as caught:
        merge_candidate(base=base, candidate=candidate, latest=latest)

    assert caught.value.conflicts[0]["pointer"] == "/story/title"


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
def test_jq_uses_args_and_returns_one_object() -> None:
    result = JqProjectTransformer().transform(
        {"name": "before", "story": {"title": ""}},
        ".name = $name | .story.title = $title",
        string_args={"name": 'A "quoted" name', "title": "中文\n标题"},
    )
    assert result["name"] == 'A "quoted" name'
    assert result["story"]["title"] == "中文\n标题"


def test_jq_process_starts_at_the_system_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = "/opt/tools/bin/jq"
    recorded_command: list[str] = []

    def run(
        command: list[str],
        **kwargs,
    ) -> subprocess.CompletedProcess[bytes]:
        recorded_command.extend(command)
        kwargs["stdout"].write(b'{"name":"after"}\n')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(jq_transform_module.subprocess, "run", run)

    result = JqProjectTransformer(executable=executable).transform(
        {"name": "before"},
        '.name = "after"',
    )

    assert result == {"name": "after"}
    assert recorded_command == [
        executable,
        "--compact-output",
        "--exit-status",
        "--argjson",
        "stringArgs",
        "{}",
        "--argjson",
        "jsonArgs",
        "{}",
        '.name = "after"',
    ]


def test_jq_timeout_is_structured_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command: list[str], **_kwargs):
        raise subprocess.TimeoutExpired(command, 0.01)

    monkeypatch.setattr(jq_transform_module.subprocess, "run", run)
    transformer = JqProjectTransformer(
        executable="/opt/tools/bin/jq",
        limits=JqLimits(timeout_seconds=0.01),
    )

    with pytest.raises(JqTransformError) as caught:
        transformer.transform({"name": "before"}, '.name = "after"')

    assert caught.value.code == "JQ_TIMEOUT"
    assert caught.value.retryable is True
    assert caught.value.details == {"timeoutSeconds": 0.01}


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
def test_jq_rejects_multiple_outputs() -> None:
    with pytest.raises(JqTransformError, match="exactly one") as caught:
        JqProjectTransformer().transform({"name": "x"}, "., .")

    assert caught.value.code == "JQ_RESULT_NOT_PROJECT_ROOT"
    assert caught.value.details["resultCount"] == 2


def _project() -> dict:
    return {
        "assets": {
            "files_by_id": {
                "file-img": {"media_type": "image/png"},
                "file-vid": {"media_type": "video/mp4"},
            },
            "artifact_versions_by_id": {
                "ver-storyboard": {
                    "version_id": "ver-storyboard",
                    "slot_id": "element:el-1:storyboard",
                    "kind": "r2v_storyboard_image",
                    "owner_ref": "element:el-1",
                    "file_id": "file-img",
                },
                "ver-video": {
                    "version_id": "ver-video",
                    "slot_id": "element:el-1:main",
                    "kind": "r2v_video",
                    "owner_ref": "element:el-1",
                    "file_id": "file-vid",
                },
                "ver-char": {
                    "version_id": "ver-char",
                    "slot_id": "asset:char-7:image",
                    "kind": "visual_asset_image",
                    "owner_ref": "asset:char-7",
                    "file_id": "file-img",
                },
                "ver-lineup": {
                    "version_id": "ver-lineup",
                    "slot_id": "lineup:lineup:duo:image",
                    "kind": "cast_lineup_image",
                    "owner_ref": "lineup:lineup:duo",
                    "file_id": "file-img",
                },
            },
            "artifact_slots_by_id": {
                "element:el-1:storyboard": {
                    "slot_id": "element:el-1:storyboard",
                    "kind": "r2v_storyboard_image",
                    "owner_ref": "element:el-1",
                    "version_ids": ["ver-storyboard"],
                    "selected_version_id": "ver-storyboard",
                },
                "element:el-1:main": {
                    "slot_id": "element:el-1:main",
                    "kind": "r2v_video",
                    "owner_ref": "element:el-1",
                    "version_ids": ["ver-video"],
                    "selected_version_id": "ver-video",
                },
                "asset:char-7:image": {
                    "slot_id": "asset:char-7:image",
                    "kind": "visual_asset_image",
                    "owner_ref": "asset:char-7",
                    "version_ids": ["ver-char"],
                    "selected_version_id": "ver-char",
                },
            },
        },
    }


def test_storyboard_version_points_to_element_workbench() -> None:
    locator = derive_ui_locator(
        "/assets/artifact_versions_by_id/ver-storyboard",
        _project(),
    )
    assert locator == {
        "page": "element",
        "elementId": "el-1",
        "mediaType": "image",
        "artifactKind": "r2v_storyboard_image",
        "artifactVersionId": "ver-storyboard",
    }


def test_character_image_points_to_asset_detail() -> None:
    locator = derive_ui_locator(
        "/assets/artifact_versions_by_id/ver-char",
        _project(),
    )
    assert locator["page"] == "assets"
    assert locator["assetId"] == "char-7"
    assert locator["mediaType"] == "image"
    assert locator["artifactKind"] == "visual_asset_image"


def test_lineup_image_points_to_asset_card() -> None:
    locator = derive_ui_locator(
        "/assets/artifact_versions_by_id/ver-lineup",
        _project(),
    )
    assert locator == {
        "page": "assets",
        "assetId": "lineup:duo",
        "mediaType": "image",
        "artifactKind": "cast_lineup_image",
        "artifactVersionId": "ver-lineup",
    }


def test_element_text_pointer_carries_pointer_as_field() -> None:
    locator = derive_ui_locator(
        "/timelines/items/tl-1/elements_by_id/el-1/creation/video_prompt",
        _project(),
    )
    assert locator == {
        "page": "plan",
        "mediaType": "text",
        "elementId": "el-1",
        "field": (
            "/timelines/items/tl-1/elements_by_id/el-1/creation/video_prompt"
        ),
    }


def test_unknown_pointer_returns_empty() -> None:
    assert not derive_ui_locator(None, _project())
    assert not derive_ui_locator("", _project())


def test_model_authored_r2v_requires_a_generation_plan():
    project = Project.new(project_id="project-plan", name="Plan")
    candidate = project.model_dump(mode="json")
    timeline = candidate["timelines"]["items"]["timeline:main"]
    timeline["elements_by_id"]["new-shot"] = {
        "element_id": "new-shot",
        "span": {"start_tick": 0, "duration_tick": 4000},
        "location": {},
        "creation": {"type": "r2v", "continuity": "接续上一个动作"},
    }
    with pytest.raises(AgentProjectToolError, match="generation_plan"):
        AgentProjectTools._require_authored_video_plans(project, candidate)
    timeline["elements_by_id"]["new-shot"]["creation"]["generation_plan"] = {
        "method": "r2v",
        "use_storyboard": False,
    }
    AgentProjectTools._require_authored_video_plans(project, candidate)
    Project.model_validate(candidate)
