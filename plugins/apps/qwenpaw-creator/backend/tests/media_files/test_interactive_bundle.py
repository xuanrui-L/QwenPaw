"""Interactive bundle assembly tests (branching deliverable, plan §2.7b)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone

import pytest

from services.media_files.interactive_bundle import (
    InteractiveBundleError,
    assemble_interactive_bundle,
    derive_interactive_manifest,
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


def _branching_project() -> tuple[Project, dict[str, bytes]]:
    source = Timeline(
        timeline_id="tl:ep3",
        title="第3集 · 双重身份",
        elements_by_id={
            "el:choice": TimelineElement(
                element_id="el:choice",
                label="观众抉择",
                span=TimelineSpan(start_tick=88_000, duration_tick=4_000),
                creation=InteractionCreation(
                    type="interaction",
                    question="是否当众揭发沈修？",
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
    branch_a = Timeline(timeline_id="tl:ep4a", title="第4集A · 真相大白")
    branch_b = Timeline(timeline_id="tl:ep4b", title="第4集B · 沉默代价")
    project = Project(
        project_id="project-branching",
        created_at=NOW,
        updated_at=NOW,
        name="雾山谜案",
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
    project.narrative_edges = project.narrative_edges[:1]

    with pytest.raises(InteractiveBundleError, match="edge:b"):
        derive_interactive_manifest(project)


def test_bundle_zip_contains_player_manifest_and_segments() -> None:
    project, payloads = _branching_project()

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
        assert manifest["segments"]["tl:ep4a"] == "segments/tl_ep4a.mp4"
        assert manifest["edge_index"]["edge:a"] == {
            "label": "选择A · 揭发真相",
            "prompt": "",
            "target_timeline_id": "tl:ep4a",
        }
        assert manifest["titles"]["tl:ep3"] == "第3集 · 双重身份"
        assert (
            archive.read("segments/tl_ep3.mp4") == b"video-bytes-tl:ep3"
        )
        player = archive.read("index.html").decode()
        assert "edge_index" in player and "countdown" in player


def test_linear_project_bundles_every_ordered_timeline() -> None:
    project, payloads = _branching_project()
    project.narrative_edges = []
    # Drop the choice element so the linear cut stays plain video.
    project.timelines.items["tl:ep3"].elements_by_id.clear()

    manifest = derive_interactive_manifest(project)

    assert list(manifest.segments) == ["tl:ep3", "tl:ep4a", "tl:ep4b"]
    assert manifest.interactions == []
