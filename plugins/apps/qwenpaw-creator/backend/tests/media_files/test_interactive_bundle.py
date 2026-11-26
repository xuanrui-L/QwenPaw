# -*- coding: utf-8 -*-
"""Interactive bundle assembly tests (branching deliverable, plan §2.7b)."""

# pylint: disable=protected-access

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from api import interactive_bundle_routes as routes
from domain.errors import ConflictError
from services.media_files.interactive_bundle import (
    InteractiveBundleError,
    InteractiveBundlePlan,
    assemble_interactive_bundle,
    derive_interactive_manifest,
    plan_interactive_bundle,
    write_interactive_bundle,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    InteractionCreation,
    InteractionOption,
    MotionGraphic,
    NarrativeEdge,
    Project,
    Timeline,
    TimelineElement,
    TimelineSpan,
)
from services.runtime_files.execution_models import TaskRecord
from services.runtime_files.execution_store import ProjectExecutionStore

from .conftest import add_final_video

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _draft_presentation(project):
    from services.media_files.presentation_authoring import (
        presentation_fingerprint,
    )
    from services.project_files.models import narrative_timeline_ids

    fixture = (
        Path(__file__).resolve().parents[3]
        / "player/tests/fixtures/authored-presentation.html"
    )
    html = fixture.read_text().replace(
        "__NODES__",
        "".join(
            f'<button data-action="jump" data-node-ref="{tid}">{tid}</button>'
            for tid in narrative_timeline_ids(project)
        ),
    )
    project.interactive_presentation.motion = MotionGraphic(
        html=html,
        design_notes=f"input_fingerprint={presentation_fingerprint(project)}",
    )


def _branching_project() -> tuple[Project, dict[str, bytes]]:
    source = Timeline(
        timeline_id="tl:ep3",
        title="第3集 · 双重身份",
        synopsis="沈修的双重身份被当众戳穿。",
        elements_by_id={
            "el:choice": TimelineElement(
                element_id="el:choice",
                label="观众抉择",
                span=TimelineSpan(start_tick=88_000, duration_tick=4_000),
                creation=InteractionCreation(
                    type="interaction",
                    question="是否当众揭发沈修？",
                    motion=MotionGraphic(
                        html=(
                            "<html><body>"
                            "<span data-interaction-countdown></span>"
                            '<button data-edge-ref="edge:a">'
                            '选择A</button><button data-edge-ref="edge:b">'
                            "选择B</button></body></html>"
                        ),
                    ),
                    options=[
                        InteractionOption(edge_ref="edge:a"),
                        InteractionOption(edge_ref="edge:b"),
                    ],
                    countdown_seconds=10,
                    default_edge_ref="edge:a",
                ),
            ),
        },
    )
    branch_a = Timeline(
        timeline_id="tl:ep4a",
        title="第4集A · 真相大白",
        synopsis="真相大白，正义得到伸张。",
    )
    branch_b = Timeline(timeline_id="tl:ep4b", title="第4集B · 沉默代价")
    project = Project(
        project_id="project-branching",
        created_at=NOW,
        updated_at=NOW,
        name="雾山谜案",
        description="雾山深处的双重身份悬疑剧。\n第二行不进 tagline。",
        strategy={"creative_brief": "互动悬疑短剧《雾山谜案》创意简报。"},
        timelines={
            "items": {
                "tl:ep3": source,
                "tl:ep4a": branch_a,
                "tl:ep4b": branch_b,
            },
            "order": ["tl:ep3", "tl:ep4a", "tl:ep4b"],
        },
        narrative_edges=[
            NarrativeEdge(
                edge_id="edge:a",
                source_timeline_id="tl:ep3",
                target_timeline_id="tl:ep4a",
                label="选择A · 揭发真相",
            ),
            NarrativeEdge(
                edge_id="edge:b",
                source_timeline_id="tl:ep3",
                target_timeline_id="tl:ep4b",
                label="选择B · 保持沉默",
            ),
        ],
    )
    payloads: dict[str, bytes] = {}
    for timeline_id in ("tl:ep3", "tl:ep4a", "tl:ep4b"):
        data = f"video-bytes-{timeline_id}".encode()
        add_final_video(project, timeline_id, data)
        payloads[f"file:{timeline_id}:final"] = data
    _draft_presentation(project)
    return project, payloads


