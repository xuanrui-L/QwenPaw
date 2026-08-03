# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""File-native Creator specialist contracts and prompt selection."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain.enums import SpecialistRole
from models import config as model_config
from models.video_capabilities import video_model_prompt_guidance
from services.file_agent_runtime.prompts import render_file_agent_prompt
from services.project_files.models import Project
from services.project_files.schema_prompt import build_project_schema_prompt


DELEGATE_TOOL_NAME = "delegate_to_agent"

_DELEGATABLE_ROLES = (
    SpecialistRole.SOURCE_INTELLIGENCE,
    SpecialistRole.VISUAL_DEVELOPMENT,
    SpecialistRole.R2V_GENERATION_DIRECTOR,
    SpecialistRole.AI_EDITING_DIRECTOR,
)

_ROLE_TARGETS: dict[SpecialistRole, tuple[set[str], set[str]]] = {
    SpecialistRole.SOURCE_INTELLIGENCE: ({"asset"}, set()),
    SpecialistRole.VISUAL_DEVELOPMENT: (
        {"project", "element", "asset", "artifact"},
        {"assets"},
    ),
    SpecialistRole.R2V_GENERATION_DIRECTOR: ({"element"}, set()),
    SpecialistRole.AI_EDITING_DIRECTOR: ({"timeline"}, set()),
}

_TARGET_GUIDANCE = {
    SpecialistRole.SOURCE_INTELLIGENCE: "asset:<logicalAssetId>",
    SpecialistRole.VISUAL_DEVELOPMENT: (
        "overall visuals: project:assets; or element:<id>, "
        "asset:<id>, artifact:<id>"
    ),
    SpecialistRole.R2V_GENERATION_DIRECTOR: "an existing r2v element:<id>",
    SpecialistRole.AI_EDITING_DIRECTOR: "an existing timeline:<id>",
}

_ROLE_PROMPT_IDS = {
    SpecialistRole.SOURCE_INTELLIGENCE: "source_intelligence_agent.system",
    SpecialistRole.VISUAL_DEVELOPMENT: "visual_development_agent.system",
    SpecialistRole.R2V_GENERATION_DIRECTOR: "r2v_generation_director.system",
    SpecialistRole.AI_EDITING_DIRECTOR: "ai_editing_director.system",
}

# Visual entity ids are keyed char:/scene:/prop:<x> in project.json and the
# UI displays them as visual-entity:<id>, so models keep deriving targetRefs
# in those spellings. They map onto exactly one canonical asset ref.
_VISUAL_ENTITY_ALIAS_KINDS = frozenset({"char", "scene", "prop"})

# TTS sections are injected only when the capability is configured, mirroring
# the dynamic tool registration: the prompt never mentions absent tools.
_TTS_GUIDANCE = {
    SpecialistRole.VISUAL_DEVELOPMENT: (
        "\n# 语音与角色音色\n\n"
        "- `tts_generation` 把台词或旁白合成为音频 SourceAssetVersion 并返回"
        " exact version id；单次文本不超过约 512 token，超长文案按句子拆分"
        "分次生成。\n"
        "- `create_character_voice` 为 character 实体复刻专属音色，属可选增强："
        "characterRef 传目标角色的 exact asset:<entityId>；样本用已存在的"
        " exact 音频 version，或用 sampleText 先试音；绑定后该角色的"
        " tts_generation 传 characterRef 即自动沿用其音色，重新复刻会"
        "替换旧绑定。\n"
        "- 生成后用 read_project 验证音频 version 已写入 Asset Index、音色已"
        "绑定到目标实体。"
    ),
    SpecialistRole.AI_EDITING_DIRECTOR: (
        "\n# 旁白与配音\n\n"
        "- `tts_generation` 把旁白文本合成为音频 SourceAssetVersion 并返回"
        " exact version id；需要角色声线时传 characterRef（已绑定音色的"
        " character 实体）。\n"
        "- 旁白必须按镜头或语义段落拆分：每段单独调一次 tts_generation，"
        "对应一个独立的 audio Element，span 只覆盖它解说的画面区间；"
        "禁止用一条音频贯穿整条 Timeline。每段文本长度要与画面时长匹配"
        "（中文语速约每秒 4–5 字），生成后用返回的 durationSeconds 校准"
        " span，避免音频被截断或留白过长。\n"
        "- 音频上片：用 jq_project 在目标 Timeline 创建 creation.type=audio 的"
        " Element，引用对应 version id；音频 Element 不需要 location，"
        "gain_db 调音量、pan 调声像。\n"
        "- 合成时旁白按 span 混入成片，旁白播放区间内画面原声会自动压低，"
        "两者不会互相干扰；若某段原声本身是内容重点（台词、现场声），"
        "该段不要安排旁白。"
    ),
}


