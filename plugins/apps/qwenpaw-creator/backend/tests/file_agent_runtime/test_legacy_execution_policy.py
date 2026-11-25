# -*- coding: utf-8 -*-
"""Execution policy specific to the Creator branch."""

from __future__ import annotations

import asyncio
import pytest
from services.file_agent_runtime import AgentModelTurn
from services.file_agent_runtime import driver as driver_module
from services.file_agent_runtime.work_graph import (
    WorkGraph,
    WorkNode,
    WorkNodeStatus,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project, VisualEntity
from test_driver import (
    CONVERSATION_ID,
    GOAL_ID,
    PROJECT_ID,
    SESSION_ID,
    _create_project,
    _driver,
)

from services.file_agent_runtime.work_graph import derive_work_graph
from test_work_graph import _add_element, _element, _project, _select_slot

from services.project_files.edit_impact import apply_frontend_edit_impacts
from services.runtime_files.models import ChangeOrigin, ReviewPolicy
from media_files.test_script_execution import (
    DRAFT,
    PROJECT_ID as SCRIPT_PROJECT_ID,
    _draft,
    _mock_chat,
    _services,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "reason",
    ["no_committed_progress", "media_budget_exhausted"],
)
def test_yolo_pause_reasons_are_durable_not_new_model_requests(
    tmp_path,
    monkeypatch,
    reason,
):
    services, snapshot = _create_project(tmp_path, initial_goal="完成短剧")
    driver = _driver(services, lambda *_args: AgentModelTurn())
    node = WorkNode(
        node_id="visual:hero",
        kind="visual",
        label="角色状态图",
        status=WorkNodeStatus.FAILED,
        error="请修改输入文本",
    )
    monkeypatch.setattr(
        driver_module,
        "get_media_review_mode",
        lambda: "auto_approve",
    )
    monkeypatch.setattr(
        driver_module,
        "derive_work_graph",
        lambda *_a, **_kw: WorkGraph(
            nodes=(node,),
            generation=snapshot.generation,
        ),
    )
    monkeypatch.setattr(
        driver.work_scheduler,
        "wake",
        lambda _project_id: None,
    )
    if reason == "no_committed_progress":
        services.sessions.append_message(
            PROJECT_ID,
            SESSION_ID,
            CONVERSATION_ID,
            role="user",
            source=driver.YOLO_RESUME_SOURCE,
            content_parts=[{"type": "text", "text": "修正输入文本"}],
            metadata={"projectGeneration": snapshot.generation},
        )
    else:

        def exhausted(*_args):
            raise driver_module.MediaCallBudgetExhausted("used budget")

        monkeypatch.setattr(
            driver_module,
            "ensure_media_call_budget",
            exhausted,
        )
    before = services.sessions.list_messages(PROJECT_ID, SESSION_ID)
    asyncio.run(
        driver._queue_yolo_completion_resume(
            project_id=PROJECT_ID,
            session_id=SESSION_ID,
            conversation_id=CONVERSATION_ID,
            run_id="paused-run",
        ),
    )
    after = services.sessions.list_messages(PROJECT_ID, SESSION_ID)
    assert [m.message_id for m in before if m.role == "user"] == [
        m.message_id for m in after if m.role == "user"
    ]
    assert after[-1].source == "creator_execution_notice"
    assert after[-1].metadata["executionPause"]["reason"] == reason
    assert not driver._wake.is_set()


def test_stale_final_render_reopens_compose() -> None:
    # Render review revises an overlay after the master render: edit
    # impact marks the final_video version stale. The compose node must
    # drop back to READY so the unattended loop re-renders the corrected
    # cut instead of reporting DONE one compose short.
    project = _project()
    _add_element(project, _element("elem:one"))
    _select_slot(
        project,
        slot_id="element:elem:one:storyboard",
        kind="r2v_storyboard_image",
        owner_ref="element:elem:one",
        version_id="art:sb",
    )
    _select_slot(
        project,
        slot_id="element:elem:one:main",
        kind="element_video",
        owner_ref="element:elem:one",
        version_id="art:vid",
    )
    _select_slot(
        project,
        slot_id="timeline:timeline:main:render",
        kind="final_video",
        owner_ref="timeline:timeline:main",
        version_id="art:final",
    )

    graph = derive_work_graph(project)
    assert graph.by_id["compose:timeline:main"].status is WorkNodeStatus.DONE

    project.assets.artifact_versions_by_id["art:final"].stale = True
    graph = derive_work_graph(project)
    compose = graph.by_id["compose:timeline:main"]
    assert compose.status is WorkNodeStatus.READY
    assert compose in graph.ready_media_nodes()


def test_superseded_render_source_reopens_compose_without_stale_flag() -> None:
    """Frozen selections are authoritative even if impact missed stale."""

    project = _project()
    _add_element(project, _element("elem:one"))
    _select_slot(
        project,
        slot_id="element:elem:one:storyboard",
        kind="r2v_storyboard_image",
        owner_ref="element:elem:one",
        version_id="art:sb",
    )
    _select_slot(
        project,
        slot_id="element:elem:one:main",
        kind="element_video",
        owner_ref="element:elem:one",
        version_id="art:vid-old",
    )
    _select_slot(
        project,
        slot_id="timeline:timeline:main:render",
        kind="final_video",
        owner_ref="timeline:timeline:main",
        version_id="art:final",
    )
    project.assets.artifact_versions_by_id["art:final"].metadata = {
        "sourceSelections": [
            {
                "sourceRef": "element:elem:one",
                "versionId": "art:vid-old",
            },
        ],
    }
    assert (
        derive_work_graph(project).by_id["compose:timeline:main"].status
        is WorkNodeStatus.DONE
    )

    _select_slot(
        project,
        slot_id="element:elem:one:main",
        kind="element_video",
        owner_ref="element:elem:one",
        version_id="art:vid-new",
    )
    # The lifecycle bug observed in the real run left this false.
    assert not project.assets.artifact_versions_by_id["art:final"].stale
    graph = derive_work_graph(project)
    compose = graph.by_id["compose:timeline:main"]
    assert compose.status is WorkNodeStatus.READY
    assert compose in graph.ready_media_nodes()


@pytest.mark.parametrize("legacy", [False, True])
def test_project_edit_invalidates_saved_draft_and_replays_new_version(
    tmp_path,
    monkeypatch,
    legacy,
):
    services = _services(tmp_path)
    calls = _mock_chat(monkeypatch, [DRAFT])
    first = _draft(services, key="original")

    if legacy:
        # Captured from Creator before the project-level inputs were hashed.
        base = services.projects.read(SCRIPT_PROJECT_ID)
        data = base.project.model_dump(mode="json")
        selected = data["assets"]["artifact_versions_by_id"][
            first.artifact_version_id
        ]
        selected["input_fingerprint"] = (
            "sha256:6bfccdd989dbb00d64bf7921c1674e0a"
            "2489d6991c09f59fc234604f75220b24"
        )
        services.commits.commit(
            base=base,
            candidate=data,
            origin=ChangeOrigin.RUNTIME_TASK,
            review_policy=ReviewPolicy.AUTO_FIX,
        )
    assert _draft(services, key="before-edit").replayed
    assert len(calls) == 1

    base = services.projects.read(SCRIPT_PROJECT_ID)
    original = base.project.model_dump(mode="json")
    candidate, impact = apply_frontend_edit_impacts(
        {**original, "description": "改为赛车纪录片"},
        ["/description"],
        base=original,
    )
    assert impact.invalidated_artifact_version_ids == {
        first.artifact_version_id,
    }
    services.commits.commit(
        base=base,
        candidate=candidate,
        origin=ChangeOrigin.FRONTEND_EDIT,
    )
    second = _draft(services, key="after-edit")
    assert not second.replayed
    assert second.artifact_version_id != first.artifact_version_id
    assert "改为赛车纪录片" in calls[-1]["prompt"]
    assert _draft(services, key="retry").replayed
    assert len(calls) == 2
