# -*- coding: utf-8 -*-
# Continuity payment/recovery guards (migrated from the retired
# test_segment_continuity.py suite: only the discriminating cases with
# no equivalent coverage elsewhere, per PR#290 review item 3).

# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access
# pylint: disable=use-implicit-booleaness-not-comparison
# pylint: disable=unused-argument
"""Unit coverage for the segment-continuity injection layer.

Pure helpers (predecessor lookup, capability split, prompt formatting,
fingerprint stability, budget accounting) sit alongside mocked ffmpeg
cases that exercise the atomic write and tail-clip soft-failure paths
without invoking a real provider. All cases are unit-level and never
reach out to a live video model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from models.video_capabilities import supports_video_continuation
from services.media_files.segment_continuity import (
    continuity_prompt_suffix,
    continuity_reference_ids,
    find_predecessor_video_element,
    resolve_predecessor_video_version,
)
from services.project_files.models import (
    ArtifactSlot,
    ElementLocation,
    I2VCreation,
    Project,
    R2VCreation,
    T2VCreation,
    Timeline,
    TimelineElement,
    TimelineSpan,
)

pytestmark = pytest.mark.unit


def _element(
    element_id: str,
    *,
    start: int,
    duration: int = 4000,
    enabled: bool = True,
    creation_type: str = "r2v",
) -> TimelineElement:
    if creation_type == "i2v":
        creation = I2VCreation(video_prompt="续写")
    elif creation_type == "t2v":
        creation = T2VCreation(video_prompt="纯文本")
    else:
        creation = R2VCreation(
            narrative="动作",
            storyboard_prompt="分镜",
            video_prompt="视频",
        )
    return TimelineElement(
        element_id=element_id,
        label=element_id,
        span=TimelineSpan(start_tick=start, duration_tick=duration),
        location=ElementLocation(),
        creation=creation,
        enabled=enabled,
    )


def _project(elements: list[TimelineElement]) -> tuple[Project, Timeline]:
    project = Project.new(project_id="p-continuity", name="Continuity")
    timeline = Timeline(timeline_id="timeline:main", ticks_per_second=1000)
    timeline = timeline.model_copy(
        update={
            "elements_by_id": {item.element_id: item for item in elements},
        },
    )
    project.timelines.items["timeline:main"] = timeline
    project.timelines.order = ["timeline:main"]
    return project, timeline


def _select_prev_video(
    project: Project,
    prev_element_id: str,
    version: str,
) -> None:
    project.assets.artifact_slots_by_id[
        f"element:{prev_element_id}:main"
    ] = ArtifactSlot(
        slot_id=f"element:{prev_element_id}:main",
        owner_ref=f"element:{prev_element_id}",
        kind="element_video",
        version_ids=[version],
        selected_version_id=version,
    )


def _publish_artifact(
    project: Project,
    element_id: str,
    version_id: str,
    *,
    stale: bool = False,
    media_type: str = "video/mp4",
) -> None:
    """Select a published artifact version on one element's main slot."""

    from services.project_files.models import ArtifactVersion, IndexedFile

    slot_id = f"element:{element_id}:main"
    project.assets.artifact_slots_by_id[slot_id] = ArtifactSlot(
        slot_id=slot_id,
        owner_ref=f"element:{element_id}",
        kind="element_video",
        version_ids=[version_id],
        selected_version_id=version_id,
    )
    project.assets.files_by_id[f"file-{version_id}"] = IndexedFile(
        file_id=f"file-{version_id}",
        kind="artifact_payload",
        media_type=media_type,
        relative_uri=f"assets/artifacts/file-{version_id}",
        sha256="0" * 64,
        size_bytes=1,
        created_at="2026-08-05T00:00:00Z",
    )
    project.assets.artifact_versions_by_id[version_id] = ArtifactVersion(
        version_id=version_id,
        name=version_id,
        slot_id=slot_id,
        kind="element_video",
        owner_ref=f"element:{element_id}",
        file_id=f"file-{version_id}",
        checksum="0" * 64,
        based_on_generation=1,
        provenance_refs=[],
        stale=stale,
        created_at="2026-08-05T00:00:00Z",
    )


