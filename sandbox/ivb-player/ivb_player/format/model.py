# -*- coding: utf-8 -*-
"""内容层 / 表现层的解析结果模型。

刻意不用 pydantic:本模块不承担"校验"职责,只做一层带默认值的结构化视图。
校验规则全部在 :mod:`ivb_player.format.validate`,产出带点名的 Diagnostic,
而不是 pydantic 那种脱离业务语境的报错字符串。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: 放映端支持的 manifest 版本区间。改语义/删字段/改必填 -> MIN 抬升;
#: 只加字段 -> 不动版本号(见 bundle-format.md §7)。
MIN_SUPPORTED_SCHEMA_VERSION = 1
MAX_SUPPORTED_SCHEMA_VERSION = 1

PRESENTATION_SCHEMA_VERSION = 1

TONE_VALUES = ("safe", "risky", "danger")

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
#: bundle_id 要进 SQL 参数和文件名,收紧到路径安全字符集。
_SAFE_BUNDLE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def is_hex_color(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX_COLOR.match(value))


def expand_hex_color(value: str) -> str:
    """``#abc`` -> ``#aabbcc``,统一小写。非 hex 原样返回交给调用方判断。"""

    if not _HEX_COLOR.match(value):
        return value
    body = value[1:]
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    return f"#{body.lower()}"


def hex_to_rgb_triplet(value: str) -> str:
    """``#b8ff2e`` -> ``"184, 255, 46"``,供 ``rgba()`` 拼装。"""

    body = expand_hex_color(value)[1:]
    if len(body) != 6:
        return "255, 255, 255"
    return ", ".join(str(int(body[i : i + 2], 16)) for i in (0, 2, 4))


@dataclass(frozen=True, slots=True)
class BundleMeta:
    bundle_id: str
    title: str
    tagline: str = ""
    synopsis: str = ""
    accent: str = "#b8ff2e"

    @property
    def is_path_safe(self) -> bool:
        return bool(_SAFE_BUNDLE_ID.match(self.bundle_id))


@dataclass(frozen=True, slots=True)
class NodeInfo:
    timeline_id: str
    title: str
    synopsis: str
    children: tuple[str, ...]
    is_ending: bool

    @property
    def display_title(self) -> str:
        return self.title or self.timeline_id


@dataclass(frozen=True, slots=True)
class EdgeInfo:
    edge_id: str
    label: str
    prompt: str
    target_timeline_id: str
    tone: str | None = None


@dataclass(frozen=True, slots=True)
class OptionRef:
    edge_ref: str
    hotspot: dict[str, float] | None = None


@dataclass(frozen=True, slots=True)
class InteractionPoint:
    source_timeline_id: str
    at_seconds: float
    question: str
    options: tuple[OptionRef, ...]
    countdown_seconds: float | None = None
    default_edge_ref: str | None = None

    def option_edges(self) -> tuple[str, ...]:
        return tuple(option.edge_ref for option in self.options)


@dataclass(frozen=True, slots=True)
class Theme:
    """表现层配色。缺项由 :data:`BUILTIN_THEME` 回填。"""

    accent: str = "#b8ff2e"
    danger: str = "#ff3355"
    warning: str = "#ffb547"
    success: str = "#5fd68a"
    background: str = "#05070a"
    surface: str = "#0a0d11"
    surface_alt: str = "#11161c"
    text: str = "#e8f0d8"
    text_dim: str = "#7d8a72"
    fog: str = "#1a222b"

    def as_css_vars(self) -> dict[str, str]:
        return {
            "--ivb-accent": self.accent,
            "--ivb-accent-rgb": hex_to_rgb_triplet(self.accent),
            "--ivb-danger": self.danger,
            "--ivb-danger-rgb": hex_to_rgb_triplet(self.danger),
            "--ivb-warning": self.warning,
            "--ivb-warning-rgb": hex_to_rgb_triplet(self.warning),
            "--ivb-success": self.success,
            "--ivb-success-rgb": hex_to_rgb_triplet(self.success),
            "--ivb-bg": self.background,
            "--ivb-surface": self.surface,
            "--ivb-surface-alt": self.surface_alt,
            "--ivb-text": self.text,
            "--ivb-text-dim": self.text_dim,
            "--ivb-fog": self.fog,
        }