def test_manifest_derivation_covers_reachable_branches() -> None:
    project, _ = _branching_project()

    manifest = derive_interactive_manifest(project)

    assert manifest.entry_timeline_id == "tl:ep3"
    assert set(manifest.segments) == {"tl:ep3", "tl:ep4a", "tl:ep4b"}
    assert manifest.segments["tl:ep4a"].startswith("artifact-version:")
    (point,) = manifest.interactions
    assert point.source_timeline_id == "tl:ep3"
    assert point.at_seconds == pytest.approx(88.0)
    assert point.default_edge_ref == "edge:a"
    assert [option.edge_ref for option in point.options] == [
        "edge:a",
        "edge:b",
    ]


def test_segment_names_are_readable_unique_and_safe() -> None:
    """导出片段名:可读标题 + 稳定 hash 后缀,跨平台安全且不受分支增删影响。"""

    project, _ = _branching_project()
    # 两条分支同名 + 一个塞满路径分隔符与 URL 危险字符的敌意标题。
    project.timelines.items["tl:ep4b"].title = "第4集A · 真相大白"
    project.timelines.items["tl:ep3"].title = '结局/真相:C"暗?#100%段'

    from services.media_files.interactive_bundle import _player_manifest

    segments = _player_manifest(
        project,
        derive_interactive_manifest(project),
    )["segments"]
    names = list(segments.values())

    # 每个片段一个文件,标题相同也各自唯一(靠 id 派生的 hash 后缀)。
    assert len(set(names)) == len(names) == 3
    for name in names:
        base = name.rsplit("/", 1)[-1]
        assert re.fullmatch(r".+-[0-9a-f]{8}\.mp4", base)
        # basename 不含任何跨平台非法或 URL 危险字符(# 会被播放器当 fragment、
        # % 会被当转义,离线包据此 404)。
        assert not any(ch in base for ch in '\\/:*?"<>|#%')
    # 标题的可读文字在净化后仍然保留。
    assert any("真相" in n for n in names)

    def _stem(path: str) -> str:
        return re.sub(r"-[0-9a-f]{8}\.mp4$", "", path.rsplit("/", 1)[-1])

    # 同名分支去掉 hash 后缀后完全一致,唯一性只来自后缀。
    assert segments["tl:ep4a"] != segments["tl:ep4b"]
    assert _stem(segments["tl:ep4a"]) == _stem(segments["tl:ep4b"])
    # 敌意字符被中和,且命名只依赖 timeline id(与遍历顺序无关,稳定)。
    assert _stem(segments["tl:ep3"]) == "结局-真相-C-暗-100-段"


def test_display_stem_sanitizes_reserves_and_caps() -> None:
    """zip 下载名的清洗、Windows 保留名保护与长度上限。"""

    from api.interactive_bundle_routes import _display_stem

    def named(name: str) -> Project:
        project, _ = _branching_project()
        project.name = name
        return project

    # 路径分隔符与 URL 危险字符被中和。
    stem = _display_stem(named("雾山#谜案%20/x:y"))
    assert not any(ch in stem for ch in "#%/:")
    # Windows 保留名加前缀，避免 CON.zip 落盘失败。
    assert _display_stem(named("CON")) == "_CON"
    # 超长名截断到上限。
    assert len(_display_stem(named("雾" * 200))) <= 80


def test_missing_segment_fails_closed() -> None:
    project, _ = _branching_project()
    del project.assets.artifact_slots_by_id["timeline:tl:ep4b:render"]

    with pytest.raises(InteractiveBundleError, match="tl:ep4b"):
        derive_interactive_manifest(project)


def test_unknown_edge_ref_fails_closed() -> None:
    project, _ = _branching_project()
    with pytest.raises(ValueError, match="edge:b"):
        project.narrative_edges = project.narrative_edges[:1]


