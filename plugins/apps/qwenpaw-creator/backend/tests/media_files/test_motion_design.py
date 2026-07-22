# -*- coding: utf-8 -*-
# pylint: disable=unused-argument,protected-access
"""Unit tests for motion design and text-overlay styled rendering."""

from __future__ import annotations

import asyncio

import pytest

from domain.errors import ValidationError
from services.media_files import local_execution as local_execution_module
from services.media_files import motion_design
from services.media_files.local_execution import (
    FfmpegLocalMediaRunner,
    LocalMediaInput,
)
from services.media_files.motion_design import (
    _select_decoration_ids,
    _validated_design,
)
from services.media_files.motion_overlay import _alpha_plane_stats
from services.media_files.overlay import OverlayRenderResult
from services.project_files.models import (
    EditCreation,
    ElementLocation,
    MotionGraphic,
    OverlayCreation,
    Timeline,
    TimelineElement,
    TimelineSpan,
)

_HTML = "<html><body><div class='card'>本喵要发光</div></body></html>"


def _motion() -> MotionGraphic:
    return MotionGraphic(html=_HTML, fps=24, loop=False)


class TestOverlayCreationMotionValidator:
    def test_pet_os_accepts_motion_styling(self) -> None:
        creation = OverlayCreation(
            overlay_kind="pet_os",
            text="本喵要发光",
            motion=_motion(),
        )
        assert creation.motion is not None

    def test_interview_summary_accepts_motion_styling(self) -> None:
        creation = OverlayCreation(
            overlay_kind="interview_summary",
            text="核心观点",
            motion=_motion(),
        )
        assert creation.motion is not None

    def test_media_rejects_motion(self) -> None:
        with pytest.raises(ValueError, match="overlay_kind=media"):
            OverlayCreation(
                overlay_kind="media",
                prompt="一张贴纸",
                motion=_motion(),
            )


class TestValidatedDesignTextMode:
    _BASE = {
        "concept": "发光字幕卡",
        "fps": 24,
        "location": {"x": 0.9, "y": 0.3, "width": 0.2, "height": 0.4},
    }

    def test_verbatim_text_required(self) -> None:
        raw = {**self._BASE, "html": _HTML.replace("本喵要发光", "本喵想发光")}
        with pytest.raises(ValidationError, match="一字不差"):
            _validated_design(raw, required_text="本喵要发光")

    def test_text_may_wrap_across_lines(self) -> None:
        raw = {
            **self._BASE,
            "html": _HTML.replace("本喵要发光", "本喵\n要发光"),
        }
        design = _validated_design(
            raw,
            required_text="本喵要发光",
            default_loop=False,
        )
        assert not isinstance(design, str)

    def test_text_mode_ignores_needed_and_defaults_loop_false(self) -> None:
        raw = {**self._BASE, "html": _HTML, "needed": False}
        design = _validated_design(
            raw,
            required_text="本喵要发光",
            default_loop=False,
        )
        assert not isinstance(design, str)
        motion, _location, _concept = design
        assert motion.loop is False

    def test_extra_visible_text_is_rejected(self) -> None:
        raw = {
            **self._BASE,
            "html": _HTML.replace(
                "本喵要发光",
                "os-04-pond → 本喵要发光",
            ),
        }
        with pytest.raises(ValidationError, match="台词本身"):
            _validated_design(raw, required_text="本喵要发光")

    def test_small_decorative_text_is_tolerated(self) -> None:
        raw = {
            **self._BASE,
            "html": _HTML.replace("本喵要发光", "本喵要发光！"),
        }
        design = _validated_design(
            raw,
            required_text="本喵要发光",
            default_loop=False,
        )
        assert not isinstance(design, str)

    def test_css_only_text_fails_verbatim_check(self) -> None:
        raw = {
            **self._BASE,
            "html": (
                "<html><head><style>.card::after{content:'本喵要发光';}"
                "</style></head><body><div class='card'></div></body></html>"
            ),
        }
        with pytest.raises(ValidationError, match="一字不差"):
            _validated_design(raw, required_text="本喵要发光")

    def test_decoration_mode_still_honors_needed(self) -> None:
        raw = {
            **self._BASE,
            "html": _HTML,
            "needed": False,
            "skip_reason": "画面已经很满",
        }
        assert _validated_design(raw) == "画面已经很满"


class TestAlphaPlaneStats:
    def _plane(
        self,
        width: int,
        height: int,
        visible: set[tuple[int, int]],
    ) -> bytes:
        return bytes(
            255 if (x, y) in visible else 0
            for y in range(height)
            for x in range(width)
        )

    def test_centered_content_has_no_edge_contact(self) -> None:
        visible = {(x, y) for x in range(2, 8) for y in range(2, 8)}
        coverage, edge = _alpha_plane_stats(
            self._plane(10, 10, visible),
            10,
            10,
        )
        assert coverage == pytest.approx(0.36)
        assert edge == 0.0

    def test_clipped_content_touches_edge(self) -> None:
        visible = {(9, y) for y in range(3, 8)}
        coverage, edge = _alpha_plane_stats(
            self._plane(10, 10, visible),
            10,
            10,
        )
        assert coverage == pytest.approx(0.05)
        assert edge == pytest.approx(0.5)

    def test_geometry_mismatch_returns_unknown(self) -> None:
        assert _alpha_plane_stats(b"\xff" * 10, 10, 10) == (-1.0, -1.0)


def _edit_element(
    element_id: str,
    start_tick: int,
    intent: str,
) -> TimelineElement:
    return TimelineElement(
        element_id=element_id,
        span=TimelineSpan(start_tick=start_tick, duration_tick=1000),
        location=ElementLocation(),
        creation=EditCreation(intent=intent),
    )


