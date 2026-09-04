# -*- coding: utf-8 -*-
"""包阅读器:zip 与目录双入口 + 内容层/表现层解析。

设计要点
--------
1. **路径收敛**。包内路径一律 POSIX 相对路径,读取前校验不越界;zip 条目名
   按字面取,绝不 ``/`` 拼接后再 ``resolve``。
2. **读取与校验分离**。本模块只负责"能不能读出来、读出来长什么样";
   业务规则(引用闭合 / DAG / at_seconds)全在 :mod:`.validate`。
3. **不抛异常地收集错误**。``inspect_bundle`` 永远返回 ``(bundle|None, 诊断列表)``,
   供 ``/api/validate`` 直接输出;``read_bundle`` 是把致命诊断翻成异常的薄壳。
"""

from __future__ import annotations

import json
import struct
import zipfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import errors
from .model import (
    BUILTIN_THEME,
    MAX_SUPPORTED_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    PRESENTATION_SCHEMA_VERSION,
    Bundle,
    BundleMeta,
    EdgeInfo,
    InteractionPoint,
    NodeInfo,
    OptionRef,
    Presentation,
    Theme,
    is_hex_color,
)
from .errors import Diagnostic

MANIFEST_NAME = "manifest.json"
PRESENTATION_NAME = "presentation.json"
INDEX_NAME = "index.html"


class BundleError(ValueError):
    """致命诊断的异常视图。``diagnostics`` 保留完整列表。"""

    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        self.diagnostics = diagnostics
        fatal = [d for d in diagnostics if d.is_fatal]
        super().__init__(
            "; ".join(str(d) for d in fatal) or "bundle is invalid"
        )


def is_safe_member_name(name: str) -> bool:
    """包内条目名的合法性:拒绝绝对路径、``..``、反斜杠、盘符。"""

    if not name or name.startswith("/") or "\\" in name:
        return False
    if len(name) >= 2 and name[1] == ":":  # windows 盘符
        return False
    parts = name.split("/")
    return all(part not in ("", ".", "..") for part in parts)


class BundleSource(ABC):
    """只读的包内容来源。"""

    #: 人类可读的来源标识,进诊断的 ``where``。
    label: str

    @abstractmethod
    def names(self) -> set[str]:
        """包内全部文件条目名(不含目录条目)。"""

    @abstractmethod
    def read_bytes(self, name: str) -> bytes:
        """整读一个条目。调用方保证 ``name`` 已通过 :func:`is_safe_member_name`。"""

    def close(self) -> None:  # pragma: no cover - 目录实现无需清理
        return None

    def __enter__(self) -> "BundleSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def read_text(self, name: str) -> str:
        return self.read_bytes(name).decode("utf-8", errors="replace")

    def read_json(self, name: str) -> Any:
        return json.loads(self.read_text(name))

    def size(self, name: str) -> int | None:
        try:
            return len(self.read_bytes(name))
        except (KeyError, OSError):  # pragma: no cover - 覆盖两个实现的异常
            return None

    def stream(self, name: str, start: int, end: int) -> Iterator[bytes]:
        """闭区间 ``[start, end]`` 的字节流。分段响应 HTTP Range 用。"""

        yield self.read_bytes(name)[start : end + 1]


