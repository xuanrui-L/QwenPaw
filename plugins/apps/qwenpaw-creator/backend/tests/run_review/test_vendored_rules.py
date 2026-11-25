# -*- coding: utf-8 -*-
"""Vendored video review probes, binary resolution, and rubric integration."""

# pylint: disable=protected-access

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from services.run_review.rubric_prompts import (
    build_appeal_system_prompt,
    build_image_check_system_prompt,
    build_scene_check_system_prompt,
)
from services.runtime_files import runtime_dependencies
from vendor.media_toolkit import frame_stats, review_gates
from vendor.media_toolkit.review_rubrics import (
    APPEAL_RUBRIC_ROWS,
    SCENE_REVIEW_CHECKS,
)

pytestmark = pytest.mark.unit


def _executable() -> str:
    # Any absolute executable works; the resolver only checks the path.
    return sys.executable


def test_frame_stats_resolves_ffmpeg_through_the_creator_contract(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        runtime_dependencies.CREATOR_FFMPEG_PATH_ENV,
        _executable(),
    )
    # The contract wins even when nothing is on PATH.
    monkeypatch.setattr(frame_stats.shutil, "which", lambda name: None)

    assert frame_stats._require_tool("ffmpeg") == os.path.realpath(
        _executable(),
    )


def test_review_gates_resolve_ffprobe_through_the_creator_contract(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        runtime_dependencies.CREATOR_FFPROBE_PATH_ENV,
        _executable(),
    )
    monkeypatch.setattr(review_gates.shutil, "which", lambda name: None)

    assert review_gates._require_tool("ffprobe") == os.path.realpath(
        _executable(),
    )


def test_frame_stats_still_falls_back_to_path(monkeypatch) -> None:
    monkeypatch.delenv(
        runtime_dependencies.CREATOR_FFMPEG_PATH_ENV,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_dependencies,
        "resolve_ffmpeg",
        lambda: None,
    )
    monkeypatch.setattr(
        frame_stats.shutil,
        "which",
        lambda name: "/usr/local/bin/ffmpeg",
    )

    assert frame_stats._require_tool("ffmpeg") == "/usr/local/bin/ffmpeg"


def test_missing_binary_fails_actionably(monkeypatch) -> None:
    monkeypatch.delenv(
        runtime_dependencies.CREATOR_FFPROBE_PATH_ENV,
        raising=False,
    )
    monkeypatch.delenv(
        runtime_dependencies.CREATOR_FFMPEG_PATH_ENV,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_dependencies,
        "resolve_ffprobe",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        runtime_dependencies,
        "resolve_ffmpeg",
        lambda: None,
    )
    monkeypatch.setattr(frame_stats.shutil, "which", lambda name: None)

    with pytest.raises(frame_stats.FrameStatsError, match="ffprobe"):
        frame_stats._require_tool("ffprobe")

    with pytest.raises(review_gates.ReviewGateError, match="ffmpeg"):
        review_gates._require_tool("ffmpeg")


def test_probe_duration_uses_the_resolved_binary(
    monkeypatch,
    tmp_path,
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_check_output(argv, **kwargs):
        captured["argv"] = argv
        return b"12.5\n"

    monkeypatch.setattr(
        frame_stats.subprocess,
        "check_output",
        fake_check_output,
    )
    monkeypatch.setattr(
        frame_stats,
        "_require_tool",
        lambda name: "/opt/tools/ffprobe",
    )

    assert frame_stats.probe_duration(tmp_path / "clip.mp4") == 12.5
    assert captured["argv"][0] == "/opt/tools/ffprobe"


_FFMPEG = shutil.which("ffmpeg")
requires_ffmpeg = pytest.mark.skipif(_FFMPEG is None, reason="needs ffmpeg")


def _make_clip(path: Path, *, interior_black: bool, silent: bool) -> None:
    audio = (
        "anullsrc=r=44100:cl=mono:d=3"
        if silent
        else "sine=frequency=440:sample_rate=44100:d=3"
    )
    command = [_FFMPEG, "-y", "-hide_banner", "-nostats", "-f", "lavfi"]
    command += ["-i", "color=c=red:s=192x108:d=3"]
    command += ["-f", "lavfi", "-i", audio, "-shortest"]
    command += ["-pix_fmt", "yuv420p"]
    if interior_black:
        command += [
            "-vf",
            "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:"
            "enable='between(t,1,2)'",
        ]
    command.append(str(path))
    subprocess.run(command, check=True, capture_output=True, timeout=120)


@requires_ffmpeg
def test_gates_emit_evidence_and_flag_interior_black(tmp_path: Path) -> None:
    clip = tmp_path / "black.mp4"
    _make_clip(clip, interior_black=True, silent=False)
    block = review_gates.run_review_gates(clip)
    assert [gate.name for gate in block.gates] == [
        "ffprobe",
        "loudness",
        "black",
    ]
    assert len(block.sha1_head12) == 12
    assert block.passed is False
    black = next(gate for gate in block.gates if gate.name == "black")
    gap = black.metrics["interior_gaps"][0]
    assert 0.5 < gap["start"] < 1.5


@requires_ffmpeg
def test_loudness_flags_silence_and_clean_clip_passes(tmp_path: Path) -> None:
    silent = tmp_path / "silent.mp4"
    _make_clip(silent, interior_black=False, silent=True)
    result = review_gates.loudness_gate(silent)
    assert result.passed is False
    assert result.metrics.get("digital_silence") or result.metrics.get(
        "effectively_silent",
    )
    # A clean clip passes the black gate and yields judgeable stats.
    clip = tmp_path / "画面 [red], 'clip'.mp4"
    _make_clip(clip, interior_black=False, silent=False)
    assert review_gates.black_gate(clip).passed is True
    stats = frame_stats.sample_stats(clip)
    assert set(stats) == {"y_mean", "y_range", "sat_mean"}
    judgment = frame_stats.judge_stats(stats)
    assert set(judgment) == {"exposure", "contrast", "saturation"}


def test_appeal_prompt_embeds_verbatim_rows_without_veto() -> None:
    text_prompt = build_appeal_system_prompt("text")
    for row in APPEAL_RUBRIC_ROWS[:3]:
        assert row.anchor_questions in text_prompt
    motion_prompt = build_appeal_system_prompt("motion")
    assert APPEAL_RUBRIC_ROWS[3].anchor_questions in motion_prompt
    scene = build_scene_check_system_prompt()
    for check in SCENE_REVIEW_CHECKS:
        assert check.key in scene
        assert check.description in scene
    image = build_image_check_system_prompt()
    for key in ("devices", "type_fonts", "composition_safety", "craft"):
        assert key in image
