# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Regression coverage for video continuity and reference handling."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest

from domain.errors import ValidationError
from models.video_task_plans import VideoTaskKind, VideoTaskPlan
from services.file_agent_runtime.work_graph import derive_work_graph
from services.media_files import r2v_execution as rx
from services.media_files import segment_continuity as sc
from services.project_files.models import (
    ArtifactSlot,
    ArtifactVersion,
    AudioCreation,
    ElementOutput,
    I2VCreation,
    IndexedFile,
    Project,
    R2VCreation,
    R2VGenerationPlan,
    SourceAssetVersion,
    Timeline,
    TimelineElement,
    TimelineSpan,
)
from services.runtime_files import runtime_dependencies as rd

pytestmark = pytest.mark.unit


def _element(element_id: str, start: int, *, creation=None) -> TimelineElement:
    return TimelineElement(
        element_id=element_id,
        label=element_id,
        span=TimelineSpan(start_tick=start, duration_tick=4000),
        location={},
        creation=creation
        or R2VCreation(
            narrative="动作", storyboard_prompt="分镜", video_prompt="视频"
        ),
    )


def _project(*elements: TimelineElement) -> tuple[Project, Timeline]:
    project = Project.new(project_id="p-continuity", name="Continuity")
    timeline = project.timelines.items["timeline:main"]
    timeline.elements_by_id.update({el.element_id: el for el in elements})
    return project, timeline


def _publish_artifact(project, element, version, *, storyboard=False):
    output = "storyboard" if storyboard else "main"
    kind = "r2v_storyboard_image" if storyboard else "element_video"
    owner = f"element:{element.element_id}"
    slot = f"{owner}:{output}"
    file = f"file-{version}"
    checksum = "0" * 64
    created = "2026-08-05T00:00:00Z"
    project.assets.artifact_slots_by_id[slot] = ArtifactSlot(
        slot_id=slot,
        owner_ref=owner,
        kind=kind,
        version_ids=[version],
        selected_version_id=version,
    )
    project.assets.files_by_id[file] = IndexedFile(
        file_id=file,
        kind="artifact_payload",
        media_type="image/png" if storyboard else "video/mp4",
        relative_uri=f"assets/artifacts/{file}",
        sha256=checksum,
        size_bytes=1,
        created_at=created,
    )
    project.assets.artifact_versions_by_id[version] = ArtifactVersion(
        version_id=version,
        name=version,
        slot_id=slot,
        kind=kind,
        owner_ref=owner,
        file_id=file,
        checksum=checksum,
        based_on_generation=1,
        created_at=created,
    )
    if storyboard:
        element.outputs[output] = ElementOutput(slot_id=slot)


def _two_segment_project(prev_version: str | None = None):
    first, second = _element("e1", 0), _element("e2", 4000)
    second.creation.generation_plan = R2VGenerationPlan(
        method="r2v_last_frame",
        use_storyboard=False,
        predecessor_element_id=first.element_id,
        anchor_instruction="保留上一动作",
    )
    project, timeline = _project(first, second)
    if prev_version:
        _publish_artifact(project, first, prev_version)
    return project, timeline, first, second


def _frozen_task(*references: str, mode="r2v", **snapshot):
    return SimpleNamespace(
        task_id="task-inflight",
        project_id="p-continuity",
        kind="r2v_generation",
        status="RUNNING",
        input_refs=["element:e2"],
        metadata={
            "requestSnapshot": {
                "elementId": "e2",
                "mode": mode,
                "referenceVersionIds": list(references),
                **snapshot,
            }
        },
    )


def _inputs_current(project, task):
    return rx.FileR2VExecutionService._frozen_inputs_still_current(
        project, task
    )


def _bgm_project(tmp_path, *, with_bgm=True):
    project, timeline = _project(_element("e2", 4000))
    if with_bgm:
        relative_uri = "assets/sources/bed.mp3"
        music = tmp_path / relative_uri
        music.parent.mkdir(parents=True, exist_ok=True)
        music.write_bytes(b"ID3")
        timeline.elements_by_id["bgm"] = TimelineElement(
            element_id="bgm",
            label="bgm",
            span=TimelineSpan(start_tick=0, duration_tick=30000),
            creation=AudioCreation(
                source_asset_version_id="audio-v1", role="bgm"
            ),
        )
        created = "2026-08-05T00:00:00Z"
        checksum = "2" * 64
        project.assets.source_versions_by_id["audio-v1"] = SourceAssetVersion(
            version_id="audio-v1",
            logical_asset_id="asset-a1",
            name="bed",
            file_id="file-audio-v1",
            checksum=checksum,
            media_kind="audio",
            media_type="audio/mpeg",
            created_at=created,
        )
        project.assets.files_by_id["file-audio-v1"] = IndexedFile(
            file_id="file-audio-v1",
            kind="source_original",
            media_type="audio/mpeg",
            relative_uri=relative_uri,
            sha256=checksum,
            size_bytes=3,
            created_at=created,
        )
    return project, timeline


