# -*- coding: utf-8 -*-
"""Hash-verified prompts owned by the file-native Creator Runtime."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

_PROMPT_ROOT = Path(__file__).resolve().parent
_PLACEHOLDER = re.compile(r"\{\{([a-zA-Z0-9_]+)\}\}")


@dataclass(frozen=True, slots=True)
class FileAgentPromptSpec:
    prompt_id: str
    filename: str
    sha256: str
    placeholders: frozenset[str]


def _spec(
    prompt_id: str,
    filename: str,
    sha256: str,
    *placeholders: str,
) -> FileAgentPromptSpec:
    return FileAgentPromptSpec(
        prompt_id=prompt_id,
        filename=filename,
        sha256=sha256,
        placeholders=frozenset(placeholders),
    )


FILE_AGENT_PROMPT_SPECS = {
    item.prompt_id: item
    for item in (
        # ── 核心 Agent 提示词 ──
        _spec(
            "creator_agent.system",
            "creator_agent.system.txt",
            "c8a69d6b0493936b21f36ccfdb182f7bf984417f9402f180f098281b744531b6",
            "project_id",
            "workspace_schema",
        ),
        _spec(
            "source_intelligence_agent.system",
            "source_intelligence_agent.system.txt",
            "5e07a77d98d5216e87cf64fa24658e3a52db3cd3af6ef9d052fec3d0cc7dda54",
            "project_id",
            "workspace_schema",
        ),
        _spec(
            "visual_development_agent.system",
            "visual_development_agent.system.txt",
            "6994a144559490204c772ef40f9c9152c4ddca6b40ebb346cf49b01459359cfb",
            "project_id",
            "workspace_schema",
        ),
        _spec(
            "r2v_generation_director.system",
            "r2v_generation_director.system.txt",
            "772cb14a3e603774b937e5a14d56f05b33e6f8ea3ba22ced7ba38070573cc939",
            "project_id",
            "workspace_schema",
            "story_skill_content",
            "art_skill_content",
        ),
        _spec(
            "r2v_prompt_techniques.system",
            "r2v_prompt_techniques.system.txt",
            "919a1aa65b909c5d161aa83b27727ccfb008f2241581906b74787fc0799fab30",
        ),
        _spec(
            "ai_editing_director.system",
            "ai_editing_director.system.txt",
            "b3ee71e796447a46e0b3ab9dc40bf9bd264d0dd022e49e3a2a24fc55118358fc",
            "project_id",
            "workspace_schema",
            "content_type",
            "target_duration_seconds",
        ),
        # ── story_skills 题材技能 ──
        _spec("story_skill.modern_romance", "story_skills/modern_romance.system.txt", "1010592a695a1acd6f48d222786a535c7ea541d7b5328d15c184b84f7cb4bc4b"),
        _spec("story_skill.xianxia_fantasy", "story_skills/xianxia_fantasy.system.txt", "8de7bbb8acf7a69aed3d458260221ed5e5164e6152e4f510fc7f2fb70f49804c"),
        _spec("story_skill.warrior_rise", "story_skills/warrior_rise.system.txt", "fc8bc7ce39efe60795567e2a9ea59ad40991358d362969861963356cee8c05e1"),
        _spec("story_skill.mystery_thriller", "story_skills/mystery_thriller.system.txt", "edf255edc20e9a44f327de1f4c620824fcf31f6e015770eb1ac6c671b78dcd14"),
        _spec("story_skill.urban_workplace", "story_skills/urban_workplace.system.txt", "0b1c73ca01c2bea82199928a64fe87a1f21f56042ff710de2e36814d2fbbc35c"),
        _spec("story_skill.period_nostalgia", "story_skills/period_nostalgia.system.txt", "af08e746e2dabdfa7282871b5a9610a55fd3728d05d26ef9796abf5f1f7660e6"),
        _spec("story_skill.comedy_humor", "story_skills/comedy_humor.system.txt", "2b9ff0b09ca53ff96e03cd0eb6360b1a25068db4688727216d6e63893d79868b"),
        _spec("story_skill.child_family", "story_skills/child_family.system.txt", "9e9fe62dc096932a0419ced231b5b7a0a88bfe4689211a7a7fe00901af1a705c"),
        _spec("story_skill.youth_campus", "story_skills/youth_campus.system.txt", "e251be60a6adcaacd6d3cd3253c7ef0928d553f631739219484cecbf64ab904f"),
        _spec("story_skill.folk_supernatural", "story_skills/folk_supernatural.system.txt", "cd8b5f0207a205b45859f146b4ef31c5aad87b0e94b9c387bde99b4164906a4d"),
        _spec("story_skill.martial_arts", "story_skills/martial_arts.system.txt", "0c5f8d1b6a82d122a2e66d937ec98b5aec00c9e74761f03de932dbda003f5880"),
        _spec("story_skill.gun_battle", "story_skills/gun_battle.system.txt", "5d2fdfe4d106c6560065c9784544b314776c12b1fc6a34c546d9404162723d60"),
        _spec("story_skill.survival_escape", "story_skills/survival_escape.system.txt", "b2211d6d8b78912f573aa593048c1a8f010bf2a0172b0f968c4157aea0daff76"),
        # ── art_skills 美术风格技能 ──
        _spec("art_skill.realistic_modern", "art_skills/realistic_modern.system.txt", "ec4458e781bbaac5d4ddee8a84198fa4bc3e88e5eead2835953caf4a898380e7"),
        _spec("art_skill.realistic_period", "art_skills/realistic_period.system.txt", "28ed28ca066e2b0f705f98f8d479f01e4395d15d219a8e4b0590624adeb4d38d"),
        _spec("art_skill.anime_2d", "art_skills/anime_2d.system.txt", "9feb8178a3bd871fe7d64925856b1bb72154ed26c11fabe5144a60902aca3a68"),
        _spec("art_skill.anime_3d_render", "art_skills/anime_3d_render.system.txt", "9ccd4e0fd38dc8f2292dda6cc528ee421d4c03c5181ead2e3e6ded6d26b2ea87"),
        _spec("art_skill.chinese_guofeng", "art_skills/chinese_guofeng.system.txt", "367f3bd15459f8c72218d974a665891aa3d24003f5dd955cd9991e2eb02bd0aa"),
        _spec("art_skill.horror_dark", "art_skills/horror_dark.system.txt", "570d66028d44f8fcad0f889aff8b736249fdbf43f5627cc445032032b13050e8"),
    )
}


def load_file_agent_prompt(prompt_id: str) -> str:
    try:
        spec = FILE_AGENT_PROMPT_SPECS[prompt_id]
    except KeyError as exc:
        raise KeyError(
            f"File Agent prompt is not allowlisted: {prompt_id}",
        ) from exc
    data = (_PROMPT_ROOT / spec.filename).read_bytes()
    if hashlib.sha256(data).hexdigest() != spec.sha256:
        raise RuntimeError(f"Prompt hash mismatch: {prompt_id}")
    text = data.decode("utf-8").strip()
    actual = frozenset(_PLACEHOLDER.findall(text))
    if actual != spec.placeholders:
        raise RuntimeError(
            f"Prompt placeholders mismatch for {prompt_id}: "
            f"expected={sorted(spec.placeholders)} actual={sorted(actual)}",
        )
    return text


def render_file_agent_prompt(prompt_id: str, **values: str) -> str:
    spec = FILE_AGENT_PROMPT_SPECS[prompt_id]
    supplied = frozenset(values)
    if supplied != spec.placeholders:
        raise ValueError(
            f"Prompt values mismatch for {prompt_id}: "
            f"expected={sorted(spec.placeholders)} actual={sorted(supplied)}",
        )
    rendered = load_file_agent_prompt(prompt_id)
    for name, value in values.items():
        rendered = rendered.replace("{{" + name + "}}", value)
    if _PLACEHOLDER.search(rendered):
        raise RuntimeError(f"Unresolved prompt placeholder: {prompt_id}")
    return rendered


def render_creator_system_prompt(
    *,
    project_id: str,
    workspace_schema: str | None = None,
) -> str:
    if workspace_schema is None:
        from services.project_files.schema_prompt import (
            build_project_schema_prompt,
        )

        workspace_schema = build_project_schema_prompt().text
    return render_file_agent_prompt(
        "creator_agent.system",
        project_id=project_id,
        workspace_schema=workspace_schema,
    )


__all__ = [
    "FILE_AGENT_PROMPT_SPECS",
    "FileAgentPromptSpec",
    "load_file_agent_prompt",
    "render_creator_system_prompt",
    "render_file_agent_prompt",
]
