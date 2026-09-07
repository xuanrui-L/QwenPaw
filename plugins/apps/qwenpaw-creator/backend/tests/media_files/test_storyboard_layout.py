# -*- coding: utf-8 -*-
"""Storyboard keyframes are authored independently from video shot count."""

import pytest

from services.storyboard_layout import declared_storyboard_panel_count

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("9:16画布，3列×3行，共9个关键帧", 9),
        ("3列×3行，共9个等尺寸分镜格；每格内部画幅均为9:16", 9),
        ("9:16画布，3×3等尺寸网格", 9),
        ("九宫格布局", 9),
        ("十六宫格布局", 16),
        ("6个分镜格，3×3九宫格，余下留白", 6),
        ("six seconds; 9 storyboard panels", 9),
        ("panels: 9", 9),
        ("1 个分镜格，每格9:16", 1),
        ("单张静态关键帧", 1),
        ("单格分镜", 1),
        ("6秒，1个Shot，动作持续推进", None),
        ("16:9故事板，3格角色造型研究", None),
        ("每一个分镜格内部均为16:9", None),
        ("每 1 个分镜格内部均为16:9", None),
        ("第 9 个关键帧展示转折", None),
        ("4个关键帧，同时要求9个分镜格", None),
        ("2列×2行，共4个等尺寸分镜格，展示9个时间关键帧", None),
        ("3列×3行，共9个分镜格，展示9个时间关键帧", 9),
        ("", None),
    ],
)
def test_reads_only_explicit_layout(prompt, expected):
    assert declared_storyboard_panel_count(prompt) == expected