def _bind_bgm_config(monkeypatch, model="wan3.0-video"):
    monkeypatch.setattr("models.config.get_video_model_name", lambda: model)
    monkeypatch.setattr("models.config.get_video_backend", lambda: "wan")


@pytest.mark.parametrize(
    ("storyboard_version", "current"),
    [("sb-v1", True), ("sb-v2", False), (None, True)],
    ids=["predecessor-reroll", "storyboard-reselected", "no-storyboard"],
)
def test_frozen_r2v_publish_guard(storyboard_version, current):
    project, _, first, second = _two_segment_project("prev-v1")
    references = ["continuity:last_frame:prev-v1"]
    if storyboard_version:
        second.creation.generation_plan = None
        _publish_artifact(project, second, storyboard_version, storyboard=True)
        references += ["sb-v1", "continuity:tail_clip:prev-v1"]
    task = _frozen_task(*references)

    if current:
        _publish_artifact(project, first, "prev-v2")
    assert _inputs_current(project, task) is current


def _mock_ffmpeg_paths(monkeypatch):
    monkeypatch.setattr(rd, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(rd, "resolve_ffprobe", lambda **kwargs: "ffprobe")


def _frame_entry(tmp_path: Path, prev_version: str = "prev-version-1"):
    png = tmp_path / "last_frame.png"
    png.write_bytes(b"fake-png")
    return sc._build_entry(
        "last_frame",
        path=png,
        prev_version=prev_version,
        prev_element_id="e1",
        media_type="image/png",
        role_label="上一段末帧",
    )


def test_extract_registers_inflight_anchor(tmp_path, monkeypatch) -> None:
    project, timeline, _f, second = _two_segment_project("v1")
    source = tmp_path / "prev.mp4"
    source.write_bytes(b"fake-video")
    entry = _frame_entry(tmp_path)
    _mock_ffmpeg_paths(monkeypatch)
    monkeypatch.setattr(
        sc,
        "_resolve_prev_video_path",
        lambda *a, **k: source,
    )
    monkeypatch.setattr(sc, "_cut_last_frame", lambda **kw: (entry,))
    monkeypatch.setattr(sc, "_continuity_cache", OrderedDict())
    monkeypatch.setattr(sc, "_continuity_extract_locks", {})
    inflight: dict = {}
    monkeypatch.setattr(sc, "_continuity_inflight", inflight)

    sc.extract_continuity_artifacts(
        project=project,
        project_root=tmp_path,
        timeline=timeline,
        element=second,
    )
    assert sc._digest_for("v1") in inflight


def test_missing_ffmpeg_blocks_the_selected_method():
    project, _timeline, _first, _second = _two_segment_project("prev-v1")
    missing = derive_work_graph(project, ffmpeg_ready=False).by_id["video:e2"]
    assert "video:e1" in missing.deps
    assert missing.status.value == "gated"
    assert "ffmpeg" in missing.missing[0]


@pytest.fixture
def bgm_excerpt(tmp_path, monkeypatch):
    project, _ = _bgm_project(tmp_path)
    source = tmp_path / "bed.wav"
    source.write_bytes(b"source")
    _mock_ffmpeg_paths(monkeypatch)
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"cut")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(rx.subprocess, "run", run)
    monkeypatch.setattr(
        sc,
        "_probe_duration",
        lambda path, *_: 60.0 if path == source else 10.0,
    )

    def excerpt(budget, identity):
        return rx._bgm_excerpt(
            project,
            tmp_path,
            source,
            "e2",
            max_seconds=budget,
            source_identity=identity,
        )

    return excerpt, commands


def test_bgm_excerpt_parallel_same_source_budget(bgm_excerpt):
    excerpt, _ = bgm_excerpt
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = set(pool.map(lambda _: excerpt(15.0, "sha-x"), range(8)))
    assert len(results) == 1
    assert Path(unquote(urlsplit(results.pop()).path)).read_bytes() == b"cut"