def _two_segment_project(prev_version: str | None = None):
    first = _element("e1", start=0)
    second = _element("e2", start=4000)
    # Confirmed same scene (scene unknown = empty refs never anchors
    # by default, so the default-continuous fixtures must declare it).
    from services.project_files.models import VisualEntity

    for el in (first, second):
        el.creation = el.creation.model_copy(
            update={"scene_ref": "scene:stop"},
        )
    # Structured intent defaults: anchored simple continuation. The
    # planner no longer guesses routing from prose, so fixtures state
    # the intent explicitly.
    second.creation = second.creation.model_copy(
        update={
            "transition_intent": "continue",
            "storyboard_intent": "none",
        },
    )
    project, timeline = _project([first, second])
    project.visual.entities.items["scene:stop"] = VisualEntity(
        entity_id="scene:stop",
        kind="scene",
        name="站台",
        required_variant_ids=[],
    )
    if prev_version:
        _select_prev_video(project, "e1", prev_version)
    return project, timeline, first, second


def _frozen_task(snapshot: dict):
    from types import SimpleNamespace

    return SimpleNamespace(
        task_id="task-inflight",
        project_id="p-continuity",
        kind="r2v_generation",
        status="RUNNING",
        input_refs=["element:e2"],
        metadata={"requestSnapshot": snapshot},
    )


def _storyboard_selected(
    project,
    element,
    *,
    element_id: str = "e2",
    version_id: str = "sb-v1",
) -> None:
    """Select a storyboard artifact for an element (slot+file+version)."""

    from services.project_files.models import (
        ArtifactVersion,
        ElementOutput,
        IndexedFile,
    )

    slot_id = f"element:{element_id}:storyboard"
    project.assets.artifact_slots_by_id[slot_id] = ArtifactSlot(
        slot_id=slot_id,
        owner_ref=f"element:{element_id}",
        kind="r2v_storyboard_image",
        version_ids=[version_id],
        selected_version_id=version_id,
    )
    project.assets.files_by_id[f"file-{version_id}"] = IndexedFile(
        file_id=f"file-{version_id}",
        kind="artifact_payload",
        media_type="image/png",
        relative_uri=f"assets/artifacts/file-{version_id}",
        sha256="1" * 64,
        size_bytes=1,
        created_at="2026-08-05T00:00:00Z",
    )
    project.assets.artifact_versions_by_id[version_id] = ArtifactVersion(
        version_id=version_id,
        name=version_id,
        slot_id=slot_id,
        kind="r2v_storyboard_image",
        owner_ref=f"element:{element_id}",
        file_id=f"file-{version_id}",
        checksum="1" * 64,
        based_on_generation=1,
        provenance_refs=[],
        stale=False,
        created_at="2026-08-05T00:00:00Z",
    )
    element.outputs["storyboard"] = ElementOutput(slot_id=slot_id)


def _bgm_project(tmp_path, *, with_bgm: bool = True):
    from services.project_files.models import (
        AudioCreation,
        SourceAssetVersion,
    )

    first = _element("e1", start=0)
    second = _element("e2", start=4000)
    project, timeline = _project([first, second])
    if not with_bgm:
        return project, timeline, second
    music = tmp_path / "assets" / "sources" / "bed.mp3"
    music.parent.mkdir(parents=True, exist_ok=True)
    music.write_bytes(b"ID3" + b"\0" * 100)
    bgm_el = TimelineElement(
        element_id="bgm-1",
        label="bgm-1",
        span=TimelineSpan(start_tick=0, duration_tick=30000),
        creation=AudioCreation(
            source_asset_version_id="audio-v1",
            role="bgm",
        ),
    )
    timeline = timeline.model_copy(
        update={
            "elements_by_id": {
                **timeline.elements_by_id,
                "bgm-1": bgm_el,
            },
        },
    )
    project.timelines.items["timeline:main"] = timeline
    project.assets.source_versions_by_id["audio-v1"] = SourceAssetVersion(
        version_id="audio-v1",
        logical_asset_id="asset-a1",
        name="bed",
        file_id="file-audio-v1",
        checksum="2" * 64,
        media_kind="audio",
        media_type="audio/mpeg",
        created_at="2026-08-05T00:00:00Z",
    )
    from services.project_files.models import IndexedFile

    project.assets.files_by_id["file-audio-v1"] = IndexedFile(
        file_id="file-audio-v1",
        kind="source_original",
        media_type="audio/mpeg",
        relative_uri="assets/sources/bed.mp3",
        sha256="2" * 64,
        size_bytes=103,
        created_at="2026-08-05T00:00:00Z",
    )
    return project, timeline, second


