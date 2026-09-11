# -*- coding: utf-8 -*-
"""Interactive bundle assembly for branching projects.

A branching project's final deliverable is NOT a single mp4: the audience
must actually tap a choice. The deliverable is a self-hosted bundle
(zip) containing:

- ``manifest.json``  — the persisted :class:`InteractiveManifest` dump plus a
  derived ``edge_index`` (edge_id -> label/prompt/target) so the player can
  join option ``edge_ref`` to display copy without a second lookup;
- ``index.html``     — a dependency-free HTML5 player that walks segments and
  renders tappable choice overlays (countdown + default edge);
- ``segments/*.mp4`` — one final-cut video per reachable timeline.

Zero new domain models beyond schema v9: segments come from the existing
``timeline:{id}:render`` final_video slots and interactions from the
``interaction`` elements sitting at the tail of their source timeline.
"""

from __future__ import annotations

import io
import base64
from pathlib import Path
import json
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from services.project_files.models import (
    narrative_timeline_ids,
    InteractionCreation,
    InteractionPoint,
    InteractiveManifest,
    Project,
    Timeline,
)


class InteractiveBundleError(ValueError):
    """Raised when the project cannot be assembled into a bundle yet."""


def _final_video_slot_id(timeline_id: str) -> str:
    return f"timeline:{timeline_id}:render"


def _selected_final_video(project: Project, timeline_id: str) -> str | None:
    slot = project.assets.artifact_slots_by_id.get(
        _final_video_slot_id(timeline_id),
    )
    if slot is None or slot.kind != "final_video":
        return None
    version = project.assets.artifact_versions_by_id.get(
        slot.selected_version_id or "",
    )
    from services.file_agent_runtime.work_graph import (
        _final_render_reads_current_versions,
    )

    if (
        version is None
        or version.stale
        or not _final_render_reads_current_versions(project, version)
    ):
        return None
    return slot.selected_version_id


def _interaction_points(
    timeline: Timeline,
) -> list[InteractionPoint]:
    points: list[InteractionPoint] = []
    for element in timeline.elements_by_id.values():
        creation = element.creation
        if not isinstance(creation, InteractionCreation):
            continue
        if not element.enabled:
            continue
        points.append(
            InteractionPoint(
                source_timeline_id=timeline.timeline_id,
                at_seconds=element.span.start_tick / timeline.ticks_per_second,
                question=creation.question,
                options=list(creation.options),
                countdown_seconds=creation.countdown_seconds,
                default_edge_ref=creation.default_edge_ref,
                motion_html=creation.motion.html if creation.motion else None,
            ),
        )
    points.sort(key=lambda point: point.at_seconds)
    return points


def reachable_timeline_ids(project: Project) -> list[str]:
    """Entry timeline plus everything reachable through narrative edges.

    Linear projects have no edges; every ordered timeline is then part of the
    single path and included in order.
    """

    order = list(narrative_timeline_ids(project))
    if not order:
        raise InteractiveBundleError("project has no timelines to bundle")
    if not project.narrative_edges:
        return order
    entry = order[0]
    adjacency: dict[str, list[str]] = {}
    for edge in project.narrative_edges:
        adjacency.setdefault(edge.source_timeline_id, []).append(
            edge.target_timeline_id,
        )
    seen: list[str] = []
    stack = [entry]
    while stack:
        current = stack.pop(0)
        if current in seen:
            continue
        seen.append(current)
        stack.extend(adjacency.get(current, []))
    return seen


def _narrative_adjacency(project: Project) -> dict[str, list[str]]:
    """source timeline -> de-duplicated targets, in edge declaration order."""

    adjacency: dict[str, list[str]] = {}
    for edge in project.narrative_edges:
        targets = adjacency.setdefault(edge.source_timeline_id, [])
        if edge.target_timeline_id not in targets:
            targets.append(edge.target_timeline_id)
    return adjacency


_UNVISITED, ON_PATH, DONE = 0, 1, 2


def _find_cycle(
    adjacency: Mapping[str, Sequence[str]],
) -> list[str] | None:
    """One cycle as a node path ending on the repeated node, else ``None``.

    Iterative white/grey/black DFS: recursion would blow the stack on a
    long hand-edited chain, and the path form is what makes the message
    actionable (``tl:a -> tl:b -> tl:a``) instead of "the graph is bad".
    """

    state: dict[str, int] = {}
    for root in adjacency:
        if state.get(root, _UNVISITED) != _UNVISITED:
            continue
        state[root] = ON_PATH
        path: list[str] = [root]
        pending: list[Iterator[str]] = [iter(adjacency.get(root, ()))]
        while path:
            target = next(pending[-1], None)
            if target is None:
                state[path[-1]] = DONE
                path.pop()
                pending.pop()
                continue
            mark = state.get(target, _UNVISITED)
            if mark == ON_PATH:
                return path[path.index(target) :] + [target]
            if mark == DONE:
                continue
            state[target] = ON_PATH
            path.append(target)
            pending.append(iter(adjacency.get(target, ())))
    return None


