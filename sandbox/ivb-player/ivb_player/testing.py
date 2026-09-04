# -*- coding: utf-8 -*-
"""包工厂:产出结构合法(或按开关故意损坏)的 IVB 包。

用途有二:
1. 测试夹具 —— 端到端回归不必调用模型;
2. ``ivb demo`` 命令行 —— 手边没有 Creator 导出时,也能立刻把放映端跑起来。

生成的 ``.mp4`` 不是真视频,只带一个合法的 ``ftyp``/``moov``/``mvhd`` 头,
足以让 :func:`ivb_player.format.reader.probe_mp4_duration` 读出时长。
"""

from __future__ import annotations

import json
import shutil
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TIMESCALE = 1000


def fake_mp4(duration_seconds: float = 12.0, *, pad: int = 2048) -> bytes:
    """最小可探测的 mp4 头 + 填充字节。"""

    duration = max(1, int(round(duration_seconds * _TIMESCALE)))
    mvhd_body = (
        b"\x00"  # version
        + b"\x00\x00\x00"  # flags
        + struct.pack(">II", 0, 0)  # creation / modification time
        + struct.pack(">I", _TIMESCALE)  # timescale
        + struct.pack(">I", duration)  # duration
        + b"\x00" * 20  # rate + volume + reserved + matrix
    )
    mvhd = struct.pack(">I", len(mvhd_body) + 8) + b"mvhd" + mvhd_body
    moov = struct.pack(">I", len(mvhd) + 8) + b"moov" + mvhd
    ftyp = (
        struct.pack(">I", 24)
        + b"ftyp"
        + b"isom"
        + struct.pack(">I", 512)
        + b"isomiso2"
    )
    free = struct.pack(">I", pad) + b"free" + b"\x00" * pad
    return ftyp + moov + free


@dataclass(slots=True)
class OptionSpec:
    edge_id: str
    target: str
    label: str = "选项"
    prompt: str = ""
    tone: str | None = None


@dataclass(slots=True)
class InteractionSpec:
    source: str
    at_seconds: float
    question: str
    options: list[OptionSpec]
    countdown_seconds: float | None = None
    default_edge_ref: str | None = None


@dataclass(slots=True)
class BundleSpec:
    """描述一个包。默认值就是一棵合法的三岔悬疑树。"""

    bundle_id: str = "project-smoke-0001"
    title: str = "深夜便利店"
    tagline: str = "每一盏灯都在撒谎"
    synopsis: str = "四名夜班店员,一段被剪掉的监控。"
    accent: str = "#b8ff2e"
    entry: str = "timeline:open"
    durations: dict[str, float] = field(
        default_factory=lambda: {
            "timeline:open": 20.0,
            "timeline:counter": 15.0,
            "timeline:storage": 15.0,
            "timeline:good_end": 10.0,
            "timeline:bad_end": 10.0,
        },
    )
    node_titles: dict[str, str] = field(
        default_factory=lambda: {
            "timeline:open": "序章 · 打烊前",
            "timeline:counter": "柜台后面",
            "timeline:storage": "仓库里",
            "timeline:good_end": "结局 · 天亮了",
            "timeline:bad_end": "结局 · 门铃再响",
        },
    )
    children: dict[str, list[str]] = field(
        default_factory=lambda: {
            "timeline:open": ["timeline:counter", "timeline:storage"],
            "timeline:counter": ["timeline:good_end"],
            "timeline:storage": ["timeline:bad_end"],
            "timeline:good_end": [],
            "timeline:bad_end": [],
        },
    )
    interactions: list[InteractionSpec] = field(
        default_factory=lambda: [
            InteractionSpec(
                source="timeline:open",
                at_seconds=18.0,
                question="监控死角里那个人,你要提醒同事吗?",
                countdown_seconds=8.0,
                default_edge_ref="edge:go_counter",
                options=[
                    OptionSpec(
                        "edge:go_counter",
                        "timeline:counter",
                        "走向柜台",
                        "你假装整理货架",
                        "safe",
                    ),
                    OptionSpec(
                        "edge:go_storage",
                        "timeline:storage",
                        "去仓库查看",
                        "灯没开",
                        "risky",
                    ),
                ],
            ),
        ],
    )
    presentation: dict[str, Any] | None = None
    include_index: bool = False
    #: 破坏性开关,仅测试用。键见 :func:`_apply_breach`。
    breaches: tuple[str, ...] = ()


def _segment_name(timeline_id: str) -> str:
    return f"segments/{timeline_id.replace(':', '_')}.mp4"


def build_manifest(spec: BundleSpec) -> dict[str, Any]:
    edge_index: dict[str, dict[str, Any]] = {}
    interactions: list[dict[str, Any]] = []
    for point in spec.interactions:
        options = []
        for option in point.options:
            edge: dict[str, Any] = {
                "label": option.label,
                "prompt": option.prompt,
                "target_timeline_id": option.target,
            }
            if option.tone is not None:
                edge["tone"] = option.tone
            edge_index[option.edge_id] = edge
            options.append({"edge_ref": option.edge_id, "hotspot": None})
        interactions.append(
            {
                "source_timeline_id": point.source,
                "at_seconds": point.at_seconds,
                "question": point.question,
                "options": options,
                "countdown_seconds": point.countdown_seconds,
                "default_edge_ref": point.default_edge_ref,
            },
        )
    nodes = {
        timeline_id: {
            "title": spec.node_titles.get(timeline_id, ""),
            "synopsis": "",
            "children": list(spec.children.get(timeline_id, [])),
            "is_ending": not spec.children.get(timeline_id, []),
        }
        for timeline_id in spec.durations
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "entry_timeline_id": spec.entry,
        "segments": {
            timeline_id: _segment_name(timeline_id)
            for timeline_id in spec.durations
        },
        "interactions": interactions,
        "edge_index": edge_index,
        "titles": {
            timeline_id: spec.node_titles.get(timeline_id, timeline_id)
            for timeline_id in spec.durations
        },
        "meta": {
            "bundle_id": spec.bundle_id,
            "title": spec.title,
            "tagline": spec.tagline,
            "synopsis": spec.synopsis,
            "accent": spec.accent,
        },
        "nodes": nodes,
    }
    return _apply_breaches(manifest, spec)