def _bind_bgm_config(monkeypatch, model="wan3.0-video"):
    from models import config as model_config

    monkeypatch.setattr(model_config, "get_video_model_name", lambda: model)
    monkeypatch.setattr(model_config, "get_video_backend", lambda: "wan")


def test_frozen_r2v_publishes_after_prev_reroll() -> None:
    """Major-1: a prev-segment reroll while the successor render is
    in flight (already paid) must not quarantine the finished clip.
    The Project references are unchanged; only the synthetic
    ``continuity:*:prevV`` IDs shifted, and those were the submit
    path's anchors, not a re-billable input edit."""

    from services.media_files.r2v_execution import (
        FileR2VExecutionService,
    )
    from services.project_files.models import ElementOutput

    first = _element("e1", start=0)
    second = _element("e2", start=4000)
    project, _timeline = _project([first, second])
    # Storyboard selected on e2 so the r2v branch can resolve its plan.
    second.outputs["storyboard"] = ElementOutput(
        slot_id="element:e2:storyboard",
    )
    project.assets.artifact_slots_by_id[
        "element:e2:storyboard"
    ] = ArtifactSlot(
        slot_id="element:e2:storyboard",
        owner_ref="element:e2",
        kind="r2v_storyboard_image",
        version_ids=["sb-v1"],
        selected_version_id="sb-v1",
    )
    # Prev published as V1 when the task was admitted.
    _select_prev_video(project, "e1", "prevV1")
    frozen = _frozen_task(
        {
            "elementId": "e2",
            "mode": "r2v",
            "referenceVersionIds": [
                "sb-v1",
                "continuity:last_frame:prevV1",
                "continuity:tail_clip:prevV1",
            ],
        },
    )
    # Predecessor rerolled to V2 mid-render.
    _select_prev_video(project, "e1", "prevV2")

    assert FileR2VExecutionService._frozen_inputs_still_current(
        project,
        frozen,
    )


def test_frozen_r2v_quarantines_when_storyboard_changes() -> None:
    """Major-1 control: the publish tolerance is scoped to the prev
    reroll. A genuine Project-input change (storyboard reselected)
    still quarantines, so the fix did not widen into a blanket pass."""

    from services.media_files.r2v_execution import (
        FileR2VExecutionService,
    )
    from services.project_files.models import ElementOutput

    first = _element("e1", start=0)
    second = _element("e2", start=4000)
    project, _timeline = _project([first, second])
    second.outputs["storyboard"] = ElementOutput(
        slot_id="element:e2:storyboard",
    )
    project.assets.artifact_slots_by_id[
        "element:e2:storyboard"
    ] = ArtifactSlot(
        slot_id="element:e2:storyboard",
        owner_ref="element:e2",
        kind="r2v_storyboard_image",
        version_ids=["sb-v2"],
        selected_version_id="sb-v2",
    )
    _select_prev_video(project, "e1", "prevV1")
    frozen = _frozen_task(
        {
            "elementId": "e2",
            "mode": "r2v",
            "referenceVersionIds": [
                "sb-v1",
                "continuity:last_frame:prevV1",
                "continuity:tail_clip:prevV1",
            ],
        },
    )
    # Storyboard moved sb-v1 -> sb-v2: a real input change.
    assert not FileR2VExecutionService._frozen_inputs_still_current(
        project,
        frozen,
    )


def test_frozen_i2v_publishes_after_prev_reroll() -> None:
    """Major-1 (i2v): an auto-framed i2v render paid against the prev
    last frame publishes across a prev reroll, but quarantines once an
    explicit first frame is authored (a genuine input change)."""

    from services.media_files.r2v_execution import (
        FileR2VExecutionService,
    )

    first = _element("e1", start=0)
    second = _element("e2", start=4000, creation_type="i2v")
    project, _timeline = _project([first, second])
    _select_prev_video(project, "e1", "prevV1")
    frozen = _frozen_task(
        {
            "elementId": "e2",
            "mode": "i2v",
            "referenceVersionIds": ["continuity:last_frame:prevV1"],
            "firstFrameVersionId": "continuity:last_frame:prevV1",
        },
    )
    _select_prev_video(project, "e1", "prevV2")

    assert FileR2VExecutionService._frozen_inputs_still_current(
        project,
        frozen,
    )

    # Authoring an explicit first frame mid-render is a real change.
    second.creation.first_frame_version_id = "user-frame"
    assert not FileR2VExecutionService._frozen_inputs_still_current(
        project,
        frozen,
    )