def _validate_story_graph(
    project: Project,
    reachable: list[str],
    interactions: list[InteractionPoint],
) -> None:
    """Refuse the two shapes that export a bundle which only *looks* done.

    A cycle never terminates at an ending, and a fork with no tappable
    decision leaves the audience staring at two segments they cannot pick
    between. Both are cheap to catch here and undiagnosable after export.
    """

    adjacency = _narrative_adjacency(project)
    cycle = _find_cycle(adjacency)
    if cycle is not None:
        raise InteractiveBundleError(
            "narrative edges form a cycle ("
            + " -> ".join(cycle)
            + "); the player can never reach an ending",
        )
    reachable_set = set(reachable)
    decided = {point.source_timeline_id for point in interactions}
    forks = sorted(
        source
        for source, targets in adjacency.items()
        if len(targets) > 1
        and source in reachable_set
        and source not in decided
    )
    if forks:
        raise InteractiveBundleError(
            "these timelines branch into several segments but carry no "
            "enabled interaction element, so the audience cannot choose: "
            + ", ".join(forks),
        )
    from .interaction_fingerprint import motion_matches_request

    edges = {edge.edge_id: edge for edge in project.narrative_edges}
    for tid in reachable:
        timeline = project.timelines.items[tid]
        elements = [
            e
            for e in timeline.elements_by_id.values()
            if e.enabled and isinstance(e.creation, InteractionCreation)
        ]
        if len(elements) > 1:
            raise InteractiveBundleError(
                f"{tid}: only one enabled interaction per node",
            )
        for element in elements:
            creation = element.creation
            refs = {o.edge_ref for o in creation.options}
            outgoing = {
                e.edge_id
                for e in project.narrative_edges
                if e.source_timeline_id == tid
            }
            if refs != outgoing:
                raise InteractiveBundleError(
                    f"{tid}: interaction options must cover exactly "
                    "its outgoing edges",
                )
            if creation.motion is None:
                raise InteractiveBundleError(
                    f"{tid}: interaction motion is missing",
                )
            if creation.motion and not motion_matches_request(
                creation.motion,
                creation,
                edges,
                project,
            ):
                raise InteractiveBundleError(
                    f"{tid}: interaction motion is stale; "
                    "regenerate and review it",
                )
            version_id = _selected_final_video(project, tid)
            version = project.assets.artifact_versions_by_id.get(
                version_id or "",
            )
            if (
                version
                and version.duration_seconds is not None
                and element.span.start_tick / timeline.ticks_per_second
                > version.duration_seconds
            ):
                raise InteractiveBundleError(
                    f"{tid}: interaction at_seconds exceeds "
                    "final video duration",
                )
    thin = sorted(
        point.source_timeline_id
        for point in interactions
        if len(point.options) < 2
    )
    if thin:
        raise InteractiveBundleError(
            "an interaction is only a decision with at least two options; "
            "these have one: " + ", ".join(thin),
        )


def derive_interactive_manifest(project: Project) -> InteractiveManifest:
    """Project → manifest. Fails closed when a reachable segment lacks its
    final cut, mirroring the work-graph assembly gate."""

    reachable = reachable_timeline_ids(project)
    segments: dict[str, str] = {}
    interactions: list[InteractionPoint] = []
    missing: list[str] = []
    known_edges = {edge.edge_id for edge in project.narrative_edges}
    for timeline_id in reachable:
        timeline = project.timelines.items.get(timeline_id)
        if timeline is None:
            raise InteractiveBundleError(
                f"narrative references unknown timeline {timeline_id!r}",
            )
        version_id = _selected_final_video(project, timeline_id)
        if version_id is None:
            missing.append(timeline_id)
        else:
            segments[timeline_id] = f"artifact-version:{version_id}"
        for point in _interaction_points(timeline):
            for option in point.options:
                if option.edge_ref not in known_edges:
                    raise InteractiveBundleError(
                        "interaction option references unknown edge "
                        f"{option.edge_ref!r}",
                    )
            interactions.append(point)
    if missing:
        raise InteractiveBundleError(
            "cannot assemble the interactive bundle before every reachable "
            "segment has a final cut; missing: " + ", ".join(sorted(missing)),
        )
    _validate_story_graph(project, reachable, interactions)
    return InteractiveManifest(
        entry_timeline_id=reachable[0],
        segments=segments,
        interactions=interactions,
    )


