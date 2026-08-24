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
import json
import zipfile
from collections.abc import Callable
from typing import Any

from services.project_files.models import (
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
            ),
        )
    points.sort(key=lambda point: point.at_seconds)
    return points


def reachable_timeline_ids(project: Project) -> list[str]:
    """Entry timeline plus everything reachable through narrative edges.

    Linear projects have no edges; every ordered timeline is then part of the
    single path and included in order.
    """

    order = list(project.timelines.order)
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
    return InteractiveManifest(
        entry_timeline_id=reachable[0],
        segments=segments,
        interactions=interactions,
    )


def _edge_index(project: Project) -> dict[str, dict[str, str]]:
    return {
        edge.edge_id: {
            "label": edge.label,
            "prompt": edge.prompt,
            "target_timeline_id": edge.target_timeline_id,
        }
        for edge in project.narrative_edges
    }


def _player_manifest(
    project: Project,
    manifest: InteractiveManifest,
) -> dict[str, Any]:
    """The in-zip manifest: model dump + edge join + local segment paths."""

    payload = manifest.model_dump(mode="json")
    payload["segments"] = {
        timeline_id: f"segments/{timeline_id.replace(':', '_')}.mp4"
        for timeline_id in manifest.segments
    }
    payload["edge_index"] = _edge_index(project)
    payload["titles"] = {
        timeline_id: (
            project.timelines.items[timeline_id].title or timeline_id
        )
        for timeline_id in manifest.segments
    }
    return payload


PLAYER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Interactive Story</title>
<style>
  html,body{margin:0;height:100%;background:#0d0b0a;color:#fff;
    font-family:system-ui,-apple-system,"PingFang SC",sans-serif}
  #stage{position:relative;height:100%;display:flex;align-items:center;
    justify-content:center}
  video{max-width:100%;max-height:100%}
  #overlay{position:absolute;inset:0;display:none;flex-direction:column;
    align-items:center;justify-content:flex-end;padding-bottom:12vh;
    background:linear-gradient(transparent 40%,rgba(0,0,0,.75))}
  #overlay.open{display:flex}
  #question{font-size:18px;font-weight:700;margin-bottom:18px;
    text-shadow:0 1px 4px rgba(0,0,0,.8);padding:0 24px;text-align:center}
  .option{width:min(78%,420px);margin-bottom:12px;padding:14px 18px;
    border:1px solid rgba(255,255,255,.45);border-radius:12px;
    background:rgba(0,0,0,.35);color:#fff;font-size:15px;font-weight:700;
    cursor:pointer;backdrop-filter:blur(6px);transition:all .15s}
  .option:hover{border-color:#ff7f16;background:rgba(255,127,22,.55);
    transform:scale(1.03)}
  #countdown{position:absolute;top:16px;right:16px;width:44px;height:44px;
    border:2px solid #f79009;border-radius:50%;display:none;align-items:center;
    justify-content:center;font-weight:700;background:rgba(0,0,0,.4)}
</style>
</head>
<body>
<div id="stage">
  <video id="video" playsinline></video>
  <div id="overlay">
    <div id="question"></div>
    <div id="options"></div>
  </div>
  <div id="countdown"></div>
</div>
<script>
(async function () {
  const manifest = await (await fetch("manifest.json")).json();
  const video = document.getElementById("video");
  const overlay = document.getElementById("overlay");
  const questionEl = document.getElementById("question");
  const optionsEl = document.getElementById("options");
  const countdownEl = document.getElementById("countdown");
  let timer = null;

  function interactionsFor(timelineId) {
    return manifest.interactions.filter(
      (item) => item.source_timeline_id === timelineId,
    );
  }

  function clearTimer() {
    if (timer) { clearInterval(timer); timer = null; }
    countdownEl.style.display = "none";
  }

  function playSegment(timelineId) {
    clearTimer();
    overlay.classList.remove("open");
    const src = manifest.segments[timelineId];
    if (!src) return;
    video.src = src;
    video.play().catch(() => {});
    video.onended = () => {
      const points = interactionsFor(timelineId);
      if (points.length) showChoice(points[points.length - 1]);
    };
  }

  function showChoice(point) {
    questionEl.textContent = point.question;
    optionsEl.innerHTML = "";
    for (const option of point.options) {
      const edge = manifest.edge_index[option.edge_ref] || {};
      const button = document.createElement("button");
      button.className = "option";
      button.textContent = edge.label || option.edge_ref;
      button.onclick = () => playSegment(edge.target_timeline_id);
      optionsEl.appendChild(button);
    }
    overlay.classList.add("open");
    if (point.countdown_seconds && point.default_edge_ref) {
      let remaining = Math.ceil(point.countdown_seconds);
      countdownEl.style.display = "flex";
      countdownEl.textContent = String(remaining);
      timer = setInterval(() => {
        remaining -= 1;
        countdownEl.textContent = String(Math.max(remaining, 0));
        if (remaining <= 0) {
          const edge = manifest.edge_index[point.default_edge_ref] || {};
          playSegment(edge.target_timeline_id);
        }
      }, 1000);
    }
  }

  playSegment(manifest.entry_timeline_id);
})();
</script>
</body>
</html>
"""


def assemble_interactive_bundle(
    project: Project,
    *,
    read_artifact_file: Callable[[str], bytes],
) -> bytes:
    """Build the distributable zip.

    ``read_artifact_file`` maps an ArtifactVersion ``file_id`` to raw bytes so
    this module stays storage-agnostic (the caller owns the assets root).
    """

    manifest = derive_interactive_manifest(project)
    payload = _player_manifest(project, manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("index.html", PLAYER_HTML)
        bundle.writestr(
            "manifest.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        for timeline_id, ref in manifest.segments.items():
            version_id = ref.removeprefix("artifact-version:")
            version = project.assets.artifact_versions_by_id.get(version_id)
            if version is None:
                raise InteractiveBundleError(
                    f"segment version {version_id!r} is not in the asset index",
                )
            bundle.writestr(
                payload["segments"][timeline_id],
                read_artifact_file(version.file_id),
            )
    return buffer.getvalue()
