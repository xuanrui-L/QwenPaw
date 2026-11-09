# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=unused-argument,protected-access,redefined-outer-name

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from domain.errors import ValidationError
from services.media_files import local_execution as local_execution_module
from services.media_files import motion_design
from services.media_files.local_execution import (
    FfmpegLocalMediaRunner,
    LocalMediaInput,
)
from services.media_files.motion_design import (
    _design_canvas_size,
    _repair_common_html_slips,
    _select_decoration_ids,
    _validated_design,
    _validate_caption_location,
    _validated_location,
)
from services.media_files.motion_overlay import (
    MotionDocumentProbe,
    MotionLayerPrep,
    PreparedMotionLayer,
)
from services.media_files.motion_templates import (
    MOTION_TEMPLATE_VERSION,
    SUPPORTED_MOTIFS,
    render_caption_template,
    render_decoration_template,
)
from services.media_files.overlay import OverlayRenderResult
from services.project_files.models import (
    EditCreation,
    ElementLocation,
    Timeline,
    TimelineElement,
    TimelineSpan,
)

_HTML = "<html><body><div class='card'>本喵要发光</div></body></html>"


class TestValidatedDesignTextMode:
    _BASE = {
        "concept": "发光字幕卡",
        "fps": 24,
        "location": {"x": 0.9, "y": 0.3, "width": 0.2, "height": 0.4},
    }

    @pytest.mark.parametrize(
        ("html", "match"),
        [
            (_HTML.replace("本喵要发光", "本喵想发光"), "一字不差"),
            (
                # CSS-only text never counts as verbatim copy.
                "<html><head><style>.card::after{content:'本喵要发光';}"
                "</style></head><body><div class='card'></div></body></html>",
                "一字不差",
            ),
        ],
        ids=["rewritten-copy", "css-only-copy"],
    )
    def test_non_verbatim_text_is_rejected(
        self,
        html: str,
        match: str,
    ) -> None:
        with pytest.raises(ValidationError, match=match):
            _validated_design(
                {**self._BASE, "html": html},
                required_text="本喵要发光",
            )

    def test_wrapping_is_tolerated(self) -> None:
        design = _validated_design(
            {**self._BASE, "html": _HTML.replace("本喵要发光", "本喵\n要发光")},
            required_text="本喵要发光",
            default_loop=False,
        )
        assert not isinstance(design, str)

    def test_scene_mode_allows_visible_text(self) -> None:
        # Full-canvas motion clips may carry copy; only decorations must
        # stay text-free.
        raw = {**self._BASE, "needed": True, "html": _HTML}
        with pytest.raises(ValidationError, match="不允许包含任何可见文字"):
            _validated_design(raw, default_loop=False)
        design = _validated_design(
            raw,
            allow_visible_text=True,
            default_loop=False,
        )
        assert not isinstance(design, str)


