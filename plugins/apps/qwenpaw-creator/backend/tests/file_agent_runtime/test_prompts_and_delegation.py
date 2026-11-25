# -*- coding: utf-8 -*-
"""Agent delegation contracts and conditional prompt rendering."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from domain.enums import SpecialistRole
from models import config as model_config
from models.video_capabilities import video_model_supported_modes
from services.file_agent_runtime.prompts import render_creator_system_prompt
from services.file_agent_runtime.subagents import (
    DelegateToAgentInput,
    delegate_tool_manifest,
    specialist_system_prompt,
)
from services.project_files.models import Project

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


def _specialist_prompt(
    role: SpecialistRole, *, project: Project | None = None
) -> str:
    return specialist_system_prompt(
        role,
        project_id=_PROJECT_ID,
        workspace_schema=_WORKSPACE_SCHEMA,
        project=project,
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
    ("model", "rules"),
    [
        ("qwen-image-3.0", ("总数必须不超过 3，", "400 拒绝")),
        ("gpt-image-2", ("单次最多 16 张输入参考图",)),
    ],
)
def test_image_model_guidance_uses_the_configured_reference_limit(
    monkeypatch,
    model,
    rules,
) -> None:
    monkeypatch.setattr(model_config, "get_image_model_name", lambda: model)
    prompt = render_creator_system_prompt(
        project_id=_PROJECT_ID, external_skills=""
    )
    assert f"当前图片生成模型是 `{model}`" in prompt
    for rule in rules:
        assert rule in prompt


def test_image_budget_guidance_blocks_paid_calls_before_exceeding_user_limit():
    prompt = render_creator_system_prompt(project_id=_PROJECT_ID)

    assert "共享设计图数 + R2V Element 数" in prompt
    assert re.search(r"超出上限时[^。]*付费调用前[^。]*\[BLOCKED\]", prompt)
    assert re.search(r"不能用共享设计 Artifact[^。]*跨 Element 复用", prompt)
    assert re.search(r"不得建立 cast lineup[^。]*第二个可调度视觉节点", prompt)
    assert re.search(
        r"共享角色/场景设计资产只能生成 1 张[^。]*" r"一个复合 `scene` 实体及其一个 Variant",
        prompt,
    )
    assert re.search(
        r"全部图片调用上限[^。]*不足以覆盖[^。]*storyboard[^。]*" r"不得先行占用视觉设计预算",
        prompt,
    )


def test_storyboard_keeps_the_users_stage_specific_medium() -> None:
    prompt = render_creator_system_prompt(
        project_id=_PROJECT_ID,
        external_skills="",
    )
    rule = next(
        line
        for line in prompt.splitlines()
        if line.startswith("- **渲染媒介必须写进 prompt")
    )
    assert "`storyboard_prompt` 是中间分镜" in rule
    assert "不套用最终交付媒介" in rule
    assert "只补足用户没有指明的目标" in rule
    assert re.search(r"粗铅笔、炭笔[^，]*绘制媒介时保留该媒介", rule)
    assert "成片仍按项目媒介生成" in rule


@pytest.mark.parametrize(
    ("model", "backend", "rules"),
    [
        (
            "happyhorse-1.1-r2v",
            "happyhorse",
            (
                "生成时长必须为 3–15 秒整数。",
                "。3 秒短段合法；30 秒单段不合法，",
                "HappyHorse：只能使用 `[Image 1]`、`[Image 2]`",
                "storyboard 固定为第一张，因此是 `[Image 1]`",
                "不支持参考视频",
                "分辨率仅支持 720P/1080P；",
            ),
        ),
        (
            "doubao-seedance-2-5-260628",
            "seedance2",
            ("支持 4–30 秒整数", "或 30 秒长段", "不套用 8–10 秒默认值"),
        ),
        (
            "wan2.7-r2v",
            "wan",
            (
                "中文 Prompt 用“图1、图2",
                "“视频1、视频2",
                "图片最多 5 张",
                "视频最多 5 个",
                "图片与视频合计最多 5 个",
            ),
        ),
        ("wan3.0-video", "wan", ("Wan3.0", "支持 2–30 秒整数。")),
        (
            "veo-3.1-generate-preview",
            "veo",
            ("只支持 4、6 或 8 秒；", "r2v 或 1080p/4k 时必须为 8 秒。"),
        ),
        ("VIDUQ2-PRO", "vidu", ("支持 1–10 秒整数。",)),
    ],
    ids=["happyhorse", "seedance-2.5", "wan-2.7", "wan-3.0", "veo", "vidu-q2"],
)
def test_creator_prompt_includes_configured_video_model_rules(
    monkeypatch,
    model,
    backend,
    rules,
) -> None:
    monkeypatch.setattr(model_config, "get_video_model_name", lambda: model)
    monkeypatch.setattr(model_config, "get_video_backend", lambda: backend)
    prompt = render_creator_system_prompt(
        project_id=_PROJECT_ID, external_skills=""
    )
    for rule in rules:
        assert rule in prompt


@pytest.mark.parametrize(
    ("model", "protocol", "marker", "constraint"),
    [
        (
            "kling/kling-v3-omni-video-generation",
            "DashScope（百炼）",
            "<<<{}>>>",
            "图片最多 7 张",
        ),
        ("kling-3.0-omni", "Kling", "@{}", "分辨率仅支持 720p/1080p/4k。"),
    ],
    ids=["bailian", "direct"],
)
def test_kling_guidance_uses_only_the_configured_channel_reference_syntax(
    monkeypatch,
    model,
    protocol,
    marker,
    constraint,
) -> None:
    video_settings = {"model": model, "protocol": protocol}
    monkeypatch.setattr(
        model_config,
        "get_request_tool_config",
        lambda tool: video_settings
        if tool == model_config.CREATOR_VIDEO_CONFIG_TOOL
        else {},
    )
    assert "r2v" in video_model_supported_modes(
        model, model_config.get_video_backend()
    )
    prompt = render_creator_system_prompt(
        project_id=_PROJECT_ID, external_skills=""
    )

    assert constraint in prompt
    wire_token = r"<<<(?:image|video)_(?:N|\d+)>>>|@(?:image|video)_(?:N|\d+)"
    references = set(re.findall(wire_token, prompt))
    assert {
        marker.format(ref) for ref in ("image_1", "image_N", "video_1")
    } <= references
    prefix = marker.split("{", 1)[0]
    assert all(reference.startswith(prefix) for reference in references)
    storyboard_references = set(
        re.findall(rf"storyboard\s*(?:是|为|用)\s*`({wire_token})`", prompt)
    )
    assert storyboard_references == {marker.format("image_1")}


def _tts(monkeypatch, *, model: str, configured: bool = True) -> None:
    monkeypatch.setattr(model_config, "is_tts_configured", lambda: configured)
    monkeypatch.setattr(model_config, "get_tts_model_name", lambda: model)


@pytest.mark.parametrize("configured", [False, True])
def test_configured_tts_guidance_preserves_native_dialogue(
    monkeypatch, configured
) -> None:
    _tts(monkeypatch, model="qwen3-tts-flash", configured=configured)
    project = Project.new(
        project_id=_PROJECT_ID, name="Drama", scenario="short_drama"
    )
    editing = _specialist_prompt(
        SpecialistRole.AI_EDITING_DIRECTOR, project=project
    )
    source = _specialist_prompt(SpecialistRole.SOURCE_INTELLIGENCE)
    creator = render_creator_system_prompt(
        project_id=_PROJECT_ID, external_skills=""
    )
    assert "tts_generation" not in source

    _, tts_heading, creator_tts = creator.partition("\n# 旁白与配音能力\n")
    assert bool(tts_heading) is configured
    for tool_name in ("create_character_voice", "tts_generation"):
        assert (tool_name in creator) is configured, tool_name
    creator_tts = creator_tts.partition("\n# ")[0]
    for rule in (
        "台词与旁白优先由视频模型原生生成",
        "只有两种例外才允许 TTS 合成",
        "用户提供了自己的音色音频文件",
        "需要外部驱动音频才能开口的 s2v 数字人",
    ):
        assert (rule in creator_tts) is configured, rule
    for rule in (
        "tts_generation",
        "R2V/T2V/I2V 区间的台词与旁白由视频模型原生生成",
        "不要为同一区间再合成一份 TTS 音频",
        "不再叠加一份 TTS 台词",
        "自己的音色音频文件",
        "画面不会自发声",
    ):
        assert (rule in editing) is configured, rule


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
    requires_bound_voice = "必须传已绑定音色的 characterRef" in editing
    assert requires_bound_voice is not system_voice_available
    creator = render_creator_system_prompt(
        project_id=_PROJECT_ID, external_skills=""
    )
    assert ("sampleText 不可用" in creator) is not system_voice_available


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
    sync_instructions = (
        "promptSyncRequired",
        "并非审阅冷却",
        "保留现有内容并生成",
        "不要声称会自动重试",
    )
    for instruction in sync_instructions:
        assert (instruction in prompt) is requires_approval, instruction