def test_frozen_none_tier_publishes_without_storyboard() -> None:
    """CR P2-1: a none-tier render has no storyboard by design. The
    publish gate must apply the same tier rule as the submit path, or
    any unrelated project commit (etag/generation drift) would
    quarantine an already-paid, still-valid finished clip."""

    from services.media_files.r2v_execution import (
        FileR2VExecutionService,
    )

    first = _element("e1", start=0)
    second = _element("e2", start=4000)
    second.creation = second.creation.model_copy(
        update={
            "transition_intent": "continue",
            "storyboard_intent": "none",
        },
    )
    project, _timeline = _project([first, second])
    _select_prev_video(project, "e1", "prevV1")
    frozen = _frozen_task(
        {
            "elementId": "e2",
            "mode": "r2v",
            "referenceVersionIds": ["continuity:last_frame:prevV1"],
        },
    )
    assert FileR2VExecutionService._frozen_inputs_still_current(
        project,
        frozen,
    )


def _mock_ffmpeg_paths(monkeypatch) -> None:
    import services.runtime_files.runtime_dependencies as rd

    monkeypatch.setattr(rd, "resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        rd,
        "resolve_ffprobe",
        lambda ffmpeg_path: "/usr/bin/ffprobe",
    )


def _frame_entry(tmp_path: Path, prev_version: str = "prev-version-1"):
    from services.media_files import segment_continuity as sc

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
    """Major-2: a successful extract registers the predecessor digest,
    so event-driven prunes in the same process treat the anchors a
    queued payload froze as in flight."""

    from collections import OrderedDict

    from services.media_files import segment_continuity as sc

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


def test_missing_ffmpeg_drops_prev_chain_from_r2v_nodes() -> None:
    """Minor-2 (graph side): when ffmpeg cannot run, anchors never
    ship — r2v nodes must not chain on prev, and their fingerprint must
    match the prev-less shape so a prev reroll cannot re-qualify parked
    FAILED nodes for an unchanged-wire paid rerun. The optimistic
    default still chains."""

    from services.file_agent_runtime.work_graph import derive_work_graph

    def _anchored_pair():
        pair_first = _element("e1", start=0)
        pair_second = _element("e2", start=4000)
        pair_second.creation = pair_second.creation.model_copy(
            update={
                "transition_intent": "continue",
                "storyboard_intent": "none",
            },
        )
        return _project([pair_first, pair_second])

    project, _timeline = _anchored_pair()
    _select_prev_video(project, "e1", "prev-version-1")
    solo_project = _anchored_pair()[0]

    missing = derive_work_graph(project, ffmpeg_ready=False).by_id["video:e2"]
    solo = derive_work_graph(solo_project, ffmpeg_ready=False).by_id[
        "video:e2"
    ]
    assert "video:e1" not in missing.deps
    assert missing.dispatch_fingerprint == solo.dispatch_fingerprint

    optimistic = derive_work_graph(project).by_id["video:e2"]
    assert "video:e1" in optimistic.deps


def test_bgm_excerpt_parallel_same_source_budget(
    tmp_path,
    monkeypatch,
) -> None:
    """CR round-3 P2-2: segments resolve bgm in parallel threads. Two
    cuts of the same source+identity must not share one temp path
    (the pid-only name made the second os.replace die with
    FileNotFoundError, bypassing the skip handler)."""

    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    from services.media_files import r2v_execution as rx
    from services.media_files import segment_continuity as sc
    from services.runtime_files import runtime_dependencies as rd

    project, _timeline, _second = _bgm_project(tmp_path)
    monkeypatch.setattr(rd, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(rd, "resolve_ffprobe", lambda **_k: "ffprobe")

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ARG001
        Path(cmd[-1]).write_bytes(b"cut")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(rx.subprocess, "run", fake_run)

    def probe_by_path(path, *_a, **_k):  # noqa: ANN001
        return 60.0 if Path(path).name == "bed.wav" else 10.0

    monkeypatch.setattr(sc, "_probe_duration", probe_by_path)
    source = tmp_path / "bed.wav"
    source.write_bytes(b"x" * 8)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _i: rx._bgm_excerpt(
                    project,
                    tmp_path,
                    source,
                    "e2",
                    max_seconds=15.0,
                    source_identity="sha-x",
                ),
                range(8),
            ),
        )
    assert len(set(results)) == 1