class TestSelectDecorationIds:
    def _elements(self, count: int) -> list[TimelineElement]:
        return [
            _edit_element(f"seg-{index}", index * 1000, f"意图 {index}")
            for index in range(count)
        ]

    def _timeline(self) -> Timeline:
        return Timeline(timeline_id="tl-1")

    def test_zero_budget_selects_nothing(self) -> None:
        selected = asyncio.run(
            _select_decoration_ids(
                edit_elements=self._elements(5),
                timeline=self._timeline(),
                budget=0,
                brief="",
            ),
        )
        assert selected == set()

    def test_few_segments_all_selected_without_model_call(self) -> None:
        selected = asyncio.run(
            _select_decoration_ids(
                edit_elements=self._elements(2),
                timeline=self._timeline(),
                budget=3,
                brief="",
            ),
        )
        assert selected == {"seg-0", "seg-1"}

    def test_model_answer_is_filtered_and_truncated(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_chat(*args, **kwargs):
            return (
                '{"selected": ["seg-1", "ghost", "seg-4", "seg-0", "seg-2"]}'
            )

        monkeypatch.setattr(
            motion_design.vlm_model,
            "chat_completion",
            fake_chat,
        )
        selected = asyncio.run(
            _select_decoration_ids(
                edit_elements=self._elements(6),
                timeline=self._timeline(),
                budget=2,
                brief="轻快",
            ),
        )
        assert selected == {"seg-1", "seg-4"}

    def test_model_failure_falls_back_to_even_sampling(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def broken_chat(*args, **kwargs):
            raise RuntimeError("model down")

        monkeypatch.setattr(
            motion_design.vlm_model,
            "chat_completion",
            broken_chat,
        )
        elements = self._elements(9)
        selected = asyncio.run(
            _select_decoration_ids(
                edit_elements=elements,
                timeline=self._timeline(),
                budget=3,
                brief="",
            ),
        )
        assert len(selected) == 3
        assert selected <= {element.element_id for element in elements}


class TestApplyOverlayStyledRouting:
    def _runner(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> FfmpegLocalMediaRunner:
        runner = FfmpegLocalMediaRunner(executable="ffmpeg")
        monkeypatch.setattr(
            FfmpegLocalMediaRunner,
            "_probe_video_size",
            lambda self, path: (1280, 720),
        )
        return runner

    def _input(
        self,
        tmp_path,
        *,
        motion: dict | None,
    ) -> tuple[LocalMediaInput, object]:
        segment = tmp_path / "segment.mp4"
        segment.write_bytes(b"original")
        overlay = {
            "kind": "pet_os",
            "text": "本喵要发光",
            "vibe": "chill",
            "appear_at": 0.0,
            "duration": 4.0,
            "motion": motion,
        }
        item = LocalMediaInput(
            version_id="ver-1",
            file_id=None,
            checksum="0" * 64,
            media_type="video/mp4",
            path=segment,
            source_ref="source:src-1",
            start_seconds=0.0,
            end_seconds=4.0,
            overlay=overlay,
        )
        return item, segment

    def test_motion_success_skips_fixed_template(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        def fake_motion(**kwargs):
            calls.append("motion")
            kwargs["output_path"].write_bytes(b"styled")
            return OverlayRenderResult(success=True)

        def fake_pet_os(**kwargs):
            calls.append("pet_os")
            return OverlayRenderResult(success=True)

        monkeypatch.setattr(
            local_execution_module,
            "render_motion_overlay",
            fake_motion,
        )
        monkeypatch.setattr(
            local_execution_module,
            "render_pet_os_overlay",
            fake_pet_os,
        )
        runner = self._runner(monkeypatch)
        item, segment = self._input(
            tmp_path,
            motion={"html": _HTML, "fps": 24, "loop": False},
        )
        warning = runner._apply_overlay(item, segment)
        assert warning is None
        assert calls == ["motion"]
        assert segment.read_bytes() == b"styled"

    def test_motion_failure_falls_back_with_warning(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        def fake_motion(**kwargs):
            calls.append("motion")
            return OverlayRenderResult(success=False, error="capture crashed")

        def fake_pet_os(**kwargs):
            calls.append("pet_os")
            kwargs["output_path"].write_bytes(b"bubble")
            return OverlayRenderResult(success=True)

        monkeypatch.setattr(
            local_execution_module,
            "render_motion_overlay",
            fake_motion,
        )
        monkeypatch.setattr(
            local_execution_module,
            "render_pet_os_overlay",
            fake_pet_os,
        )
        runner = self._runner(monkeypatch)
        item, segment = self._input(
            tmp_path,
            motion={"html": _HTML, "fps": 24, "loop": False},
        )
        warning = runner._apply_overlay(item, segment)
        assert warning is not None
        assert "回退固定样式" in warning
        assert "capture crashed" in warning
        assert calls == ["motion", "pet_os"]
        assert segment.read_bytes() == b"bubble"

    def test_without_motion_uses_fixed_template(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        def fake_motion(**kwargs):
            calls.append("motion")
            return OverlayRenderResult(success=True)

        def fake_pet_os(**kwargs):
            calls.append("pet_os")
            kwargs["output_path"].write_bytes(b"bubble")
            return OverlayRenderResult(success=True)

        monkeypatch.setattr(
            local_execution_module,
            "render_motion_overlay",
            fake_motion,
        )
        monkeypatch.setattr(
            local_execution_module,
            "render_pet_os_overlay",
            fake_pet_os,
        )
        runner = self._runner(monkeypatch)
        item, segment = self._input(tmp_path, motion=None)
        warning = runner._apply_overlay(item, segment)
        assert warning is None
        assert calls == ["pet_os"]
