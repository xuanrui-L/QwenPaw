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
  .gate{position:absolute;inset:0;display:flex;flex-direction:column;
    align-items:center;justify-content:center;gap:14px;
    background:rgba(13,11,10,.82)}
  .gate[hidden]{display:none}
  .gate .gate-play{width:84px;height:84px;border-radius:50%;cursor:pointer;
    border:2px solid #ff7f16;background:rgba(255,127,22,.25);color:#fff;
    font-size:30px;line-height:1;transition:all .15s}
  .gate .gate-play:hover{background:rgba(255,127,22,.6);transform:scale(1.06)}
  .gate p{font-size:14px;color:rgba(255,255,255,.85);padding:0 24px;
    text-align:center}
  .corner{position:absolute;top:14px;width:38px;height:38px;z-index:6;
    border-radius:50%;border:1px solid rgba(255,255,255,.35);cursor:pointer;
    background:rgba(0,0,0,.45);color:#fff;font-size:16px;line-height:1;
    -webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);
    transition:all .15s;display:none;align-items:center;justify-content:center}
  .corner:hover{background:rgba(255,127,22,.55);transform:scale(1.06)}
  #map-btn{left:14px}
  #map{background:rgba(13,11,10,.92)}
  #map h2{font-size:17px;font-weight:800;letter-spacing:.08em;
    margin-bottom:4px}
  #map .hint{font-size:11px;color:rgba(255,255,255,.55);margin-bottom:14px}
  #map-grid{display:flex;flex-direction:column;gap:10px;
    width:min(80%,340px);max-height:56vh;overflow-y:auto}
  .map-node{display:flex;align-items:center;gap:12px;padding:12px 16px;
    border:1px solid rgba(255,255,255,.3);border-radius:14px;cursor:pointer;
    background:rgba(255,255,255,.07);color:#fff;text-align:left;
    -webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);
    transition:all .15s}
  .map-node:hover{border-color:#ff7f16;background:rgba(255,127,22,.35);
    transform:scale(1.02)}
  .map-node .idx{width:26px;height:26px;border-radius:50%;flex-shrink:0;
    display:inline-flex;align-items:center;justify-content:center;
    background:rgba(255,127,22,.3);border:1px solid rgba(255,127,22,.7);
    font-size:12px;font-weight:800}
  .map-node .title{font-size:13px;font-weight:700;flex:1}
  .map-node .replay{font-size:10px;color:rgba(255,255,255,.6)}
  #map .close{position:absolute;top:14px;right:14px;width:34px;height:34px;
    border-radius:50%;border:1px solid rgba(255,255,255,.35);cursor:pointer;
    background:rgba(0,0,0,.45);color:#fff;font-size:15px}
</style>
</head>
<body>
<div id="stage">
  <video id="video" controls playsinline></video>
  <div id="overlay">
    <div id="question"></div>
    <div id="options"></div>
  </div>
  <div id="countdown"></div>
  <div id="start" class="gate">
    <button type="button" class="gate-play" aria-label="play">▶</button>
    <p id="start-title"></p>
  </div>
  <div id="restart" class="gate" hidden>
    <button type="button" class="gate-play" aria-label="replay">↺</button>
    <p id="restart-title"></p>
  </div>
  <button id="map-btn" type="button" class="corner" aria-label="story map"
    title="故事地图">▦</button>
  <div id="map" class="gate" hidden>
    <h2>故事地图</h2>
    <p class="hint">走过的节点会亮起，可随时重看；未探索的路径保持隐藏</p>
    <div id="map-grid"></div>
    <button type="button" class="close" aria-label="close">×</button>
  </div>
</div>
<script id="if-manifest" type="application/json">__MANIFEST_JSON__</script>
<script>
(function () {
  // Manifest is inlined so the bundle works when index.html is opened
  // directly from disk (file:// blocks fetch of sibling files).
  const manifest = JSON.parse(
    document.getElementById("if-manifest").textContent,
  );
  const video = document.getElementById("video");
  const overlay = document.getElementById("overlay");
  const questionEl = document.getElementById("question");
  const optionsEl = document.getElementById("options");
  const countdownEl = document.getElementById("countdown");
  const startGate = document.getElementById("start");
  const restartGate = document.getElementById("restart");
  const mapBtn = document.getElementById("map-btn");
  const mapGate = document.getElementById("map");
  const mapGrid = document.getElementById("map-grid");
  let timer = null;

  // Exploration progress: nodes the viewer has actually seen. Persisted so
  // reopening the bundle keeps the map; unseen branches stay hidden.
  const segmentOrder = Object.keys(manifest.segments);
  const storageKey =
    "if-visited:" + manifest.entry_timeline_id + ":" + segmentOrder.length;
  let visited;
  try {
    visited = new Set(JSON.parse(localStorage.getItem(storageKey) || "[]"));
  } catch (e) {
    visited = new Set();
  }

  function saveVisited() {
    try {
      localStorage.setItem(storageKey, JSON.stringify([...visited]));
    } catch (e) { /* file:// storage may be unavailable; map stays session-local */ }
  }

  function markVisited(timelineId) {
    if (!visited.has(timelineId)) {
      visited.add(timelineId);
      saveVisited();
    }
  }

  function renderMap() {
    mapGrid.innerHTML = "";
    segmentOrder.forEach((timelineId, index) => {
      if (!visited.has(timelineId)) return;
      const node = document.createElement("button");
      node.type = "button";
      node.className = "map-node";
      node.innerHTML =
        '<span class="idx">' + (index + 1) + "</span>" +
        '<span class="title"></span>' +
        '<span class="replay">重看 ▶</span>';
      node.querySelector(".title").textContent =
        (manifest.titles || {})[timelineId] || timelineId;
      node.onclick = () => {
        mapGate.hidden = true;
        playSegment(timelineId);
      };
      mapGrid.appendChild(node);
    });
  }

  mapBtn.onclick = () => {
    renderMap();
    mapGate.hidden = false;
    video.pause();
  };
  mapGate.querySelector(".close").onclick = () => {
    mapGate.hidden = true;
    if (!overlay.classList.contains("open") && restartGate.hidden) {
      video.play().catch(() => {});
    }
  };

  const entryTitle =
    (manifest.titles || {})[manifest.entry_timeline_id] || "";
  document.getElementById("start-title").textContent = entryTitle;

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
    restartGate.hidden = true;
    mapGate.hidden = true;
    const src = manifest.segments[timelineId];
    if (!src) return;
    markVisited(timelineId);
    mapBtn.style.display = "inline-flex";
    video.src = src;
    video.play().catch(() => {});
    video.onended = () => {
      const points = interactionsFor(timelineId);
      if (points.length) {
        showChoice(points[points.length - 1]);
      } else {
        // An ending segment: offer replay instead of a dead frame.
        document.getElementById("restart-title").textContent =
          (manifest.titles || {})[timelineId] || "";
        restartGate.hidden = false;
      }
    };
  }

  // Browsers block autoplay without a user gesture: playback starts from
  // the tap on the start gate (and later from option taps).
  startGate.querySelector("button").onclick = () => {
    startGate.hidden = true;
    playSegment(manifest.entry_timeline_id);
  };
  restartGate.querySelector("button").onclick = () => {
    playSegment(manifest.entry_timeline_id);
  };

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
        bundle.writestr(
            "index.html",
            PLAYER_HTML.replace(
                "__MANIFEST_JSON__",
                # </script> inside a JSON string would close the tag early.
                json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"),
            ),
        )
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
