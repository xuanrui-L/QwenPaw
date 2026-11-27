# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name
# flake8: noqa: E501
# pylint: disable=protected-access
"""Local editing, media rendering, and composition filters."""

from __future__ import annotations

import asyncio
import glob
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.enums import CreatorCommandType
from domain.errors import StorageIntegrityError
from domain.errors import ValidationError as DomainValidationError
from services.media_files import keyframe_cache
from services.project_files.assets import AssetFileStore
from services.media_files.element_adapter import reconcile_candidate_span
from services.media_files.local_execution import (
    FfmpegLocalMediaRunner,
    LocalMediaExecutionSpec,
    LocalMediaInput,
    _caption_fallback_style_kwargs,
    _measured_output_seconds,
    _motion_document_matches_text,
    _plan_timeline_transitions,
    _trim_edit_chain,
    _validate_contiguous_edit_elements,
    reconcile_edit_timelines_across_projects,
)
from services.media_files.motion_blueprints import subtitle_pack_animation
from services.media_files.transitions import (
    TransitionClip,
    TransitionJoin,
    build_transition_filter_chain,
)
from services.project_files.models import (
    Project,
    TimelineElement,
    TimelineSpan,
    TransitionCreation,
)

from .conftest import make_r2v_element


def test_motion_copy_match_uses_browser_visible_html_entities() -> None:
    html = "<div><b>GitHub</b><i>&nbsp;</i><b>新手</b></div>"
    assert _motion_document_matches_text(html, "GitHub 新手")
    assert not _motion_document_matches_text(html, "GitHub 高手")


def _candidate(ticks_per_second: int = 1000) -> dict:
    return {
        "timelines": {
            "items": {
                "timeline:main": {
                    "ticks_per_second": ticks_per_second,
                    "elements_by_id": {
                        "el:talk": {
                            "span": {"start_tick": 0, "duration_tick": 5000},
                        },
                        "el:shot2": {
                            "span": {
                                "start_tick": 5000,
                                "duration_tick": 5000,
                            },
                        },
                        "el:narration2": {
                            "span": {
                                "start_tick": 5000,
                                "duration_tick": 5000,
                            },
                        },
                        # Starts inside the shrunk segment: stays put.
                        "el:overlap": {
                            "span": {
                                "start_tick": 1000,
                                "duration_tick": 1000,
                            },
                        },
                    },
                },
            },
        },
    }


def _reconcile(
    candidate: dict,
    seconds: float | None,
    element_id: str = "el:talk",
) -> bool:
    return reconcile_candidate_span(
        candidate,
        element_id=element_id,
        actual_duration_seconds=seconds,
    )


def _elements(candidate: dict) -> dict:
    return candidate["timelines"]["items"]["timeline:main"]["elements_by_id"]


def test_shorter_footage_shrinks_span_and_ripples_later_elements() -> None:
    candidate = _candidate()
    assert _reconcile(candidate, 2.8) is True
    elements = _elements(candidate)
    assert elements["el:talk"]["span"]["duration_tick"] == 2800
    # Everything that started at/after the old end moves forward together.
    assert elements["el:shot2"]["span"]["start_tick"] == 2800
    assert elements["el:narration2"]["span"]["start_tick"] == 2800
    # An element inside the shrunk window keeps its position.
    assert elements["el:overlap"]["span"]["start_tick"] == 1000
    # Replay is idempotent: the span is already reconciled.
    assert _reconcile(candidate, 2.8) is False
    assert elements["el:shot2"]["span"]["start_tick"] == 2800


def _filter(**kwargs: object) -> str:
    return FfmpegLocalMediaRunner._placement_filter(
        None,
        canvas_size=(1280, 720),
        duration_seconds=6.0,
        **kwargs,
    )


def test_freeze_pads_video_and_references_audio_only_when_present():
    chain = _filter(freeze_duration=2.0, freeze_audio=True)
    assert "tpad=stop_mode=clone:stop_duration=2.000000" in chain
    assert "[0:a]apad=pad_dur=2.000000[a]" in chain
    # Generated R2V footage usually has no audio track; referencing [0:a]
    # would make ffmpeg reject the whole filtergraph.
    silent = _filter(freeze_duration=2.0, freeze_audio=False)
    assert "tpad=stop_mode=clone:stop_duration=2.000000" in silent
    assert "[0:a]" not in silent


def test_playback_rate_retimes_picture_and_source_audio() -> None:
    chain = _filter(playback_rate=0.25, retime_audio=True)
    assert "setpts=(PTS-STARTPTS)/0.25" in chain
    assert "[0:a]atempo=0.5,atempo=0.5[a]" in chain


@pytest.mark.parametrize(
    "playback_rate",
    [0.0, -1.0, float("nan"), float("inf")],
)
def test_atempo_filters_reject_invalid_playback_rate(
    playback_rate: float,
) -> None:
    with pytest.raises(ValueError, match="finite and greater than zero"):
        FfmpegLocalMediaRunner._atempo_filters(playback_rate)


