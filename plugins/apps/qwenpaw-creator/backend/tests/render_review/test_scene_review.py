# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=redefined-outer-name,unused-argument,protected-access
"""Scene review evidence and frame sampling."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.errors import ValidationError
from services.project_files.models import (
    EditCreation,
    EditPlan,
    EditPlanDesignFloor,
    ElementLocation,
    Project,
    SceneLedgerRow,
    TimelineElement,
    TimelineSpan,
)
from services.render_review import scene_review as scene_review_module
from services.render_review.frames import (
    RenderReviewError,
    _frame_timestamps,
    extract_review_frames,
    probe_audio_profile,
)
from services.render_review.scene_review import (
    scene_content_fingerprint,
    validate_scene_ledger_locked,
)
from services.runtime_files.media_probe import probe_media
from services.runtime_files.runtime_dependencies import resolve_ffmpeg
from vendor.media_toolkit.image_budget import (
    IMAGE_BUDGET_TOKENS,
    IMAGE_MIN_PIXELS,
    TOKEN_SIZE,
    VIDEO_BUDGET_TOKENS,
    VIDEO_MIN_PIXELS,
    budget_to_pixels,
    smart_resize,
)

pytestmark = pytest.mark.unit


def _plan(rows: list[SceneLedgerRow], **overrides) -> EditPlan:
    defaults: dict = {
        "concept": "猫的越狱日记",
        "pacing": "hook 1.2s",
        "signature_device": "爪印转场",
        "design_floor": EditPlanDesignFloor(
            opening="标题卡",
            transitions="硬切",
            body="设计节拍",
            ending="硬停",
        ),
        "scene_ledger": rows,
    }
    defaults.update(overrides)
    return EditPlan(**defaults)


def _timeline_with_scene(*, edit_plan: EditPlan | None, scene_count: int = 1):
    project = Project.new(project_id="project-1", name="Scene")
    timeline = project.timelines.items["timeline:main"]
    elements = {
        f"el-{index}": TimelineElement(
            element_id=f"el-{index}",
            span=TimelineSpan(start_tick=0, duration_tick=1000),
            location=ElementLocation(),
            creation=EditCreation(intent="pick"),
        )
        for index in range(1, scene_count + 1)
    }
    updated = timeline.model_copy(
        update={"elements_by_id": elements, "edit_plan": edit_plan},
    )
    project.timelines.items["timeline:main"] = updated
    return project, updated


def test_gate_skips_unplanned_blocks_draft_and_stale_passes_fresh() -> None:
    row = SceneLedgerRow(scene_id="scene-1", element_ids=["el-1"])
    plans = (None, _plan([]), _plan([row], mechanical_exemption=True))
    for edit_plan in plans:
        _project, timeline = _timeline_with_scene(edit_plan=edit_plan)
        validate_scene_ledger_locked(timeline)

    _project, timeline = _timeline_with_scene(edit_plan=_plan([row]))
    with pytest.raises(ValidationError, match="未锁定场景: scene-1"):
        validate_scene_ledger_locked(timeline)
    fingerprint = scene_content_fingerprint(timeline, row)
    fresh = row.model_copy(
        update={"status": "locked", "locked_fingerprint": fingerprint},
    )
    _project, locked = _timeline_with_scene(edit_plan=_plan([fresh]))
    validate_scene_ledger_locked(locked)

    stale = fresh.model_copy(update={"locked_fingerprint": "sha256:stale"})
    _project, drifted = _timeline_with_scene(edit_plan=_plan([stale]))
    with pytest.raises(ValidationError, match="需重审的场景: scene-1"):
        validate_scene_ledger_locked(drifted)


def _checks_payload(**overrides) -> str:
    keys = (
        "devices type_fonts composition_safety "
        "motion_quality technical watch_once"
    ).split()
    checks = []
    for key in keys:
        entry = {"key": key, "passed": True, "severity": "minor"}
        entry |= {"evidence": "", "suggestion": "", **overrides.get(key, {})}
        checks.append(entry)
    return json.dumps(
        {"checks": checks, "impression": "节拍成立"},
        ensure_ascii=False,
    )


def _services(project) -> SimpleNamespace:
    committed: list = []

    class _Commits:
        @staticmethod
        def commit(*, base, candidate, **kwargs):
            committed.append(candidate)
            return SimpleNamespace(snapshot=SimpleNamespace(project=None))

    services = SimpleNamespace(
        root="/tmp/does-not-matter",
        projects=SimpleNamespace(
            read=lambda project_id: SimpleNamespace(project=project),
            project_root=lambda project_id: "/tmp/project-root",
        ),
        commits=_Commits(),
        poller=SimpleNamespace(note_commit=lambda snapshot: None),
    )
    return services, committed


def _stub_review_env(monkeypatch, *, payload: str) -> None:
    async def fake_evidence(**kwargs):
        return [], [], ["该场景不含 Edit 片段：仅按动效/文本事实评审。"]

    async def fake_chat(content, **kwargs):
        return payload

    monkeypatch.setattr(
        scene_review_module,
        "_collect_evidence",
        fake_evidence,
    )
    monkeypatch.setattr(
        scene_review_module,
        "ProjectExecutionStore",
        lambda root: SimpleNamespace(),
    )
    monkeypatch.setattr(
        scene_review_module.vlm_model,
        "chat_completion",
        fake_chat,
    )


def _run_review(services) -> dict:
    return asyncio.run(
        scene_review_module.review_scene(
            services,
            project_id="project-1",
            timeline_ref="timeline:timeline:main",
            scene_id="scene-1",
            idempotency_key="call-1",
        ),
    )


def test_review_scene_rejects_major_failure_then_locks_on_pass(
    monkeypatch,
) -> None:
    row = SceneLedgerRow(scene_id="scene-1", element_ids=["el-1"])
    project, _timeline = _timeline_with_scene(edit_plan=_plan([row]))
    services, committed = _services(project)
    _stub_review_env(
        monkeypatch,
        payload=_checks_payload(
            devices={
                "passed": False,
                "severity": "major",
                "evidence": "契约声明的爪印转场未出现在任何帧",
            },
        ),
    )
    result = _run_review(services)
    assert result["status"] == "rejected"
    assert result["failedChecks"] == ["devices"]
    assert not committed, "a rejected review must not lock anything"

    _stub_review_env(monkeypatch, payload=_checks_payload())
    result = _run_review(services)
    assert result["status"] == "locked"
    plan = committed[0]["timelines"]["items"]["timeline:main"]["edit_plan"]
    raw_row = plan["scene_ledger"][0]
    assert raw_row["locked_fingerprint"] == result["fingerprint"]


def test_auto_rereview_evaluates_scenes_concurrently(monkeypatch) -> None:
    """Scene evaluation overlaps; every ledger write stays serialized."""
    rows = [
        SceneLedgerRow(scene_id=f"scene-{index}", element_ids=[f"el-{index}"])
        for index in range(1, 5)
    ]
    project, timeline = _timeline_with_scene(
        edit_plan=_plan(rows),
        scene_count=4,
    )
    services, committed = _services(project)
    _stub_review_env(monkeypatch, payload=_checks_payload())

    in_flight = 0
    peak_in_flight = 0
    commits_during_evaluation = 0

    async def fake_chat(content, **kwargs):
        nonlocal in_flight, peak_in_flight, commits_during_evaluation
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        commits_during_evaluation = max(
            commits_during_evaluation,
            len(committed),
        )
        await asyncio.sleep(0.05)
        in_flight -= 1
        return _checks_payload()

    monkeypatch.setattr(
        scene_review_module.vlm_model,
        "chat_completion",
        fake_chat,
    )
    remaining = asyncio.run(
        scene_review_module.auto_review_stale_scenes(
            services,
            project_id="project-1",
            timeline_ref="timeline:timeline:main",
            timeline=timeline,
        ),
    )
    assert remaining == []
    assert peak_in_flight > 1, "scene reviews must overlap"
    assert commits_during_evaluation == 0, "writes must follow evaluation"
    assert len(committed) == 4
    locked = [
        raw_row["scene_id"]
        for candidate in committed
        for raw_row in candidate["timelines"]["items"]["timeline:main"][
            "edit_plan"
        ]["scene_ledger"]
        if raw_row["status"] == "locked"
    ]
    # Serial commits keep the ledger order deterministic.
    assert locked == ["scene-1", "scene-2", "scene-3", "scene-4"]


def _peak_scene_fan_out(monkeypatch, *, scenes: int, limit: int) -> int:
    """Run the pass and report how many scenes were ever in flight.

    The probe sits in ``_collect_evidence`` — i.e. before the VLM call,
    where frame upload happens — because that stage runs outside
    ``model_slot("vlm")`` and is exactly what the review-side gate has
    to bound.
    """
    rows = [
        SceneLedgerRow(scene_id=f"scene-{index}", element_ids=[f"el-{index}"])
        for index in range(1, scenes + 1)
    ]
    project, timeline = _timeline_with_scene(
        edit_plan=_plan(rows),
        scene_count=scenes,
    )
    services, _committed = _services(project)
    _stub_review_env(monkeypatch, payload=_checks_payload())
    monkeypatch.setattr(
        scene_review_module,
        "get_vlm_concurrency",
        lambda: limit,
    )

    in_flight = 0
    peak = 0

    async def fake_evidence(**kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return [], [], ["该场景不含 Edit 片段：仅按动效/文本事实评审。"]

    monkeypatch.setattr(
        scene_review_module,
        "_collect_evidence",
        fake_evidence,
    )
    asyncio.run(
        scene_review_module.auto_review_stale_scenes(
            services,
            project_id="project-1",
            timeline_ref="timeline:timeline:main",
            timeline=timeline,
        ),
    )
    return peak


def test_scene_fan_out_follows_the_model_concurrency(monkeypatch) -> None:
    """Evidence upload is bounded by the configured VLM concurrency.

    ``model_slot("vlm")`` only wraps the HTTP request, so without a
    review-side gate the frame transfers of every scene would start at
    once. The gate borrows the model's own limit — no second knob.
    """
    assert _peak_scene_fan_out(monkeypatch, scenes=6, limit=2) == 2
    assert _peak_scene_fan_out(monkeypatch, scenes=6, limit=6) == 6
    assert _peak_scene_fan_out(monkeypatch, scenes=6, limit=1) == 1


def test_auto_rereview_isolates_one_failing_scene(monkeypatch) -> None:
    """One scene blowing up neither aborts nor blocks its siblings."""
    rows = [
        SceneLedgerRow(scene_id=f"scene-{index}", element_ids=[f"el-{index}"])
        for index in range(1, 4)
    ]
    project, timeline = _timeline_with_scene(
        edit_plan=_plan(rows),
        scene_count=3,
    )
    services, committed = _services(project)
    _stub_review_env(monkeypatch, payload=_checks_payload())

    async def fake_chat(content, **kwargs):
        text = next(
            part["text"] for part in content if part.get("type") == "text"
        )
        if "scene-2" in text:
            raise RuntimeError("provider exploded")
        return _checks_payload()

    monkeypatch.setattr(
        scene_review_module.vlm_model,
        "chat_completion",
        fake_chat,
    )
    remaining = asyncio.run(
        scene_review_module.auto_review_stale_scenes(
            services,
            project_id="project-1",
            timeline_ref="timeline:timeline:main",
            timeline=timeline,
        ),
    )
    assert remaining == ["scene-2"]
    assert len(committed) == 2


_FFMPEG = resolve_ffmpeg()
requires_ffmpeg = pytest.mark.skipif(
    _FFMPEG is None,
    reason="ffmpeg is not available",
)


def _make_video(path, *, duration=2.0, silent_audio=False):
    assert _FFMPEG is not None
    command = [_FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi"]
    command += ["-i", f"testsrc=size=640x360:rate=24:duration={duration}"]
    source = (
        f"anullsrc=r=44100:cl=stereo:d={duration}"
        if silent_audio
        else f"sine=frequency=440:duration={duration}"
    )
    command += ["-f", "lavfi", "-i", source, "-shortest"]
    command += "-pix_fmt yuv420p -c:v libx264 -preset ultrafast -y".split()
    command.append(str(path))
    subprocess.run(command, check=True, capture_output=True, timeout=120)
    return path


def test_frame_timestamps_always_include_first_and_last() -> None:
    stamps = _frame_timestamps(10.0, 24)
    assert stamps[0] == 0.0 and stamps == sorted(stamps)
    assert len(_frame_timestamps(300.0, 24)) == 24
    assert _frame_timestamps(0.0, 24) == [0.0]


@requires_ffmpeg
def test_extract_review_frames_respects_budget(tmp_path: Path) -> None:
    video = _make_video(tmp_path / "clip.mp4", duration=2.0)
    frames = extract_review_frames(
        video,
        max_frames=24,
        output_dir=tmp_path / "frames",
    )
    assert frames[0].timestamp_ms == 0
    assert frames[-1].timestamp_ms >= 1900
    max_pixels = budget_to_pixels("normal", VIDEO_BUDGET_TOKENS)
    frame_probe = probe_media(frames[0].image_path)
    assert frame_probe.width % TOKEN_SIZE == 0
    assert frame_probe.height % TOKEN_SIZE == 0
    assert frame_probe.width * frame_probe.height <= max_pixels


def test_extract_review_frames_missing_video(tmp_path: Path) -> None:
    with pytest.raises(RenderReviewError, match="video not found"):
        extract_review_frames(tmp_path / "absent.mp4")


@requires_ffmpeg
def test_probe_audio_profile_tone_and_silence(tmp_path: Path) -> None:
    tone = probe_audio_profile(_make_video(tmp_path / "tone.mp4"))
    assert tone.has_audio is True
    assert any(not item.silent for item in tone.loudness_segments)

    silent = probe_audio_profile(
        _make_video(tmp_path / "silent.mp4", silent_audio=True),
    )
    assert silent.has_audio is True
    assert all(item.silent for item in silent.loudness_segments)
    assert silent.loudness_segments[-1].end_ms == 2000


def test_smart_resize_never_exceeds_budget() -> None:
    """Patch-grid rounding must not overshoot the pixel budget."""
    video_budget = budget_to_pixels("normal", VIDEO_BUDGET_TOKENS)
    image_budget = budget_to_pixels("normal", IMAGE_BUDGET_TOKENS)
    cases = [
        (274, 913, VIDEO_MIN_PIXELS, video_budget),
        (1, 10_000, VIDEO_MIN_PIXELS, video_budget),
        (2160, 3840, IMAGE_MIN_PIXELS, image_budget),
    ]
    for height, width, min_pixels, max_pixels in cases:
        out_h, out_w = smart_resize(height, width, min_pixels, max_pixels)
        assert out_h % TOKEN_SIZE == 0 and out_w % TOKEN_SIZE == 0
        assert out_h * out_w <= max_pixels, (height, width, out_h, out_w)
