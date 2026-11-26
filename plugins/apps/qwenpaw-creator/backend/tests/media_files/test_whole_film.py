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


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_join_preserves_mixed_size_and_silent_episodes(tmp_path: Path):
    first, second, film = (
        tmp_path / name for name in ("first.mp4", "second.mp4", "film.mp4")
    )
    for path, streams in (
        (first, "color=red:s=96x64:d=1"),
        (second, "color=blue:s=64x96:d=1[out0];sine=duration=1[out1]"),
    ):
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                streams,
                str(path),
            ],
            check=True,
        )
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
