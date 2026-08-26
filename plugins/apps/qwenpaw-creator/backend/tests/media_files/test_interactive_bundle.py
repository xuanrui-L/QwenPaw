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
        synopsis="沈修的双重身份被当众戳穿。",
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
        # Legacy field kept for backward compatibility with old players.
        assert manifest["titles"]["tl:ep3"] == "第3集 · 双重身份"
        # Game-shell additions: title-screen meta + story-map node index.
        assert manifest["meta"]["bundle_id"] == "project-branching"
        assert manifest["meta"]["title"] == "雾山谜案"
        assert manifest["meta"]["tagline"] == "雾山深处的双重身份悬疑剧。"
        assert manifest["meta"]["synopsis"] == (
            "互动悬疑短剧《雾山谜案》创意简报。"
        )
        assert manifest["meta"]["accent"] == "#b8ff2e"
        assert manifest["nodes"]["tl:ep3"] == {
            "title": "第3集 · 双重身份",
            "synopsis": "沈修的双重身份被当众戳穿。",
            "children": ["tl:ep4a", "tl:ep4b"],
            "is_ending": False,
        }
        assert manifest["nodes"]["tl:ep4a"]["is_ending"] is True
        assert manifest["nodes"]["tl:ep4a"]["synopsis"] == (
            "真相大白，正义得到伸张。"
        )
        assert (
            archive.read("segments/tl_ep3.mp4") == b"video-bytes-tl:ep3"
        )
        player = archive.read("index.html").decode()
        assert "edge_index" in player and "countdown" in player
        # Playback must start behind a user gesture: the game shell only
        # ever starts a segment from a click (title menu / map node /
        # choice card), so the exported bundle can never be a dead page.
        assert "开始故事" in player
        assert "showGate(" in player
        assert "playSegment(entryId)" in player
        # No bare auto-play bootstrap may come back.
        assert "\n  playSegment(entryId);" not in player
        assert "\n  playSegment(manifest.entry_timeline_id);" not in player
        # A missing / unrenderable branch must surface a visible notice.
        assert "素材未就绪" in player
        assert "video.onerror" in player


def test_player_game_shell_contract() -> None:
    """The game-shell player: 3 screens, fog-of-war story map, persisted
    progress, and graceful degradation (reduced motion, old manifests)."""

    project, payloads = _branching_project()

    bundle = assemble_interactive_bundle(
        project,
        read_artifact_file=lambda file_id: payloads[file_id],
    )

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        player = archive.read("index.html").decode()
    # Three screens + title menu (copy is data-driven via manifest.meta).
    for marker in (
        'id="scr-title"',
        'id="scr-map"',
        'id="scr-play"',
        "继续上次",
        "重新开始",
        "剧情地图",
        "结局图鉴",
    ):
        assert marker in player, marker
    # Progress persists per bundle in localStorage; 重新开始 must confirm.
    assert '"qwenpaw-if:" + (meta.bundle_id || entryId)' in player
    assert "localStorage.setItem" in player
    assert "localStorage.removeItem" in player
    assert "window.confirm" in player
    # Fog of war: only visited nodes + their direct "?" children render.
    assert "revealMap" in player
    assert "？？？" in player
    # CSS-only atmosphere must respect prefers-reduced-motion.
    assert "prefers-reduced-motion" in player
    # Old manifests without meta/nodes must keep working.
    assert "fallbackNodes" in player
    assert "manifest.nodes || fallbackNodes()" in player
    assert "manifest.meta ||" in player
    # The manifest stays inlined: file:// blocks fetch of sibling files.
    assert 'type="application/json"' in player
    assert "fetch(" not in player


