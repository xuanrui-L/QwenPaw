# -*- coding: utf-8 -*-
"""Reading IVB bundles and validating their media, graph, and HTML."""

from __future__ import annotations

import ast
import json
import zipfile
from pathlib import Path

import pytest

from ivb.format.interaction_html import validate_interaction_html
from ivb.format.presentation_html import validate_presentation_html
from ivb.format.reader import (
    BundleError,
    DirBundleSource,
    ZipBundleSource,
    inspect_bundle,
    is_safe_member_name,
    open_source,
    probe_mp4_duration,
    read_bundle,
)
from ivb.testing import (
    BundleSpec,
    fake_mp4,
    write_bundle_dir,
    write_bundle_zip,
)


def codes(diagnostics) -> set[str]:
    return {item.code for item in diagnostics}


def inspect_dir(tmp_path, name, **kwargs):
    return inspect_bundle(
        write_bundle_dir(tmp_path / name, BundleSpec(**kwargs)),
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
            write_bundle_zip(tmp_path / "m.zip", BundleSpec(**kwargs)),
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
            write_bundle_zip(tmp_path / "e.zip", BundleSpec(**kwargs)),
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


def test_at_seconds_overrun_is_fatal(tmp_path):
    """成片真实长度由 compose 决定,导出前不可靠预算 —— 越界只能提醒,
    不能废包(Creator 真实包的 at_seconds 就是 88.0 这种大值)。"""

    inspection = inspect_dir(
        tmp_path,
        "overrun",
        breaches=("at_seconds_overrun",),
    )
    assert "AT_SECONDS_OUT_OF_RANGE" in codes(inspection.fatal)
    assert inspection.bundle is None


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


def test_member_name_guard_rejects_escapes():
    assert is_safe_member_name("segments/a.mp4")
    for bad in (
        "/etc/passwd",
        "../secret",
        "a/../../b",
        "C:\\x",
        "",
        "a//b",
        "./a",
    ):
        assert not is_safe_member_name(bad), bad


@pytest.mark.parametrize("kind", ["dir", "zip"])
def test_both_entries_agree_on_a_clean_bundle(kind, tmp_path):
    path = (
        write_bundle_zip(tmp_path / "b.zip", BundleSpec())
        if kind == "zip"
        else _dir(tmp_path)
    )
    bundle = read_bundle(path)
    assert bundle.entry_timeline_id == "timeline:open"
    assert bundle.bundle_id == "project-smoke-0001"
    assert bundle.endings == ("timeline:good_end", "timeline:bad_end")
    assert bundle.nodes["timeline:open"].children == (
        "timeline:counter",
        "timeline:storage",
    )
    assert bundle.edges["edge:go_storage"].tone == "risky"
    assert len(bundle.interactions) == 1


def _dir(tmp_path: Path) -> Path:
    from ivb.testing import write_bundle_dir

    return write_bundle_dir(tmp_path / "b.dir", BundleSpec())


def test_source_roundtrip_reads_identical_bytes(tmp_path):
    spec = BundleSpec()
    zip_path = write_bundle_zip(tmp_path / "x.zip", spec)
    dir_path = _dir(tmp_path)
    with (
        ZipBundleSource(zip_path) as zipped,
        DirBundleSource(dir_path) as flat,
    ):
        assert zipped.names() == flat.names()
        for name in ("manifest.json", "segments/timeline_open.mp4"):
            assert zipped.read_bytes(name) == flat.read_bytes(name)
        assert zipped.size("manifest.json") == flat.size("manifest.json")


def test_stream_matches_full_read_for_both_sources(tmp_path):
    spec = BundleSpec()
    zip_path = write_bundle_zip(tmp_path / "s.zip", spec)
    dir_path = _dir(tmp_path)
    name = "segments/timeline_open.mp4"
    for source_path in (zip_path, dir_path):
        with open_source(source_path) as source:
            whole = source.read_bytes(name)
            head = b"".join(source.stream(name, 0, 19))
            tail = b"".join(
                source.stream(name, len(whole) - 8, len(whole) - 1),
            )
            mid = b"".join(source.stream(name, 30, 70))
        assert head == whole[:20]
        assert tail == whole[-8:]
        assert mid == whole[30:71]


def test_probe_reads_declared_duration(tmp_path):
    (tmp_path / "a.mp4").write_bytes(fake_mp4(20.0))
    with DirBundleSource(tmp_path) as source:
        assert probe_mp4_duration(source, "a.mp4") == pytest.approx(20.0)


def test_probe_returns_none_for_garbage(tmp_path):
    (tmp_path / "b.mp4").write_bytes(b"not a mp4 at all" * 200)
    with DirBundleSource(tmp_path) as source:
        assert probe_mp4_duration(source, "b.mp4") is None


def test_missing_manifest_is_fatal(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    inspection = inspect_bundle(empty)
    assert inspection.bundle is None
    assert [d.code for d in inspection.fatal] == ["MANIFEST_MISSING"]


def test_broken_json_is_fatal(tmp_path):
    directory = _dir(tmp_path)
    (directory / "manifest.json").write_text("{nope", encoding="utf-8")
    inspection = inspect_bundle(directory)
    assert [d.code for d in inspection.fatal] == ["MANIFEST_UNREADABLE"]


def test_nonexistent_path_raises(tmp_path):
    with pytest.raises(BundleError) as exc:
        inspect_bundle(tmp_path / "ghost.zip")
    assert exc.value.diagnostics[0].code == "BUNDLE_NOT_FOUND"


def test_bad_zip_is_fatal(tmp_path):
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"PK\x03\x04 but truncated")
    with pytest.raises(BundleError) as exc:
        inspect_bundle(junk)
    assert exc.value.diagnostics[0].code == "BUNDLE_UNREADABLE"


def test_unsupported_version_reports_supported_range(tmp_path):
    path = write_bundle_zip(
        tmp_path / "v.zip",
        BundleSpec(breaches=("bad_version",)),
    )
    inspection = inspect_bundle(path)
    fatal = [
        d for d in inspection.fatal if d.code == "MANIFEST_VERSION_UNSUPPORTED"
    ]
    assert fatal
    assert "99" in fatal[0].message
    assert "1" in fatal[0].message


def test_unknown_extra_fields_are_ignored(tmp_path):
    directory = _dir(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    manifest["future_thing"] = {"whatever": True}
    manifest["nodes"]["timeline:open"]["brand_new_field"] = 1
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    bundle = read_bundle(directory)
    assert bundle.entry_timeline_id == "timeline:open"


def test_zip_member_with_escaping_name_is_flagged(tmp_path):
    target = tmp_path / "evil.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("../outside.txt", "nope")
    inspection = inspect_bundle(target)
    assert any(d.code == "PATH_ESCAPE" for d in inspection.diagnostics)


def test_inspection_report_shape(tmp_path):
    inspection = inspect_bundle(_dir(tmp_path))
    report = inspection.as_report()
    assert report["ok"] is True
    assert report["counts"]["nodes"] == 5
    assert report["counts"]["endings"] == 2
    assert report["bundle_id"] == "project-smoke-0001"


@pytest.mark.parametrize("presentation", [False, True])
@pytest.mark.parametrize(
    "prefix,suffix",
    [("这是完整的页面。\n```html\n", ""), ("", "\n```\n### 优化建议")],
)
def test_rejects_commentary_around_html(presentation, prefix, suffix):
    if presentation:
        html = (
            (Path(__file__).parent / "fixtures/authored-presentation.html")
            .read_text()
            .replace("__NODES__", "")
        )
        validate = validate_presentation_html
    else:
        html = '<html><body><button data-edge-ref="a">A</button></body></html>'
        validate = lambda value: validate_interaction_html(value, ["a"])
    assert not validate(html)
    assert "text outside the HTML document is forbidden" in validate(
        prefix + html + suffix,
    )
    assert not validate(
        html.replace("</body>", "<p>正文中的优化建议和 ``` 保留。</p></body>"),
    )


def test_legacy_choice_fragment_remains_valid():
    assert not validate_interaction_html(
        '<button data-edge-ref="a">选择这条分支</button>',
        ["a"],
    )


@pytest.mark.parametrize(
    "name", ["interaction_html.py", "presentation_html.py"]
)
def test_html_validator_rules_match_creator(name):
    player_root = Path(__file__).resolve().parents[1]
    creator_backend = player_root.parent / "backend"
    if not creator_backend.is_dir():
        pytest.skip("Standalone player checkout has no Creator source")

    def validator_rules(source: Path) -> str:
        tree = ast.parse(
            source.read_text(encoding="utf-8"), filename=str(source)
        )
        for block in ast.walk(tree):
            if isinstance(
                block,
                (
                    ast.Module,
                    ast.ClassDef,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                ),
            ):
                if ast.get_docstring(block, clean=False) is not None:
                    del block.body[0]
        return ast.dump(tree, include_attributes=False)

    # The dependency-free validators are intentional mirrors. Compare their
    # parsed rules here so this check runs even without Creator installed.
    assert validator_rules(
        player_root / "ivb/format" / name
    ) == validator_rules(
        creator_backend / "services/project_files" / name,
    )
