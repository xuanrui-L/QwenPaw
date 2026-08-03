# -*- coding: utf-8 -*-
# flake8: noqa: E501

from __future__ import annotations

from domain.enums import SpecialistRole
from models import config as model_config
from services.file_agent_runtime import subagents
from services.file_agent_runtime.subagents import specialist_system_prompt


def _render(role: SpecialistRole) -> str:
    return specialist_system_prompt(
        role,
        project_id="project-voice-guidance-check",
        workspace_schema="SCHEMA",
    )


def test_unconfigured_tts_leaves_no_trace(monkeypatch) -> None:
    monkeypatch.setattr(model_config, "is_tts_configured", lambda: False)
    monkeypatch.setattr(
        subagents.model_config,
        "is_tts_configured",
        lambda: False,
    )
    for role in (
        SpecialistRole.VISUAL_DEVELOPMENT,
        SpecialistRole.AI_EDITING_DIRECTOR,
    ):
        prompt = _render(role)
        assert "tts" not in prompt.lower()
        assert "音色" not in prompt
        assert "旁白与配音" not in prompt
        assert "{{tts_guidance}}" not in prompt


def test_configured_tts_injects_role_sections(monkeypatch) -> None:
    monkeypatch.setattr(
        subagents.model_config,
        "is_tts_configured",
        lambda: True,
    )
    visual = _render(SpecialistRole.VISUAL_DEVELOPMENT)
    assert "# 语音与角色音色" in visual
    assert "tts_generation" in visual
    assert "create_character_voice" in visual
    assert "{{tts_guidance}}" not in visual

    editing = _render(SpecialistRole.AI_EDITING_DIRECTOR)
    assert "# 旁白与配音" in editing
    assert "creation.type=audio" in editing
    assert "create_character_voice" not in editing
    assert "{{tts_guidance}}" not in editing


def test_other_roles_never_reference_tts(monkeypatch) -> None:
    monkeypatch.setattr(
        subagents.model_config,
        "is_tts_configured",
        lambda: True,
    )
    prompt = _render(SpecialistRole.SOURCE_INTELLIGENCE)
    assert "tts_generation" not in prompt