BUILTIN_THEME = Theme()

#: screens 允许出现的键,以及每个屏允许的字段。未知字段 -> 忽略 + 告警。
SCREEN_FIELDS: dict[str, frozenset[str]] = {
    "title": frozenset({"cta_label", "secondary_label"}),
    "choice": frozenset({"layout", "badge_labels"}),
    "map": frozenset({"reveal_depth"}),
    "ending": frozenset({"show_review"}),
}

DEFAULT_BADGE_LABELS: dict[str, str] = {
    "safe": "○ 稳妥",
    "risky": "△ 冒险",
    "danger": "✕ 危险",
}


@dataclass(frozen=True, slots=True)
class Presentation:
    """``presentation.json`` 的解析结果。

    ``present`` 区分"文件不存在"(全默认,零告警)与"文件存在但字段有问题"
    (告警)。样式表内容不进模型,由阅读器按需读取。
    """

    present: bool = False
    theme: Theme = BUILTIN_THEME
    screens: dict[str, Any] = field(default_factory=dict)
    stylesheets: tuple[str, ...] = ()

    def badge_label(self, tone: str | None) -> str:
        if tone is None:
            return ""
        choice = self.screens.get("choice") or {}
        labels = choice.get("badge_labels") or {}
        return str(labels.get(tone) or DEFAULT_BADGE_LABELS.get(tone, ""))

    @property
    def reveal_depth(self) -> int:
        mapping = self.screens.get("map") or {}
        try:
            return max(0, int(mapping.get("reveal_depth", 1)))
        except (TypeError, ValueError):
            return 1

    @property
    def ending_shows_review(self) -> bool:
        ending = self.screens.get("ending") or {}
        return bool(ending.get("show_review", True))


@dataclass(frozen=True, slots=True)
class Bundle:
    """一个包的完整内容层视图。构造它之前必须已过 :func:`validate_bundle`。"""

    meta: BundleMeta
    schema_version: int
    entry_timeline_id: str
    nodes: dict[str, NodeInfo]
    segments: dict[str, str]
    edges: dict[str, EdgeInfo]
    interactions: tuple[InteractionPoint, ...]
    presentation: Presentation = Presentation()

    @property
    def bundle_id(self) -> str:
        return self.meta.bundle_id

    @property
    def timeline_ids(self) -> tuple[str, ...]:
        return tuple(self.nodes)

    @property
    def endings(self) -> tuple[str, ...]:
        return tuple(
            timeline_id
            for timeline_id, node in self.nodes.items()
            if node.is_ending
        )

    def interactions_of(
        self, timeline_id: str
    ) -> tuple[InteractionPoint, ...]:
        return tuple(
            point
            for point in self.interactions
            if point.source_timeline_id == timeline_id
        )

    def outgoing_of(self, timeline_id: str) -> tuple[EdgeInfo, ...]:
        targets = set(self.nodes[timeline_id].children)
        return tuple(
            edge
            for edge in self.edges.values()
            if edge.target_timeline_id in targets
        )


__all__ = [
    "BUILTIN_THEME",
    "MAX_SUPPORTED_SCHEMA_VERSION",
    "MIN_SUPPORTED_SCHEMA_VERSION",
    "PRESENTATION_SCHEMA_VERSION",
    "TONE_VALUES",
    "Bundle",
    "BundleMeta",
    "EdgeInfo",
    "InteractionPoint",
    "NodeInfo",
    "OptionRef",
    "Presentation",
    "Theme",
    "expand_hex_color",
    "hex_to_rgb_triplet",
    "is_hex_color",
]