def test_bundle_zip_contains_player_manifest_and_segments() -> None:
    project, payloads = _branching_project()

    _draft_presentation(project)
    bundle = assemble_interactive_bundle(
        project,
        read_artifact_file=lambda file_id: payloads[file_id],
    )

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        names = set(archive.namelist())
        assert "index.html" in names
        assert "manifest.json" in names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["entry_timeline_id"] == "tl:ep3"
        ep4a_name = manifest["segments"]["tl:ep4a"]
        assert re.fullmatch(
            r"segments/第4集A-·-真相大白-[0-9a-f]{8}\.mp4",
            ep4a_name,
        )
        assert manifest["edge_index"]["edge:a"] == {
            "label": "选择A · 揭发真相",
            "prompt": "",
            "target_timeline_id": "tl:ep4a",
            "source_timeline_id": "tl:ep3",
        }
        # Legacy field kept for backward compatibility with old players.
        assert manifest["titles"]["tl:ep3"] == "第3集 · 双重身份"
        # Game-shell additions: title-screen meta + story-map node index.
        assert manifest["meta"]["bundle_id"] == "project-branching"
        assert manifest["meta"]["title"] == "雾山谜案"
        assert manifest["meta"]["tagline"] == "雾山深处的双重身份悬疑剧。"
        assert manifest["meta"]["synopsis"] == project.description
        assert manifest["meta"]["accent"] == "#b8ff2e"
        assert manifest["nodes"]["tl:ep3"] == {
            "title": "第3集 · 双重身份",
            "synopsis": "沈修的双重身份被当众戳穿。",
            "children": ["tl:ep4a", "tl:ep4b"],
            "is_ending": False,
        }
        assert manifest["nodes"]["tl:ep4a"]["is_ending"] is True
        assert manifest["nodes"]["tl:ep4a"]["synopsis"] == ("真相大白，正义得到伸张。")
        ep3_bytes = archive.read(manifest["segments"]["tl:ep3"])
        assert ep3_bytes == b"video-bytes-tl:ep3"
        player = archive.read("index.html").decode()
        assert "edge_index" in player and "countdown" in player
        assert "IVBAuthoredPlayer.offline" in player
        assert "localStorage.setItem" in player
        assert (
            archive.read("presentation.html").decode()
            == project.interactive_presentation.motion.html
        )
        assert (
            manifest["authored_html"]
            == project.interactive_presentation.motion.html
        )
        assert "__MANIFEST_JSON__" not in player


def test_bundle_ships_stored_cover_and_skips_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.media_files import cover_generation

    project, payloads = _branching_project()
    _draft_presentation(project)
    poster = b"\xff\xd8\xff\xe0 stored poster"
    payloads["file:cover"] = poster
    project.interactive_presentation = (
        project.interactive_presentation.model_copy(
            update={
                "cover_file_id": "file:cover",
                "cover_checksum": _sha(poster),
            },
        )
    )

    def _no_fallback(video_bytes: bytes):  # pragma: no cover
        raise AssertionError("a stored cover must skip the frame fallback")

    monkeypatch.setattr(
        cover_generation,
        "poster_frame_from_video",
        _no_fallback,
    )

    bundle = assemble_interactive_bundle(
        project,
        read_artifact_file=payloads.__getitem__,
    )

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert archive.read("cover.jpg") == poster


def test_bundle_falls_back_to_entry_frame_without_generated_cover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.media_files import cover_generation

    project, payloads = _branching_project()
    _draft_presentation(project)
    frame = b"\xff\xd8\xff\xe1 fake frame"
    seen: dict[str, object] = {}

    def fake_poster(video_bytes: bytes) -> bytes:
        seen["video"] = video_bytes
        return frame

    monkeypatch.setattr(
        cover_generation,
        "poster_frame_from_video",
        fake_poster,
    )

    bundle = assemble_interactive_bundle(
        project,
        read_artifact_file=payloads.__getitem__,
    )

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert archive.read("cover.jpg") == frame
    # The fallback draws from the entry segment (tl:ep3), not some other cut.
    entry_file_id = "file:tl:ep3:final"
    assert seen["video"] == payloads[entry_file_id]


def test_bundle_omits_cover_when_no_poster_and_no_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.media_files import cover_generation

    project, payloads = _branching_project()
    _draft_presentation(project)
    monkeypatch.setattr(
        cover_generation,
        "poster_frame_from_video",
        lambda video_bytes: None,
    )

    bundle = assemble_interactive_bundle(
        project,
        read_artifact_file=payloads.__getitem__,
    )

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert "cover.jpg" not in archive.namelist()


def test_no_generated_interface_means_no_export():
    project, payloads = _branching_project()
    project.interactive_presentation.motion = None
    with pytest.raises(InteractiveBundleError, match="interface is missing"):
        assemble_interactive_bundle(
            project,
            read_artifact_file=payloads.__getitem__,
        )