class TestMotionDesignSafety:
    _DECOR = {
        "needed": True,
        "concept": "纯图形闪光",
        "fps": 24,
        "location": {
            "x": 0.1,
            "y": 0.1,
            "width": 0.2,
            "height": 0.2,
            "anchor_x": 0,
            "anchor_y": 0,
        },
    }

    def test_location_box_must_stay_inside_canvas(self) -> None:
        # An overshooting box is translated back inside (the size and
        # edge-hugging intent are unambiguous), never rejected outright.
        clamped = _validated_location(
            {
                "x": 0.95,
                "y": 0.1,
                "width": 0.2,
                "height": 0.2,
                "anchor_x": 0,
                "anchor_y": 0,
            },
        )
        assert clamped.x == pytest.approx(0.8)
        assert clamped.y == pytest.approx(0.1)
        left = clamped.x - clamped.anchor_x * clamped.width
        assert 0.0 <= left and left + clamped.width <= 1.0 + 1e-9
        # A box larger than the canvas is stopped by the size gate.
        with pytest.raises(ValidationError, match="1% 到 100%"):
            _validated_location(
                {"x": 0.5, "y": 0.5, "width": 1.0, "height": 1.2},
            )

    def test_caption_geometry_gate_rejects_narrow_accepts_wide(self) -> None:
        narrow = ElementLocation(
            x=0.9,
            y=0.3,
            width=0.12,
            height=0.40,
            anchor_x=0.5,
            anchor_y=0.5,
        )
        with pytest.raises(ValidationError, match="location.width 太窄"):
            _validate_caption_location(narrow, "这红色是什么", (1280, 720))
        wide = ElementLocation(
            x=0.5,
            y=0.85,
            width=0.60,
            height=0.20,
            anchor_x=0.5,
            anchor_y=0.5,
        )
        _validate_caption_location(wide, "这红色是什么", (1280, 720))

    def test_default_subtitle_box_fits_two_languages(self) -> None:
        from services.media_files.motion_templates import (
            DEFAULT_CAPTION_LOCATION,
        )

        _validate_caption_location(
            ElementLocation(**DEFAULT_CAPTION_LOCATION),
            "让画面自己讲故事\nLet the picture tell its story",
            (1280, 720),
        )

    # Each caption_role is documented in the agent prompt with the kind of
    # text it carries, so its preset has to render that text rather than an
    # arbitrary sample.  These budgets are the documented worst case.
    _ROLE_TEXTS = {
        "dialogue": [
            "让画面自己讲故事\nLet the picture tell its story",
            "那天晚上他站在门口很久什么都没有说",
        ],
        "narration": [
            "让画面自己讲故事\nLet the picture tell its story",
            "后来的很多年里，我常常想起那个下午",
        ],
        "opening_title": ["星河入梦", "星河入梦·第一幕"],
        "opening_credit": ["导演 张三\n编剧 李四", "主演 王五 赵六"],
        "ending_credit": ["感谢\n联合出品 星辰影业\n特别鸣谢 各位观众"],
        "location": ["巴黎，1942", "三年后"],
        "chapter": ["第二章 雨夜告别", "幕间"],
        "interview_summary": [
            "让画面自己讲故事\nLet the picture tell its story",
            "这座城市的光伏装机量在五年内增长了十二倍",
        ],
    }
    # The guard has to sample the canvases the design path can actually hand
    # it.  ``_design_canvas_size`` only ever uses a 720 or 1080 short side, so
    # a hand-written 4K row tested a geometry that never reaches this code.
    _CAPTION_CANVASES = [
        _design_canvas_size(
            SimpleNamespace(
                settings=SimpleNamespace(
                    aspect_ratio=ratio,
                    resolution=resolution,
                ),
            ),
        )
        for ratio in ("16:9", "9:16", "1:1", "4:5")
        for resolution in ("1280x720", "1920x1080")
    ]

    @pytest.mark.parametrize("role", sorted(_ROLE_TEXTS))
    def test_caption_role_preset_renders_its_documented_text(
        self,
        role: str,
    ) -> None:
        """A box the renderer rejects silently clips words in the cut.

        Compose only logs a rejected caption geometry and keeps going, so
        this is the layer that catches an illegal preset before it ships.
        """
        from services.media_files.motion_templates import (
            CAPTION_ROLE_LOCATIONS,
        )

        assert role in CAPTION_ROLE_LOCATIONS, f"{role} 没有预设"
        location = ElementLocation(**CAPTION_ROLE_LOCATIONS[role])
        for canvas in self._CAPTION_CANVASES:
            for text in self._ROLE_TEXTS[role]:
                _validate_caption_location(location, text, canvas)

    def test_caption_role_presets_stay_inside_the_safe_area(self) -> None:
        """An off-canvas caption is unreadable no matter how the box is sized.

        The preset table and the validator now read the same numbers, so this
        guard proves the table the gate judges is self-consistent: some
        presets sit exactly on the 0.96 line by construction.
        """
        from services.media_files.motion_templates import (
            CAPTION_ROLE_LOCATIONS,
            CAPTION_SAFE_AREA_EDGES,
            caption_box_edges,
            caption_box_out_of_safe_area,
        )

        safe_left, safe_top, safe_right, safe_bottom = CAPTION_SAFE_AREA_EDGES
        for role, preset in CAPTION_ROLE_LOCATIONS.items():
            assert not caption_box_out_of_safe_area(preset), f"{role} 越出安全区"
            left, top, right, bottom = caption_box_edges(preset)
            assert left >= safe_left, f"{role} 左边越界 {left:.3f}"
            assert top >= safe_top, f"{role} 顶边越界 {top:.3f}"
            assert right <= safe_right, f"{role} 右边越界 {right:.3f}"
            assert bottom <= safe_bottom, f"{role} 底边越界 {bottom:.3f}"

    def test_caption_box_outside_the_safe_area_is_rejected(self) -> None:
        """The size floors alone leave the frame-edge box perfectly legal.

        The prompt used to teach exactly this box: 0.18 high with a centre
        anchor reaches 0.97 and burns the last line into the frame edge, so
        the gate has to reject it by name instead of only warning about it
        in a comment.
        """
        from services.media_files.motion_templates import (
            CAPTION_ROLE_LOCATIONS,
        )

        burnt = ElementLocation(
            x=0.5,
            y=0.88,
            width=0.8,
            height=0.18,
            anchor_x=0.5,
            anchor_y=0.5,
        )
        text = "那天晚上他站在门口很久什么都没有说"
        with pytest.raises(ValidationError, match="安全区"):
            _validate_caption_location(burnt, text, (1280, 720))
        # The role preset that replaces it stays legal on the same canvas, so
        # the gate cannot reject its own table.
        _validate_caption_location(
            ElementLocation(**CAPTION_ROLE_LOCATIONS["dialogue"]),
            text,
            (1280, 720),
        )

    def test_documented_copy_budget_fits_the_preset_it_promises(self) -> None:
        """A prompt ceiling the preset cannot hold silently clips words.

        The director prompt caps an interview summary at 30 CJK characters,
        so 30 must be legal on that box.  The documented margin is thin (the
        preset holds 33), which is exactly why it is pinned here.
        """
        from services.media_files.motion_templates import (
            CAPTION_ROLE_LOCATIONS,
        )

        preset = ElementLocation(
            **CAPTION_ROLE_LOCATIONS["interview_summary"],
        )
        _validate_caption_location(preset, "访谈要点摘要" * 5, (1280, 720))
        with pytest.raises(ValidationError, match="height"):
            _validate_caption_location(preset, "访" * 40, (1280, 720))

    def test_caption_roles_cover_every_prompted_value(self) -> None:
        """A role the model can write but the preset table lacks would fall
        through to the generic box, silently undoing the layout intent.

        The truth source has to be the model's own Literal, not a list this
        file keeps by hand, or adding a role would leave this guard green.
        """
        from typing import Literal, get_args, get_origin

        from services.media_files.motion_templates import (
            CAPTION_ROLE_LOCATIONS,
        )
        from services.project_files.models import OverlayCreation

        def literal_members(annotation: object) -> set[str]:
            for arg in get_args(annotation):
                if get_origin(arg) is Literal:
                    return set(str(value) for value in get_args(arg))
                nested = literal_members(arg)
                if nested:
                    return nested
            return set()

        prompted = literal_members(
            OverlayCreation.model_fields["caption_role"].annotation,
        )
        assert prompted, "caption_role 不再是 Literal，本守卫已失效"
        assert set(CAPTION_ROLE_LOCATIONS) == prompted
        # The rendering guard above only samples the roles listed here, so a
        # new role must arrive with a text budget, not just a preset.
        assert set(self._ROLE_TEXTS) == prompted

    def test_active_or_embedded_content_is_rejected(self) -> None:
        html = (
            "<html><body><iframe src='file:///etc/passwd'>"
            "</iframe></body></html>"
        )
        with pytest.raises(ValidationError):
            _validated_design({**self._DECOR, "html": html})

    def test_allowlisted_motif_uses_trusted_template(self) -> None:
        design = _validated_design(
            {
                **self._DECOR,
                "motif": "approval_checks",
                "primary_color": "#66aa55",
                "secondary_color": "#224422",
                "html": "",
            },
        )
        assert not isinstance(design, str)
        motion, _location, _concept = design
        assert motion.motif == "approval_checks"
        assert motion.template_version == MOTION_TEMPLATE_VERSION
        assert 'data-motion-motif="approval_checks"' in motion.html
        assert "#66aa55" in motion.html