class DirBundleSource(BundleSource):
    """解包后的目录入口(开发期直接指向 Creator 的输出目录)。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.label = str(root)

    def names(self) -> set[str]:
        found: set[str] = set()
        for path in self.root.rglob("*"):
            if path.is_file():
                found.add(path.relative_to(self.root).as_posix())
        return found

    def _member(self, name: str) -> Path:
        candidate = (self.root / name).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise errors_path_escape(name)
        return candidate

    def read_bytes(self, name: str) -> bytes:
        return self._member(name).read_bytes()

    def size(self, name: str) -> int | None:
        try:
            return self._member(name).stat().st_size
        except OSError:
            return None

    def stream(self, name: str, start: int, end: int) -> Iterator[bytes]:
        path = self._member(name)
        chunk = 1 << 20
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = handle.read(min(chunk, remaining))
                if not data:
                    return
                remaining -= len(data)
                yield data


class ZipBundleSource(BundleSource):
    """zip 入口。条目名按字面查表,不做路径运算。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.label = str(path)
        self._archive = zipfile.ZipFile(path)
        self._members = {
            info.filename
            for info in self._archive.infolist()
            if not info.is_dir()
        }

    def names(self) -> set[str]:
        return set(self._members)

    def read_bytes(self, name: str) -> bytes:
        return self._archive.read(name)

    def size(self, name: str) -> int | None:
        try:
            return self._archive.getinfo(name).file_size
        except KeyError:
            return None

    def stream(self, name: str, start: int, end: int) -> Iterator[bytes]:
        chunk = 1 << 20
        with self._archive.open(name) as handle:
            offset = 0
            if handle.seekable() and start:
                handle.seek(start)
                offset = start
            while offset < start:  # 不可 seek 时顺序丢弃
                skip = min(chunk, start - offset)
                data = handle.read(skip)
                if not data:
                    return
                offset += len(data)
            remaining = end - offset + 1
            while remaining > 0:
                data = handle.read(min(chunk, remaining))
                if not data:
                    return
                remaining -= len(data)
                yield data

    def close(self) -> None:
        self._archive.close()


def errors_path_escape(
    name: str,
) -> BundleError:  # pragma: no cover - 兜底分支
    return BundleError([errors.make("PATH_ESCAPE", name, f"条目名={name!r}")])


def open_source(path: str | Path) -> BundleSource:
    """按入口类型选择实现。zip 文件 -> :class:`ZipBundleSource`,目录 ->
    :class:`DirBundleSource`,其余抛 :class:`BundleError`。"""

    target = Path(path)
    if not target.exists():
        raise BundleError([errors.make("BUNDLE_NOT_FOUND", str(target))])
    if target.is_dir():
        return DirBundleSource(target)
    try:
        return ZipBundleSource(target)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BundleError(
            [
                errors.make(
                    "BUNDLE_UNREADABLE",
                    str(target),
                    f"{type(exc).__name__}: {exc}",
                )
            ],
        ) from exc


# --------------------------------------------------------------------------
# MP4 时长探测(纯 stdlib,不依赖 ffprobe)
# --------------------------------------------------------------------------


def probe_mp4_duration(source: BundleSource, name: str) -> float | None:
    """从 ``mvhd`` box 读时长。只取头部 2 MB,读不到返回 ``None``。

    放映端不要求 moov 前置(Creator 出的包一般已 faststart,但第三方包
    不一定),所以读失败只降级为警告。
    """

    try:
        blob = source.read_bytes(name)[: 2 * 1024 * 1024]
    except (KeyError, OSError):
        return None
    index = blob.find(b"mvhd")
    if index < 0:
        return None
    body = blob[index + 4 :]
    if len(body) < 4:
        return None
    version = body[0]
    try:
        if version == 1:
            if len(body) < 32:
                return None
            timescale, duration = struct.unpack(">IQ", body[20:32])
        else:
            if len(body) < 20:
                return None
            timescale, duration = struct.unpack(">II", body[12:20])
    except struct.error:  # pragma: no cover - 长度已核
        return None
    if not timescale or not duration:
        return None
    return duration / timescale


# --------------------------------------------------------------------------
# manifest / presentation 解析
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Inspection:
    """一次读取的结果。``bundle`` 为 ``None`` 时必然伴随致命诊断。"""

    source_label: str
    member_names: frozenset[str]
    raw_manifest: dict[str, Any]
    bundle: Bundle | None
    diagnostics: list[Diagnostic]
    durations: dict[str, float]

    @property
    def fatal(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.is_fatal]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if not d.is_fatal]

    def as_report(self) -> dict[str, Any]:
        return {
            "source": self.source_label,
            "ok": self.bundle is not None,
            "bundle_id": self.bundle.bundle_id if self.bundle else None,
            "schema_version": self.raw_manifest.get("schema_version"),
            "counts": (
                {
                    "nodes": len(self.bundle.nodes),
                    "segments": len(self.bundle.segments),
                    "edges": len(self.bundle.edges),
                    "interactions": len(self.bundle.interactions),
                    "endings": len(self.bundle.endings),
                }
                if self.bundle
                else {}
            ),
            "diagnostics": [d.as_dict() for d in self.diagnostics],
        }