def test_bgm_cache_identity_separates_budgets_and_sources(
    tmp_path,
    monkeypatch,
) -> None:
    """CR round-2 P2-C: 10.75s and 10.2s budgets (and different source
    versions) must never share one cache entry, and the cut re-encodes
    instead of stream-copying packet boundaries."""

    import itertools
    import subprocess

    from services.media_files import r2v_execution as rx
    from services.media_files import segment_continuity as sc
    from services.runtime_files import runtime_dependencies as rd

    project, _timeline, _second = _bgm_project(tmp_path)
    monkeypatch.setattr(rd, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(rd, "resolve_ffprobe", lambda **_k: "ffprobe")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ARG001
        calls.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"cut")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(rx.subprocess, "run", fake_run)
    # source probes long (60s), each cut verifies within budget (10s).
    cycle = itertools.cycle([60.0, 10.0])
    monkeypatch.setattr(
        sc,
        "_probe_duration",
        lambda *_a, **_k: next(cycle),
    )
    source = tmp_path / "bed.wav"
    source.write_bytes(b"x" * 8)

    first = rx._bgm_excerpt(
        project,
        tmp_path,
        source,
        "e2",
        max_seconds=10.75,
        source_identity="sha-a",
    )
    second = rx._bgm_excerpt(
        project,
        tmp_path,
        source,
        "e2",
        max_seconds=10.2,
        source_identity="sha-a",
    )
    third = rx._bgm_excerpt(
        project,
        tmp_path,
        source,
        "e2",
        max_seconds=10.75,
        source_identity="sha-b",
    )
    assert len({first, second, third}) == 3
    assert len(calls) == 3
    assert "-c:a" not in calls[0]
    # Exact-budget label in the cache name.
    assert "head10.75" in Path(first).name


