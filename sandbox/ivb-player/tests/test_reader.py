# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from ivb_player.format.reader import (
    BundleError,
    DirBundleSource,
    ZipBundleSource,
    inspect_bundle,
    is_safe_member_name,
    open_source,
    probe_mp4_duration,
    read_bundle,
)
from ivb_player.testing import BundleSpec, fake_mp4, write_bundle_zip


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
    from ivb_player.testing import write_bundle_dir

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
                source.stream(name, len(whole) - 8, len(whole) - 1)
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
