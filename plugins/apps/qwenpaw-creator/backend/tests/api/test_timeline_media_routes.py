# -*- coding: utf-8 -*-
"""Rough-cut composition, media routes, and timeline overlay detection."""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI

from api.dependencies import creator_error_handler, project_file_services
from api.file_execution_routes import (
    _timeline_has_text_overlays_without_motion,
)
from api.router import router
from domain.errors import CreatorError
from services.media_files.rough_cut import (
    RoughCutClip,
    RoughCutError,
    render_rough_cut,
)
from services.project_files.assets import AssetFileStore
from services.project_files.commit import ProjectCommitBoundary
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    ElementLocation,
    OverlayCreation,
    Project,
    Timeline,
    TimelineElement,
    TimelineSpan,
)

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="requires ffmpeg on PATH",
)

PROJECT_ID = "project-1"
TIMELINE_ID = "timeline:main"


def _app(tmp_path):
    services = CreatorFileServices.create(tmp_path.resolve())
    snapshot = services.projects.create(
        Project.new(project_id=PROJECT_ID, name="Rough Cut Project"),
    )
    app = FastAPI()
    app.add_exception_handler(CreatorError, creator_error_handler)
    app.include_router(router)
    app.dependency_overrides[project_file_services] = lambda: services
    return app, services, snapshot


def _make_still_bytes(tmp_path) -> bytes:
    target = tmp_path / "storyboard-src.png"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x240:d=0.1",
            "-frames:v",
            "1",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    return target.read_bytes()