def test_bgm_cache_identity_separates_budgets_and_sources(bgm_excerpt):
    excerpt, commands = bgm_excerpt
    first = excerpt(10.75, "sha-a")
    assert len({first, excerpt(10.2, "sha-a"), excerpt(10.75, "sha-b")}) == 3
    assert "head10.75" in Path(first).name
    assert len(commands) == 3
    assert "-c:a" not in commands[0]


def test_bgm_reference_budget_matrix(
    tmp_path,
    monkeypatch,
) -> None:
    def resolve(project, timeline, **kw):
        return rx._resolve_bgm_reference(
            project,
            timeline,
            tmp_path,
            element_id="e2",
            **kw,
        )

    _bind_bgm_config(monkeypatch)
    bare, bare_tl = _bgm_project(tmp_path, with_bgm=False)
    assert resolve(bare, bare_tl, voice_count=0) == ""

    with_bgm, bgm_tl = _bgm_project(tmp_path)
    assert resolve(with_bgm, bgm_tl, voice_count=5) == ""
    assert resolve(with_bgm, bgm_tl, voice_count=2, voice_seconds=14.5) == ""

    with monkeypatch.context() as unsupported:
        _bind_bgm_config(unsupported, "some-other-model")
        assert resolve(with_bgm, bgm_tl, voice_count=0) == ""

    def unavailable(*args, **kwargs):
        raise RuntimeError("ffprobe timeout")

    _mock_ffmpeg_paths(monkeypatch)
    monkeypatch.setattr(sc, "_probe_duration", unavailable)
    assert resolve(with_bgm, bgm_tl, voice_count=0) == ""


def test_over_budget_extension_cannot_fall_back_to_an_anchor(
    tmp_path, monkeypatch
):
    project, timeline, first, second = _two_segment_project("prev-v1")
    second.creation.generation_plan = R2VGenerationPlan(
        method="video_extend",
        use_storyboard=False,
        predecessor_element_id="e1",
    )
    source = tmp_path / "prev.mp4"
    source.touch()
    _mock_ffmpeg_paths(monkeypatch)
    monkeypatch.setattr(sc, "_resolve_prev_video_path", lambda *_: source)
    monkeypatch.setattr(sc, "_probe_duration", lambda *_: 31.0)
    monkeypatch.setattr(
        "models.config.get_video_model_name", lambda: "wan3.0-video-prime"
    )
    with pytest.raises(ValidationError, match="时长超过模型限制"):
        rx._build_task_plan(
            project,
            tmp_path,
            timeline,
            second,
            base_mode="r2v",
            duration_seconds=4,
            ratio="16:9",
        )
    frozen = rx._submission_input_fingerprint(
        project, "e2", mode="video_extend"
    )
    _publish_artifact(project, first, "prev-v2")
    assert not rx._matches_submission_inputs(
        project,
        "e2",
        frozen,
        mode="video_extend",
        payload_refs=["prev-v1"],
    )


@pytest.mark.parametrize(
    ("auto_first", "sent_tail", "authored_tail"),
    [
        (False, True, "tail"),
        (False, False, "tail"),
        (True, False, "tail"),
        (True, False, None),
    ],
)
def test_frozen_i2v_publish_guard(auto_first, sent_tail, authored_tail):
    first = _element("e1", 0)
    second = _element(
        "e2",
        4000,
        creation=I2VCreation(
            video_prompt="男孩走进走廊。",
            first_frame_version_id=None if auto_first else "frame",
            last_frame_version_id=authored_tail,
        ),
    )
    project, _ = _project(first, second)
    frozen_first = "continuity:last_frame:prev-v1" if auto_first else "frame"
    if auto_first:
        _publish_artifact(project, first, "prev-v1")
    task = _frozen_task(
        frozen_first,
        *(["tail"] if sent_tail else []),
        mode="i2v",
        firstFrameVersionId=frozen_first,
        firstFrameUrl="file:///first.png",
        lastFrameVersionId="tail" if sent_tail else "",
        lastFrameUrl="file:///tail.png" if sent_tail else "",
        lastFrameAuthoredVersionId=(authored_tail or "")
        if not sent_tail
        else "",
    )

    if auto_first:
        _publish_artifact(project, first, "prev-v2")
    assert _inputs_current(project, task)

    second.creation.last_frame_version_id = "tail-reselected"
    assert _inputs_current(project, task) is not sent_tail
    second.creation.first_frame_version_id = "frame-reselected"
    assert not _inputs_current(project, task)


