# -*- coding: utf-8 -*-
"""Prompt contracts for explicit paid-media call ceilings."""

from __future__ import annotations

import pytest

from services.file_agent_runtime.prompts import load_file_agent_prompt


pytestmark = pytest.mark.unit


def test_creator_prompt_makes_single_shared_image_override_asset_fanout() -> (
    None
):
    prompt = load_file_agent_prompt("creator_agent.system")

    assert "显式媒体预算覆盖默认资产拆分" in prompt
    assert "一个复合 scene VisualEntity 的一个 Variant" in prompt
    assert "不得创建需要额外角色锚点和额外图片调用的 cast lineup" in prompt
    assert "共享设计图数 + R2V Element 数" in prompt
    assert "不能用共享设计 Artifact 冒充" in prompt


def test_visual_prompt_preserves_single_shared_image_call_ceiling() -> None:
    prompt = load_file_agent_prompt("visual_development_agent.system")

    assert "单张共享设计图预算例外" in prompt
    assert "不得在视觉开发范围调用第二次 `image_generation`" in prompt
    assert "同一选定 ArtifactVersion 由所有相关 R2V Element 复用" in prompt
    assert "父任务声明的“全部图片调用上限”不足以覆盖这些 storyboard" in prompt