def _check_top_level(
    raw: dict[str, Any],
    where: str,
    sink: list[Diagnostic],
) -> bool:
    if not isinstance(raw, dict):
        sink.append(
            errors.make("MANIFEST_FIELD_TYPE", where, "顶层不是对象"),
        )
        return False
    ok = True
    version = raw.get("schema_version")
    if version is None:
        sink.append(
            errors.make(
                "MANIFEST_FIELD_MISSING", where, field="schema_version"
            ),
        )
        ok = False
    elif not isinstance(version, int) or not (
        MIN_SUPPORTED_SCHEMA_VERSION <= version <= MAX_SUPPORTED_SCHEMA_VERSION
    ):
        sink.append(
            errors.make(
                "MANIFEST_VERSION_UNSUPPORTED",
                where,
                supported=(
                    f"{MIN_SUPPORTED_SCHEMA_VERSION}"
                    if MIN_SUPPORTED_SCHEMA_VERSION
                    == MAX_SUPPORTED_SCHEMA_VERSION
                    else (
                        f"{MIN_SUPPORTED_SCHEMA_VERSION}~"
                        f"{MAX_SUPPORTED_SCHEMA_VERSION}"
                    )
                ),
                got=version,
            ),
        )
        ok = False
    for key in (
        "entry_timeline_id",
        "segments",
        "nodes",
        "interactions",
        "meta",
    ):
        if key not in raw:
            sink.append(
                errors.make("MANIFEST_FIELD_MISSING", where, field=key)
            )
            ok = False
    if not isinstance(raw.get("segments"), dict):
        sink.append(
            errors.make("MANIFEST_FIELD_TYPE", where, field="segments")
        )
        ok = False
    if not isinstance(raw.get("nodes"), dict):
        sink.append(errors.make("MANIFEST_FIELD_TYPE", where, field="nodes"))
        ok = False
    if not isinstance(raw.get("interactions"), list):
        sink.append(
            errors.make("MANIFEST_FIELD_TYPE", where, field="interactions")
        )
        ok = False
    if not isinstance(raw.get("meta"), dict):
        sink.append(errors.make("MANIFEST_FIELD_TYPE", where, field="meta"))
        ok = False
    if not isinstance(raw.get("edge_index", {}), dict):
        sink.append(
            errors.make("MANIFEST_FIELD_TYPE", where, field="edge_index")
        )
        ok = False
    return ok


