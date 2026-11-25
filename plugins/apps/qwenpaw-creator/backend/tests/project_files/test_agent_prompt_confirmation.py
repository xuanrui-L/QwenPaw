# -*- coding: utf-8 -*-
"""Agent tool effects when confirming synchronized prompts."""

import asyncio
import pytest
from services.file_agent_runtime.work_graph import WorkNodeStatus
from services.project_files.agent_tools import (
    AgentProjectToolContext,
    AgentProjectToolError,
    AgentProjectTools,
)
from services.prompt_sync_service import PromptSyncService
from .test_prompt_sync import EID, PID, TID, UPDATED, _storyboard_node, edit
from .test_prompt_sync import services as prompt_sync_services

services = prompt_sync_services

pytestmark = pytest.mark.unit


def _agent_tools(services):
    return AgentProjectTools(
        services.projects,
        context=AgentProjectToolContext(origin="runtime_task"),
    )


def test_agent_confirm_prompt_sync_clears_stage_gate_without_editing_text(
    services,
):
    """An Agent can record "checked, unchanged" instead of re-reading.

    The plan/prompt gate had no Agent-side exit: a narrative edit staled the
    storyboard stage baseline and only rewriting prompt text could clear it,
    so the model kept re-reading the same shots until the resume fuse tripped.
    """
    asyncio.run(PromptSyncService(services).confirm_current(PID, TID, EID))
    edit(services, "narrative", UPDATED["narrative"])
    assert _storyboard_node(services).prompt_sync_required is True

    tools = _agent_tools(services)
    from services.project_files.agent_tools import agent_project_tool_manifest

    assert "confirm_prompt_sync" in {
        item["function"]["name"] for item in agent_project_tool_manifest()
    }
    tools.invoke("read_project", {"projectId": PID})
    before = services.projects.read(PID)
    result = tools.invoke(
        "confirm_prompt_sync",
        {
            "projectId": PID,
            "timelineId": TID,
            "elementId": EID,
            "stage": "storyboard",
            "basis": "叙述已加入台词，分镜提示词已含对应声音意图，无需改写",
        },
    )

    assert result["statusBefore"] == "needs_update"
    assert result["statusAfter"] == "current"
    assert any(
        "/creation/prompt_sync/" in pointer
        for pointer in result["changedPointers"]
    )
    after = services.projects.read(PID)
    assert after.generation == before.generation + 1
    ready = _storyboard_node(services)
    assert ready.status is WorkNodeStatus.READY
    assert ready.prompt_sync_required is False
    old = before.project.timelines.items[TID].elements_by_id[EID].creation
    new = after.project.timelines.items[TID].elements_by_id[EID].creation
    assert (old.narrative, old.storyboard_prompt, old.video_prompt) == (
        new.narrative,
        new.storyboard_prompt,
        new.video_prompt,
    )


def test_agent_confirm_prompt_sync_is_idempotent_and_needs_an_observed_read(
    services,
):
    tools = _agent_tools(services)
    with pytest.raises(AgentProjectToolError) as unobserved:
        tools.invoke(
            "confirm_prompt_sync",
            {
                "projectId": PID,
                "timelineId": TID,
                "elementId": EID,
                "basis": "没有读过就确认",
            },
        )
    assert unobserved.value.code == "PROMPT_SYNC_NOT_OBSERVED"

    asyncio.run(PromptSyncService(services).confirm_current(PID, TID, EID))
    tools.invoke("read_project", {"projectId": PID})
    before = services.projects.read(PID)
    result = tools.invoke(
        "confirm_prompt_sync",
        {
            "projectId": PID,
            "timelineId": TID,
            "elementId": EID,
            "stage": "storyboard",
            "basis": "已核对，分镜与叙述仍然一致",
        },
    )
    # Already aligned: no write, so a repeated confirm cannot inflate history.
    assert result["changedPointers"] == []
    assert services.projects.read(PID).generation == before.generation


def test_agent_confirm_prompt_sync_refuses_untracked_legacy(services):
    tools = _agent_tools(services)
    tools.invoke("read_project", {"projectId": PID})
    with pytest.raises(AgentProjectToolError) as legacy:
        tools.invoke(
            "confirm_prompt_sync",
            {
                "projectId": PID,
                "timelineId": TID,
                "elementId": EID,
                "basis": "未建立基线时确认",
            },
        )
    assert legacy.value.code == "PROMPT_SYNC_LEGACY"


def test_prompt_sync_confirmation_is_reserved_for_the_main_agent(tmp_path):
    from domain.enums import SpecialistRole
    from services.project_files.facade import CreatorFileServices
    from services.specialist_tools import FileSpecialistToolRegistry

    services = CreatorFileServices.create(tmp_path)
    editing = FileSpecialistToolRegistry(services).manifest_for(
        SpecialistRole.AI_EDITING_DIRECTOR,
        admitted_target_refs=("timeline:timeline:main",),
    )
    assert "confirm_prompt_sync" not in {
        item["function"]["name"] for item in editing
    }


def test_agent_renaming_records_user_title_provenance(tmp_path):
    from .conftest import make_store
    from .test_agent_tools import _tools

    store, _ = make_store(tmp_path)
    tools = _tools(store)
    tools.invoke("read_project", {"projectId": "project-1"})
    written = tools.invoke(
        "jq_project",
        {"projectId": "project-1", "program": '.name = "Human title"'},
    )
    assert written["changedPointers"] == ["/name", "/name_source"]
    assert written["project"]["name_source"] == "user"


def test_jq_null_write_hint_names_the_rerooted_path() -> None:
    """A non-nullable field written as null must say what produced it.

    Models hit this by re-rooting: after ``| .interactive_presentation``
    they keep writing ``.interactive_presentation.screens``, jq reads that
    one level deeper than it exists, and the absent path evaluates to null.
    """

    from pydantic import ValidationError
    from services.project_files import agent_tools as agent_tools_module
    from services.project_files.models import Project

    payload = Project.new(project_id="project-1", name="Hint").model_dump(
        mode="json",
    )
    payload["interactive_presentation"] = {
        "design_prompt": "盘点",
        "screens": None,
        "cover_fingerprint": None,
    }
    with pytest.raises(ValidationError) as raised:
        Project.model_validate(payload)

    text = agent_tools_module._translate_project_schema_error(raised.value)

    assert "screens 被写成 null" in text
    assert "cover_fingerprint 被写成 null" in text
    assert "`.字段名`" in text