def _edge_index(project: Project) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for edge in project.narrative_edges:
        entry = {
            "label": edge.label,
            "prompt": edge.prompt,
            "target_timeline_id": edge.target_timeline_id,
            "source_timeline_id": edge.source_timeline_id,
        }
        # Risk tier is optional; absent means "neutral card" so projects
        # drafted before the tone field still play. The key is omitted
        # rather than emitted empty — the player distinguishes the two.
        if edge.tone:
            entry["tone"] = edge.tone
        index[edge.edge_id] = entry
    return index


# The approved game-shell design accent (sickly fluorescent green). A project
# may override it through ``meta.accent`` in a future schema bump; assembly
# always emits the default today so the player has one source of truth.
DEFAULT_ACCENT = "#b8ff2e"

#: Presentation-layer member name / version. Players must treat this file as
#: optional, so bumping it is never required for an older bundle to work.
PRESENTATION_FILENAME = "presentation.json"
PRESENTATION_SCHEMA_VERSION = 1


def _bundle_meta(project: Project) -> dict[str, str]:
    """Title-screen copy derived from project fields — never hardcoded theme
    copy in the player. The cover is the entry segment's first frame drawn by
    the <video> element at runtime (no thumbnail artifacts exist to reuse)."""

    description = (project.description or "").strip()
    tagline = description.splitlines()[0].strip() if description else ""
    return {
        "bundle_id": project.project_id,
        "title": project.name,
        "tagline": tagline,
        "synopsis": description or project.strategy.creative_brief.strip(),
        "accent": DEFAULT_ACCENT,
    }


def _node_index(
    project: Project,
    manifest: InteractiveManifest,
) -> dict[str, dict[str, Any]]:
    """Per-node story-map data: v9 Timeline title/synopsis plus the outgoing
    adjacency the player needs for the fog-of-war map. Branching projects get
    edges from ``narrative_edges``; linear projects chain ordered timelines so
    only the last one counts as the ending."""

    children: dict[str, list[str]] = {
        timeline_id: [] for timeline_id in manifest.segments
    }
    if project.narrative_edges:
        for edge in project.narrative_edges:
            targets = children.get(edge.source_timeline_id)
            if targets is None:
                continue
            if edge.target_timeline_id not in manifest.segments:
                continue
            if edge.target_timeline_id not in targets:
                targets.append(edge.target_timeline_id)
    else:
        order = [
            timeline_id
            for timeline_id in narrative_timeline_ids(project)
            if timeline_id in manifest.segments
        ]
        for source, target in zip(order, order[1:]):
            children[source] = [target]
    nodes: dict[str, dict[str, Any]] = {}
    for timeline_id in manifest.segments:
        timeline = project.timelines.items.get(timeline_id)
        title = (timeline.title if timeline else "") or timeline_id
        synopsis = timeline.synopsis if timeline else ""
        nodes[timeline_id] = {
            "title": title,
            "synopsis": synopsis,
            "children": children[timeline_id],
            "is_ending": not children[timeline_id],
        }
    return nodes


def _player_manifest(
    project: Project,
    manifest: InteractiveManifest,
) -> dict[str, Any]:
    """The in-zip manifest: model dump + edge join + local segment paths.

    ``meta`` / ``nodes`` are additive (game-shell player); ``titles`` stays so
    older players keep working against newly exported manifests, and the new
    player falls back gracefully when either block is missing."""

    payload = manifest.model_dump(mode="json")
    payload["segments"] = {
        timeline_id: f"segments/{timeline_id.encode().hex()}.mp4"
        for timeline_id in manifest.segments
    }
    payload["edge_index"] = _edge_index(project)
    payload["titles"] = {
        timeline_id: (
            project.timelines.items[timeline_id].title or timeline_id
        )
        for timeline_id in manifest.segments
    }
    payload["meta"] = _bundle_meta(project)
    payload["nodes"] = _node_index(project, manifest)
    return payload