class TestMotionTemplates:
    @pytest.mark.parametrize("motif", sorted(SUPPORTED_MOTIFS))
    def test_every_template_is_animated_and_visible_at_t0(
        self,
        motif: str,
    ) -> None:
        """Every motif must be tagged, animated, and show at least one
        shaped element at t=0, or the probe rejects the first frame."""
        html = render_decoration_template(motif)
        assert f'data-motion-motif="{motif}"' in html
        assert "@keyframes" in html
        assert len(html) >= 32

        style_start = html.index("<style>")
        style_end = html.index("</style>")
        css = html[style_start:style_end]
        body = html[style_end:]

        shape_classes = re.findall(r'class="shape\s+([\w-]+)', body)
        assert shape_classes, f"motif {motif!r} has no .shape elements"

        css_props: dict[str, str] = {}
        for cls in set(shape_classes):
            match = re.search(rf"\.{cls}\{{([^}}]+)\}}", css)
            if match:
                css_props[cls] = match.group(1)

        # A shape is visible at t=0 if its base opacity is non-zero, or an
        # immediate (no-delay) animation starts its 0% keyframe above zero.
        for cls in shape_classes:
            props = css_props.get(cls, "")
            opacity_match = re.search(r"(?<!\w)opacity:([^;]+)", props)
            if opacity_match is None or opacity_match.group(1).strip() != "0":
                return
            anim_match = re.search(r"animation:([\w-]+)", props)
            if anim_match and "animation-delay" not in props:
                kf = re.search(
                    rf"@keyframes\s+{anim_match.group(1)}\s*\{{0%{{([^}}]+)",
                    css,
                )
                if kf:
                    kf_opacity = re.search(r"opacity:([^;]+)", kf.group(1))
                    if (
                        kf_opacity is None
                        or kf_opacity.group(1).strip() != "0"
                    ):
                        return

        raise AssertionError(
            f"motif {motif!r} has no shaped element visible at t=0; "
            "the probe will reject it as an empty first frame",
        )

    def test_caption_fallback_keeps_exact_text_and_escapes_markup(
        self,
    ) -> None:
        html = render_caption_template("飞！<安全>", emotion="action")
        assert "飞！&lt;安全&gt;" in html
        assert 'data-motion-motif="caption_card"' in html


