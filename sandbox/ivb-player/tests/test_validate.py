# -*- coding: utf-8 -*-
"""规范 §4 图约束的逐条裁判测试。每条坏包都必须点名到具体 id。"""

from __future__ import annotations

import json

import pytest

from ivb_player.format.reader import inspect_bundle, read_bundle
from ivb_player.testing import BundleSpec, write_bundle_dir, write_bundle_zip


def codes(diagnostics) -> set[str]:
    return {item.code for item in diagnostics}


def inspect_dir(tmp_path, name, **kwargs):
    return inspect_bundle(
        write_bundle_dir(tmp_path / name, BundleSpec(**kwargs))
    )


# --- 每条致命规则都要能点名 ---------------------------------------------


@pytest.mark.parametrize(
    "breach, expected",
    [
        ("drop_meta_title", "META_FIELD_MISSING"),
        ("bad_version", "MANIFEST_VERSION_UNSUPPORTED"),
        ("missing_entry", "ENTRY_UNKNOWN"),
        ("cycle", "CYCLE_DETECTED"),
        ("unreachable", "UNREACHABLE_NODE"),
        ("unreachable", "MULTIPLE_ROOTS"),
        ("branch_without_interaction", "BRANCH_WITHOUT_INTERACTION"),
        ("branch_without_interaction", "NOT_INTERACTIVE"),
        ("single_option", "TOO_FEW_OPTIONS"),
        ("dangling_edge_ref", "EDGE_REF_UNRESOLVED"),
        ("bad_default_edge", "DEFAULT_EDGE_INVALID"),
        ("tone_out_of_range", "TONE_UNKNOWN"),
        ("nodes_segments_mismatch", "NODES_SEGMENTS_MISMATCH"),
        ("bad_accent", "ACCENT_MALFORMED"),
        ("ending_flag_mismatch", "ENDING_FLAG_MISMATCH"),
    ],
)
def test_breach_is_reported_by_name(tmp_path, breach, expected):
    inspection = inspect_dir(tmp_path, "b", breaches=(breach,))
    assert inspection.bundle is None, f"{breach} 应当致命"
    assert expected in codes(inspection.fatal), [
        str(d) for d in inspection.diagnostics
    ]


def test_cycle_diagnostic_shows_the_actual_loop(tmp_path):
    inspection = inspect_dir(tmp_path, "cyc", breaches=("cycle",))
    fatal = next(d for d in inspection.fatal if d.code == "CYCLE_DETECTED")
    assert " -> " in fatal.message
    assert "timeline:open" in fatal.message


@pytest.mark.parametrize("entry", ["dir", "zip"])
def test_missing_segment_file_is_fatal(tmp_path, entry):
    kwargs = {"breaches": ("missing_segment_file",)}
    inspection = (
        inspect_bundle(write_bundle_dir(tmp_path / "m", BundleSpec(**kwargs)))
        if entry == "dir"
        else inspect_bundle(
            write_bundle_zip(tmp_path / "m.zip", BundleSpec(**kwargs))
        )
    )
    assert "SEGMENT_MISSING" in codes(inspection.fatal)
    assert "timeline:bad_end" in str(inspection.fatal)


@pytest.mark.parametrize("entry", ["dir", "zip"])
def test_empty_segment_is_fatal(tmp_path, entry):
    kwargs = {"breaches": ("empty_segment",)}
    inspection = (
        inspect_bundle(write_bundle_dir(tmp_path / "e", BundleSpec(**kwargs)))
        if entry == "dir"
        else inspect_bundle(
            write_bundle_zip(tmp_path / "e.zip", BundleSpec(**kwargs))
        )
    )
    assert "SEGMENT_EMPTY" in codes(inspection.fatal)


# --- 不变式:bundle 非空 <=> 无致命诊断 ---------------------------------


@pytest.mark.parametrize(
    "breaches",
    [
        (),
        ("cycle",),
        ("branch_without_interaction",),
        ("dangling_edge_ref",),
        ("bad_accent",),
        ("missing_segment_file",),
    ],
)
def test_bundle_presence_matches_fatal_presence(tmp_path, breaches):
    inspection = inspect_dir(tmp_path, "inv", breaches=breaches)
    assert (inspection.bundle is None) == bool(inspection.fatal)


# --- 告警不阻断 -----------------------------------------------------------


def test_orphan_segment_is_a_warning_only(tmp_path):
    directory = write_bundle_dir(tmp_path / "orph", BundleSpec())
    (directory / "segments" / "timeline_leftover.mp4").write_bytes(b"x" * 4096)
    inspection = inspect_bundle(directory)
    assert "SEGMENT_ORPHAN" in codes(inspection.warnings)
    assert not inspection.fatal
    assert inspection.bundle is not None