@pytest.mark.parametrize(
    "prefix,suffix",
    [("前言\n```html\n", ""), ("", "\n```\n### 优化建议")],
)
@pytest.mark.parametrize("project_interface", [False, True])
def test_export_rejects_model_prose_outside_document(
    prefix,
    suffix,
    project_interface,
):
    project, payloads = _branching_project()
    motion = (
        project.interactive_presentation.motion
        if project_interface
        else project.timelines.items["tl:ep3"]
        .elements_by_id["el:choice"]
        .creation.motion
    )
    motion.html = prefix + motion.html + suffix
    # Historical snapshots must still round-trip through the Project schema
    # during recovery, while the output boundary rejects the same artifact.
    project = Project.model_validate(project.model_dump(mode="json"))
    with pytest.raises(InteractiveBundleError, match="text outside"):
        assemble_interactive_bundle(
            project,
            read_artifact_file=payloads.__getitem__,
        )


def test_replaced_video_invalidates_offline_progress_revision():
    project, payloads = _branching_project()

    def revision():
        package = assemble_interactive_bundle(
            project,
            read_artifact_file=payloads.__getitem__,
        )
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            return json.loads(archive.read("manifest.json"))[
                "content_revision"
            ]

    before = revision()
    add_final_video(project, "tl:ep4a", b"revised-video")
    payloads["file:tl:ep4a:final"] = b"revised-video"
    assert revision() != before


def test_edge_index_omits_tone_when_unset() -> None:
    """``NarrativeEdge.tone`` is optional: an untiered edge must not grow a
    tone key, so pre-tone projects keep rendering the neutral card."""

    project, payloads = _branching_project()

    _draft_presentation(project)
    bundle = assemble_interactive_bundle(
        project,
        read_artifact_file=lambda file_id: payloads[file_id],
    )

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    for entry in manifest["edge_index"].values():
        assert "tone" not in entry
        assert set(entry) == {
            "label",
            "prompt",
            "target_timeline_id",
            "source_timeline_id",
        }


def test_edge_index_carries_tone() -> None:
    """All three tiers must survive the edge → manifest join verbatim; the
    player styles the card and teaches the audience what to expect."""

    project, payloads = _branching_project()
    project.narrative_edges[0].tone = "safe"
    project.narrative_edges[1].tone = "danger"

    _draft_presentation(project)
    bundle = assemble_interactive_bundle(
        project,
        read_artifact_file=lambda file_id: payloads[file_id],
    )

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["edge_index"]["edge:a"]["tone"] == "safe"
    assert manifest["edge_index"]["edge:b"]["tone"] == "danger"


def test_story_cycle_fails_closed() -> None:
    """A cycle would let the audience "continue" forever without ever
    reaching an ending — export must name the cycle, not emit it."""

    project, _ = _branching_project()
    project.narrative_edges.append(
        NarrativeEdge(
            edge_id="edge:loop",
            source_timeline_id="tl:ep4a",
            target_timeline_id="tl:ep3",
            label="回到第3集",
        ),
    )

    with pytest.raises(
        InteractiveBundleError,
        match=r"tl:ep3 -> tl:ep4a -> tl:ep3",
    ):
        derive_interactive_manifest(project)


def test_fork_without_interaction_fails_closed() -> None:
    """Two outgoing edges and no tappable decision point is a broken fork:
    the bundle plays, but the audience cannot pick a branch."""

    project, _ = _branching_project()
    project.timelines.items["tl:ep3"].elements_by_id.clear()

    with pytest.raises(InteractiveBundleError, match="tl:ep3"):
        derive_interactive_manifest(project)


def test_single_option_interaction_fails_closed() -> None:
    """One option is a continue button, not a choice — and the player would
    reject the package as fatal, so refuse it at export time."""

    project, _ = _branching_project()
    element = project.timelines.items["tl:ep3"].elements_by_id["el:choice"]
    element.creation.options = element.creation.options[:1]
    element.creation.countdown_seconds = None
    element.creation.default_edge_ref = None

    with pytest.raises(InteractiveBundleError, match="tl:ep3"):
        derive_interactive_manifest(project)