def test_bgm_source_probe_failure_skips_instead_of_crashing(
    tmp_path,
    monkeypatch,
) -> None:
    """CR round-3 P2-1: an unreadable source (ffprobe timeout/loss)
    must degrade to the documented bgm skip. The cleanup branch used
    to touch an unassigned tmp handle and raised UnboundLocalError,
    which no caller catches — the video task died instead of skipping."""

    from services.media_files import r2v_execution as rx
    from services.media_files import segment_continuity as sc
    from services.runtime_files import runtime_dependencies as rd

    project, timeline, _second = _bgm_project(tmp_path)
    _bind_bgm_config(monkeypatch)
    monkeypatch.setattr(rd, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(rd, "resolve_ffprobe", lambda **_k: "ffprobe")

    def boom(*_a, **_k):
        raise RuntimeError("ffprobe timeout")

    monkeypatch.setattr(sc, "_probe_duration", boom)
    assert (
        rx._resolve_bgm_reference(
            project,
            timeline,
            tmp_path,
            voice_count=0,
            element_id="e2",
        )
        == ""
    )


def test_bgm_reference_budget_matrix(
    tmp_path,
    monkeypatch,
) -> None:
    """One matrix for the shared-bgm resolve rules: no bgm element,
    voice slot overflow, models without standalone audio, and voices
    consuming the 15s time budget all resolve to no bgm reference."""

    from services.media_files import r2v_execution as rx

    def resolve(project, timeline, **kw):
        return rx._resolve_bgm_reference(
            project,
            timeline,
            tmp_path,
            element_id="e2",
            **kw,
        )

    _bind_bgm_config(monkeypatch)
    bare, bare_tl, _ = _bgm_project(tmp_path, with_bgm=False)
    assert resolve(bare, bare_tl, voice_count=0) == ""

    with_bgm, bgm_tl, _ = _bgm_project(tmp_path)
    assert resolve(with_bgm, bgm_tl, voice_count=5) == ""
    assert resolve(with_bgm, bgm_tl, voice_count=2, voice_seconds=14.5) == ""

    monkeypatch.setattr(
        "models.config.get_video_model_name",
        lambda: "some-other-model",
    )
    assert resolve(with_bgm, bgm_tl, voice_count=0) == ""


def test_over_budget_one_take_claim_rejects_prev_reroll() -> None:
    """CR round-12 P2-1: an over-budget one_take falls back to the
    ANCHORED r2v shape — the fingerprint must then carry the anchor,
    so a predecessor reroll between ADMIT and SUBMIT fails the claim
    instead of paying for a stale opening."""

    from services.media_files import r2v_execution as rx
    from services.project_files.models import ArtifactVersion

    project, timeline, first, second = _two_segment_project()
    second.creation = second.creation.model_copy(
        update={"transition_intent": "one_take"},
    )
    _select_prev_video(project, "e1", "prev-v1")
    project.assets.artifact_versions_by_id["prev-v1"] = ArtifactVersion(
        version_id="prev-v1",
        name="prev-v1",
        slot_id="element:e1:video",
        kind="artifact_video",
        owner_ref="element:e1",
        file_id="file-prev-v1",
        checksum="5" * 64,
        based_on_generation=1,
        provenance_refs=[],
        stale=False,
        created_at="2026-08-05T00:00:00Z",
        duration_seconds=31.0,
    )
    # 31s material + 4s segment busts the extend budget: the plan
    # anchors instead of extending, and the mirror must agree.
    refs = rx._reference_ids_with_continuity(
        project,
        timeline,
        second,
        mode="r2v",
    )
    assert refs[0] == "continuity:last_frame:prev-v1"

    frozen = rx._submission_input_fingerprint(project, "e2", mode="r2v")
    _select_prev_video(project, "e1", "prev-v2")
    assert not rx._matches_submission_inputs(
        project,
        "e2",
        frozen,
        mode="r2v",
        payload_refs=["continuity:last_frame:prev-v1"],
    )


def test_two_frame_i2v_publish_gate() -> None:
    """CR round-12 P2-3: the publish gate compares BOTH frozen frames;
    an unrelated project commit still publishes, a re-authored tail
    frame quarantines."""

    from services.media_files.r2v_execution import (
        FileR2VExecutionService,
    )
    from services.project_files.models import I2VCreation

    first = _element("e1", start=0)
    second = _element("e2", start=4000)
    second.creation = I2VCreation(
        video_prompt="男孩走进走廊。",
        first_frame_version_id="frame-a",
        last_frame_version_id="frame-b",
    )
    project, _timeline = _project([first, second])
    frozen = _frozen_task(
        {
            "elementId": "e2",
            "mode": "i2v",
            "firstFrameVersionId": "frame-a",
            "firstFrameUrl": "file:///a.png",
            "lastFrameVersionId": "frame-b",
            "lastFrameUrl": "file:///b.png",
            "referenceVersionIds": ["frame-a", "frame-b"],
        },
    )
    assert FileR2VExecutionService._frozen_inputs_still_current(
        project,
        frozen,
    )
    second.creation = second.creation.model_copy(
        update={"last_frame_version_id": "frame-c"},
    )
    assert not FileR2VExecutionService._frozen_inputs_still_current(
        project,
        frozen,
    )


def test_first_frame_only_fallback_publishes_despite_authored_tail() -> None:
    """CR round-13 P2-1: when the model cannot receive a native tail
    frame the payload ships first-frame only — the authored-but-
    unsent tail must not make the finished clip fail the publish
    gate on an unchanged project."""

    from services.media_files.r2v_execution import (
        FileR2VExecutionService,
    )
    from services.project_files.models import I2VCreation

    first = _element("e1", start=0)
    second = _element("e2", start=4000)
    second.creation = I2VCreation(
        video_prompt="男孩走进走廊。",
        first_frame_version_id="frame-a",
        last_frame_version_id="frame-b",
    )
    project, _timeline = _project([first, second])
    frozen = _frozen_task(
        {
            "elementId": "e2",
            "mode": "i2v",
            "firstFrameVersionId": "frame-a",
            "firstFrameUrl": "file:///a.png",
            "lastFrameVersionId": "",
            "lastFrameUrl": "",
            "lastFrameAuthoredVersionId": "frame-b",
            "referenceVersionIds": ["frame-a"],
        },
    )
    assert FileR2VExecutionService._frozen_inputs_still_current(
        project,
        frozen,
    )


def test_auto_first_frame_publishes_despite_authored_tail() -> None:
    """CR round-13 P2-1 (auto-first shape): the predecessor reroll
    still publishes the paid clip, and an authored unsent tail never
    quarantines it."""

    from services.media_files.r2v_execution import (
        FileR2VExecutionService,
    )
    from services.project_files.models import I2VCreation

    first = _element("e1", start=0)
    second = _element("e2", start=4000)
    second.creation = I2VCreation(
        video_prompt="男孩走进走廊。",
        last_frame_version_id="frame-b",
    )
    project, _timeline = _project([first, second])
    _select_prev_video(project, "e1", "prev-v1")
    frozen = _frozen_task(
        {
            "elementId": "e2",
            "mode": "i2v",
            "firstFrameVersionId": "continuity:last_frame:prev-v1",
            "firstFrameUrl": "file:///prev.png",
            "lastFrameVersionId": "",
            "lastFrameUrl": "",
            "lastFrameAuthoredVersionId": "frame-b",
            "referenceVersionIds": ["continuity:last_frame:prev-v1"],
        },
    )
    assert FileR2VExecutionService._frozen_inputs_still_current(
        project,
        frozen,
    )
    # The tail was re-authored but never shipped: still publishable.
    second.creation = second.creation.model_copy(
        update={"last_frame_version_id": "frame-c"},
    )
    assert FileR2VExecutionService._frozen_inputs_still_current(
        project,
        frozen,
    )


def test_default_continuous_and_authored_cut_agree_across_layers() -> None:
    """CR round-11: the DEFAULT is continuous — missing intent anchors
    on the same scene across scheduler, executor gate, fingerprint
    mirror and work graph; an authored cut turns all layers off
    together, and an authored continue anchors even across scenes."""

    from services.file_agent_runtime.work_graph import derive_work_graph
    from services.media_files import r2v_execution as rx

    project, timeline, _first, second = _two_segment_project("prev-v")
    _storyboard_selected(project, second)
    second.creation = second.creation.model_copy(
        update={
            "transition_intent": None,
            "storyboard_intent": None,
        },
    )

    assert rx._r2v_strong_continuity(project, timeline, second) is True
    refs = rx._reference_ids_with_continuity(
        project,
        timeline,
        second,
        mode="r2v",
    )
    assert refs[0].startswith("continuity:last_frame:")
    graph = derive_work_graph(project, ffmpeg_ready=True)
    deps = graph.by_id["video:e2"].deps
    assert "video:e1" in deps
    assert "storyboard:e2" in deps

    # Authored cut switches every layer off together.
    second.creation = second.creation.model_copy(
        update={"transition_intent": "cut"},
    )
    assert rx._r2v_strong_continuity(project, timeline, second) is False
    refs = rx._reference_ids_with_continuity(
        project,
        timeline,
        second,
        mode="r2v",
    )
    assert refs == ("sb-v1",)
    graph = derive_work_graph(project, ffmpeg_ready=True)
    assert "video:e1" not in graph.by_id["video:e2"].deps

    # Authored continue anchors even across scenes.
    second.creation = second.creation.model_copy(
        update={
            "transition_intent": "continue",
            "scene_ref": "scene:elsewhere",
        },
    )
    assert rx._r2v_strong_continuity(project, timeline, second) is True


def test_missing_intent_fields_default_to_continuous() -> None:
    """Product default: continuity is ON. Absent intent fields fall
    back to the scene/cast hard default (same scene anchors, scene
    change does not) — prose is never consulted."""

    from models.video_task_plans import (
        r2v_continuity_storyboard_need,
        r2v_may_consume_predecessor,
    )

    project, timeline, first, second = _two_segment_project("prev-v")
    second.creation = second.creation.model_copy(
        update={
            "transition_intent": None,
            "storyboard_intent": None,
            "video_prompt": "镜头环绕人物一周。",
        },
    )
    assert (
        r2v_may_consume_predecessor(
            timeline.elements_by_id,
            second,
            first,
        )
        is True
    )
    assert (
        r2v_continuity_storyboard_need(project, timeline, second) == "combined"
    )

    second.creation = second.creation.model_copy(
        update={"scene_ref": "scene:elsewhere"},
    )
    assert (
        r2v_may_consume_predecessor(
            timeline.elements_by_id,
            second,
            first,
        )
        is False
    )
    assert r2v_continuity_storyboard_need(project, timeline, second) == "plain"
