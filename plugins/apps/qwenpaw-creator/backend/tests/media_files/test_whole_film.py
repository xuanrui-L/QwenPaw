# -*- coding: utf-8 -*-
"""Regression coverage for ordered, versioned multi-episode exports."""

from pathlib import Path
import shutil
import subprocess

import pytest

from domain.errors import ConflictError
from services.media_files.local_execution import FfmpegLocalMediaRunner
from services.media_files.whole_film import (
    materialize_whole_film,
    plan_whole_film,
)
from services.project_files.models import Project, Timeline
from services.runtime_files.media_probe import probe_media

from .conftest import add_final_video


def test_whole_film_uses_current_episodes_in_order(tmp_path, monkeypatch):
    project = Project.new(project_id="p", name="连续剧")
    for tid in ("ep2", "snapshot:v1"):
        project.timelines.items[tid] = Timeline(timeline_id=tid)
    project.timelines.order = ["ep2", "snapshot:v1", "timeline:main"]
    versions = [
        add_final_video(project, "ep2", b"second"),
        add_final_video(project, "timeline:main", b"first"),
    ]
    slots = project.assets.artifact_slots_by_id
    legacy = slots.pop("timeline:ep2:render")
    legacy.slot_id, legacy.kind = "older-slot", "timeline_render"
    slots[legacy.slot_id] = legacy
    project.assets.artifact_versions_by_id[
        versions[0]
    ].slot_id = legacy.slot_id
    plan = plan_whole_film(project, versions)
    for file, data in zip(plan.files, (b"second", b"first")):
        path = tmp_path / file.relative_uri
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def join(_runner, inputs, output, **_kwargs):
        assert not output.exists()
        output.write_bytes(b"|".join(path.read_bytes() for path in inputs))

    with monkeypatch.context() as patch:
        patch.setattr(FfmpegLocalMediaRunner, "join_final_cuts", join)
        film = materialize_whole_film(tmp_path, plan)
    assert film.read_bytes() == b"second|first"
    assert materialize_whole_film(tmp_path, plan) == film

    project.timelines.order.reverse()
    with pytest.raises(ConflictError):
        plan_whole_film(project, versions)
    assert plan_whole_film(project, versions[::-1]).key != plan.key
    project.assets.artifact_versions_by_id[versions[0]].stale = True
    with pytest.raises(ConflictError):
        plan_whole_film(project, versions[::-1])


requires_ffmpeg = pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="ffmpeg is required",
)


def _ffmpeg(*args):
    return subprocess.run(
        ["ffmpeg", "-loglevel", "error", *map(str, args)],
        check=True,
        capture_output=True,
    ).stdout


def _episode(path, streams, *, audio_first=False):
    tracks = ("0:a", "0:v") if audio_first else ("0:v", "0:a?")
    mapping = ["-map", tracks[0], "-map", tracks[1]]
    _ffmpeg("-f", "lavfi", "-i", streams, *mapping, "-c:v", "libx264", path)


def _frame(path, position, filters):
    return _ffmpeg(
        "-ss",
        position,
        "-i",
        path,
        "-vf",
        filters,
        "-frames:v",
        1,
        "-f",
        "rawvideo",
        "-",
    )


@requires_ffmpeg
def test_join_preserves_mixed_size_and_silent_episodes(tmp_path: Path):
    first, second, film = (
        tmp_path / name for name in ("first.mp4", "second.mp4", "film.mp4")
    )
    for path, streams in (
        (first, "color=red:s=96x64:d=1"),
        (second, "color=blue:s=64x96:d=1[out0];sine=duration=1[out1]"),
    ):
        _episode(path, streams)
    FfmpegLocalMediaRunner().join_final_cuts(
        [first, second],
        film,
        work_dir=tmp_path,
    )
    info = probe_media(str(film))
    assert (info.width, info.height) == (96, 64)
    assert info.has_audio and info.duration_seconds == pytest.approx(
        2,
        abs=0.1,
    )


@requires_ffmpeg
@pytest.mark.parametrize(
    ("silence_seconds", "audio_first"),
    [(1, False), (4, True)],
    ids=["short-audio", "long-audio-and-reordered-tracks"],
)
def test_join_keeps_frames_and_sound_on_episode_boundaries(
    tmp_path,
    silence_seconds,
    audio_first,
):
    first, second, film = (
        tmp_path / name for name in ("a.mp4", "b.mp4", "film.mp4")
    )
    _episode(
        first,
        f"color=red:s=96x64:d=3[out0];anullsrc=d={silence_seconds}[out1]",
    )
    _episode(
        second,
        "color=blue:s=96x64:d=3[out0];sine=duration=3[out1]",
        audio_first=audio_first,
    )
    FfmpegLocalMediaRunner().join_final_cuts(
        [first, second],
        film,
        work_dir=tmp_path,
    )
    for position, channel in ((2.75, 0), (3.25, 2), (5.75, 2)):
        pixel = _frame(film, position, "scale=1:1,format=rgb24")
        assert len(pixel) == 3 and pixel[channel] > 200
    pcm = _ffmpeg(
        "-i",
        film,
        "-map",
        "0:a:0",
        "-ac",
        1,
        "-ar",
        8000,
        "-f",
        "u8",
        "-",
    )
    assert len(pcm) / 8000 == pytest.approx(6, abs=0.05)
    for position, audible in (
        (1.5, False),
        (2.75, False),
        (3.25, True),
        (5.75, True),
    ):
        start = int(position * 8000)
        window = pcm[start : start + 800]
        assert (max(window) - min(window) > 10) == audible


@requires_ffmpeg
@pytest.mark.parametrize("rotated_first", [False, True])
def test_join_preserves_each_episodes_display_rotation(
    tmp_path,
    rotated_first,
):
    normal, rotated, film = (
        tmp_path / name for name in ("normal.mp4", "rotated.mp4", "film.mp4")
    )
    _episode(
        normal,
        "color=white:s=96x64:d=1,drawbox=x=0:y=0:w=24:h=ih:color=black:t=fill",
    )
    if b"-display_rotation" in _ffmpeg("-h", "full"):
        _ffmpeg("-display_rotation", 90, "-i", normal, "-c", "copy", rotated)
    else:  # FFmpeg before 6 writes the track matrix from the rotate tag.
        _ffmpeg(
            "-i",
            normal,
            "-c",
            "copy",
            "-metadata:s:v:0",
            "rotate=90",
            rotated,
        )
    episodes = [rotated, normal] if rotated_first else [normal, rotated]
    width, height = (64, 96) if rotated_first else (96, 64)
    FfmpegLocalMediaRunner().join_final_cuts(episodes, film, work_dir=tmp_path)
    info = probe_media(str(film))
    assert (info.width, info.height) == (width, height)
    fit = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
    )
    thumb = "scale=12:12,format=gray"
    for index, episode in enumerate(episodes):
        reference = _frame(episode, 0.5, fit + thumb)
        joined = _frame(film, index + 0.5, thumb)
        assert len(joined) == len(reference) == 144
        assert (
            sum(abs(a - b) for a, b in zip(reference, joined)) / len(joined)
            < 6
        )