class TestSelectDecorationIds:
    def _elements(self, count: int) -> list[TimelineElement]:
        return [
            TimelineElement(
                element_id=f"seg-{index}",
                span=TimelineSpan(start_tick=index * 1000, duration_tick=1000),
                location=ElementLocation(),
                creation=EditCreation(intent=f"意图 {index}"),
            )
            for index in range(count)
        ]

    def _select(self, elements, budget):
        return asyncio.run(
            _select_decoration_ids(
                edit_elements=elements,
                timeline=Timeline(timeline_id="tl-1"),
                budget=budget,
                brief="",
            ),
        )

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
        assert self._select(self._elements(6), 2) == {"seg-1", "seg-4"}

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
        selected = self._select(elements, 3)
        assert len(selected) == 3
        assert selected <= {element.element_id for element in elements}


def _layer_prep(tmp_path) -> MotionLayerPrep:
    """A minimal prepared layer for mocking the capture stage."""
    return MotionLayerPrep(
        layer=PreparedMotionLayer(
            frames_dir=tmp_path,
            frame_count=1,
            effective_fps=24.0,
            appear_at=0.0,
            duration=1.0,
            left=0,
            top=0,
            opacity=1.0,
            period_mode=False,
            managed_exit=False,
        ),
    )


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
        monkeypatch.setattr(
            local_execution_module,
            "probe_motion_document",
            lambda *args, **kwargs: MotionDocumentProbe(
                ok=True,
                animation_count=1,
                edge_contact=0.0,
                text_occlusion=0.0,
            ),
        )
        return runner

    def _input(
        self,
        tmp_path,
        *,
        motion,
        location=None,
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
            "location": location,
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
            overlays=(overlay,),
        )
        return item, segment

    def test_unsafe_stored_motion_falls_back_during_compose(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        calls: list[str] = []
        safe_motion_args: list[tuple[dict, str]] = []

        def fake_prepare(**kwargs):
            calls.append("prepare")
            safe_motion_args.append((kwargs["location"], kwargs["html"]))
            return _layer_prep(tmp_path)

        def fake_burn(**kwargs):
            calls.append("burn")
            kwargs["output_path"].write_bytes(b"safe motion")
            return OverlayRenderResult(success=True)

        monkeypatch.setattr(
            local_execution_module,
            "prepare_motion_layer",
            fake_prepare,
        )
        monkeypatch.setattr(
            local_execution_module,
            "composite_motion_layers",
            fake_burn,
        )
        runner = self._runner(monkeypatch)
        item, segment = self._input(
            tmp_path,
            motion={"html": _HTML, "fps": 24, "loop": False},
            location={
                "x": 0.9,
                "y": 0.3,
                "width": 0.12,
                "height": 0.4,
                "anchor_x": 0.5,
                "anchor_y": 0.5,
            },
        )

        warnings = runner._apply_overlay(item, segment)

        assert calls == ["prepare", "burn"]
        assert segment.read_bytes() == b"safe motion"
        assert len(warnings) == 1
        assert "未通过合成安全检查" in warnings[0]
        assert "location.width 太窄" in warnings[0]
        assert "统一安全动效模板" in warnings[0]
        location, html = safe_motion_args[0]
        assert location["width"] == 0.8 and location["y"] == 0.86
        assert 'data-motion-motif="caption_card"' in html


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{}, {"captionStyle": "uniform"}, {"captionStyle": "varied"}],
)
async def test_plain_subtitles_need_no_edit_or_design_model(
    tmp_path,
    monkeypatch,
    arguments,
):
    from unittest.mock import AsyncMock
    from services.project_files.facade import CreatorFileServices
    from services.project_files.models import OverlayCreation, Project

    services = CreatorFileServices.create(tmp_path)
    project = Project.new(project_id="plain-subtitle", name="普通字幕")
    elements = project.timelines.items["timeline:main"].elements_by_id
    for index, text in enumerate(("让画面自己讲故事", "Simple words, clear picture")):
        elements[f"caption-{index}"] = TimelineElement(
            element_id=f"caption-{index}",
            span=TimelineSpan(start_tick=index * 3000, duration_tick=3000),
            location=ElementLocation(x=0.5, y=0.5, width=0.12, height=0.4),
            creation=OverlayCreation(text=text),
        )
    services.projects.create(project)
    model = AsyncMock(
        side_effect=AssertionError(
            "Plain subtitles must not request a generated style",
        ),
    )
    monkeypatch.setattr(motion_design, "_design_document", model)
    monkeypatch.setattr(motion_design.vlm_model, "chat_completion", model)
    result = await motion_design.design_motion_overlays(
        services,
        project_id=project.project_id,
        target_ref="timeline:main",
        arguments=arguments,
        idempotency_key="plain-subtitles",
    )
    assert result["designedCount"] == 2
    current = services.projects.read(project.project_id).project
    for element in current.timelines.items[
        "timeline:main"
    ].elements_by_id.values():
        motion = element.creation.motion
        assert motion.format == "html_css" and motion.template_version == 2
        assert element.location.y == 0.86
        assert element.location.y + element.location.height / 2 <= 0.96
        indexed = current.assets.files_by_id[motion.html_file_id]
        html = (
            services.projects.project_root(project.project_id)
            / indexed.relative_uri
        ).read_text()
        assert 'data-caption-style="plain"' in html
        assert element.creation.text in html
        assert "border:" not in html and "border-radius:" not in html
        assert "box-shadow:" not in html and "transform:" not in html
        assert 'data-motion-exit="none"' in html
    model.assert_not_called()