@pytest.mark.parametrize(
    "playback_rate",
    [
        float.fromhex("0x0.0000000000001p-1022"),
        float.fromhex("0x1.fffffffffffffp+1023"),
    ],
)
def test_atempo_filters_bound_extreme_finite_rates(
    playback_rate: float,
) -> None:
    with pytest.raises(ValueError, match="supported audio retiming range"):
        FfmpegLocalMediaRunner._atempo_filters(playback_rate)


_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")
requires_ffmpeg = pytest.mark.skipif(
    _FFMPEG is None or _FFPROBE is None,
    reason="ffmpeg is not installed",
)


def _ffmpeg(*args, log_level="error"):
    return subprocess.run(
        [_FFMPEG, "-v", log_level, *map(str, args)],
        check=True,
        capture_output=True,
    )


def _probe_csv(path, entries):
    return subprocess.run(
        [
            _FFPROBE,
            "-v",
            "error",
            "-show_entries",
            entries,
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _render_spec(
    source,
    media_type,
    *,
    duration=2.0,
    element="clip-1",
    **input_options,
):
    work = source.parent / "work"
    work.mkdir()
    return LocalMediaExecutionSpec(
        command=CreatorCommandType.COMPOSE_FINAL_VIDEO,
        target_ref="timeline:main",
        task_id="task-render",
        work_dir=work,
        output_path=source.parent / "output.mp4",
        inputs=(
            LocalMediaInput(
                version_id=f"version:{element}",
                file_id=f"file:{element}",
                checksum=f"sha256:{element}",
                media_type=media_type,
                path=source,
                source_ref=f"element:{element}",
                start_seconds=0.0,
                end_seconds=2.0,
                **input_options,
            ),
        ),
        transitions=(),
        audio_plan="preserve",
        expected_duration_seconds=duration,
        canvas_size=(640, 360),
    )


@requires_ffmpeg
def test_still_image_input_renders_a_timed_segment(tmp_path) -> None:
    from PIL import Image

    image = tmp_path / "backdrop.png"
    Image.new("RGB", (640, 360), "orange").save(image)
    spec = _render_spec(image, "image/png")
    result = asyncio.run(FfmpegLocalMediaRunner(_FFMPEG).render(spec))

    assert result["media_type"] == "video/mp4"
    assert float(
        _probe_csv(spec.output_path, "format=duration"),
    ) == pytest.approx(2.0, abs=0.15)


@requires_ffmpeg
@pytest.mark.parametrize(
    ("playback_rate", "expected_duration"),
    ((0.5, 4.0), (2.0, 1.0)),
)
def test_video_playback_rate_controls_real_output_duration(
    tmp_path,
    playback_rate: float,
    expected_duration: float,
) -> None:
    source = tmp_path / "source.mp4"
    _ffmpeg(
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=30:duration=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100:duration=2",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        source,
    )
    spec = _render_spec(
        source,
        "video/mp4",
        duration=expected_duration,
        duration_seconds=2.0,
        playback_rate=playback_rate,
    )
    asyncio.run(FfmpegLocalMediaRunner(_FFMPEG).render(spec))

    durations = dict(
        line.split(",")
        for line in _probe_csv(
            spec.output_path,
            "stream=codec_type,duration",
        ).splitlines()
    )
    assert float(durations["video"]) == pytest.approx(
        expected_duration,
        abs=0.15,
    )
    assert float(durations["audio"]) == pytest.approx(
        expected_duration,
        abs=0.15,
    )


def _materialize_keyframe(project_root: Path, source: Path, timestamp: float):
    return keyframe_cache.materialize_keyframe(
        project_root,
        source_path=source,
        source_identity="sha256:source",
        timestamp_seconds=timestamp,
        width=640,
        ffmpeg_path="/fake/ffmpeg",
    )


def test_materialize_keyframe_persists_and_reuses_deterministic_jpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project-1"
    project_root.mkdir()
    source = project_root / "source.mp4"
    source.write_bytes(b"source-video")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"JPEG@" + command[6].encode("ascii"))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(
        getattr(keyframe_cache, "media_gate", subprocess),
        "run",
        fake_run,
    )
    first = _materialize_keyframe(project_root, source, 4.5004)
    replay = _materialize_keyframe(project_root, source, 4.5)

    assert first == replay
    assert first.path.parent == project_root / "runtime" / "keyframe-cache"
    assert first.path.suffix == ".jpg"
    assert first.path.read_bytes() == b"JPEG@4.500"
    assert first.timestamp_seconds == 4.5
    assert len(calls) == 1
    assert calls[0][0] == "/fake/ffmpeg"
    assert calls[0][5:7] == ["-ss", "4.500"]


def test_materialize_keyframe_rejects_symlink_source(
    tmp_path: Path,
    file_symlink,
) -> None:
    project_root = tmp_path / "project-1"
    project_root.mkdir()
    real_source = project_root / "real.mp4"
    real_source.write_bytes(b"source-video")
    symlink = project_root / "source.mp4"
    file_symlink(real_source, symlink)

    with pytest.raises(StorageIntegrityError, match="安全的普通文件"):
        _materialize_keyframe(project_root, symlink, 1)


@pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg is not installed")
def test_real_keyframe_is_published_and_reused(tmp_path: Path) -> None:
    from PIL import Image

    project_root = tmp_path / "中文 project"
    project_root.mkdir()
    source = project_root / "源视频.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=orange:s=640x360:d=1",
        "-pix_fmt",
        "yuv420p",
        source,
    )
    kwargs = {
        "source_path": source,
        "source_identity": "test-source-v1",
        "timestamp_seconds": 0.5,
        "width": 320,
        "ffmpeg_path": _FFMPEG,
    }
    first = keyframe_cache.materialize_keyframe(project_root, **kwargs)
    with Image.open(first.path) as frame:
        assert frame.format == "JPEG"
        assert frame.size == (320, 180)
        frame.load()
    published_at = first.path.stat().st_mtime_ns
    assert keyframe_cache.materialize_keyframe(project_root, **kwargs) == first
    assert first.path.stat().st_mtime_ns == published_at
    assert not list(first.path.parent.glob("*.part"))