def _apply_breaches(
    manifest: dict[str, Any],
    spec: BundleSpec,
) -> dict[str, Any]:
    #: 文件级破坏不碰 manifest,由 write_* 自己处理。
    file_level = {"missing_segment_file", "empty_segment"}
    for breach in (b for b in spec.breaches if b not in file_level):
        if breach == "drop_meta_title":
            manifest["meta"].pop("title")
        elif breach == "bad_version":
            manifest["schema_version"] = 99
        elif breach == "missing_entry":
            manifest["entry_timeline_id"] = "timeline:nowhere"
        elif breach == "cycle":
            manifest["nodes"]["timeline:counter"]["children"] = [
                "timeline:open"
            ]
            manifest["nodes"]["timeline:counter"]["is_ending"] = False
        elif breach == "unreachable":
            # 仓库也指向 good_end => bad_end 入度 0且不可达(成菱形汇合)。
            manifest["nodes"]["timeline:storage"]["children"] = [
                "timeline:good_end",
            ]
            manifest["nodes"]["timeline:bad_end"]["children"] = []
        elif breach == "branch_without_interaction":
            manifest["interactions"] = []
        elif breach == "single_option":
            point = manifest["interactions"][0]
            point["options"] = point["options"][:1]
        elif breach == "dangling_edge_ref":
            manifest["interactions"][0]["options"][0][
                "edge_ref"
            ] = "edge:ghost"
        elif breach == "bad_default_edge":
            manifest["interactions"][0]["default_edge_ref"] = "edge:ghost"
        elif breach == "tone_out_of_range":
            next(iter(manifest["edge_index"]))  # 确认非空
            manifest["edge_index"]["edge:go_counter"]["tone"] = "heroic"
        elif breach == "at_seconds_overrun":
            manifest["interactions"][0]["at_seconds"] = 9999.0
        elif breach == "nodes_segments_mismatch":
            manifest["nodes"].pop("timeline:bad_end")
        elif breach == "bad_accent":
            manifest["meta"]["accent"] = "neon"
        elif breach == "ending_flag_mismatch":
            manifest["nodes"]["timeline:good_end"]["is_ending"] = False
        else:  # pragma: no cover - 测试写错码时立刻暴露
            raise KeyError(f"unknown breach {breach!r}")
    return manifest


def write_bundle_dir(target: Path, spec: BundleSpec | None = None) -> Path:
    """把包写成目录(放映端同样支持目录入口)。"""

    spec = spec or BundleSpec()
    if target.exists():
        shutil.rmtree(target)
    (target / "segments").mkdir(parents=True)
    manifest = build_manifest(spec)
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for timeline_id, path in manifest["segments"].items():
        (target / path).write_bytes(
            fake_mp4(spec.durations.get(timeline_id, 10.0))
        )
    if spec.presentation is not None:
        (target / "presentation.json").write_text(
            json.dumps(spec.presentation, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if spec.include_index:
        (target / "index.html").write_text(
            "<!DOCTYPE html><title>smoke</title>",
            encoding="utf-8",
        )
    if "missing_segment_file" in spec.breaches:
        (target / "segments" / "timeline_bad_end.mp4").unlink()
    if "empty_segment" in spec.breaches:
        (target / "segments" / "timeline_counter.mp4").write_bytes(b"")
    return target


def write_bundle_zip(target: Path, spec: BundleSpec | None = None) -> Path:
    """打包成 zip。``target`` 是 ``.zip`` 路径。"""

    spec = spec or BundleSpec()
    staging = target.with_suffix(".staging")
    write_bundle_dir(staging, spec)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(staging).as_posix())
    shutil.rmtree(staging)
    return target


DEMO_THEME_CSS = """/* styles/demo.css —— presentation.stylesheets 的示例覆盖 */
#choice-overlay .choice-card{border-radius:18px}
"""


def demo_spec() -> BundleSpec:
    spec = BundleSpec()
    spec.presentation = {
        "schema_version": 1,
        "theme": {"accent": "#ff8ad8", "danger": "#ff3355"},
        "screens": {"choice": {"layout": "list"}, "map": {"reveal_depth": 1}},
        "stylesheets": ["styles/demo.css"],
    }
    return spec


def write_demo_bundle(target: Path) -> Path:
    """给 CLI 用的开箱包(带 presentation 覆盖)。"""

    spec = demo_spec()
    directory = target.with_suffix(".dir")
    write_bundle_dir(directory, spec)
    (directory / "styles").mkdir(exist_ok=True)
    (directory / "styles" / "demo.css").write_text(
        DEMO_THEME_CSS, encoding="utf-8"
    )
    if target.suffix != ".zip":
        return directory
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory).as_posix())
    shutil.rmtree(directory, ignore_errors=True)
    return target


__all__ = [
    "BundleSpec",
    "InteractionSpec",
    "OptionSpec",
    "build_manifest",
    "demo_spec",
    "fake_mp4",
    "write_bundle_dir",
    "write_bundle_zip",
    "write_demo_bundle",
]