def _tts_guidance(role: SpecialistRole) -> str:
    if role not in _TTS_GUIDANCE:
        return ""
    if not model_config.is_tts_configured():
        return ""
    return _TTS_GUIDANCE[role]


def _normalize_asset_target_ref(target_ref: str) -> str:
    kind, separator, identifier = target_ref.partition(":")
    if not separator or not identifier:
        return target_ref
    if kind == "visual-entity":
        return f"asset:{identifier}"
    if kind in _VISUAL_ENTITY_ALIAS_KINDS:
        return f"asset:{target_ref}"
    return target_ref


class DelegateToAgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    role: SpecialistRole
    target_refs: list[str] = Field(alias="target_refs", min_length=1)
    task: str = Field(min_length=1)

    @field_validator("target_refs")
    @classmethod
    def validate_unique_target_refs(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("target_refs must contain unique values")
        return value

    def validate_contract(self, *, project_id: str) -> None:
        del project_id
        if self.role not in _DELEGATABLE_ROLES:
            raise ValueError(
                f"specialist role is not delegatable: {self.role.value}",
            )
        allowed_kinds, allowed_project_targets = _ROLE_TARGETS[self.role]
        if "asset" in allowed_kinds:
            # Accept-and-map the unambiguous visual-entity spellings instead
            # of failing the delegation for a guessable reason; anything
            # still unknown falls through to the strict check below.
            self.target_refs = list(
                dict.fromkeys(
                    _normalize_asset_target_ref(target_ref)
                    for target_ref in self.target_refs
                ),
            )
        for target_ref in self.target_refs:
            kind, separator, identifier = target_ref.partition(":")
            if not separator or not identifier or kind not in allowed_kinds:
                raise ValueError(
                    f"{self.role.value} does not allow targetRef {target_ref!r}; "
                    f"use {_TARGET_GUIDANCE[self.role]}",
                )
            if kind == "project" and identifier not in allowed_project_targets:
                raise ValueError(
                    f"{self.role.value} does not allow targetRef {target_ref!r}; "
                    f"use {_TARGET_GUIDANCE[self.role]}",
                )


def delegate_tool_manifest() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": DELEGATE_TOOL_NAME,
            "description": (
                "把一个边界明确的素材理解、视觉媒体、R2V 或 AI 剪辑任务委派给"
                "对应 Creator Specialist。source_intelligence_agent 使用 asset:<id>；"
                "visual_development_agent 的整体视觉使用 project:assets；"
                "r2v_generation_director 使用 element:<id>，"
                "ai_editing_director 使用 timeline:<id>。"
            ),
            "parameters": deepcopy(
                {
                    "type": "object",
                    "properties": {
                        "role": {
                            "type": "string",
                            "enum": [
                                role.value for role in _DELEGATABLE_ROLES
                            ],
                        },
                        "target_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "uniqueItems": True,
                            "description": (
                                "Use canonical refs only. Never use JSON paths, scenario "
                                "names, bare words, or a raw project id."
                            ),
                        },
                        "task": {"type": "string", "minLength": 1},
                    },
                    "required": ["role", "target_refs", "task"],
                    "additionalProperties": False,
                },
            ),
        },
    }


def specialist_system_prompt(
    role: SpecialistRole,
    *,
    project_id: str,
    project: Project | None = None,
    workspace_schema: str | None = None,
) -> str:
    if role not in _DELEGATABLE_ROLES:
        raise ValueError(f"specialist role has no active prompt: {role.value}")
    values: dict[str, str] = {
        "project_id": project_id,
        "workspace_schema": workspace_schema
        or build_project_schema_prompt().text,
    }
    if role in _TTS_GUIDANCE:
        values["tts_guidance"] = _tts_guidance(role)
    if role is SpecialistRole.R2V_GENERATION_DIRECTOR:
        # Model-specific prompt rules (e.g. HappyHorse [Image N] citations)
        # are injected from the runtime-resolved video model so the static
        # prompt stays model-agnostic.
        values["video_model_guidance"] = video_model_prompt_guidance(
            model_config.get_video_model_name(),
        )
    if role is SpecialistRole.AI_EDITING_DIRECTOR:
        content_type = (
            project.settings.content_type if project is not None else None
        )
        target_duration = (
            project.settings.target_duration_seconds
            if project is not None
            else None
        )
        values.update(
            {
                "content_type": content_type or "general",
                "target_duration_seconds": (
                    f"{target_duration:g}"
                    if target_duration is not None
                    else "null"
                ),
            },
        )
    return render_file_agent_prompt(_ROLE_PROMPT_IDS[role], **values)


__all__ = [
    "DELEGATE_TOOL_NAME",
    "DelegateToAgentInput",
    "delegate_tool_manifest",
    "specialist_system_prompt",
]