def test_keyframe_cache_rejects_directory_redirect(tmp_path, directory_link):
    project_root = tmp_path / "project-1"
    (project_root / "runtime").mkdir(parents=True)
    source = project_root / "source.mp4"
    source.write_bytes(b"source-video")
    outside = tmp_path / "outside"
    outside.mkdir()
    directory_link(outside, project_root / "runtime" / "keyframe-cache")
    with pytest.raises(StorageIntegrityError, match="安全的真实目录"):
        _materialize_keyframe(project_root, source, 1)
    assert not list(outside.iterdir())


@pytest.mark.parametrize(
    "pick",
    [None, {"entrance": "pop", "exit": "shrink", "loop": True}],
)
def test_normalized_overlay_respects_user_or_pack_animation(pick) -> None:
    overlay = {
        "kind": "pet_os",
        "text": "你好",
        "appear_at": 0.0,
        "duration": 2.0,
        "subtitle_template": "warm_note",
    }
    if pick is not None:
        overlay.update(appear_at=0.5, caption_animation=pick)
    normalized = FfmpegLocalMediaRunner._normalized_overlay(overlay, 5.0)
    assert normalized is not None
    assert normalized["caption_animation"] == pick
    kwargs = _caption_fallback_style_kwargs(normalized)
    expected = pick or subtitle_pack_animation("warm_note")
    assert {
        key: kwargs[key] for key in ("entrance", "exit", "loop")
    } == expected


def _gray_clip(path) -> None:
    _ffmpeg(
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=gray:s=64x64:d=0.5",
        "-pix_fmt",
        "yuv420p",
        path,
    )


def _mean_rgb(path) -> tuple[float, float, float]:
    out = _ffmpeg(
        "-i",
        path,
        "-vf",
        "scale=1:1,format=rgb24",
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-",
    ).stdout
    return float(out[0]), float(out[1]), float(out[2])


def _spec(tmp_path, output, grade: str) -> LocalMediaExecutionSpec:
    return LocalMediaExecutionSpec(
        command=CreatorCommandType.COMPOSE_FINAL_VIDEO,
        target_ref="timeline:timeline:main",
        task_id="task-grade",
        work_dir=tmp_path,
        output_path=output,
        inputs=(),
        transitions=(),
        audio_plan="",
        expected_duration_seconds=None,
        canvas_size=(64, 64),
        color_grade=grade,
    )


@requires_ffmpeg
def test_warm_bright_grade_shifts_the_cut_towards_warm(tmp_path) -> None:
    output = tmp_path / "output.mp4"
    _gray_clip(output)
    neutral_r, _neutral_g, neutral_b = _mean_rgb(output)

    runner = FfmpegLocalMediaRunner(_FFMPEG)
    warning = runner._apply_color_grade(_spec(tmp_path, output, "warm_bright"))

    assert warning is None
    graded_r, _graded_g, graded_b = _mean_rgb(output)
    # The warm preset pushes reds up and blues down relative to neutral.
    assert graded_r - graded_b > (neutral_r - neutral_b) + 2


@requires_ffmpeg
def test_unknown_grade_degrades_to_a_warning_and_keeps_the_cut(
    tmp_path,
) -> None:
    output = tmp_path / "output.mp4"
    _gray_clip(output)
    original = output.read_bytes()

    runner = FfmpegLocalMediaRunner(_FFMPEG)
    warning = runner._apply_color_grade(_spec(tmp_path, output, "sepia_1920"))

    assert warning is not None and "sepia_1920" in warning
    assert output.read_bytes() == original


@requires_ffmpeg
def test_model_boundary_only_admits_the_named_preset_vocabulary() -> None:
    from services.media_files.local_execution import _COLOR_GRADE_FILTERS
    from services.project_files.models import (
        COLOR_GRADE_PRESETS,
        Timeline,
    )

    assert set(_COLOR_GRADE_FILTERS) == set(COLOR_GRADE_PRESETS)
    project = Project.new(project_id="project-grade", name="Grade")
    base = project.timelines.items["timeline:main"].model_dump()
    with pytest.raises(ValueError, match="color_grade 必须是命名预设"):
        Timeline(**{**base, "color_grade": "明亮温暖清新"})
    # Named presets and the empty string stay valid.
    Timeline(**{**base, "color_grade": "warm_bright"})
    Timeline(**{**base, "color_grade": ""})


