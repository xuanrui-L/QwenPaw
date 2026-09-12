# -*- coding: utf-8 -*-
"""Interactive bundle assembly tests (branching deliverable, plan §2.7b)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from datetime import datetime, timezone

import pytest

from services.media_files.interactive_bundle import (
    assemble_interactive_bundle,
    derive_interactive_manifest,
    InteractiveBundleError,
    PLAYER_HTML,
)
from services.project_files.models import (
    ArtifactSlot,
    ArtifactVersion,
    InteractionCreation,
    InteractionOption,
    IndexedFile,
    NarrativeEdge,
    Project,
    Timeline,
    TimelineElement,
    TimelineSpan,
    MotionGraphic,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _with_final_video(project: Project, timeline_id: str, data: bytes) -> None:
    file_id = f"file:{timeline_id}:final"
    version_id = f"{timeline_id}:final:v1"
    slot_id = f"timeline:{timeline_id}:render"
    project.assets.files_by_id[file_id] = IndexedFile(
        file_id=file_id,
        kind="artifact_payload",
        relative_uri=f"assets/final/{timeline_id.replace(':', '_')}.mp4",
        sha256=_sha(data),
        size_bytes=len(data),
        media_type="video/mp4",
        created_at=NOW,
    )
    project.assets.artifact_slots_by_id[slot_id] = ArtifactSlot(
        slot_id=slot_id,
        kind="final_video",
        owner_ref=f"timeline:{timeline_id}",
        version_ids=[version_id],
        selected_version_id=version_id,
    )
    project.assets.artifact_versions_by_id[version_id] = ArtifactVersion(
        version_id=version_id,
        slot_id=slot_id,
        kind="final_video",
        owner_ref=f"timeline:{timeline_id}",
        name=f"{timeline_id} final",
        file_id=file_id,
        checksum=_sha(data),
        based_on_generation=0,
        created_at=NOW,
    )


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
        _with_final_video(project, timeline_id, data)
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
        assert manifest["segments"]["tl:ep4a"] == "segments/746c3a65703461.mp4"
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
        assert (
            archive.read("segments/746c3a657033.mp4") == b"video-bytes-tl:ep3"
        )
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


def test_no_generated_interface_means_no_export():
    project, payloads = _branching_project()
    project.interactive_presentation.motion = None
    with pytest.raises(InteractiveBundleError, match="interface is missing"):
        assemble_interactive_bundle(
            project,
            read_artifact_file=payloads.__getitem__,
        )


def test_interface_style_is_not_in_the_runtime_shell():
    assert "#b8ff2e" not in PLAYER_HTML
    assert "data-screen" not in PLAYER_HTML
    assert "开始故事" not in PLAYER_HTML


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
    _with_final_video(project, "tl:ep4a", b"revised-video")
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
    _with_final_video(project, "tl_ep3", b"other-bytes")
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