def _parse_meta(
    raw: dict[str, Any],
    where: str,
    sink: list[Diagnostic],
) -> BundleMeta:
    meta_raw = _as_dict(raw.get("meta"))
    bundle_id = str(meta_raw.get("bundle_id") or "")
    if not bundle_id:
        sink.append(
            errors.make("META_FIELD_MISSING", where, field="bundle_id")
        )
    if bundle_id and not BundleMeta(bundle_id, "", "").is_path_safe:
        sink.append(errors.make("BUNDLE_ID_MALFORMED", where, value=bundle_id))
    title = str(meta_raw.get("title") or "")
    if not title:
        sink.append(errors.make("META_FIELD_MISSING", where, field="title"))
    accent = str(meta_raw.get("accent") or "")
    if not is_hex_color(accent):
        sink.append(
            errors.make("ACCENT_MALFORMED", where, value=accent or "(空)"),
        )
        accent = Theme.accent
    return BundleMeta(
        bundle_id=bundle_id,
        title=title,
        tagline=str(meta_raw.get("tagline") or ""),
        synopsis=str(meta_raw.get("synopsis") or ""),
        accent=accent,
    )


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _parse_nodes(
    raw: dict[str, Any],
    sink: list[Diagnostic],
) -> dict[str, NodeInfo]:
    nodes: dict[str, NodeInfo] = {}
    for timeline_id, item in _as_dict(raw.get("nodes")).items():
        where = f"nodes[{timeline_id}]"
        if not isinstance(item, dict):
            sink.append(
                errors.make("NODE_FIELD_MISSING", where, "节点不是对象")
            )
            continue
        children_raw = item.get("children", [])
        if not isinstance(children_raw, list):
            sink.append(
                errors.make("MANIFEST_FIELD_TYPE", where, field="children"),
            )
            children_raw = []
        children = tuple(str(child) for child in children_raw)
        is_ending = bool(item.get("is_ending", False))
        nodes[str(timeline_id)] = NodeInfo(
            timeline_id=str(timeline_id),
            title=str(item.get("title") or ""),
            synopsis=str(item.get("synopsis") or ""),
            children=children,
            is_ending=is_ending,
        )
    return nodes


def _parse_edges(
    raw: dict[str, Any],
    sink: list[Diagnostic],
) -> dict[str, EdgeInfo]:
    edges: dict[str, EdgeInfo] = {}
    for edge_id, item in _as_dict(raw.get("edge_index")).items():
        where = f"edge_index[{edge_id}]"
        if not isinstance(item, dict):
            sink.append(errors.make("EDGE_FIELD_MISSING", where, "边不是对象"))
            continue
        target = str(item.get("target_timeline_id") or "")
        if not target:
            sink.append(
                errors.make(
                    "EDGE_FIELD_MISSING", where, field="target_timeline_id"
                ),
            )
        tone = item.get("tone")
        edges[str(edge_id)] = EdgeInfo(
            edge_id=str(edge_id),
            label=str(item.get("label") or ""),
            prompt=str(item.get("prompt") or ""),
            target_timeline_id=target,
            tone=str(tone) if tone is not None else None,
        )
    return edges


def _parse_interactions(
    raw: dict[str, Any],
    sink: list[Diagnostic],
) -> tuple[InteractionPoint, ...]:
    points: list[InteractionPoint] = []
    for position, item in enumerate(_as_list(raw.get("interactions"))):
        where = f"interactions[{position}]"
        if not isinstance(item, dict):
            sink.append(
                errors.make("MANIFEST_FIELD_TYPE", where, "抉择点不是对象")
            )
            continue
        options: list[OptionRef] = []
        for option_raw in _as_list(item.get("options")):
            if isinstance(option_raw, dict) and option_raw.get("edge_ref"):
                hotspot = option_raw.get("hotspot")
                options.append(
                    OptionRef(
                        edge_ref=str(option_raw["edge_ref"]),
                        hotspot=hotspot if isinstance(hotspot, dict) else None,
                    ),
                )
            else:
                sink.append(
                    errors.make(
                        "EDGE_REF_UNRESOLVED",
                        where,
                        "选项缺少 edge_ref",
                    ),
                )
        countdown = item.get("countdown_seconds")
        try:
            at_seconds = float(item.get("at_seconds", 0.0))
        except (TypeError, ValueError):
            sink.append(
                errors.make("MANIFEST_FIELD_TYPE", where, field="at_seconds"),
            )
            at_seconds = 0.0
        points.append(
            InteractionPoint(
                source_timeline_id=str(item.get("source_timeline_id") or ""),
                at_seconds=at_seconds,
                question=str(item.get("question") or ""),
                options=tuple(options),
                countdown_seconds=(
                    float(countdown) if countdown is not None else None
                ),
                default_edge_ref=(
                    str(item["default_edge_ref"])
                    if item.get("default_edge_ref") is not None
                    else None
                ),
            ),
        )
    return tuple(points)