def _r2v_element(
    element_id: str,
    *,
    start: int,
    duration: int = 5_000,
) -> TimelineElement:
    return make_r2v_element(
        element_id,
        label=element_id,
        video_prompt="动画，猫从左向右追逐老鼠，动作连续",
        start_tick=start,
        duration_tick=duration,
    )


def _transition_element(
    element_id: str,
    *,
    from_id: str,
    to_id: str,
    start: int,
    duration: int,
    kind: str = "crossfade",
) -> TimelineElement:
    return TimelineElement(
        element_id=element_id,
        span=TimelineSpan(start_tick=start, duration_tick=duration),
        creation=TransitionCreation(
            from_element_id=from_id,
            to_element_id=to_id,
            transition_kind=kind,
        ),
    )


def _timeline_with_transition(kind: str = "crossfade"):
    project = Project.new(project_id="transition-plan", name="Transition")
    timeline = project.timelines.items["timeline:main"]
    timeline.elements_by_id = {
        "a": _r2v_element("a", start=0, duration=5_000),
        "b": _r2v_element("b", start=4_600, duration=5_000),
        "fade": _transition_element(
            "fade",
            from_id="a",
            to_id="b",
            start=4_600,
            duration=400,
            kind=kind,
        ),
    }
    return timeline


def _visual(timeline, *ids):
    return [timeline.elements_by_id[element_id] for element_id in ids]


@pytest.mark.unit
def test_filter_chain_emits_xfade_and_acrossfade_with_running_offsets():
    chain = build_transition_filter_chain(
        [
            TransitionClip(duration_seconds=5.0, has_audio=True),
            TransitionClip(duration_seconds=4.0, has_audio=True),
            TransitionClip(duration_seconds=3.0, has_audio=True),
        ],
        [
            TransitionJoin(kind="crossfade", blend_seconds=0.4),
            TransitionJoin(kind="fadeblack", blend_seconds=0.5),
        ],
        canvas_size=(1280, 720),
    )
    assert "xfade=transition=fade:duration=0.400000:offset=4.600000" in chain
    # The second pair's offset builds on the chain duration with the blend
    # already consumed: 4.6 + 4 - 0.5 = 8.1.
    assert (
        "xfade=transition=fadeblack:duration=0.500000:offset=8.100000" in chain
    )
    assert chain.count("acrossfade=d=") == 2
    assert chain.endswith("[aout]")
    assert "[vout]" in chain


@pytest.mark.unit
def test_filter_chain_edge_fades_hard_cut_joins_only():
    # a →(cut)→ b →(xfade)→ c: the cut sides get the 40ms edge fades the
    # plain-concat path applies, xfade sides and outer edges stay clean.
    chain = build_transition_filter_chain(
        [
            TransitionClip(duration_seconds=12.0, has_audio=True),
            TransitionClip(duration_seconds=12.0, has_audio=True),
            TransitionClip(duration_seconds=12.0, has_audio=True),
        ],
        [
            TransitionJoin(kind="cut", blend_seconds=0.0),
            TransitionJoin(kind="crossfade", blend_seconds=0.5),
        ],
        canvas_size=(1280, 720),
    )
    # Clip 0: fade-out into the cut only (no fade-in at film start).
    assert "[0:a]" in chain
    assert (
        "afade=t=in"
        not in chain.split("[ai0]", maxsplit=1)[0].split("[0:a]")[1]
    )
    assert (
        "afade=t=out:st=11.960:d=0.040" in chain.split("[ai0]", maxsplit=1)[0]
    )
    # Clip 1: fade-in out of the cut; trailing side is an xfade → no
    # fade-out (acrossfade owns that ramp).
    clip1_audio = chain.split("[ai1]", maxsplit=1)[0].split("[1:a]")[1]
    assert "afade=t=in:d=0.040" in clip1_audio
    assert "afade=t=out" not in clip1_audio
    # Clip 2: both sides are xfade/film-end → untouched.
    clip2_audio = chain.split("[ai2]", maxsplit=1)[0].split("[2:a]")[1]
    assert "afade" not in clip2_audio
    # The cut join still concats; the xfade join still acrossfades.
    assert "concat=n=2:v=0:a=1" in chain
    assert chain.count("acrossfade=d=") == 1


@pytest.mark.unit
def test_filter_chain_micro_clip_skips_edge_fade():
    chain = build_transition_filter_chain(
        [
            TransitionClip(duration_seconds=0.2, has_audio=True),
            TransitionClip(duration_seconds=5.0, has_audio=True),
        ],
        [TransitionJoin(kind="cut", blend_seconds=0.0)],
        canvas_size=(1280, 720),
    )
    # The 0.2s micro-clip passes through unfaded (ramps would eat it);
    # its long neighbour still fades in out of the cut.
    clip0_audio = chain.split("[ai0]", maxsplit=1)[0].split("[0:a]")[1]
    assert "afade" not in clip0_audio
    assert (
        "afade=t=in:d=0.040"
        in chain.split("[ai1]", maxsplit=1)[0].split("[1:a]")[1]
    )