PLAYER_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Interactive video</title><style>
html,body,#player{margin:0;width:100%;height:100%;overflow:hidden}
</style></head>
<body><div id="player"></div><script>__INTERACTION_RUNTIME__</script>
<script>__AUTHORED_RUNTIME__</script>
<script>IVBAuthoredPlayer.offline(__MANIFEST_JSON__);</script>
</body></html>"""


def assemble_interactive_bundle(
    project: Project,
    *,
    read_artifact_file: Callable[[str], bytes],
) -> bytes:
    """Build the distributable zip.

    ``read_artifact_file`` maps an ArtifactVersion ``file_id`` to raw bytes so
    this module stays storage-agnostic (the caller owns the assets root).
    """

    from .presentation_authoring import presentation_is_current
    from services.project_files.presentation_html import (
        validate_presentation_html,
    )

    if not presentation_is_current(project):
        raise InteractiveBundleError(
            "Project interface is missing or stale; "
            "generate and review interactive_presentation first",
        )
    presentation = project.interactive_presentation.motion
    authored_html = (
        read_artifact_file(presentation.html_file_id).decode("utf-8")
        if presentation.html_file_id
        else presentation.html
    )
    problems = validate_presentation_html(
        authored_html,
        narrative_timeline_ids(project),
        {
            key: value.model_dump(mode="json")
            for key, value in project.interactive_presentation.screens.items()
        },
    )
    if problems:
        raise InteractiveBundleError(
            "invalid project interface: " + "; ".join(problems),
        )
    manifest = derive_interactive_manifest(project)
    payload = _player_manifest(project, manifest)
    payload["authored_html"] = authored_html
    from services.project_files.interaction_html import (
        validate_interaction_html,
    )

    for point in payload["interactions"]:
        timeline = project.timelines.items[point["source_timeline_id"]]
        creation = next(
            e.creation
            for e in timeline.elements_by_id.values()
            if e.enabled and isinstance(e.creation, InteractionCreation)
        )
        if creation.base_frame_ref:
            ref = creation.base_frame_ref.removeprefix("artifact-version:")
            version = project.assets.artifact_versions_by_id.get(
                ref,
            ) or project.assets.source_versions_by_id.get(ref)
            indexed = (
                project.assets.files_by_id.get(version.file_id)
                if version
                else None
            )
            if indexed is None or indexed.media_type not in {
                "image/png",
                "image/jpeg",
                "image/webp",
            }:
                raise InteractiveBundleError(
                    "interaction base_frame_ref must reference "
                    "a PNG/JPEG/WebP image",
                )
            frame_data = read_artifact_file(indexed.file_id)
            if len(frame_data) > 8 * 1024 * 1024:
                raise InteractiveBundleError(
                    "interaction base frame exceeds 8 MB",
                )
            point[
                "base_frame_data_uri"
            ] = f"data:{indexed.media_type};base64," + base64.b64encode(
                frame_data,
            ).decode(
                "ascii",
            )
        if creation.motion and creation.motion.html_file_id:
            point["motion_html"] = read_artifact_file(
                creation.motion.html_file_id,
            ).decode("utf-8")
        if point["motion_html"]:
            problems = validate_interaction_html(
                point["motion_html"],
                [o["edge_ref"] for o in point["options"]],
                require_countdown=bool(
                    point["countdown_seconds"] and point["default_edge_ref"],
                ),
            )
            if problems:
                raise InteractiveBundleError(
                    "invalid interaction motion: " + "; ".join(problems),
                )
    runtime = (
        Path(__file__).resolve().parents[3]
        / "player/ivb/static/interaction-runtime.js"
    ).read_text(encoding="utf-8")
    import hashlib

    payload["content_revision"] = hashlib.sha256(
        json.dumps(
            {
                "presentation": payload,
                "media": {
                    tid: project.assets.artifact_versions_by_id[
                        ref.removeprefix("artifact-version:")
                    ].checksum
                    for tid, ref in manifest.segments.items()
                },
            },
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    authored_runtime = (
        Path(__file__).resolve().parents[3]
        / "player/ivb/static/authored-player.js"
    ).read_text(encoding="utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            "index.html",
            PLAYER_HTML.replace("__INTERACTION_RUNTIME__", runtime)
            .replace("__AUTHORED_RUNTIME__", authored_runtime)
            .replace(
                "__MANIFEST_JSON__",
                # </script> inside a JSON string would close the tag early.
                json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"),
            ),
        )
        bundle.writestr(
            "manifest.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        bundle.writestr(
            PRESENTATION_FILENAME,
            json.dumps(
                {
                    "schema_version": PRESENTATION_SCHEMA_VERSION,
                    "format": "agent_html_css",
                    "document": "presentation.html",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        bundle.writestr("presentation.html", authored_html)
        for timeline_id, ref in manifest.segments.items():
            version_id = ref.removeprefix("artifact-version:")
            version = project.assets.artifact_versions_by_id.get(version_id)
            if version is None:
                raise InteractiveBundleError(
                    f"segment version {version_id!r} "
                    "is not in the asset index",
                )
            bundle.writestr(
                payload["segments"][timeline_id],
                read_artifact_file(version.file_id),
            )
    return buffer.getvalue()