def test_player_uniform_fog_hides_ending_identity() -> None:
    """彻底迷雾: every unvisited frontier node is the SAME anonymous "?"
    silhouette. Ending identity (★ / red pill / ENDING tag / title) may
    only appear after the node was reached, and the map layout must not
    reserve slots that hint at structure beyond the frontier."""

    project, payloads = _branching_project()

    bundle = assemble_interactive_bundle(
        project,
        read_artifact_file=lambda file_id: payloads[file_id],
    )

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        player = archive.read("index.html").decode()
    # The ending pill class is applied only inside the visited branch;
    # the old unconditional `cls += " node--end"` must not come back.
    assert 'if (info.is_ending) { el.classList.add("node--end"); }' in player
    assert 'cls += " node--end"' not in player
    # Pre-visit defaults: same tag / icon / name for every fogged node.
    assert 'let tag = "?";' in player
    assert 'let icon = "?";' in player
    assert 'let name = "？？？";' in player
    # No pre-visit ★ leak for locked endings (old branch removed).
    assert "} else if (info.is_ending)" not in player
    assert ".node--end.node--locked" not in player
    # One shared tooltip for all fogged nodes — the per-node ternary that
    # showed "未达成的结局" for ending frontiers must be gone (the
    # aggregate 结局图鉴 toast copy is allowed: it leaks nothing per-node).
    assert 'info.is_ending ? "未达成的结局"' not in player
    assert "未探索的剧情点" in player
    # Layout is computed over the revealed subgraph only: no reserved
    # empty slots / x-scale leaking depth beyond the frontier.
    assert "function layoutPositions(reveal)" in player
    assert "positions = layoutPositions(reveal);" in player
    # The aggregate endings counter (已解锁 X/Y) stays — aggregate-only.
    assert "endingIds.length" in player


def test_player_themed_choice_cards_contract() -> None:
    """主题化选项卡: accent-derived palette, runtime first-frame faces with
    graceful canvas-taint fallback, fog-blur for unvisited targets, and
    optional tone passthrough."""

    project, payloads = _branching_project()

    bundle = assemble_interactive_bundle(
        project,
        read_artifact_file=lambda file_id: payloads[file_id],
    )

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        player = archive.read("index.html").decode()
    # Palette derives from meta.accent: rgb triplet feeds hover glow,
    # borders and the countdown ring via rgba(var(--accent-rgb),X).
    assert "--accent-rgb" in player
    assert 'style.setProperty("--accent-rgb", rgb.join(","));' in player
    assert "rgba(var(--accent-rgb),.7)" in player  # card hover border
    # Choice-area CSS (countdown ring + cards) no longer hardcodes the
    # default green — it must follow the derived accent palette.
    choice_css = player[
        player.index(".cd-ring"):player.index("gate (tap-to-start")
    ]
    assert "rgba(184,255,46" not in choice_css
    # Face capture: offscreen <video> + <canvas>, taint-safe.
    assert "captureFrame" in player
    assert "toDataURL" in player
    assert "drawImage" in player
    # Fog interaction: unvisited targets are blurred silhouettes.
    assert "is-fogged" in player
    assert "blur(16px) brightness(.3)" in player
    # Gradient fallback face exists even when capture fails (c-face
    # keeps its accent-tinted gradient background).
    assert "c-face" in player
    # Optional tone: option-level wins over edge-level; absent = neutral.
    assert "option.tone || edge.tone" in player
    assert "data-tone" in player


def test_edge_index_tone_is_absent_today() -> None:
    """No v9 field carries tone yet: edge_index entries must not grow a
    tone key (backward-compatible passthrough only)."""

    project, payloads = _branching_project()

    bundle = assemble_interactive_bundle(
        project,
        read_artifact_file=lambda file_id: payloads[file_id],
    )

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    for entry in manifest["edge_index"].values():
        assert "tone" not in entry
        assert set(entry) == {"label", "prompt", "target_timeline_id"}


def test_linear_project_nodes_chain_in_order() -> None:
    """Without narrative edges the node index chains ordered timelines so
    the story map stays a path and only the last node is the ending."""

    project, payloads = _branching_project()
    project.narrative_edges = []
    project.timelines.items["tl:ep3"].elements_by_id.clear()

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
    project, payloads = _branching_project()
    project.narrative_edges = []
    # Drop the choice element so the linear cut stays plain video.
    project.timelines.items["tl:ep3"].elements_by_id.clear()

    manifest = derive_interactive_manifest(project)

    assert list(manifest.segments) == ["tl:ep3", "tl:ep4a", "tl:ep4b"]
    assert manifest.interactions == []