def _parse_presentation(
    source: BundleSource,
    sink: list[Diagnostic],
) -> Presentation:
    if not source.names() or PRESENTATION_NAME not in source.names():
        return Presentation()
    where = PRESENTATION_NAME
    try:
        raw = source.read_json(PRESENTATION_NAME)
    except (ValueError, OSError, KeyError) as exc:
        sink.append(
            errors.make("PRESENTATION_UNREADABLE", where, type(exc).__name__),
        )
        return Presentation(present=True)
    if not isinstance(raw, dict):
        sink.append(
            errors.make("PRESENTATION_UNREADABLE", where, "顶层不是对象")
        )
        return Presentation(present=True)
    version = raw.get("schema_version")
    if version != PRESENTATION_SCHEMA_VERSION:
        sink.append(
            errors.make(
                "PRESENTATION_VERSION_UNSUPPORTED",
                where,
                got=version,
            ),
        )
        return Presentation(present=True)

    theme_fields = {
        "accent": "accent",
        "danger": "danger",
        "warning": "warning",
        "success": "success",
        "background": "background",
        "surface": "surface",
        "surface_alt": "surface_alt",
        "text": "text",
        "text_dim": "text_dim",
        "fog": "fog",
    }
    overrides: dict[str, str] = {}
    theme_raw = raw.get("theme") or {}
    if isinstance(theme_raw, dict):
        for key, field_name in theme_fields.items():
            value = theme_raw.get(key)
            if value is None:
                continue
            if is_hex_color(str(value)):
                overrides[field_name] = str(value)
            else:
                sink.append(
                    errors.make(
                        "THEME_COLOR_MALFORMED",
                        f"{where}.theme.{key}",
                        value=str(value),
                    ),
                )
    theme = Theme(**overrides) if overrides else BUILTIN_THEME

    screens: dict[str, Any] = {}
    screens_raw = raw.get("screens") or {}
    if isinstance(screens_raw, dict):
        for screen, config in screens_raw.items():
            if not isinstance(config, dict):
                sink.append(
                    errors.make(
                        "SCREEN_FIELD_UNKNOWN",
                        f"{where}.screens.{screen}",
                        detail="屏配置不是对象",
                    ),
                )
                continue
            screens[str(screen)] = dict(config)
    _normalize_screens(screens, where, sink)

    stylesheets: list[str] = []
    styles_raw = raw.get("stylesheets") or []
    if isinstance(styles_raw, list):
        members = source.names()
        for entry in styles_raw:
            name = str(entry or "")
            if not name or not is_safe_member_name(name):
                sink.append(
                    errors.make(
                        "PATH_ESCAPE", f"{where}.stylesheets", value=name
                    )
                )
                continue
            if name not in members:
                sink.append(
                    errors.make(
                        "STYLESHEET_MISSING",
                        f"{where}.stylesheets",
                        value=name,
                    )
                )
                continue
            stylesheets.append(name)
    return Presentation(
        present=True,
        theme=theme,
        screens=screens,
        stylesheets=tuple(stylesheets),
    )


def _normalize_screens(
    screens: dict[str, Any],
    where: str,
    sink: list[Diagnostic],
) -> None:
    from .model import SCREEN_FIELDS  # 局部导入避开循环

    for screen in list(screens):
        allowed = SCREEN_FIELDS.get(screen)
        if allowed is None:
            sink.append(
                errors.make(
                    "SCREEN_FIELD_UNKNOWN",
                    f"{where}.screens.{screen}",
                    detail="未知屏幕",
                ),
            )
            screens.pop(screen)
            continue
        config = screens[screen]
        for key in list(config):
            if key not in allowed:
                sink.append(
                    errors.make(
                        "SCREEN_FIELD_UNKNOWN",
                        f"{where}.screens.{screen}.{key}",
                    ),
                )
                config.pop(key)
    choice = screens.get("choice")
    if isinstance(choice, dict):
        layout = choice.get("layout")
        if layout is not None and layout != "list":
            sink.append(
                errors.make(
                    "SCREEN_LAYOUT_UNSUPPORTED",
                    f"{where}.screens.choice.layout",
                    got=str(layout),
                ),
            )
            choice["layout"] = "list"