@pytest.mark.unit
def test_filter_chain_rejects_blend_longer_than_adjacent_clip():
    with pytest.raises(ValueError, match="must be shorter"):
        build_transition_filter_chain(
            [
                TransitionClip(duration_seconds=1.0, has_audio=True),
                TransitionClip(duration_seconds=5.0, has_audio=True),
            ],
            [TransitionJoin(kind="fade", blend_seconds=1.0)],
            canvas_size=(1280, 720),
        )


@pytest.mark.unit
def test_plan_covers_overlap_and_validates_contiguity():
    timeline = _timeline_with_transition()
    visual = _visual(timeline, "a", "b")
    plans = _plan_timeline_transitions(timeline, visual)
    assert len(plans) == 1
    assert plans[0]["kind"] == "fade"
    # The blend consumes the whole 400ms overlap, no tail trim.
    assert plans[0]["duration_ms"] == 400
    assert plans[0]["tail_trim_ms"] == 0
    _validate_contiguous_edit_elements(visual, plans)
    with pytest.raises(DomainValidationError, match="期望 5000"):
        _validate_contiguous_edit_elements(visual)


@pytest.mark.unit
def test_plan_rejects_non_adjacent_endpoints():
    timeline = _timeline_with_transition()
    timeline.elements_by_id["c"] = _r2v_element(
        "c",
        start=9_600,
        duration=5_000,
    )
    timeline.elements_by_id["fade"] = _transition_element(
        "fade",
        from_id="a",
        to_id="c",
        start=4_600,
        duration=400,
    )
    with pytest.raises(DomainValidationError, match="不相邻"):
        _plan_timeline_transitions(timeline, _visual(timeline, "a", "b", "c"))


@pytest.mark.unit
def test_runner_accepts_whitelisted_kinds_and_rejects_unknown():
    def spec_with(kind: str):
        transition = {
            "fromElementId": "a",
            "toElementId": "b",
            "kind": kind,
            "duration_ms": 400,
            "tail_trim_ms": 0,
        }
        return SimpleNamespace(
            transitions=(transition,),
            inputs=(),
            audio_plan="",
        )

    FfmpegLocalMediaRunner._validate_supported_directives(spec_with("fade"))
    with pytest.raises(DomainValidationError, match="仅支持"):
        FfmpegLocalMediaRunner._validate_supported_directives(
            spec_with("swirl"),
        )


# ── beat-sync snapping (WT-B5) ───────────────────────────────────────────────


def _beat_spec(*, audio_tracks=()):
    def _input(ref: str, duration: float):
        return SimpleNamespace(
            source_ref=ref,
            start_seconds=0.0,
            end_seconds=duration,
            duration_seconds=duration,
        )

    transition = {
        "fromElementId": "a",
        "toElementId": "b",
        "kind": "fade",
        "duration_ms": 400,
        "tail_trim_ms": 300,
    }
    return SimpleNamespace(
        transitions=(transition,),
        inputs=(_input("element:a", 5.0), _input("element:b", 5.0)),
        audio_tracks=audio_tracks,
    )


@pytest.mark.unit
def test_beat_snap_moves_the_xfade_end_onto_a_beat(monkeypatch) -> None:
    from services.media_files import local_execution as le
    from services.media_files.beat_grid import BeatGrid

    # Chain math: d0 = 5.0 - 0.3 = 4.7s, so the xfade resolves at 4.7s.
    # The nearest beat is 4.8s → δ=+0.1s comes out of the tail trim.
    monkeypatch.setattr(
        le,
        "extract_beat_grid",
        lambda path: BeatGrid(beats_ms=(4_800,), tempo_bpm=120.0),
    )
    spec = _beat_spec(
        audio_tracks=(
            {
                "path": "/tmp/bgm.mp3",
                "offset_seconds": 0.0,
                "max_duration_seconds": 10.0,
            },
        ),
    )
    tail_trims, joins = FfmpegLocalMediaRunner._transition_directives(spec)
    FfmpegLocalMediaRunner._snap_transitions_to_beats(spec, tail_trims, joins)
    join = joins[("element:a", "element:b")]
    assert join.effective_blend() == pytest.approx(0.5)
    assert tail_trims["element:a"] == pytest.approx(0.2)
    # Total chain duration is invariant: the overlap split changed, not
    # the committed cut layout.
    assert (5.0 - 0.2) + 5.0 - 0.5 == pytest.approx(9.3)


_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


def _playwright_browser_ready() -> bool:
    """Whether the chromium binary the motion renderer launches is present.

    The playwright *package* being importable is not enough: `playwright
    install` has to have downloaded a browser. Without it the render raises a
    launch error, so the case is skipped rather than failed on an
    under-provisioned machine.
    """

    if not _PLAYWRIGHT:
        return False
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root:
        if sys.platform == "darwin":
            root = os.path.expanduser("~/Library/Caches/ms-playwright")
        elif sys.platform.startswith("win"):
            root = os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                "ms-playwright",
            )
        else:
            root = os.path.expanduser("~/.cache/ms-playwright")
    for name in ("chrome-headless-shell", "chrome", "Chromium", "chromium"):
        if glob.glob(
            os.path.join(root, "chromium*", "**", name),
            recursive=True,
        ):
            return True
    return False


