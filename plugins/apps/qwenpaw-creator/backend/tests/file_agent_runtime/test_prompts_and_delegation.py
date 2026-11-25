# -*- coding: utf-8 -*-
"""Agent delegation contracts and conditional prompt rendering."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from domain.enums import SpecialistRole
from models import config as model_config
from services.file_agent_runtime.prompts import render_creator_system_prompt
from services.file_agent_runtime.subagents import (
    DelegateToAgentInput,
    delegate_tool_manifest,
    specialist_system_prompt,
)

pytestmark = pytest.mark.unit


def test_only_current_element_specialists_are_delegatable() -> None:
    roles = delegate_tool_manifest()["function"]["parameters"]["properties"][
        "role"
    ]["enum"]
    assert roles == [
        "source_intelligence_agent",
        "ai_editing_director",
    ]

    with pytest.raises(ValidationError):
        DelegateToAgentInput.model_validate(
            {
                "role": "retired_planning_agent",
                "target_refs": ["project:plan"],
                "task": "这个职责现在属于 Creator 主 Agent",
            },
        )


def test_r2v_is_not_delegatable_and_edit_uses_timeline_targets() -> None:
    r2v = DelegateToAgentInput.model_validate(
        {
            "role": "r2v_generation_director",
            "target_refs": ["element:r2v-1"],
            "task": "生成目标 Element",
        },
    )
    with pytest.raises(ValueError, match="not delegatable"):
        r2v.validate_contract(project_id="project-1")

    edit = DelegateToAgentInput.model_validate(
        {
            "role": "ai_editing_director",
            "target_refs": ["timeline:timeline:main"],
            "task": "选择并执行 Edit Elements",
        },
    )
    edit.validate_contract(project_id="project-1")


def test_visual_development_is_not_delegatable() -> None:
    """Visual asset prompts are authored by the main Agent directly; the
    retired visual development surface rejects new delegations."""

    for target_ref in ("asset:char:haaland", "lineup:argentina-trio"):
        delegated = DelegateToAgentInput.model_validate(
            {
                "role": "visual_development_agent",
                "target_refs": [target_ref],
                "task": "为角色和场景生成设计图",
            },
        )
        with pytest.raises(ValueError, match="not delegatable"):
            delegated.validate_contract(project_id="project-1")


def test_source_intelligence_delegation_takes_exactly_one_asset() -> None:
    """A batched delegation reads every image in one model turn and blows
    the per-turn wall-clock budget; the contract forces per-asset fan-out."""

    def contract(*refs: str) -> None:
        DelegateToAgentInput.model_validate(
            {
                "role": "source_intelligence_agent",
                "target_refs": list(refs),
                "task": "理解素材",
            },
        ).validate_contract(project_id="project-1")

    contract("asset:asset-a")
    with pytest.raises(ValueError, match="exactly one asset target"):
        contract("asset:asset-a", "asset:asset-b")


def test_unknown_target_kinds_still_fail_the_contract() -> None:
    delegated = DelegateToAgentInput.model_validate(
        {
            "role": "source_intelligence_agent",
            "target_refs": ["storyline:act-1"],
            "task": "非法目标",
        },
    )
    with pytest.raises(ValueError, match="does not allow targetRef"):
        delegated.validate_contract(project_id="project-1")


_PROJECT_ID = "project-guidance-test"
_WORKSPACE_SCHEMA = "CUSTOM_PROJECT_SCHEMA"


def test_runtime_prompts_scope_workspace_schema_by_role() -> None:
    assert _WORKSPACE_SCHEMA in render_creator_system_prompt(
        project_id=_PROJECT_ID,
        workspace_schema=_WORKSPACE_SCHEMA,
    )
    editing = _specialist_prompt(SpecialistRole.AI_EDITING_DIRECTOR)
    assert _WORKSPACE_SCHEMA in editing

    source = _specialist_prompt(SpecialistRole.SOURCE_INTELLIGENCE)
    assert "commit_source_intelligence" in source
    assert _WORKSPACE_SCHEMA not in source


def _specialist_prompt(role: SpecialistRole) -> str:
    return specialist_system_prompt(
        role,
        project_id=_PROJECT_ID,
        workspace_schema=_WORKSPACE_SCHEMA,
    )


@pytest.mark.parametrize(
    "role",
    [
        SpecialistRole.R2V_GENERATION_DIRECTOR,
        SpecialistRole.VISUAL_DEVELOPMENT,
    ],
)
def test_retired_specialists_have_no_active_prompt(role) -> None:
    with pytest.raises(ValueError, match="no active prompt"):
        _specialist_prompt(role)


@pytest.mark.parametrize(
    ("model", "limit"),
    [("qwen-image-3.0", "3"), ("gpt-image-2", "16")],
)
def test_image_model_guidance_uses_the_configured_reference_limit(
    monkeypatch,
    model,
    limit,
) -> None:
    monkeypatch.setattr(model_config, "get_image_model_name", lambda: model)
    prompt = render_creator_system_prompt(project_id=_PROJECT_ID)
    assert model in prompt
    assert f"{limit} 张" in prompt


@pytest.mark.parametrize(
    ("model", "backend", "duration"),
    [
        ("happyhorse-1.1-r2v", "happyhorse", "3–15"),
        ("doubao-seedance-2-5-260628", "seedance2", "4–30"),
        ("wan3.0-video", "wan", "2–30"),
    ],
)
def test_video_model_guidance_uses_the_configured_duration(
    monkeypatch,
    model,
    backend,
    duration,
) -> None:
    monkeypatch.setattr(model_config, "get_video_model_name", lambda: model)
    monkeypatch.setattr(model_config, "get_video_backend", lambda: backend)
    prompt = render_creator_system_prompt(project_id=_PROJECT_ID)
    assert duration in prompt


def _tts(monkeypatch, *, model: str, configured: bool = True) -> None:
    monkeypatch.setattr(model_config, "is_tts_configured", lambda: configured)
    monkeypatch.setattr(model_config, "get_tts_model_name", lambda: model)


@pytest.mark.parametrize("configured", [False, True])
def test_only_configured_editing_can_use_tts(monkeypatch, configured) -> None:
    _tts(monkeypatch, model="qwen3-tts-flash", configured=configured)
    editing = _specialist_prompt(SpecialistRole.AI_EDITING_DIRECTOR)
    source = _specialist_prompt(SpecialistRole.SOURCE_INTELLIGENCE)

    assert ("tts_generation" in editing) is configured
    assert "tts_generation" not in source


@pytest.mark.parametrize(
    ("model", "system_voice_available"),
    [("qwen3-tts-flash", True), ("cosyvoice-v3.5-plus", False)],
)
def test_editing_guidance_reflects_voice_enrollment_needs(
    monkeypatch,
    model,
    system_voice_available,
) -> None:
    _tts(monkeypatch, model=model)
    editing = _specialist_prompt(SpecialistRole.AI_EDITING_DIRECTOR)
    assert ("Cherry" in editing) is system_voice_available
    assert ("create_character_voice" in editing) is not system_voice_available


@pytest.mark.parametrize(
    ("video_model", "backend", "supports_reference_voice"),
    [
        ("wan3.0-video-prime", "wan", True),
        ("wan2.7-r2v", "wan", True),
        ("happyhorse-1.1-r2v", "happyhorse", False),
    ],
)
def test_native_dialogue_voice_guidance_matches_video_capability(
    monkeypatch,
    video_model,
    backend,
    supports_reference_voice,
) -> None:
    _tts(monkeypatch, model="cosyvoice-v3.5-plus")
    monkeypatch.setattr(
        model_config, "get_video_model_name", lambda: video_model
    )
    monkeypatch.setattr(model_config, "get_video_backend", lambda: backend)
    prompt = render_creator_system_prompt(project_id=_PROJECT_ID)

    assert (
        "voice.sample_source_version_id" in prompt
    ) is supports_reference_voice


@pytest.mark.parametrize("requires_approval", [False, True])
def test_execution_guidance_tracks_authorization_mode(
    monkeypatch,
    requires_approval,
) -> None:
    mode = (
        model_config.EXECUTION_AUTHORIZATION_REQUIRED
        if requires_approval
        else model_config.EXECUTION_AUTHORIZATION_ALLOW_ALL
    )
    monkeypatch.setattr(
        model_config, "get_execution_authorization_mode", lambda: mode
    )
    prompt = render_creator_system_prompt(project_id=_PROJECT_ID)
    assert ("promptSyncRequired" in prompt) is requires_approval