def test_presentation_json_points_to_authored_document():
    project, payloads = _branching_project()
    _draft_presentation(project)
    bundle = assemble_interactive_bundle(
        project,
        read_artifact_file=payloads.__getitem__,
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert json.loads(archive.read("presentation.json")) == {
            "schema_version": 1,
            "format": "agent_html_css",
            "document": "presentation.html",
        }


def test_linear_project_nodes_chain_in_order() -> None:
    """Without narrative edges the node index chains ordered timelines so
    the story map stays a path and only the last node is the ending."""

    project, payloads = _branching_project()
    project.timelines.items["tl:ep3"].elements_by_id.clear()
    project.narrative_edges = []

    _draft_presentation(project)
    bundle = assemble_interactive_bundle(
        project,
        read_artifact_file=lambda file_id: payloads[file_id],
    )

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    nodes = manifest["nodes"]
    assert nodes["tl:ep3"]["children"] == ["tl:ep4a"]
    assert nodes["tl:ep4a"]["children"] == ["tl:ep4b"]
    assert nodes["tl:ep3"]["is_ending"] is False
    assert nodes["tl:ep4b"]["is_ending"] is True


def test_linear_project_bundles_every_ordered_timeline() -> None:
    project, _payloads = _branching_project()
    # Drop the choice element so the linear cut stays plain video.
    project.timelines.items["tl:ep3"].elements_by_id.clear()
    project.narrative_edges = []

    manifest = derive_interactive_manifest(project)

    assert list(manifest.segments) == ["tl:ep3", "tl:ep4a", "tl:ep4b"]
    assert manifest.interactions == []


def test_export_rejects_missing_or_stale_motion_and_stale_video():
    project, _ = _branching_project()
    creation = (
        project.timelines.items["tl:ep3"].elements_by_id["el:choice"].creation
    )
    creation.motion = None
    with pytest.raises(InteractiveBundleError, match="motion is missing"):
        derive_interactive_manifest(project)
    creation.fallback = "static_endcard"
    with pytest.raises(InteractiveBundleError, match="motion is missing"):
        derive_interactive_manifest(project)
    creation.motion = MotionGraphic(
        html="<html><body>valid authored choice document</body></html>",
    )
    project.assets.artifact_versions_by_id["tl:ep4a:final:v1"].stale = True
    with pytest.raises(InteractiveBundleError, match="tl:ep4a"):
        derive_interactive_manifest(project)


def test_segment_filename_mapping_is_injective_for_valid_entity_ids():
    from services.media_files.interactive_bundle import _player_manifest

    project, _ = _branching_project()
    raw = project.model_dump(mode="json")
    raw["timelines"]["items"]["tl_ep3"] = raw["timelines"]["items"].pop(
        "tl:ep4a",
    )
    raw["timelines"]["items"]["tl_ep3"]["timeline_id"] = "tl_ep3"
    raw["timelines"]["order"][1] = "tl_ep3"
    raw["narrative_edges"][0]["target_timeline_id"] = "tl_ep3"
    project = Project.model_validate(raw)
    add_final_video(project, "tl_ep3", b"other-bytes")
    manifest = _player_manifest(project, derive_interactive_manifest(project))
    assert len(set(manifest["segments"].values())) == 3
    assert manifest["segments"]["tl:ep3"] != manifest["segments"]["tl_ep3"]


def test_export_and_project_reject_foreign_edges_and_multiple_choices():
    project, _ = _branching_project()
    raw = project.model_dump(mode="json")
    raw["narrative_edges"][0]["source_timeline_id"] = "tl:ep4b"
    with pytest.raises(ValueError, match="outgoing edge"):
        Project.model_validate(raw)
    raw = project.model_dump(mode="json")
    point = dict(
        raw["timelines"]["items"]["tl:ep3"]["elements_by_id"]["el:choice"],
    )
    point["element_id"] = "el:second"
    raw["timelines"]["items"]["tl:ep3"]["elements_by_id"]["el:second"] = point
    with pytest.raises(ValueError, match="only one"):
        Project.model_validate(raw)


def test_streamed_package_matches_the_in_memory_one() -> None:
    """Streaming must not change the deliverable, only how it is held.

    The export path pipes each Asset straight into the archive instead of
    reading it whole; that is only safe while every member -- name, order,
    bytes and compression -- stays identical to the in-memory assembler.
    """

    project, payloads = _branching_project()
    _draft_presentation(project)
    in_memory = assemble_interactive_bundle(
        project,
        read_artifact_file=payloads.__getitem__,
    )

    plan = plan_interactive_bundle(
        project,
        read_artifact_file=payloads.__getitem__,
    )
    streamed = io.BytesIO()
    write_interactive_bundle(
        plan,
        streamed,
        read_artifact_file=payloads.__getitem__,
        copy_artifact_file=lambda file_id, sink: sink.write(payloads[file_id]),
    )

    with zipfile.ZipFile(io.BytesIO(in_memory)) as reference:
        with zipfile.ZipFile(io.BytesIO(streamed.getvalue())) as actual:
            assert reference.namelist() == actual.namelist()
            for name in reference.namelist():
                assert actual.read(name) == reference.read(name)
                assert (
                    actual.getinfo(name).compress_type
                    == reference.getinfo(name).compress_type
                )


def _export_services(tmp_path):
    services = CreatorFileServices.create(tmp_path.resolve())
    services.projects.create(Project.new(project_id="bundle", name="Bundle"))
    return services


def test_package_is_written_after_the_lifecycle_lock_is_released(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A save must stay possible while a package is being streamed.

    The probe re-enters the Project lock from the writer: if export ever
    takes the lock back over the byte phase, this blocks on the lock's own
    timeout and fails instead of quietly serializing every commit.
    """

    services = _export_services(tmp_path)
    project = services.projects.read("bundle").project
    plan = InteractiveBundlePlan(
        payload={},
        authored_html="",
        segments=(),
        cover_file_id=None,
        cover_fallback_file_id=None,
    )
    reachables: list[bool] = []

    def write_outside_lock(_plan, target, **_kwargs):
        with services.projects.lifecycle_lock("bundle"):
            reachables.append(True)
        target.write(b"zip-bytes")

    monkeypatch.setattr(routes, "_plan_locked", lambda *_args: (project, plan))
    monkeypatch.setattr(routes, "write_interactive_bundle", write_outside_lock)

    size, chunks, _stem = routes._assemble("bundle", services)

    assert reachables == [True]
    assert size == len(b"zip-bytes")
    assert b"".join(chunks) == b"zip-bytes"


def test_scratch_package_is_removed_on_both_paths(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The package on disk is scratch: drained or failed, nothing is left."""

    services = _export_services(tmp_path)
    project = services.projects.read("bundle").project
    plan = InteractiveBundlePlan(
        payload={},
        authored_html="",
        segments=(),
        cover_file_id=None,
        cover_fallback_file_id=None,
    )
    exports = services.projects.root / "exports"
    monkeypatch.setattr(routes, "_plan_locked", lambda *_args: (project, plan))

    def write_and_leave(_plan, target, **_kwargs):
        target.write(b"complete-package")

    monkeypatch.setattr(routes, "write_interactive_bundle", write_and_leave)
    _size, chunks, _stem = routes._assemble("bundle", services)
    assert list(exports.glob("*.zip"))
    assert b"".join(chunks) == b"complete-package"
    assert not list(exports.glob("*.zip"))

    def write_then_fail(_plan, target, **_kwargs):
        target.write(b"partial")
        raise InteractiveBundleError("segment file is missing")

    monkeypatch.setattr(routes, "write_interactive_bundle", write_then_fail)
    with pytest.raises(ConflictError, match="segment file is missing"):
        routes._assemble("bundle", services)
    assert not list(exports.glob("*.zip"))


@pytest.mark.unit
@pytest.mark.parametrize("state", ["idle", "QUEUED", "RUNNING", "review"])
def test_export_waits_for_inflight_work(tmp_path, monkeypatch, state):
    services = CreatorFileServices.create(tmp_path.resolve())
    services.projects.create(Project.new(project_id="bundle", name="Bundle"))
    if state in {"QUEUED", "RUNNING"}:
        execution = ProjectExecutionStore(services.root)
        execution.create_task(
            TaskRecord(
                project_id="bundle",
                task_id="regenerate",
                kind="interaction_draft",
                request_fingerprint="sha256:test",
            ),
        )
        if state == "RUNNING":
            execution.transition_task(
                "bundle",
                "regenerate",
                expected_status="QUEUED",
                status="RUNNING",
            )
    monkeypatch.setattr(
        routes,
        "active_media_review_slots",
        lambda _pid: frozenset({"selected-video"})
        if state == "review"
        else frozenset(),
    )
    # Assembly/content validity is exercised by the bundle suite. This
    # probe checks the admission boundary even when old content is valid.
    sentinel = object()
    monkeypatch.setattr(
        routes,
        "plan_interactive_bundle",
        lambda *_args, **_kwargs: sentinel,
    )
    if state == "idle":
        _project, plan = routes._plan_locked("bundle", services)
        assert plan is sentinel
    else:
        with pytest.raises(ConflictError, match="generation and media review"):
            routes._plan_locked("bundle", services)