_CLIP_HTML = (
    "<!DOCTYPE html><html><head><style>"
    "html,body{margin:0;width:100%;height:100%;overflow:hidden}"
    ".stage{position:fixed;inset:0;"
    "background:linear-gradient(160deg,#1c2f5e,#7a3d8f);"
    "animation:pulse 2s ease-in-out infinite}"
    "@keyframes pulse{0%,100%{filter:brightness(1)}50%{filter:brightness(1.08)}}"
    "</style></head><body><div class='stage'></div></body></html>"
)


@requires_ffmpeg
@pytest.mark.skipif(
    not _PLAYWRIGHT,
    reason="playwright is not installed (motion frames render through it)",
)
@pytest.mark.skipif(
    not _playwright_browser_ready(),
    reason="playwright chromium browser is not downloaded (run: playwright install chromium)",
)
def test_motion_clip_segment_renders_the_document_as_the_picture(
    tmp_path,
) -> None:
    spec = _render_spec(
        tmp_path / "unused.html",
        "text/html",
        motion_clip={
            "element_id": "clip-1",
            "format": "html_css",
            "html": _CLIP_HTML,
            "checksum": "clip",
            "fps": 24,
            "loop": True,
            "location": None,
            "appear_at": 0.0,
            "duration": 2.0,
        },
    )
    result = asyncio.run(FfmpegLocalMediaRunner(_FFMPEG).render(spec))
    assert result["media_type"] == "video/mp4"
    # The motion document must paint over the black base canvas.
    signature = _ffmpeg(
        "-ss",
        "1.0",
        "-i",
        spec.output_path,
        "-frames:v",
        "1",
        "-vf",
        "signalstats,metadata=print:key=lavfi.signalstats.YAVG",
        "-f",
        "null",
        "-",
        log_level="info",
    ).stderr.decode()
    levels = [
        line.rsplit("=", 1)[1]
        for line in signature.splitlines()
        if "YAVG=" in line
    ]
    assert levels, signature
    assert float(levels[0]) > 24.0, f"frame is almost black (YAVG={levels[0]})"


# ── Edit spans capped at the media that actually exists ──────────────────────
#
# Field shape: an imported comparison cut described two screen recordings as
# 180 s and 52 s. The prose was believed, the files were never measured, and
# the compose produced 29.918 + 52 = 81.9 s. The plan page then showed
# 03:00 + 00:52 in the rough-cut strip while the same artifact played as 1:21
# in the overview rail -- two numbers, both faithful to their own source.


def _edit_candidate(
    *,
    first_seconds: float | None,
    second_seconds: float | None,
) -> dict:
    def version(version_id: str, seconds: float | None) -> dict:
        row = {
            "version_id": version_id,
            "file_id": f"{version_id}-file",
            "media_kind": "video",
            "media_type": "video/mp4",
        }
        if seconds is not None:
            row["duration_seconds"] = seconds
        return row

    def cut(
        element_id: str,
        version_id: str,
        start: int,
        duration: int,
    ) -> dict:
        return {
            "element_id": element_id,
            "enabled": True,
            "span": {"start_tick": start, "duration_tick": duration},
            "creation": {
                "type": "edit",
                "intent": "pick",
                "original_sound": "preserve",
            },
            "render_source": {
                "type": "source_asset_version",
                "version_id": version_id,
                "source_in_tick": 0,
                "source_out_tick": duration,
                "playback_rate": 1.0,
            },
        }

    return {
        "assets": {
            "source_versions_by_id": {
                "v-qwen4": version("v-qwen4", first_seconds),
                "v-qoduck": version("v-qoduck", second_seconds),
            },
        },
        "timelines": {
            "items": {
                "timeline:main": {
                    "ticks_per_second": 1000,
                    "elements_by_id": {
                        "el-qwen4": cut("el-qwen4", "v-qwen4", 0, 180_000),
                        "el-qoduck": cut(
                            "el-qoduck",
                            "v-qoduck",
                            180_000,
                            52_000,
                        ),
                        # A caption anchored past the first cut's old end
                        # must ride the same shift, or it would burn onto
                        # footage that is no longer anywhere in the film.
                        "el-caption": {
                            "element_id": "el-caption",
                            "enabled": True,
                            "span": {
                                "start_tick": 200_000,
                                "duration_tick": 3_000,
                            },
                            "creation": {"type": "overlay", "text": "字幕"},
                        },
                    },
                },
            },
        },
    }


def _trim(candidate: dict, tmp_path: Path) -> bool:
    return _trim_edit_chain(
        candidate,
        project=Project.new(project_id="p-trim", name="trim"),
        file_store=AssetFileStore(tmp_path),
        target_ref="timeline:timeline:main",
    )


