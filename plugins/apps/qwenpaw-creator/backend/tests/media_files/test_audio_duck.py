# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Narration ducking in the final audio mix.

Field run e4cd: r2v footage carries its own dialogue, and the -9dB
`enable=between` gate left the narration fighting the scene audio. The
duck must be deep (~-16.5dB) and ramped, and windows must merge so the
base track is never ducked twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.media_files import local_execution as local_execution_module
from services.media_files.local_execution import (
    FfmpegLocalMediaRunner,
    LocalMediaExecutionSpec,
    _duck_filter,
    _merge_windows,
)


def test_duck_volume_is_decisive_for_speech_on_speech() -> None:
    # -9dB (0.35) is an ambience-bed level; dialogue-bearing footage needs
    # a decisive duck for the narration to stay intelligible.
    assert local_execution_module._DUCK_VOLUME <= 0.2


def test_duck_filter_ramps_and_depth() -> None:
    expression = _duck_filter(2.0, 5.0)
    assert expression.startswith("volume=eval=frame:volume=")
    # depth = 1 - _DUCK_VOLUME, formatted to three decimals
    depth = 1.0 - local_execution_module._DUCK_VOLUME
    assert f"1-{depth:.3f}" in expression
    fade = local_execution_module._DUCK_FADE_SECONDS
    # Ramp-in starts fade seconds before the window, ramp-out ends after.
    assert f"(t-{2.0 - fade:.3f})/{fade:.3f}" in expression
    assert f"({5.0 + fade:.3f}-t)/{fade:.3f}" in expression


def test_merge_windows_collapses_overlaps() -> None:
    assert _merge_windows([(4.0, 8.0), (0.0, 5.0), (9.0, 10.0)]) == [
        (0.0, 8.0),
        (9.0, 10.0),
    ]


class TestMixAudioTracksFilterGraph:
    def _spec(self, tmp_path: Path) -> LocalMediaExecutionSpec:
        output = tmp_path / "output.mp4"
        output.write_bytes(b"composed")
        narration = tmp_path / "narration.wav"
        narration.write_bytes(b"wav")
        return LocalMediaExecutionSpec(
            command="COMPOSE_FINAL_VIDEO",
            target_ref="timeline:main",
            task_id="task-test",
            work_dir=tmp_path,
            output_path=output,
            inputs=(),
            transitions=(),
            audio_plan="",
            expected_duration_seconds=12.0,
            canvas_size=(1280, 720),
            audio_tracks=(
                {
                    "path": narration,
                    "offset_seconds": 1.0,
                    "max_duration_seconds": 4.0,
                    "gain_db": 0.0,
                    "pan": 0.0,
                },
                {
                    "path": narration,
                    "offset_seconds": 4.0,
                    "max_duration_seconds": 3.0,
                    "gain_db": 0.0,
                    "pan": 0.0,
                },
            ),
        )

    def test_base_track_gets_one_ramped_duck_for_merged_windows(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = FfmpegLocalMediaRunner(executable="ffmpeg")
        captured: list[list[str]] = []
        monkeypatch.setattr(
            FfmpegLocalMediaRunner,
            "_run",
            lambda self, arguments, cwd=None: captured.append(
                list(arguments),
            ),
        )
        monkeypatch.setattr(
            FfmpegLocalMediaRunner,
            "_probe_has_audio",
            lambda self, path: True,
        )
        runner._mix_audio_tracks(self._spec(tmp_path))
        assert captured, "ffmpeg was not invoked"
        arguments = captured[0]
        graph = arguments[arguments.index("-filter_complex") + 1]
        # The two overlapping narration windows [1,5) and [4,7) merge into
        # one duck window, so the base chain ducks exactly once.
        assert graph.count("volume=eval=frame") == 1
        assert "clip(" in graph
        # No hard gate remains.
        assert "enable=" not in graph
        # Mix keeps both narration tracks plus the ducked base.
        assert "amix=inputs=3" in graph