def test_explicit_plan_controls_reference_identity_when_prose_suggests_a_cut():
    project, timeline, _first, second = _two_segment_project("prev-v")
    _publish_artifact(project, second, "sb-v1", storyboard=True)
    second.creation.continuity = "切去几年以后"
    refs = rx._reference_ids_with_continuity(project, timeline, second)
    assert refs == ("continuity:last_frame:prev-v",)
    assert derive_work_graph(project, ffmpeg_ready=True).by_id[
        "video:e2"
    ].deps == ("video:e1",)
    second.creation.generation_plan = R2VGenerationPlan(
        method="r2v", use_storyboard=True
    )
    second.creation.continuity = "接上前一帧"
    assert rx._reference_ids_with_continuity(project, timeline, second) == (
        "sb-v1",
    )
    assert derive_work_graph(project, ffmpeg_ready=True).by_id[
        "video:e2"
    ].deps == ("storyboard:e2",)


def test_legacy_project_keeps_its_mode_and_can_resume_a_frozen_anchor():
    project, timeline, first, second = _two_segment_project("prev-v1")
    second.creation.generation_plan = None
    second.creation.transition_intent = "one_take"
    second.creation.storyboard_intent = "none"
    assert rx._reference_ids_with_continuity(project, timeline, second) == ()
    assert derive_work_graph(project).by_id["video:e2"].deps == (
        "storyboard:e2",
    )
    refs = ("continuity:last_frame:prev-v1",)
    digest = rx._submission_input_fingerprint_legacy(
        project, "e2", references=refs
    )
    assert rx._matches_submission_inputs(
        project, "e2", digest, payload_refs=refs
    )
    _publish_artifact(project, first, "prev-v2")
    assert not rx._matches_submission_inputs(
        project, "e2", digest, payload_refs=refs
    )


@pytest.mark.parametrize("legacy", [True, False])
def test_frozen_extension_checks_material_without_requiring_a_storyboard(
    legacy,
):
    project, _, first, second = _two_segment_project("prev-v1")
    if legacy:
        second.creation.generation_plan = None
        second.creation.transition_intent = "one_take"
        snapshot = {}
    else:
        plan = R2VGenerationPlan(
            method="video_extend",
            use_storyboard=False,
            predecessor_element_id="e1",
        )
        second.creation.generation_plan = plan
        snapshot = {"generationPlan": plan.model_dump(mode="json")}

    task = _frozen_task("prev-v1", mode="video_extend", **snapshot)
    project.name = "无关的项目重命名"
    assert _inputs_current(project, task)

    _publish_artifact(project, first, "prev-v2")
    assert not _inputs_current(project, task)


@pytest.mark.parametrize("method", ["r2v_last_frame", "video_extend"])
def test_reference_preview_marks_a_disabled_predecessor_unavailable(method):
    from services.media_files.visual_reference_resolution import (
        preview_r2v_reference_order,
    )

    project, _, first, second = _two_segment_project("prev-v1")
    if method == "video_extend":
        second.creation.generation_plan = R2VGenerationPlan(
            method=method, use_storyboard=False, predecessor_element_id="e1"
        )

    def available():
        return preview_r2v_reference_order(project, "e2")["references"][0][
            "available"
        ]

    assert available()
    first.enabled = False
    assert not available()


def test_v10_frozen_i2v_claim_survives_the_upgrade():
    shot = _element(
        "e2",
        0,
        creation=I2VCreation(
            video_prompt="镜头推进", first_frame_version_id="first"
        ),
    )
    project, timeline = _project(shot)
    creation = shot.creation.model_dump(mode="json")
    for field in (
        "last_frame_version_id",
        "predecessor_element_id",
        "generate_audio",
    ):
        creation.pop(field)
    digest = rx._fingerprint(
        {
            "creation": creation,
            "span": shot.span.model_dump(mode="json"),
            "ticksPerSecond": timeline.ticks_per_second,
            "ratio": project.settings.aspect_ratio,
            "resolution": project.settings.resolution,
            "references": (),
        }
    )
    refs = ["first"]
    assert rx._matches_submission_inputs(
        project,
        "e2",
        digest,
        mode="i2v",
        payload_refs=refs,
    )
    shot.creation.last_frame_version_id = "new-tail"
    assert not rx._matches_submission_inputs(
        project,
        "e2",
        digest,
        mode="i2v",
        payload_refs=refs,
    )