def _cut_elements(candidate: dict) -> dict:
    return candidate["timelines"]["items"]["timeline:main"]["elements_by_id"]


def test_edit_span_longer_than_its_media_is_capped_and_the_chain_closes_up(
    tmp_path: Path,
) -> None:
    candidate = _edit_candidate(first_seconds=29.918, second_seconds=54.153)
    assert _trim(candidate, tmp_path) is True
    elements = _cut_elements(candidate)
    first = elements["el-qwen4"]
    assert first["span"]["duration_tick"] == 29_918
    assert first["render_source"]["source_out_tick"] == 29_918
    second = elements["el-qoduck"]
    # Its own media is long enough, so only its position moves: the second
    # cut now starts where the shortened first one ends.
    assert second["span"]["start_tick"] == 29_918
    assert second["span"]["duration_tick"] == 52_000
    assert second["render_source"]["source_out_tick"] == 52_000
    assert elements["el-caption"]["span"]["start_tick"] == 49_918
    # The Timeline now measures what the rendered file measures.
    assert (
        max(
            item["span"]["start_tick"] + item["span"]["duration_tick"]
            for item in elements.values()
        )
        == 81_918
    )
    # Replaying costs nothing: nothing is over-long any more.
    assert _trim(candidate, tmp_path) is False


def test_media_that_fits_the_plan_is_left_untouched(tmp_path: Path) -> None:
    candidate = _edit_candidate(first_seconds=180.0, second_seconds=52.0)
    assert _trim(candidate, tmp_path) is False
    assert (
        _cut_elements(candidate)["el-qwen4"]["span"]["duration_tick"]
        == 180_000
    )