@pytest.mark.asyncio
async def test_plain_subtitle_redesign_preserves_authored_position(tmp_path):
    from services.project_files.facade import CreatorFileServices
    from services.project_files.models import (
        MotionGraphic,
        OverlayCreation,
        Project,
    )

    services = CreatorFileServices.create(tmp_path)
    project = Project.new(project_id="caption-position", name="字幕位置")
    location = ElementLocation(x=0.28, y=0.3, width=0.4, height=0.22)
    project.timelines.items["timeline:main"].elements_by_id[
        "caption"
    ] = TimelineElement(
        element_id="caption",
        span=TimelineSpan(start_tick=0, duration_tick=3000),
        location=location,
        creation=OverlayCreation(
            text="清晰，简单",
            motion=MotionGraphic(html=_HTML, template_version=1),
        ),
    )
    services.projects.create(project)
    preserved = await motion_design.design_motion_overlays(
        services,
        project_id=project.project_id,
        target_ref="timeline:main",
        arguments={},
        idempotency_key="keep-authored",
    )
    assert preserved["designedCount"] == 0
    updated = await motion_design.design_motion_overlays(
        services,
        project_id=project.project_id,
        target_ref="timeline:main",
        arguments={"elementIds": ["caption"]},
        idempotency_key="redesign-caption",
    )
    assert updated["designedCount"] == 1
    element = (
        services.projects.read(project.project_id)
        .project.timelines.items["timeline:main"]
        .elements_by_id["caption"]
    )
    assert element.location == location
    assert element.creation.motion.template_version == 2