def test_titles_divergence_is_a_warning(tmp_path):
    directory = write_bundle_dir(tmp_path / "div", BundleSpec())
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    manifest["titles"]["timeline:open"] = "旧标题"
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    inspection = inspect_bundle(directory)
    assert "TITLES_DIVERGED" in codes(inspection.warnings)
    assert inspection.bundle is not None


def test_at_seconds_overrun_does_not_kill_the_bundle(tmp_path):
    """成片真实长度由 compose 决定,导出前不可靠预算 —— 越界只能提醒,
    不能废包(Creator 真实包的 at_seconds 就是 88.0 这种大值)。"""

    inspection = inspect_dir(
        tmp_path, "overrun", breaches=("at_seconds_overrun",)
    )
    assert "AT_SECONDS_OUT_OF_RANGE" in codes(inspection.warnings)
    assert not inspection.fatal
    assert inspection.bundle is not None


def test_undurable_segment_downgrades_to_warning(tmp_path):
    directory = write_bundle_dir(tmp_path / "ud", BundleSpec())
    (directory / "manifest.json").write_text(
        (directory / "manifest.json").read_text("utf-8"),
        encoding="utf-8",
    )
    # 把入口分段的 mvhd 抹掉:at_seconds 无法核对,但包仍可放映
    target = directory / "segments" / "timeline_open.mp4"
    payload = bytearray(target.read_bytes())
    index = payload.find(b"mvhd")
    payload[index + 1 : index + 5] = b"junk"
    target.write_bytes(bytes(payload))
    inspection = inspect_bundle(directory)
    assert "SEGMENT_UNDURABLE" in codes(inspection.warnings)
    assert "AT_SECONDS_OUT_OF_RANGE" not in codes(inspection.diagnostics)


# --- 合法形态不能被误杀 ---------------------------------------------------


def test_single_node_linear_bundle_is_valid(tmp_path):
    spec = BundleSpec(
        entry="timeline:only",
        durations={"timeline:only": 10.0},
        node_titles={"timeline:only": "单段"},
        children={"timeline:only": []},
        interactions=[],
    )
    bundle = read_bundle(write_bundle_dir(tmp_path / "lin", spec))
    assert bundle.endings == ("timeline:only",)


def test_diamond_reconvergence_is_valid(tmp_path):
    """A 型两路在 C 汇合:DAG 允许汇合,只要汇合点只有一个入边来源集合。"""

    spec = BundleSpec(
        durations={
            "timeline:open": 20.0,
            "timeline:counter": 15.0,
            "timeline:storage": 15.0,
            "timeline:meet": 12.0,
        },
        node_titles={
            "timeline:open": "序章",
            "timeline:counter": "柜台",
            "timeline:storage": "仓库",
            "timeline:meet": "汇合",
        },
        children={
            "timeline:open": ["timeline:counter", "timeline:storage"],
            "timeline:counter": ["timeline:meet"],
            "timeline:storage": ["timeline:meet"],
            "timeline:meet": [],
        },
        interactions=BundleSpec().interactions,
    )
    bundle = read_bundle(write_bundle_dir(tmp_path / "dia", spec))
    assert bundle.nodes["timeline:meet"].children == ()
    assert bundle.endings == ("timeline:meet",)


def test_missing_presentation_is_silent(tmp_path):
    inspection = inspect_dir(tmp_path, "nopres")
    assert inspection.bundle is not None
    assert inspection.bundle.presentation.present is False
    assert codes(inspection.diagnostics) == set()


def test_unanswered_branch_point_lists_its_own_id(tmp_path):
    """两个分岔、只有一个有抉择点:必须只点名缺的那个。"""

    spec = BundleSpec(
        durations={
            "timeline:open": 20.0,
            "timeline:counter": 20.0,
            "timeline:storage": 10.0,
            "timeline:good_end": 10.0,
            "timeline:bad_end": 10.0,
        },
        node_titles={
            "timeline:open": "序章",
            "timeline:counter": "柜台",
            "timeline:storage": "仓库",
            "timeline:good_end": "好",
            "timeline:bad_end": "坏",
        },
        children={
            "timeline:open": ["timeline:counter", "timeline:storage"],
            "timeline:counter": ["timeline:good_end", "timeline:bad_end"],
            "timeline:storage": [],
            "timeline:good_end": [],
            "timeline:bad_end": [],
        },
        interactions=BundleSpec().interactions,
    )
    inspection = inspect_bundle(write_bundle_dir(tmp_path / "half", spec))
    fatal = [
        d for d in inspection.fatal if d.code == "BRANCH_WITHOUT_INTERACTION"
    ]
    assert [d.where for d in fatal] == ["nodes[timeline:counter]"]