def test_unmeasured_source_is_probed_once_and_the_answer_is_stamped(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A version row with no length gets one probe, then carries the number."""

    from services.media_files import local_execution

    class _Probe:
        duration_seconds = 29.918

    monkeypatch.setattr(
        local_execution,
        "probe_media",
        lambda *_args, **_kwargs: _Probe(),
    )
    monkeypatch.setattr(
        local_execution,
        "_indexed_version",
        lambda _project, version_id, **_kwargs: (
            SimpleNamespace(
                file_id=f"{version_id}-file",
                relative_uri="assets/sources/x.mp4",
            ),
            object(),
        ),
    )
    monkeypatch.setattr(
        AssetFileStore,
        "resolve_verified_path",
        lambda _self, indexed: Path(f"/tmp/{indexed.file_id}.mp4"),
    )
    candidate = _edit_candidate(first_seconds=None, second_seconds=54.153)
    assert _trim(candidate, tmp_path) is True
    row = candidate["assets"]["source_versions_by_id"]["v-qwen4"]
    assert row["duration_seconds"] == 29.918
    assert (
        _cut_elements(candidate)["el-qwen4"]["span"]["duration_tick"] == 29_918
    )

    # The stamped row means a later pass stays arithmetic: no second subprocess.
    def _no_more_probes(*_args, **_kwargs):
        raise AssertionError("re-probed an already measured source")

    monkeypatch.setattr(local_execution, "probe_media", _no_more_probes)
    assert _trim(candidate, tmp_path) is False


def test_composed_duration_is_measured_from_the_file_not_the_plan(
    monkeypatch,
    tmp_path,
) -> None:
    spec = LocalMediaExecutionSpec(
        command=CreatorCommandType.COMPOSE_FINAL_VIDEO,
        target_ref="timeline:timeline:main",
        task_id="task-1",
        work_dir=tmp_path,
        output_path=tmp_path / "final.mp4",
        inputs=(),
        transitions=(),
        audio_plan="preserve",
        expected_duration_seconds=232.0,
        canvas_size=(1280, 720),
    )

    from services.media_files import local_execution

    class _Probe:
        duration_seconds = 81.922

    monkeypatch.setattr(
        local_execution,
        "probe_media",
        lambda *_args, **_kwargs: _Probe(),
    )
    assert _measured_output_seconds(spec) == 81.922

    def _unreadable(*_args, **_kwargs):
        raise local_execution.MediaProbeError("no such file")

    monkeypatch.setattr(local_execution, "probe_media", _unreadable)
    # A file that cannot be probed keeps the planned number rather than
    # publishing a null duration every later reader has to guard against.
    assert _measured_output_seconds(spec) == 232.0


class _StubProjects:
    def __init__(self, root: Path, documents: dict) -> None:
        self.root = root
        self.documents = documents

    def discover_project_ids(self):
        return tuple(self.documents)

    def project_root(self, project_id: str) -> Path:
        return self.root / project_id

    def read(self, project_id: str):
        self.reads = getattr(self, "reads", 0) + 1
        return SimpleNamespace(project=self.documents[project_id])


class _StubCommits:
    def __init__(self, projects: _StubProjects) -> None:
        self.projects = projects
        self.calls: list[dict] = []

    def commit(self, *, base, candidate, **_kwargs):
        self.calls.append(candidate)
        project_id = base.project.project_id
        self.projects.documents[project_id] = Project.model_validate(candidate)
        return SimpleNamespace(snapshot=self.projects.documents[project_id])


def _project_with(candidate: dict, project_id: str) -> Project:
    """A valid Project carrying one candidate timeline."""

    dumped = Project.new(project_id=project_id, name="edit").model_dump(
        mode="json",
    )
    timeline = candidate["timelines"]["items"]["timeline:main"]
    timeline["timeline_id"] = "timeline:main"
    for element in timeline["elements_by_id"].values():
        # Visual Elements carry a canvas location; the candidate dict is
        # otherwise a pure-tick fixture.
        element.setdefault("location", {})
    dumped["timelines"]["items"]["timeline:main"] = timeline
    dumped["assets"]["source_versions_by_id"] = {
        version_id: {
            "logical_asset_id": f"asset-{version_id}",
            "name": f"{version_id}.mp4",
            "checksum": "c" * 64,
            "created_at": "2026-11-16T00:00:00+00:00",
            **row,
        }
        for version_id, row in candidate["assets"][
            "source_versions_by_id"
        ].items()
    }
    dumped["assets"]["files_by_id"] = {
        row["file_id"]: {
            "file_id": row["file_id"],
            "kind": "source_original",
            "relative_uri": f"assets/sources/{row['file_id']}.mp4",
            "sha256": "c" * 64,
            "size_bytes": 1,
            "media_type": "video/mp4",
            "created_at": "2026-11-16T00:00:00+00:00",
        }
        for row in dumped["assets"]["source_versions_by_id"].values()
    }
    return Project.model_validate(dumped)


def _stub_media_probe(monkeypatch, seconds_by_file: dict[str, float]) -> None:
    """Answer ffprobe per file and keep the probe off the real filesystem.

    The Project model already refuses a source range past a *recorded*
    duration; the shape that actually survives in the field is a row with
    no duration at all, so every case here starts from ``None`` and lets
    the pass measure it.
    """

    from services.media_files import local_execution

    class _Probe:
        def __init__(self, seconds: float) -> None:
            self.duration_seconds = seconds

    def fake_probe(target, **_kwargs):
        return _Probe(seconds_by_file[Path(target).name])

    monkeypatch.setattr(local_execution, "probe_media", fake_probe)
    monkeypatch.setattr(
        local_execution,
        "_indexed_version",
        lambda _project, version_id, **_kwargs: (
            SimpleNamespace(
                file_id=f"{version_id}-file",
                relative_uri="assets/sources/x.mp4",
            ),
            object(),
        ),
    )
    monkeypatch.setattr(
        AssetFileStore,
        "resolve_verified_path",
        lambda _self, indexed: Path(f"/tmp/{indexed.file_id}.mp4"),
    )


def test_startup_pass_caps_a_stale_timeline_without_being_asked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The upgrade case: nobody composes, yet the plan must stop lying."""

    _stub_media_probe(
        monkeypatch,
        {"v-qwen4-file.mp4": 29.918, "v-qoduck-file.mp4": 54.153},
    )
    candidate = _edit_candidate(first_seconds=None, second_seconds=None)
    project_id = "project-1"
    root = tmp_path / project_id
    root.mkdir(parents=True)
    (root / "project.json").write_text("{}", encoding="utf-8")
    projects = _StubProjects(
        tmp_path,
        {project_id: _project_with(candidate, project_id)},
    )
    commits = _StubCommits(projects)
    services = SimpleNamespace(
        root=tmp_path,
        projects=projects,
        commits=commits,
    )

    report = reconcile_edit_timelines_across_projects(services)
    assert report.scanned_projects == 1
    assert report.reconciled_projects == 1
    assert len(commits.calls) == 1
    elements = commits.calls[0]["timelines"]["items"]["timeline:main"][
        "elements_by_id"
    ]
    assert elements["el-qwen4"]["span"]["duration_tick"] == 29_918
    assert elements["el-qoduck"]["span"]["start_tick"] == 29_918
    # The measured lengths are stamped in the same commit, so the next read
    # is arithmetic and the model's own range check finally has teeth.
    rows = commits.calls[0]["assets"]["source_versions_by_id"]
    assert rows["v-qwen4"]["duration_seconds"] == 29.918
    assert rows["v-qoduck"]["duration_seconds"] == 54.153


def test_startup_pass_reloads_only_documents_that_moved(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A repeat pass must not re-read every Project: one stat decides."""

    _stub_media_probe(
        monkeypatch,
        {"v-qwen4-file.mp4": 29.918, "v-qoduck-file.mp4": 54.153},
    )
    candidate = _edit_candidate(first_seconds=None, second_seconds=None)
    project_id = "project-1"
    root = tmp_path / project_id
    root.mkdir(parents=True)
    (root / "project.json").write_text("{}", encoding="utf-8")
    projects = _StubProjects(
        tmp_path,
        {project_id: _project_with(candidate, project_id)},
    )
    services = SimpleNamespace(
        root=tmp_path,
        projects=projects,
        commits=_StubCommits(projects),
    )
    reconcile_edit_timelines_across_projects(services)
    first_reads = projects.reads
    second = reconcile_edit_timelines_across_projects(services)
    assert second.scanned_projects == 0
    assert projects.reads == first_reads