class TestSegmentCache:
    """Finished-segment cache: identity coverage and round trip."""

    def _spec(self, tmp_path):
        from domain.enums import CreatorCommandType
        from services.media_files.local_execution import (
            LocalMediaExecutionSpec,
        )

        return LocalMediaExecutionSpec(
            command=CreatorCommandType.COMPOSE_FINAL_VIDEO,
            target_ref="timeline:main",
            task_id="task-1",
            work_dir=tmp_path,
            output_path=tmp_path / "out.mp4",
            inputs=(),
            transitions=(),
            audio_plan="",
            expected_duration_seconds=None,
            canvas_size=(1280, 720),
        )

    def _item(self, tmp_path, *, checksum="a" * 64, overlays=()):
        segment = tmp_path / "src.mp4"
        segment.write_bytes(b"src")
        return LocalMediaInput(
            version_id="ver-1",
            file_id=None,
            checksum=checksum,
            media_type="video/mp4",
            path=segment,
            source_ref="element:clip-1",
            start_seconds=0.0,
            end_seconds=4.0,
            overlays=tuple(overlays),
        )

    def test_key_tracks_burned_layer_checksum_not_html(self, tmp_path) -> None:
        runner = FfmpegLocalMediaRunner(executable="ffmpeg")
        spec = self._spec(tmp_path)

        def overlay(checksum: str, html: str) -> dict:
            return {
                "kind": "pet_os",
                "text": "第一句",
                "vibe": "chill",
                "appear_at": 0.0,
                "duration": 2.0,
                "location": None,
                "element_id": "overlay-1",
                "motion": {
                    "format": "html_js",
                    "html": html,
                    "checksum": checksum,
                    "fps": 24,
                    "loop": False,
                },
            }

        def key(spec_, item_):
            return runner._segment_cache_key(
                spec_,
                item_,
                segment_duration=4.0,
                freeze_duration=0.0,
            )

        base = key(
            spec,
            self._item(tmp_path, overlays=[overlay("c1", "<html>a")]),
        )
        # Identical content with a different (never-fingerprinted) html
        # body: hydration state must not split the cache.
        assert base == key(
            spec,
            self._item(tmp_path, overlays=[overlay("c1", "<html>b")]),
        )
        assert base != key(
            spec,
            self._item(tmp_path, overlays=[overlay("c2", "<html>a")]),
        )
        # Canvas geometry always reaches the key.
        other_canvas = self._spec(tmp_path)
        object.__setattr__(other_canvas, "canvas_size", (1920, 1080))
        assert base != key(
            other_canvas,
            self._item(tmp_path, overlays=[overlay("c1", "<html>a")]),
        )
        # No stable source checksum -> never cached.
        assert key(spec, self._item(tmp_path, checksum="")) is None


def test_repair_recovers_missing_script_close_tag() -> None:
    # Field run 2026-08-09: the model dropped </script> after the vendor
    # include, browsers swallowed the inline timeline, and every retry
    # died on "__hf 未注册" — a pure syntax slip, fixed deterministically.
    nested = '<script src="vendor/gsap.min.js">\n<script>var tl = 1;</script>'
    fixed = _repair_common_html_slips(nested)
    assert fixed.count("</script>") == 2
    inline_in_src = '<script src="vendor/gsap.min.js">var tl = 2;</script>'
    fixed2 = _repair_common_html_slips(inline_in_src)
    assert fixed2.count("<script") == 2 and "var tl = 2;" in fixed2
    # Well-formed documents pass through unchanged.
    good = '<script src="vendor/gsap.min.js"></script><script>var tl = 3;</script>'
    assert _repair_common_html_slips(good) == good


def test_repair_lifts_zero_starting_opacity() -> None:
    html = (
        "<style>.a{opacity:0;} .b{opacity:0.8}</style>"
        '<script src="vendor/gsap.min.js"></script>'
        "<script>tl.from('.a',{autoAlpha:0,y:20});"
        "tl.fromTo('.b',{opacity: 0.0},{opacity:1});"
        "tl.to('.c',{opacity:0.6});</script>"
    )
    fixed = _repair_common_html_slips(html)
    assert "autoAlpha:0.25" in fixed
    assert "opacity: 0.25" in fixed or "opacity:0.25" in fixed
    # Non-zero values stay untouched.
    assert "opacity:0.8" in fixed and "opacity:0.6" in fixed
