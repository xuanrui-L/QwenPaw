# -*- coding: utf-8 -*-
"""Unit tests for the extension material-prefix trim.

ffmpeg is always faked here; the only real-binary coverage lives in the
server acceptance run, per the house rule against real external calls
in default-collected tests.
"""

# pylint: disable=unused-argument,use-implicit-booleaness-not-comparison

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.errors import ValidationError
from services.media_files import extension_trim as et
from services.media_files.secure_video_stream import MaterializedVideo


def _mv(path: Path) -> MaterializedVideo:
    return MaterializedVideo(
        path=path,
        sha256="0" * 64,
        size_bytes=path.stat().st_size,
        media_type="video/mp4",
        container="mp4",
        source_kind="remote",
    )


def _wire(monkeypatch, src_name: str, total: float, rest: float):
    def fake_probe(path, ffmpeg_path=None):  # noqa: ARG001
        return SimpleNamespace(
            duration_seconds=total if Path(path).name == src_name else rest,
        )

    monkeypatch.setattr(et, "probe_media", fake_probe)
    monkeypatch.setattr(et, "resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")


def test_trim_replaces_descriptor_with_cut_bytes(
    tmp_path,
    monkeypatch,
) -> None:
    """Success path: new path/sha/size, input bytes untouched, seek
    placed BEFORE -i so the cut is keyframe-free re-encode."""

    src = tmp_path / "response.mp4"
    src.write_bytes(b"full-response")
    _wire(monkeypatch, src.name, 13.0, 8.0)
    seen: dict = {}

    def fake_run(cmd, **kwargs):  # noqa: ARG001
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"tail-only")

    monkeypatch.setattr(et.media_gate, "run", fake_run)
    out = et.trim_extension_prefix(_mv(src), 5.0)

    assert out.path != src
    assert src.read_bytes() == b"full-response"
    assert out.size_bytes == len(b"tail-only")
    assert out.sha256 == hashlib.sha256(b"tail-only").hexdigest()
    cmd = seen["cmd"]
    assert cmd[cmd.index("-ss") + 1] == "5.000000"
    assert cmd.index("-ss") < cmd.index("-i")
    assert "-c:v" in cmd and "libx264" in cmd


def test_trim_rejects_nonpositive_offset(tmp_path) -> None:
    src = tmp_path / "response.mp4"
    src.write_bytes(b"x")
    with pytest.raises(ValidationError):
        et.trim_extension_prefix(_mv(src), 0.0)


def test_trim_refuses_response_shorter_than_material(
    tmp_path,
    monkeypatch,
) -> None:
    """A provider response no longer than the material prefix cannot
    contain any new segment; publishing it would corrupt the timeline."""

    src = tmp_path / "response.mp4"
    src.write_bytes(b"x")
    _wire(monkeypatch, src.name, 3.0, 0.0)
    with pytest.raises(ValidationError):
        et.trim_extension_prefix(_mv(src), 5.0)


def test_trim_failure_leaves_no_scratch_residue(
    tmp_path,
    monkeypatch,
) -> None:
    src = tmp_path / "response.mp4"
    src.write_bytes(b"x")
    _wire(monkeypatch, src.name, 13.0, 8.0)

    def explode(cmd, **kwargs):  # noqa: ARG001
        raise OSError("ffmpeg vanished")

    monkeypatch.setattr(et.media_gate, "run", explode)
    with pytest.raises(ValidationError):
        et.trim_extension_prefix(_mv(src), 5.0)
    assert list(tmp_path.glob("exttrim-*")) == []