def test_extension_authorization_uses_the_same_inputs_as_submission(
    tmp_path, monkeypatch
):
    from services.file_agent_runtime.driver import (
        _authorization_summary,
        billing_arguments_for,
    )
    from services.file_agent_runtime.workgraph_execution import (
        requested_work_node,
    )

    project, _, first, second = _two_segment_project("prev-v1")
    second.creation.generation_plan = R2VGenerationPlan(
        method="video_extend",
        use_storyboard=False,
        predecessor_element_id="e1",
    )
    _bind_bgm_config(monkeypatch, "wan3.0-video-prime")
    _mock_ffmpeg_paths(monkeypatch)
    source = tmp_path / "prev.mp4"
    source.touch()
    monkeypatch.setattr(sc, "_resolve_prev_video_path", lambda *args: source)
    monkeypatch.setattr(sc, "_probe_duration", lambda *args: 4.0)
    graph = derive_work_graph(project, ffmpeg_ready=True)
    snapshot = SimpleNamespace(project=project)
    request = requested_work_node(snapshot, graph.by_id["video:e2"])
    services = SimpleNamespace(
        projects=SimpleNamespace(
            read=lambda _: snapshot,
            project_root=lambda _: tmp_path,
        )
    )

    def terms():
        return asyncio.run(
            billing_arguments_for(
                services,
                project_id=project.project_id,
                spec=request.spec,
                tool_arguments=request.parameters,
            )
        )

    from services.media_files import visual_reference_resolution
    from services.prompt_sync_service import _references

    def unused(*args, **kwargs):
        pytest.fail("video extension must only resolve the predecessor video")

    with monkeypatch.context() as refs_only:
        refs_only.setattr(
            visual_reference_resolution, "video_reference_plan", unused
        )
        refs_only.setattr(
            visual_reference_resolution, "storyboard_reference_plan", unused
        )
        preview = _references(project, "e2")
        assert preview["storyboard"] == []
        assert [row["versionId"] for row in preview["video"]] == ["prev-v1"]

    assert request.parameters["mode"] == "video_extend"
    approved = terms()
    assert approved["extensionMaterialVersionId"] == "prev-v1"
    assert approved["timelineDurationSeconds"] == 4
    assert approved["durationSeconds"] == 8
    assert approved["ratio"] == "adaptive"
    summary = _authorization_summary(
        request.spec,
        target_ref="element:e2",
        provider="wan",
        model="wan3.0-video-prime",
        tool_arguments=approved,
    )
    assert "新增 4 秒" in summary and "提交 8 秒" in summary

    _publish_artifact(project, first, "prev-v2")
    with pytest.raises(ValidationError, match="续写前段版本已修改"):
        terms()


def test_extend_rejects_a_prompt_referencing_an_unsent_image(tmp_path):
    project, _timeline, _first, second = _two_segment_project("prev-v")
    with pytest.raises(ValidationError, match="不能引用未发送的图片"):
        rx._resolve_extend_request(
            project=project,
            project_root=tmp_path,
            element=second,
            element_id=second.element_id,
            target_ref="element:e2",
            plan=VideoTaskPlan(kind=VideoTaskKind.VIDEO_EXTEND),
            prompt="参考 [Image 1] 的服装。",
            resolution="720P",
            watermark=False,
            generate_audio=True,
            duration_seconds=4,
        )


def test_authored_dependency_is_exact_even_when_a_newer_shot_is_available():
    first = _element("first", start=0)
    closer = _element("closer", start=4000)
    third = _element("third", start=8000)
    third.creation.generation_plan = R2VGenerationPlan(
        method="r2v_last_frame",
        use_storyboard=False,
        predecessor_element_id="first",
        anchor_instruction="匹配第一镜尾帧动作",
    )
    project, timeline = _project(first, closer, third)
    _publish_artifact(project, first, "first-version")
    _publish_artifact(project, closer, "closer-version")
    node = derive_work_graph(project, ffmpeg_ready=True).by_id["video:third"]
    assert node.deps == ("video:first",)
    assert rx._reference_ids_with_continuity(project, timeline, third) == (
        "continuity:last_frame:first-version",
    )
