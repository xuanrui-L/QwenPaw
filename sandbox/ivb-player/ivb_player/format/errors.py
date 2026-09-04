# -*- coding: utf-8 -*-
"""诊断码字典。

规范 ``docs/bundle-format.md`` 里出现的每个诊断码都必须在这里登记,
否则 :func:`Diagnostic.make` 会直接抛错 —— 这条约束保证代码和文档不脱节。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """``fatal`` 阻断加载;``warning`` 只提示,包仍可放映。"""

    FATAL = "fatal"
    WARNING = "warning"


#: 诊断码 -> (级别, 人话说明)。与 bundle-format.md §2/§4/§5 一一对应。
CODEBOOK: dict[str, tuple[Severity, str]] = {
    # --- 包入口 / 读取 ---
    "BUNDLE_NOT_FOUND": (Severity.FATAL, "包路径不存在"),
    "BUNDLE_UNREADABLE": (Severity.FATAL, "不是合法的 zip 或目录"),
    "PATH_ESCAPE": (Severity.FATAL, "包内路径越界(绝对路径 / .. / 反斜杠)"),
    "MANIFEST_MISSING": (Severity.FATAL, "缺少 manifest.json"),
    "MANIFEST_UNREADABLE": (Severity.FATAL, "manifest.json 不是合法 JSON"),
    "MANIFEST_VERSION_UNSUPPORTED": (
        Severity.FATAL,
        "schema_version 不在放映端支持区间内",
    ),
    "MANIFEST_FIELD_MISSING": (Severity.FATAL, "顶层必需字段缺失"),
    "MANIFEST_FIELD_TYPE": (Severity.FATAL, "字段类型与规范不符"),
    "META_FIELD_MISSING": (Severity.FATAL, "meta 必需字段缺失"),
    "BUNDLE_ID_MALFORMED": (Severity.WARNING, "bundle_id 含非路径安全字符"),
    "ACCENT_MALFORMED": (Severity.FATAL, "meta.accent 不是 3/6 位 hex"),
    # --- 图结构 ---
    "ENTRY_UNKNOWN": (Severity.FATAL, "entry_timeline_id 不在 nodes 中"),
    "NODES_SEGMENTS_MISMATCH": (
        Severity.FATAL,
        "nodes 与 segments 的键集合不一致",
    ),
    "NODE_FIELD_MISSING": (Severity.FATAL, "节点缺少必需字段"),
    "UNKNOWN_CHILD": (Severity.FATAL, "children 指向未知节点"),
    "EDGE_TARGET_UNKNOWN": (
        Severity.FATAL,
        "edge_index 的目标节点不存在",
    ),
    "EDGE_REF_UNRESOLVED": (Severity.FATAL, "option.edge_ref 找不到边"),
    "EDGE_FIELD_MISSING": (Severity.FATAL, "边缺少必需字段"),
    "CYCLE_DETECTED": (Severity.FATAL, "故事图存在环,不是 DAG"),
    "UNREACHABLE_NODE": (Severity.FATAL, "节点从入口不可达"),
    "MULTIPLE_ROOTS": (Severity.FATAL, "存在入口之外的入度为 0 节点"),
    "ENDING_FLAG_MISMATCH": (
        Severity.FATAL,
        "is_ending 与 children 是否为空不一致",
    ),
    # --- 分段素材 ---
    "SEGMENT_MISSING": (Severity.FATAL, "分段文件不在包里"),
    "SEGMENT_EMPTY": (Severity.FATAL, "分段文件长度为 0"),
    "SEGMENT_UNDURABLE": (
        Severity.WARNING,
        "取不到分段时长,at_seconds 无法核对",
    ),
    "SEGMENT_ORPHAN": (Severity.WARNING, "包内有未被引用的 mp4"),
    # --- 抉择点 ---
    "INTERACTION_SOURCE_UNKNOWN": (
        Severity.FATAL,
        "source_timeline_id 不在 nodes 中",
    ),
    "TOO_FEW_OPTIONS": (Severity.FATAL, "选项数 < 2"),
    "DUPLICATE_OPTION": (Severity.FATAL, "同一抉择点内 edge_ref 重复"),
    "QUESTION_EMPTY": (Severity.FATAL, "question 为空"),
    "DEFAULT_EDGE_INVALID": (
        Severity.FATAL,
        "default_edge_ref 不是该抉择点的选项之一",
    ),
    # 成片真实长度由 compose 决定,导出前 Creator 侧无法可靠预算;Creator 的
    # 测试里 at_seconds=88.0 就是一个合规的中后段抉择点。因此越界只说明“建议
    # 看一眼”,不能拿来废包。
    "AT_SECONDS_OUT_OF_RANGE": (
        Severity.WARNING,
        "at_seconds 超出分段探测时长",
    ),
    "INTERACTION_ORDER_UNSTABLE": (
        Severity.FATAL,
        "同一节点内的抉择点未按 at_seconds 升序",
    ),
    "BRANCH_WITHOUT_INTERACTION": (
        Severity.FATAL,
        "分岔节点上没有任何抉择点,其余分支将永久不可达",
    ),
    "NOT_INTERACTIVE": (
        Severity.FATAL,
        "分支型包但 interactions 为空,没有可点的选择",
    ),
    "TONE_UNKNOWN": (Severity.FATAL, "tone 取值不在 safe|risky|danger 内"),
    # --- 兼容字段 ---
    "TITLES_DIVERGED": (
        Severity.WARNING,
        "titles 与 nodes[*].title 不一致",
    ),
    # --- 表现层 ---
    "PRESENTATION_UNREADABLE": (
        Severity.WARNING,
        "presentation.json 存在但不是合法 JSON",
    ),
    "PRESENTATION_VERSION_UNSUPPORTED": (
        Severity.WARNING,
        "presentation.schema_version 不受支持,整体回退默认",
    ),
    "THEME_COLOR_MALFORMED": (Severity.WARNING, "theme 某项不是合法颜色"),
    "STYLESHEET_MISSING": (
        Severity.WARNING,
        "stylesheets 指向的 CSS 不在包里",
    ),
    "SCREEN_FIELD_UNKNOWN": (Severity.WARNING, "screens 含未知字段,忽略"),
    "SCREEN_LAYOUT_UNSUPPORTED": (
        Severity.WARNING,
        "choice.layout 在 v1 只支持 list",
    ),
}


class UnknownDiagnosticCode(KeyError):
    """代码里用了未登记的诊断码。"""


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """一条结构诊断结果。``where`` 让报错能点名到具体的 id / 文件 / 字段。"""

    code: str
    severity: Severity
    where: str
    message: str

    @property
    def is_fatal(self) -> bool:
        return self.severity is Severity.FATAL

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "where": self.where,
            "message": self.message,
        }

    def __str__(self) -> str:
        return (
            f"[{self.severity.value}] {self.code} @ {self.where}: "
            f"{self.message}"
        )


def make(
    code: str,
    where: str,
    detail: str = "",
    **context: Any,
) -> Diagnostic:
    """按码表构造诊断。未登记的码直接抛错,防止代码与文档悄悄分叉。"""

    try:
        severity, blurb = CODEBOOK[code]
    except KeyError as exc:  # pragma: no cover - 开发期保护
        raise UnknownDiagnosticCode(
            f"diagnostic code {code!r} is not registered in CODEBOOK; "
            "add it to ivb_player/format/errors.py and document it in "
            "docs/bundle-format.md",
        ) from exc
    message = blurb if not detail else f"{blurb}:{detail}"
    if context:
        rendered = ", ".join(
            f"{key}={value!r}" for key, value in context.items()
        )
        message = f"{message}({rendered})"
    return Diagnostic(
        code=code, severity=severity, where=where, message=message
    )


__all__ = [
    "CODEBOOK",
    "Diagnostic",
    "Severity",
    "UnknownDiagnosticCode",
    "make",
]
