# -*- coding: utf-8 -*-
"""内容层结构校验。规范见 ``docs/bundle-format.md`` §4。

规则分两档:``fatal`` 让包不可放映,``warning`` 只提示。全部诊断都点名到
具体 id / 文件,绝不出现"包无效"这种无法行动的结论。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import errors
from .errors import Diagnostic
from .model import TONE_VALUES, Bundle
from .reader import BundleSource, probe_mp4_duration

#: mp4 少于这个字节数必然是占位文件(空 write 出来的)。
MIN_SEGMENT_BYTES = 1024


def collect_durations(
    source: BundleSource,
    segments: Mapping[str, str],
    sink: list[Diagnostic],
) -> dict[str, float]:
    """逐分段探测时长。探不到只降级(见 :func:`probe_mp4_duration`)。"""

    durations: dict[str, float] = {}
    for timeline_id, path in segments.items():
        duration = probe_mp4_duration(source, path)
        if duration is None:
            sink.append(
                errors.make(
                    "SEGMENT_UNDURABLE",
                    f"segments[{timeline_id}]",
                    path=path,
                ),
            )
            continue
        durations[timeline_id] = duration
    return durations


def _check_shape(
    bundle: Bundle,
    sink: list[Diagnostic],
) -> None:
    if set(bundle.nodes) != set(bundle.segments):
        only_nodes = sorted(set(bundle.nodes) - set(bundle.segments))
        only_segments = sorted(set(bundle.segments) - set(bundle.nodes))
        sink.append(
            errors.make(
                "NODES_SEGMENTS_MISMATCH",
                "manifest",
                only_in_nodes=", ".join(only_nodes) or "(无)",
                only_in_segments=", ".join(only_segments) or "(无)",
            ),
        )
    if bundle.entry_timeline_id not in bundle.nodes:
        sink.append(
            errors.make(
                "ENTRY_UNKNOWN",
                "manifest.entry_timeline_id",
                value=bundle.entry_timeline_id or "(空)",
            ),
        )
    for timeline_id, node in bundle.nodes.items():
        where = f"nodes[{timeline_id}]"
        for child in node.children:
            if child not in bundle.nodes:
                sink.append(
                    errors.make("UNKNOWN_CHILD", where, value=child),
                )
        if node.is_ending != (not node.children):
            sink.append(
                errors.make(
                    "ENDING_FLAG_MISMATCH",
                    where,
                    is_ending=node.is_ending,
                    children=len(node.children),
                ),
            )
        if len(set(node.children)) != len(node.children):
            sink.append(
                errors.make(
                    "UNKNOWN_CHILD", where, detail="children 含重复项"
                ),
            )


def _check_edges(
    bundle: Bundle,
    sink: list[Diagnostic],
) -> None:
    for edge_id, edge in bundle.edges.items():
        where = f"edge_index[{edge_id}]"
        if edge.target_timeline_id not in bundle.nodes:
            sink.append(
                errors.make(
                    "EDGE_TARGET_UNKNOWN",
                    where,
                    value=edge.target_timeline_id,
                ),
            )
        if edge.tone is not None and edge.tone not in TONE_VALUES:
            sink.append(
                errors.make(
                    "TONE_UNKNOWN",
                    f"{where}.tone",
                    value=edge.tone,
                    allowed="|".join(TONE_VALUES),
                ),
            )
    if bundle.nodes and not bundle.edges and _is_branching(bundle):
        for timeline_id, node in bundle.nodes.items():
            if len(node.children) > 1:
                sink.append(
                    errors.make(
                        "EDGE_REF_UNRESOLVED",
                        f"nodes[{timeline_id}].children",
                        detail="分岔节点没有任何边",
                    ),
                )


def _is_branching(bundle: Bundle) -> bool:
    return any(len(node.children) > 1 for node in bundle.nodes.values())


def _check_interactions(
    bundle: Bundle,
    sink: list[Diagnostic],
    durations: Mapping[str, float],
) -> None:
    seen_by_source: dict[str, list[float]] = {}
    for position, point in enumerate(bundle.interactions):
        where = f"interactions[{position}]"
        if point.source_timeline_id not in bundle.nodes:
            sink.append(
                errors.make(
                    "INTERACTION_SOURCE_UNKNOWN",
                    f"{where}.source_timeline_id",
                    value=point.source_timeline_id or "(空)",
                ),
            )
        if not point.question.strip():
            sink.append(errors.make("QUESTION_EMPTY", f"{where}.question"))
        if len(point.options) < 2:
            sink.append(
                errors.make(
                    "TOO_FEW_OPTIONS",
                    where,
                    count=len(point.options),
                ),
            )
        refs = point.option_edges()
        if len(set(refs)) != len(refs):
            sink.append(
                errors.make("DUPLICATE_OPTION", where, refs=list(refs))
            )
        for option in point.options:
            if option.edge_ref not in bundle.edges:
                sink.append(
                    errors.make(
                        "EDGE_REF_UNRESOLVED",
                        f"{where}.options",
                        value=option.edge_ref,
                    ),
                )
        if point.default_edge_ref is not None and (
            point.default_edge_ref not in refs
        ):
            sink.append(
                errors.make(
                    "DEFAULT_EDGE_INVALID",
                    f"{where}.default_edge_ref",
                    value=point.default_edge_ref,
                ),
            )
        duration = durations.get(point.source_timeline_id)
        if duration is not None and not 0 <= point.at_seconds < duration:
            sink.append(
                errors.make(
                    "AT_SECONDS_OUT_OF_RANGE",
                    f"{where}.at_seconds",
                    at_seconds=point.at_seconds,
                    segment_duration=round(duration, 3),
                ),
            )
        history = seen_by_source.setdefault(point.source_timeline_id, [])
        if history and point.at_seconds < history[-1]:
            sink.append(
                errors.make(
                    "INTERACTION_ORDER_UNSTABLE",
                    f"{where}.at_seconds",
                    source=point.source_timeline_id,
                    previous=history[-1],
                    current=point.at_seconds,
                ),
            )
        history.append(point.at_seconds)

    covered = {point.source_timeline_id for point in bundle.interactions}
    for timeline_id, node in bundle.nodes.items():
        if len(node.children) > 1 and timeline_id not in covered:
            sink.append(
                errors.make(
                    "BRANCH_WITHOUT_INTERACTION",
                    f"nodes[{timeline_id}]",
                    branches=len(node.children),
                ),
            )
    if _is_branching(bundle) and not bundle.interactions:
        sink.append(
            errors.make("NOT_INTERACTIVE", "manifest.interactions"),
        )


def _check_graph(
    bundle: Bundle,
    sink: list[Diagnostic],
) -> None:
    """DAG 三约束:无环、单根、全可达。"""

    adjacency = {
        timeline_id: [
            child for child in node.children if child in bundle.nodes
        ]
        for timeline_id, node in bundle.nodes.items()
    }

    white, grey, black = 0, 1, 2
    colors: dict[str, int] = dict.fromkeys(adjacency, white)
    stack: list[str] = []

    def visit(current: str) -> list[str] | None:
        colors[current] = grey
        stack.append(current)
        for child in adjacency.get(current, []):
            if colors.get(child) == grey:
                return stack[stack.index(child) :] + [child]
            if colors.get(child) == white:
                found = visit(child)
                if found:
                    return found
        stack.pop()
        colors[current] = black
        return None

    cyclic: list[str] | None = None
    for start in adjacency:
        if colors[start] == white:
            cyclic = visit(start)
            if cyclic:
                break
    if cyclic:
        sink.append(
            errors.make("CYCLE_DETECTED", "nodes", path=" -> ".join(cyclic)),
        )
        return  # 有环时可达性/入度结论都不可靠,不再叠加噪音

    indegree = dict.fromkeys(adjacency, 0)
    for targets in adjacency.values():
        for target in targets:
            indegree[target] += 1
    for timeline_id in sorted(indegree):
        if (
            indegree[timeline_id] == 0
            and timeline_id != bundle.entry_timeline_id
        ):
            sink.append(
                errors.make("MULTIPLE_ROOTS", f"nodes[{timeline_id}]"),
            )

    if bundle.entry_timeline_id not in adjacency:
        return
    reachable: set[str] = {bundle.entry_timeline_id}
    queue = [bundle.entry_timeline_id]
    while queue:
        current = queue.pop()
        for child in adjacency.get(current, []):
            if child not in reachable:
                reachable.add(child)
                queue.append(child)
    for timeline_id in sorted(adjacency):
        if timeline_id not in reachable:
            sink.append(
                errors.make("UNREACHABLE_NODE", f"nodes[{timeline_id}]"),
            )


def _check_media(
    bundle: Bundle,
    source: BundleSource,
    sink: list[Diagnostic],
) -> None:
    members = source.names()
    for timeline_id, path in bundle.segments.items():
        where = f"segments[{timeline_id}]"
        if path not in members:
            sink.append(errors.make("SEGMENT_MISSING", where, path=path))
            continue
        size = source.size(path)
        if size is not None and size < MIN_SEGMENT_BYTES:
            sink.append(
                errors.make("SEGMENT_EMPTY", where, path=path, bytes=size),
            )
    referenced = set(bundle.segments.values())
    for name in sorted(members):
        if name.startswith("segments/") and name not in referenced:
            sink.append(errors.make("SEGMENT_ORPHAN", name, path=name))


def _check_titles(
    raw: Mapping[str, Any],
    bundle: Bundle,
    sink: list[Diagnostic],
) -> None:
    titles = raw.get("titles")
    if not isinstance(titles, dict):
        return
    for timeline_id, title in titles.items():
        node = bundle.nodes.get(str(timeline_id))
        if node is None or not node.title:
            continue
        if str(title or "") != node.title:
            sink.append(
                errors.make(
                    "TITLES_DIVERGED",
                    f"titles[{timeline_id}]",
                    titles=str(title or ""),
                    nodes=node.title,
                ),
            )


def validate_bundle(
    bundle: Bundle,
    source: BundleSource | None = None,
    durations: Mapping[str, float] | None = None,
    raw_manifest: Mapping[str, Any] | None = None,
) -> list[Diagnostic]:
    """跑完 §4 全部规则。顺序刻意先形状后引用,引用先于图,避免级联噪音。"""

    found: list[Diagnostic] = []
    _check_shape(bundle, found)
    _check_edges(bundle, found)
    _check_interactions(bundle, found, durations or {})
    _check_graph(bundle, found)
    _check_titles(raw_manifest or {}, bundle, found)
    if source is not None:
        _check_media(bundle, source, found)
    return found


def summarize(diagnostics: list[Diagnostic]) -> dict[str, int]:
    counts = {"fatal": 0, "warning": 0}
    for item in diagnostics:
        counts["fatal" if item.is_fatal else "warning"] += 1
    return counts


__all__ = ["collect_durations", "summarize", "validate_bundle"]