def inspect_bundle(path: str | Path) -> Inspection:
    """读出包 + 收集诊断。永不抛异常(除文件不存在)。"""

    source = open_source(path)
    diagnostics: list[Diagnostic] = []
    try:
        members = source.names()
    except (OSError, zipfile.BadZipFile) as exc:
        return Inspection(
            source_label=source.label,
            member_names=frozenset(),
            raw_manifest={},
            bundle=None,
            diagnostics=[
                errors.make(
                    "BUNDLE_UNREADABLE",
                    source.label,
                    f"{type(exc).__name__}: {exc}",
                ),
            ],
            durations={},
        )

    for name in sorted(members):
        if not is_safe_member_name(name):
            diagnostics.append(
                errors.make("PATH_ESCAPE", source.label, value=name)
            )

    where = f"{source.label}/{MANIFEST_NAME}"
    if MANIFEST_NAME not in members:
        source.close()
        return Inspection(
            source_label=source.label,
            member_names=frozenset(members),
            raw_manifest={},
            bundle=None,
            diagnostics=diagnostics + [errors.make("MANIFEST_MISSING", where)],
            durations={},
        )
    try:
        raw = source.read_json(MANIFEST_NAME)
    except ValueError as exc:
        source.close()
        return Inspection(
            source_label=source.label,
            member_names=frozenset(members),
            raw_manifest={},
            bundle=None,
            diagnostics=diagnostics
            + [errors.make("MANIFEST_UNREADABLE", where, str(exc))],
            durations={},
        )

    shape_ok = _check_top_level(raw, where, diagnostics)
    meta = _parse_meta(raw, where, diagnostics)
    nodes = _parse_nodes(raw, diagnostics)
    edges = _parse_edges(raw, diagnostics)
    interactions = _parse_interactions(raw, diagnostics)
    presentation = _parse_presentation(source, diagnostics)

    segments = {
        str(timeline_id): str(path_value)
        for timeline_id, path_value in _as_dict(raw.get("segments")).items()
    }
    durations: dict[str, float] = {}
    if shape_ok:
        from .validate import collect_durations  # 延后导入避开循环

        durations = collect_durations(source, segments, diagnostics)

    bundle: Bundle | None = None
    if shape_ok:
        bundle = Bundle(
            meta=meta,
            schema_version=int(
                raw.get("schema_version", MIN_SUPPORTED_SCHEMA_VERSION)
            ),
            entry_timeline_id=str(raw.get("entry_timeline_id") or ""),
            nodes=nodes,
            segments=segments,
            edges=edges,
            interactions=interactions,
            presentation=presentation,
        )
        from .validate import validate_bundle  # 延后导入避开循环

        diagnostics.extend(
            validate_bundle(bundle, source, durations, raw),
        )
        # 不变式:bundle 非空 <=> 无致命诊断。read_bundle 依赖这一条。
        if any(item.is_fatal for item in diagnostics):
            bundle = None

    source.close()
    return Inspection(
        source_label=source.label,
        member_names=frozenset(members),
        raw_manifest=raw if isinstance(raw, dict) else {},
        bundle=bundle,
        diagnostics=diagnostics,
        durations=durations,
    )


def read_bundle(path: str | Path) -> Bundle:
    """只要有效包;有任何致命诊断即抛 :class:`BundleError`。"""

    inspection = inspect_bundle(path)
    if inspection.bundle is None:
        raise BundleError(inspection.diagnostics)
    return inspection.bundle


__all__ = [
    "Bundle",
    "BundleError",
    "BundleSource",
    "DirBundleSource",
    "INDEX_NAME",
    "Inspection",
    "MANIFEST_NAME",
    "PRESENTATION_NAME",
    "ZipBundleSource",
    "inspect_bundle",
    "is_safe_member_name",
    "open_source",
    "probe_mp4_duration",
    "read_bundle",
]