def _commit_storyboard_element(services, snapshot, png_bytes: bytes) -> None:
    """One enabled element whose storyboard slot holds a real indexed PNG."""

    store = AssetFileStore(services.projects.project_root(PROJECT_ID))
    published = store.publish(
        store.stage_bytes(png_bytes, staging_id="storyboard"),
        "assets/artifacts/el-1/storyboard.png",
    )
    raw = snapshot.project.model_dump(mode="json")
    raw["assets"]["files_by_id"]["file-storyboard"] = {
        "file_id": "file-storyboard",
        "kind": "artifact_payload",
        "relative_uri": published.relative_uri,
        "sha256": published.sha256,
        "size_bytes": published.size_bytes,
        "media_type": "image/png",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    raw["assets"]["artifact_versions_by_id"]["storyboard-v1"] = {
        "version_id": "storyboard-v1",
        "slot_id": "element:el-1:storyboard",
        "kind": "r2v_storyboard_image",
        "owner_ref": "element:el-1",
        "name": "storyboard",
        "file_id": "file-storyboard",
        "checksum": published.sha256,
        "based_on_generation": snapshot.generation,
        "provenance_refs": [],
        "thumbnail_file_id": None,
        "duration_seconds": None,
        "input_fingerprint": None,
        "stale": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {},
    }
    raw["assets"]["artifact_slots_by_id"]["element:el-1:storyboard"] = {
        "slot_id": "element:el-1:storyboard",
        "kind": "r2v_storyboard_image",
        "owner_ref": "element:el-1",
        "version_ids": ["storyboard-v1"],
        "selected_version_id": "storyboard-v1",
        "metadata": {},
    }
    raw["timelines"]["items"][TIMELINE_ID]["elements_by_id"] = {
        "el-1": {
            "element_id": "el-1",
            "label": "Scene 1",
            "enabled": True,
            "span": {"start_tick": 0, "duration_tick": 1000},
            "location": None,
            "z_index": 0,
            "creation": {
                "type": "t2v",
                "intent": "",
                "video_prompt": "一只猫",
            },
            "outputs": {},
            "render_source": None,
            "provenance_refs": [],
        },
    }
    ProjectCommitBoundary(services.projects).commit(
        base=snapshot,
        candidate=raw,
        origin="runtime_task",
    )


@pytest.mark.usefixtures("api_runtime_root")
def test_rough_cut_unknown_timeline_is_404(tmp_path, run_scenario):
    app, _services, _snapshot = _app(tmp_path)

    async def scenario(client):
        response = await client.get(
            f"/projects/{PROJECT_ID}/timelines/timeline:nope/rough-cut",
        )
        assert response.status_code == 404

    run_scenario(app, scenario)


@pytest.mark.usefixtures("api_runtime_root")
def test_rough_cut_without_material_fails_closed_409(tmp_path, run_scenario):
    app, _services, _snapshot = _app(tmp_path)

    async def scenario(client):
        response = await client.get(
            f"/projects/{PROJECT_ID}/timelines/{TIMELINE_ID}/rough-cut",
        )
        assert response.status_code == 409
        assert "粗剪素材" in response.json()["message"]

    run_scenario(app, scenario)


@requires_ffmpeg
@pytest.mark.usefixtures("api_runtime_root")
def test_rough_cut_with_storyboard_returns_mp4(tmp_path, run_scenario):
    app, services, snapshot = _app(tmp_path)
    _commit_storyboard_element(services, snapshot, _make_still_bytes(tmp_path))

    async def scenario(client):
        response = await client.get(
            f"/projects/{PROJECT_ID}/timelines/{TIMELINE_ID}/rough-cut",
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "video/mp4"
        payload = response.content
        assert len(payload) > 1000
        assert b"ftyp" in payload[:64]

    run_scenario(app, scenario)


def _services_and_project(tmp_path):
    services = CreatorFileServices.create(tmp_path.resolve())
    snapshot = services.projects.create(
        Project.new(project_id="proj-1", name="Test"),
    )
    return services, snapshot


def _overlay(element_id, *, enabled=True, **creation) -> TimelineElement:
    creation.setdefault("text", "测试文案")
    return TimelineElement(
        element_id=element_id,
        enabled=enabled,
        span=TimelineSpan(start_tick=0, duration_tick=100),
        location=ElementLocation(),
        creation=OverlayCreation(**creation),
    )


def _with_timeline(services, snapshot, timeline):
    updated = snapshot.project.model_dump(mode="json")
    tid = timeline.timeline_id
    updated["timelines"]["items"][tid] = timeline.model_dump(mode="json")
    if tid not in updated["timelines"]["order"]:
        updated["timelines"]["order"].append(tid)
    updated["generation"] = snapshot.generation + 1
    return services.projects.replace(
        "proj-1",
        Project.model_validate(updated),
        expected_etag=snapshot.etag,
    )


def _flag(services) -> bool:
    return _timeline_has_text_overlays_without_motion(
        services,
        "proj-1",
        "tl-1",
    )


_MOTION = {
    "html": "<!doctype html><html><body>styled</body></html>",
    "fps": 24,
    "loop": True,
}


def test_no_text_overlays_returns_false(tmp_path) -> None:
    services, _ = _services_and_project(tmp_path)
    assert _flag(services) is False


@pytest.mark.parametrize(
    ("overlays", "expected"),
    [
        pytest.param(
            {"overlay-1": {"motion": _MOTION}, "overlay-2": {}},
            True,
            id="mixed-motion-and-no-motion",
        ),
        pytest.param(
            {"overlay-1": {"enabled": False}},
            False,
            id="disabled-overlay-ignored",
        ),
        # Text-free decoration overlays never need a caption motion design.
        pytest.param(
            {"overlay-1": {"text": "", "prompt": "decoration"}},
            False,
            id="text-free-decoration-overlay-ignored",
        ),
    ],
)
def test_text_overlay_motion_flag(tmp_path, overlays, expected) -> None:
    services, snapshot = _services_and_project(tmp_path)
    timeline = Timeline(
        timeline_id="tl-1",
        elements_by_id={
            element_id: _overlay(element_id, **kwargs)
            for element_id, kwargs in overlays.items()
        },
    )
    _with_timeline(services, snapshot, timeline)

    assert _flag(services) is expected


def _make_video(path: Path, seconds: float) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:s=320x240:d={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_still(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x240:d=0.1",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _duration_seconds(path_bytes: bytes, workdir: Path) -> float:
    probe_target = workdir / "probe.mp4"
    probe_target.write_bytes(path_bytes)
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(probe_target),
        ],
        check=True,
        capture_output=True,
    )
    return float(completed.stdout.decode().strip())


def test_empty_clip_list_fails_closed() -> None:
    with pytest.raises(RoughCutError, match="没有可用的粗剪素材"):
        render_rough_cut([])


@requires_ffmpeg
def test_rough_cut_concats_video_and_storyboard_still(tmp_path) -> None:
    video = tmp_path / "shot.mp4"
    still = tmp_path / "storyboard.png"
    _make_video(video, seconds=1.0)
    _make_still(still)

    payload = render_rough_cut(
        [
            RoughCutClip(
                element_id="el:1",
                kind="video",
                path=video,
                duration_seconds=1.0,
            ),
            RoughCutClip(
                element_id="el:2",
                kind="still",
                path=still,
                duration_seconds=2.0,
            ),
        ],
    )

    assert payload[:8] != b""
    duration = _duration_seconds(payload, tmp_path)
    # 1s video + 2s held still, allow container rounding.
    assert 2.5 <= duration <= 3.6


@requires_ffmpeg
def test_planned_gaps_and_missing_outputs_keep_full_preview_duration(tmp_path):
    from services.media_files.rough_cut import collect_rough_cut_clips
    from services.project_files.models import (
        Project,
        T2VCreation,
        TimelineElement,
        TimelineSpan,
    )

    project = Project.new(project_id="preview-spans", name="Draft")
    timeline = project.timelines.items["timeline:main"]
    timeline.elements_by_id["one"] = TimelineElement(
        element_id="one",
        label="Opening",
        span=TimelineSpan(start_tick=1000, duration_tick=1000),
        creation=T2VCreation(video_prompt="A cat"),
    )
    timeline.elements_by_id["two"] = TimelineElement(
        element_id="two",
        label="Ending",
        span=TimelineSpan(start_tick=3000, duration_tick=1000),
        creation=T2VCreation(video_prompt="A dog"),
    )
    clips = collect_rough_cut_clips(
        project,
        timeline,
        resolve_file=lambda _: tmp_path / "unused",
    )
    assert [clip.element_id for clip in clips] == ["gap", "one", "gap", "two"]
    assert sum(clip.duration_seconds for clip in clips) == 4
    assert abs(_duration_seconds(render_rough_cut(clips), tmp_path) - 4) < 0.05
