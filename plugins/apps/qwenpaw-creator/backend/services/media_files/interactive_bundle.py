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
    index: dict[str, dict[str, str]] = {}
    for edge in project.narrative_edges:
        entry = {
            "label": edge.label,
            "prompt": edge.prompt,
            "target_timeline_id": edge.target_timeline_id,
        }
        # Optional per-choice tone ("risky" / "safe" / "danger"). No v9
        # schema field carries it today (NarrativeEdge is strict), but a
        # future field — or a hand-edited manifest — flows straight through
        # to the player's card treatment; absent tone = neutral card.
        tone = getattr(edge, "tone", "") or ""
        if tone:
            entry["tone"] = tone
        index[edge.edge_id] = entry
    return index


# The approved game-shell design accent (sickly fluorescent green). A project
# may override it through ``meta.accent`` in a future schema bump; assembly
# always emits the default today so the player has one source of truth.
DEFAULT_ACCENT = "#b8ff2e"


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
        "synopsis": (project.strategy.creative_brief or "").strip(),
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
            for timeline_id in project.timelines.order
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
    payload["meta"] = _bundle_meta(project)
    payload["nodes"] = _node_index(project, manifest)
    return payload


PLAYER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>互动故事</title>
<style>
/* Game-shell player: midnight neon atmosphere, CSS-only effects.
   Theme copy (title / tagline / node names) all comes from manifest data. */
:root{
  --ink-0:#05070a; --ink-1:#0a0d11; --ink-2:#11161c; --ink-3:#1a222b;
  --fluor:#b8ff2e; --fluor-dim:#5f8a1a; --neon-red:#ff3355;
  --accent-rgb:184,255,46; /* JS re-derives from meta.accent at boot */
  --fog:#8fa3b0; --fog-dim:#4d5c66; --paper:#dfe8ec;
  --serif-cjk:"Songti SC","Noto Serif CJK SC","Source Han Serif SC",
    "SimSun",serif;
  --sans-cjk:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",
    system-ui,sans-serif;
  --mono:"SF Mono",ui-monospace,"Menlo","Cascadia Mono",monospace;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{background:var(--ink-0);color:var(--fog);font-family:var(--sans-cjk);
  overflow:hidden;-webkit-font-smoothing:antialiased}
button{font-family:inherit;cursor:pointer;border:none;background:none;
  color:inherit}
::selection{background:var(--fluor);color:var(--ink-0)}

/* ---------- global atmosphere: rain / fog / grain / scanlines ---------- */
.atmos{position:fixed;inset:0;pointer-events:none}
.atmos--rain{z-index:40;opacity:.5;mix-blend-mode:screen;background:
  repeating-linear-gradient(78deg,transparent 0 46px,
    rgba(150,190,210,.05) 46px 47px,transparent 47px 90px),
  repeating-linear-gradient(78deg,transparent 0 29px,
    rgba(130,170,190,.08) 29px 30px,transparent 30px 61px);
  animation:rainfall 1.1s linear infinite}
@keyframes rainfall{to{transform:translate3d(-46px,220px,0)}}
.atmos--fog{z-index:41;opacity:.35;background:
  radial-gradient(60% 45% at 18% 108%,rgba(142,163,176,.16),
    transparent 70%),
  radial-gradient(55% 40% at 85% 104%,rgba(142,163,176,.12),
    transparent 70%);
  animation:fogdrift 16s ease-in-out infinite alternate}
@keyframes fogdrift{from{transform:translateX(-2.5%) scale(1)}
  to{transform:translateX(2.5%) scale(1.06)}}
.atmos--grain{z-index:42;opacity:.5;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='160' height='160' filter='url(%23n)' opacity='0.14'/%3E%3C/svg%3E");
  animation:grainjit .5s steps(3) infinite}
@keyframes grainjit{0%{transform:translate(0,0)}
  34%{transform:translate(-14px,9px)}67%{transform:translate(9px,-13px)}
  100%{transform:translate(0,0)}}
.atmos--scan{z-index:43;opacity:.6;background:
  repeating-linear-gradient(0deg,rgba(0,0,0,.16) 0 1px,transparent 1px 3px)}
.atmos--vig{z-index:44;background:
  radial-gradient(120% 90% at 50% 42%,transparent 52%,
    rgba(2,4,6,.72) 100%)}

/* ---------- screens ---------- */
.screen{position:fixed;inset:0;z-index:10;display:none;flex-direction:column;
  opacity:0;transform:scale(1.015)}
.screen.is-active{display:flex;opacity:1;transform:none;
  animation:screenIn .55s cubic-bezier(.22,.9,.28,1) both}
@keyframes screenIn{
  from{opacity:0;transform:scale(1.02);filter:brightness(2.2) saturate(.2)}
  18%{filter:brightness(.5)}30%{filter:brightness(1.6)}
  to{opacity:1;transform:none;filter:none}}

/* ---------- screen 1: title ---------- */
#scr-title{align-items:center;justify-content:center;background:
  radial-gradient(90% 62% at 50% -8%,rgba(184,255,46,.075),transparent 62%),
  radial-gradient(46% 34% at 82% 14%,rgba(255,51,85,.06),transparent 70%),
  linear-gradient(180deg,#070a0d 0%,#05070a 55%,#030406 100%)}
#cover{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
  opacity:.16;filter:saturate(.55) brightness(.8);pointer-events:none}
.fluor-tube{position:absolute;top:5.5%;left:50%;transform:translateX(-50%);
  width:min(420px,60vw);height:10px;border-radius:5px;
  background:linear-gradient(180deg,#f4ffe0,#c9f76a 55%,#87b52c);
  box-shadow:0 0 18px 4px rgba(184,255,46,.55),
    0 0 90px 30px rgba(184,255,46,.16),0 24px 120px 40px rgba(184,255,46,.08);
  animation:tubeflicker 6.5s linear infinite}
.fluor-tube::before,.fluor-tube::after{content:"";position:absolute;
  top:-14px;width:7px;height:14px;background:#232a30;
  border-radius:2px 2px 0 0}
.fluor-tube::before{left:16%}.fluor-tube::after{right:16%}
@keyframes tubeflicker{
  0%,7.9%,9%,11.9%,13%,54.9%,56%,56.4%,57%,100%{opacity:1}
  8%,12%,55%,56.7%{opacity:.24}55.4%{opacity:.75}}
.neon-open{position:absolute;top:8%;right:6%;transform:rotate(4deg);
  font-family:var(--mono);font-size:clamp(14px,1.7vw,20px);
  letter-spacing:.55em;padding:.55em .5em .55em 1em;color:var(--neon-red);
  border:2px solid rgba(255,51,85,.75);border-radius:8px;
  text-shadow:0 0 8px rgba(255,51,85,.9),0 0 26px rgba(255,51,85,.5);
  box-shadow:0 0 14px rgba(255,51,85,.35),inset 0 0 14px rgba(255,51,85,.22);
  animation:neonbuzz 3.7s linear infinite}
@keyframes neonbuzz{0%,18.9%,20%,62.9%,64.4%,100%{opacity:1}
  19.4%,63.4%{opacity:.35}63.9%{opacity:.8}}
.shelf-sil{position:absolute;bottom:0;left:0;right:0;height:26vh;z-index:1;
  opacity:.5;pointer-events:none;background:
  linear-gradient(0deg,#020304 12%,transparent 60%),
  repeating-linear-gradient(90deg,transparent 0 7vw,
    rgba(20,27,33,.9) 7vw 7.4vw),
  repeating-linear-gradient(0deg,transparent 0 5.2vh,
    rgba(20,27,33,.9) 5.2vh 5.8vh)}
.title-stack{position:relative;text-align:center;z-index:2;padding:0 24px;
  max-width:92vw}
.title-kicker{font-family:var(--mono);font-size:12px;letter-spacing:.65em;
  text-indent:.65em;color:var(--fluor-dim);text-transform:uppercase;
  margin-bottom:1.6em;animation:riseIn .8s .15s cubic-bezier(.22,.9,.28,1)
  both}
.title-main{font-family:var(--serif-cjk);font-weight:900;
  font-size:clamp(38px,7.5vw,96px);line-height:1.08;letter-spacing:.06em;
  color:var(--paper);
  text-shadow:0 0 34px rgba(184,255,46,.22),0 2px 0 rgba(0,0,0,.6);
  animation:riseIn .9s .3s cubic-bezier(.22,.9,.28,1) both,
    titleflick 7s 2s linear infinite}
.title-main .tick{color:var(--fluor);
  text-shadow:0 0 22px rgba(184,255,46,.65)}
@keyframes titleflick{0%,41.9%,42.6%,43.9%,44.5%,100%{opacity:1}
  42.2%,44.2%{opacity:.55}}
.title-sub{margin-top:1.5em;font-size:clamp(13px,1.5vw,16px);
  letter-spacing:.24em;color:var(--fog);
  animation:riseIn .9s .5s cubic-bezier(.22,.9,.28,1) both}
.title-syn{margin:1.2em auto 0;max-width:560px;font-size:12px;
  line-height:1.8;color:var(--fog-dim);letter-spacing:.08em;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden;animation:riseIn .9s .55s both}
.title-rule{width:200px;height:1px;margin:2em auto;background:
  linear-gradient(90deg,transparent,var(--fluor-dim),transparent);
  animation:riseIn .9s .6s both}
@keyframes riseIn{from{opacity:0;transform:translateY(22px)}
  to{opacity:1;transform:none}}
.menu{display:flex;flex-direction:column;gap:10px;width:min(380px,86vw);
  margin:0 auto}
.menu-btn{position:relative;display:flex;align-items:center;gap:14px;
  padding:14px 22px;text-align:left;border:1px solid var(--ink-3);
  border-radius:4px;
  background:linear-gradient(180deg,rgba(17,22,28,.9),rgba(10,13,17,.9));
  color:var(--paper);font-size:15px;letter-spacing:.28em;
  transition:border-color .2s,transform .2s,box-shadow .25s,background .25s;
  animation:riseIn .7s both;overflow:hidden}
.menu-btn:nth-child(1){animation-delay:.7s}
.menu-btn:nth-child(2){animation-delay:.78s}
.menu-btn:nth-child(3){animation-delay:.86s}
.menu-btn:nth-child(4){animation-delay:.94s}
.menu-btn:nth-child(5){animation-delay:1.02s}
.menu-btn::before{content:"";width:16px;height:20px;flex:none;background:
  repeating-linear-gradient(90deg,var(--fog-dim) 0 2px,transparent 2px 5px);
  transition:background .2s}
.menu-btn::after{content:"";position:absolute;top:0;bottom:0;left:-70%;
  width:45%;background:linear-gradient(100deg,transparent,
    rgba(184,255,46,.1),transparent);
  transform:skewX(-18deg);transition:left .45s ease}
.menu-btn:hover,.menu-btn:focus-visible{border-color:rgba(184,255,46,.6);
  transform:translateX(6px);
  box-shadow:-4px 0 0 var(--fluor),0 0 26px rgba(184,255,46,.12);
  outline:none}
.menu-btn:hover::before{background:repeating-linear-gradient(90deg,
  var(--fluor) 0 2px,transparent 2px 5px)}
.menu-btn:hover::after{left:120%}
.menu-btn:active{transform:translateX(6px) scale(.985)}
.menu-btn .arrow{margin-left:auto;font-family:var(--mono);
  color:var(--fog-dim);letter-spacing:0;font-size:12px;max-width:46%;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  transition:color .2s,transform .2s}
.menu-btn:hover .arrow{color:var(--fluor);transform:translateX(3px)}
.menu-btn .badge{margin-left:auto;font-family:var(--mono);font-size:11px;
  letter-spacing:.1em;color:var(--fluor);
  border:1px solid rgba(184,255,46,.4);border-radius:99px;padding:3px 10px;
  background:rgba(184,255,46,.07)}
.menu-btn.is-hidden{display:none}
.menu-btn--reset{color:var(--fog-dim)}
.menu-btn--reset:hover{border-color:rgba(255,51,85,.6);
  box-shadow:-4px 0 0 var(--neon-red),0 0 26px rgba(255,51,85,.12)}

/* ---------- shared topbar ---------- */
.topbar{display:flex;align-items:center;gap:18px;padding:14px 26px;
  border-bottom:1px solid var(--ink-3);background:
  linear-gradient(180deg,rgba(10,13,17,.94),rgba(10,13,17,.7));z-index:5}
.topbar .brand{font-family:var(--serif-cjk);font-weight:900;font-size:16px;
  letter-spacing:.2em;color:var(--paper);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;max-width:34vw}
.topbar .crumb{font-family:var(--mono);font-size:11px;letter-spacing:.25em;
  color:var(--fog-dim);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.topbar .spacer{flex:1}
.chip-btn{font-family:var(--mono);font-size:11px;letter-spacing:.2em;
  padding:8px 16px;border:1px solid var(--ink-3);border-radius:3px;
  color:var(--fog);background:var(--ink-2);transition:all .2s;
  white-space:nowrap}
.chip-btn:hover{border-color:var(--fluor);color:var(--fluor);
  box-shadow:0 0 16px rgba(184,255,46,.15)}
.chip-btn--red:hover{border-color:var(--neon-red);color:var(--neon-red);
  box-shadow:0 0 16px rgba(255,51,85,.15)}

/* ---------- screen 2: story map ---------- */
#scr-map{background:linear-gradient(180deg,#080b0e,#05070a)}
.map-body{position:relative;flex:1;display:flex;min-height:0}
.map-stage{position:relative;flex:1;margin:22px;
  border:1px solid var(--ink-3);border-radius:8px;overflow:hidden;
  background:
  radial-gradient(70% 60% at 30% 20%,rgba(184,255,46,.045),transparent 60%),
  linear-gradient(180deg,#0a0e12,#070a0d)}
.map-stage::before{content:"";position:absolute;inset:0;opacity:.5;
  background:
  linear-gradient(90deg,rgba(143,163,176,.06) 1px,transparent 1px)
    0 0/56px 56px,
  linear-gradient(0deg,rgba(143,163,176,.06) 1px,transparent 1px)
    0 0/56px 56px}
.map-floor-tag{position:absolute;font-family:var(--mono);font-size:10px;
  letter-spacing:.3em;color:var(--fog-dim);opacity:.8;z-index:2}
.map-svg{position:absolute;inset:0;width:100%;height:100%}
.edge{fill:none;stroke-width:2;stroke-linecap:round}
.edge--seen{stroke:rgba(184,255,46,.5)}
.edge--locked{stroke:rgba(77,92,102,.35);stroke-dasharray:3 7}
.edge--live{stroke:var(--fluor);stroke-width:2.5;stroke-dasharray:6 10;
  animation:edgeflow 1.2s linear infinite;
  filter:drop-shadow(0 0 4px rgba(184,255,46,.6))}
@keyframes edgeflow{to{stroke-dashoffset:-16}}
.node{position:absolute;transform:translate(-50%,-50%);width:78px;
  height:78px;border-radius:50%;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:2px;border:2px solid;
  background:var(--ink-1);z-index:3;
  transition:transform .2s,box-shadow .25s,left .5s ease,top .5s ease}
.node .n-ico{font-size:19px;line-height:1}
.node .n-name{font-size:11px;letter-spacing:.08em;font-weight:600;
  max-width:70px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.node .n-tag{position:absolute;top:-22px;font-family:var(--mono);
  font-size:9px;letter-spacing:.25em;white-space:nowrap;
  color:var(--fog-dim)}
.node--seen{border-color:rgba(184,255,46,.65);color:#d8ffa0;
  box-shadow:0 0 18px rgba(184,255,46,.18),
    inset 0 0 14px rgba(184,255,46,.07)}
.node--seen:hover{transform:translate(-50%,-50%) scale(1.12);
  box-shadow:0 0 30px rgba(184,255,46,.4),
    inset 0 0 18px rgba(184,255,46,.14);z-index:6}
.node--current{border-color:var(--fluor);color:var(--fluor);
  box-shadow:0 0 26px rgba(184,255,46,.45);
  animation:nodepulse 1.8s ease-in-out infinite}
.node--current::after{content:"";position:absolute;inset:-10px;
  border-radius:50%;border:1px solid rgba(184,255,46,.5);
  animation:ringout 1.8s ease-out infinite}
@keyframes nodepulse{0%,100%{box-shadow:0 0 18px rgba(184,255,46,.3)}
  50%{box-shadow:0 0 36px rgba(184,255,46,.6)}}
@keyframes ringout{from{transform:scale(.86);opacity:1}
  to{transform:scale(1.3);opacity:0}}
.node--locked{border-color:var(--ink-3);color:var(--fog-dim);
  background:rgba(10,13,17,.9);cursor:not-allowed;opacity:.85}
.node--locked:hover{transform:translate(-50%,-50%) scale(1.04)}
/* node--end is applied ONLY after the node was visited: pre-visit every
   frontier node keeps the identical anonymous circle silhouette. */
.node--end{border-radius:14px;width:84px}
.node--end.node--seen{border-color:var(--neon-red);color:#ffb3c0;
  box-shadow:0 0 20px rgba(255,51,85,.25)}
.node--end.node--seen:hover{box-shadow:0 0 32px rgba(255,51,85,.45)}
.node--end.node--seen .n-ico{color:var(--neon-red);
  text-shadow:0 0 10px rgba(255,51,85,.8)}
.tip{position:absolute;z-index:20;width:230px;padding:13px 15px;
  border:1px solid rgba(184,255,46,.35);border-radius:6px;
  background:rgba(8,11,14,.96);
  box-shadow:0 12px 34px rgba(0,0,0,.6),0 0 20px rgba(184,255,46,.08);
  transform:translate(-50%,calc(-100% - 16px));opacity:0;
  pointer-events:none;transition:opacity .18s,transform .18s}
.tip.is-on{opacity:1;transform:translate(-50%,calc(-100% - 22px))}
.tip h4{font-family:var(--serif-cjk);font-size:14px;color:var(--paper);
  letter-spacing:.1em;margin-bottom:6px}
.tip p{font-size:12px;line-height:1.7;color:var(--fog)}
.tip .tip-act{margin-top:9px;font-family:var(--mono);font-size:10px;
  letter-spacing:.2em;color:var(--fluor)}
.tip .tip-act.is-lock{color:var(--fog-dim)}
.map-side{width:250px;flex:none;margin:22px 22px 22px 0;display:flex;
  flex-direction:column;gap:14px;overflow-y:auto}
.side-card{border:1px solid var(--ink-3);border-radius:8px;padding:18px;
  background:linear-gradient(180deg,rgba(17,22,28,.85),rgba(10,13,17,.85))}
.side-card h3{font-family:var(--mono);font-size:11px;letter-spacing:.35em;
  color:var(--fog-dim);margin-bottom:14px;padding-bottom:10px;
  border-bottom:1px dashed var(--ink-3)}
.lg-row{display:flex;align-items:center;gap:11px;font-size:12.5px;
  color:var(--fog);padding:5px 0}
.lg-dot{width:14px;height:14px;border-radius:50%;border:2px solid;
  flex:none}
.lg-dot--seen{border-color:rgba(184,255,46,.65);
  box-shadow:0 0 8px rgba(184,255,46,.3)}
.lg-dot--cur{border-color:var(--fluor);animation:nodepulse 1.8s infinite}
.lg-dot--lock{border-color:var(--ink-3)}
.lg-dot--end{border-radius:4px;border-color:var(--neon-red)}
.cov-num{font-family:var(--mono);font-size:34px;color:var(--fluor);
  text-shadow:0 0 16px rgba(184,255,46,.35)}
.cov-num small{font-size:14px;color:var(--fog-dim)}
.cov-bar{height:6px;border-radius:3px;background:var(--ink-3);
  margin:12px 0 8px;overflow:hidden}
.cov-bar i{display:block;height:100%;width:0;border-radius:3px;
  background:linear-gradient(90deg,var(--fluor-dim),var(--fluor));
  box-shadow:0 0 10px rgba(184,255,46,.5);transition:width .6s ease}
.cov-meta{font-family:var(--mono);font-size:10px;letter-spacing:.15em;
  color:var(--fog-dim);display:flex;justify-content:space-between}
.side-endings{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}
.end-slot{width:44px;height:44px;display:flex;align-items:center;
  justify-content:center;border-radius:6px;border:1px solid var(--ink-3);
  font-size:15px;color:var(--fog-dim);transition:all .2s}
.end-slot.is-got{border-color:rgba(255,51,85,.6);color:var(--neon-red);
  text-shadow:0 0 10px rgba(255,51,85,.7);background:rgba(255,51,85,.06)}
.end-slot:hover{transform:translateY(-3px)}

/* ---------- screen 3: playback ---------- */
#scr-play{background:#020304}
.play-stage{position:relative;flex:1;min-height:0;display:flex;
  align-items:center;justify-content:center;overflow:hidden}
#video{max-width:100%;max-height:100%;z-index:1;background:#000}
.hud{position:absolute;inset:0;z-index:4;pointer-events:none;
  font-family:var(--mono)}
.hud .rec{position:absolute;top:22px;left:26px;display:flex;
  align-items:center;gap:9px;font-size:12px;letter-spacing:.3em;
  color:var(--neon-red)}
.hud .rec i{width:9px;height:9px;border-radius:50%;
  background:var(--neon-red);box-shadow:0 0 10px var(--neon-red);
  animation:recblink 1.2s steps(2) infinite}
@keyframes recblink{50%{opacity:.15}}
.hud .cam{position:absolute;top:22px;right:26px;font-size:11px;
  letter-spacing:.28em;color:rgba(184,255,46,.75)}
.hud .tc{position:absolute;bottom:24px;left:26px;font-size:13px;
  letter-spacing:.2em;color:var(--paper);
  text-shadow:0 0 8px rgba(184,255,46,.4)}
.hud .chapter{position:absolute;bottom:24px;right:26px;font-size:11px;
  letter-spacing:.3em;color:var(--fog-dim)}
.hud .corner{position:absolute;width:26px;height:26px;
  border:2px solid rgba(184,255,46,.35)}
.hud .corner.tl{top:14px;left:14px;border-right:0;border-bottom:0}
.hud .corner.tr{top:14px;right:14px;border-left:0;border-bottom:0}
.hud .corner.bl{bottom:14px;left:14px;border-right:0;border-top:0}
.hud .corner.br{bottom:14px;right:14px;border-left:0;border-top:0}
.choice-layer{position:absolute;inset:0;z-index:6;display:flex;
  flex-direction:column;align-items:center;justify-content:flex-end;
  padding-bottom:7vh;gap:26px;background:
  linear-gradient(180deg,transparent 30%,rgba(2,3,5,.68) 78%);
  opacity:0;pointer-events:none;transition:opacity .4s}
.choice-layer.is-on{opacity:1;pointer-events:auto}
.choice-head{display:flex;align-items:center;gap:20px}
.choice-title{font-family:var(--serif-cjk);font-weight:900;
  font-size:clamp(19px,2.4vw,26px);letter-spacing:.3em;color:var(--paper);
  text-shadow:0 0 24px rgba(255,51,85,.35)}
.choice-layer.is-on .choice-title{animation:riseIn .5s .1s both}
.cd-ring{position:relative;width:58px;height:58px;flex:none;display:none}
.cd-ring.is-shown{display:block}
.choice-layer.is-on .cd-ring{animation:riseIn .5s .15s both}
.cd-ring svg{width:100%;height:100%;transform:rotate(-90deg)}
.cd-ring .track{fill:none;stroke:var(--ink-3);stroke-width:4}
.cd-ring .prog{fill:none;stroke:var(--fluor);stroke-width:4;
  stroke-linecap:round;
  filter:drop-shadow(0 0 5px rgba(var(--accent-rgb),.7));
  transition:stroke-dashoffset .95s linear,stroke .3s}
.cd-ring.is-urgent .prog{stroke:var(--neon-red);
  filter:drop-shadow(0 0 6px rgba(255,51,85,.8))}
.cd-ring .cd-num{position:absolute;inset:0;display:flex;
  align-items:center;justify-content:center;font-family:var(--mono);
  font-size:19px;color:var(--paper)}
.cd-ring.is-urgent .cd-num{color:var(--neon-red);
  animation:recblink 1s steps(2) infinite}
.choice-row{display:flex;gap:18px;width:min(980px,94vw);
  justify-content:center}
.choice-card{position:relative;flex:1;max-width:300px;text-align:left;
  padding:20px 20px 18px;border-radius:6px;border:1px solid var(--ink-3);
  background:linear-gradient(165deg,rgba(20,26,32,.96),rgba(9,12,15,.96));
  color:var(--paper);
  transition:transform .22s,border-color .22s,box-shadow .28s;
  opacity:0;transform:translateY(46px)}
.choice-layer.is-on .choice-card{
  animation:cardIn .55s cubic-bezier(.2,.95,.3,1.1) both}
.choice-layer.is-on .choice-card:nth-child(1){animation-delay:.2s}
.choice-layer.is-on .choice-card:nth-child(2){animation-delay:.32s}
.choice-layer.is-on .choice-card:nth-child(3){animation-delay:.44s}
.choice-layer.is-on .choice-card:nth-child(4){animation-delay:.56s}
@keyframes cardIn{from{opacity:0;transform:translateY(46px)}
  to{opacity:1;transform:none}}
/* Card face: first frame of the target segment, captured at runtime.
   Fallback = accent-tinted gradient. Unvisited targets stay an
   unrecognizable silhouette (blur + darkening) so nothing leaks. */
.choice-card .c-face{position:relative;display:block;height:92px;
  margin:-20px -20px 14px;border-radius:6px 6px 0 0;overflow:hidden;
  border-bottom:1px solid rgba(var(--accent-rgb),.18);background:
  linear-gradient(135deg,rgba(var(--accent-rgb),.2),
    rgba(var(--accent-rgb),.04) 52%,rgba(255,51,85,.1)),var(--ink-2)}
.choice-card .c-face .f-shot{position:absolute;inset:0;width:100%;
  height:100%;object-fit:cover}
.choice-card .c-face.is-fogged .f-shot{
  filter:blur(16px) brightness(.3) saturate(.3);transform:scale(1.18)}
.choice-card .c-face.is-fogged::after{content:"";position:absolute;
  inset:0;background:
  radial-gradient(60% 80% at 50% 50%,rgba(2,3,5,.35),rgba(2,3,5,.78))}
.choice-card .c-face .f-mark{position:absolute;inset:0;z-index:1;
  display:flex;align-items:center;justify-content:center;
  font-style:normal;font-family:var(--mono);font-size:30px;
  color:var(--fog-dim);text-shadow:0 0 14px rgba(0,0,0,.9)}
.choice-card .c-key{position:absolute;top:-11px;left:16px;z-index:2;
  font-family:var(--mono);font-size:10px;letter-spacing:.2em;
  padding:3px 9px;border-radius:3px;background:var(--ink-2);
  border:1px solid var(--ink-3);color:var(--fog-dim);transition:all .2s}
.choice-card .c-name{display:block;font-size:16.5px;font-weight:700;
  letter-spacing:.12em;margin-bottom:8px}
.choice-card .c-desc{font-size:12px;line-height:1.75;color:var(--fog)}
.choice-card .c-risk{margin-top:12px;font-family:var(--mono);
  font-size:10px;letter-spacing:.22em;color:var(--fog-dim);display:flex;
  align-items:center;gap:8px}
.choice-card .c-risk i{width:26px;height:3px;border-radius:2px;
  background:var(--fluor-dim)}
.choice-card .c-risk.is-hi i{background:var(--neon-red);
  box-shadow:0 0 8px rgba(255,51,85,.6)}
.choice-card .c-tone{margin-left:auto;font-style:normal;
  letter-spacing:.18em;color:var(--fog-dim)}
.choice-card:hover,.choice-card:focus-visible{transform:translateY(-8px);
  border-color:rgba(var(--accent-rgb),.7);outline:none;
  box-shadow:0 18px 44px rgba(0,0,0,.6),
    0 0 30px rgba(var(--accent-rgb),.16)}
.choice-card:hover .c-key{border-color:var(--fluor);color:var(--fluor)}
.choice-card:active{transform:translateY(-4px) scale(.98)}
/* Optional manifest `tone`: subtle border/icon shade, neutral when
   absent (backward compatible). */
.choice-card[data-tone="risky"],.choice-card[data-tone="danger"]{
  border-color:rgba(255,51,85,.4)}
.choice-card[data-tone="risky"]:hover,
.choice-card[data-tone="danger"]:hover{
  border-color:rgba(255,51,85,.8);
  box-shadow:0 18px 44px rgba(0,0,0,.6),0 0 30px rgba(255,51,85,.2)}
.choice-card[data-tone="risky"] .c-tone,
.choice-card[data-tone="danger"] .c-tone{color:var(--neon-red)}
.choice-card[data-tone="safe"]{
  border-color:rgba(var(--accent-rgb),.35)}
.choice-card[data-tone="safe"] .c-tone{color:var(--fluor)}
.choice-card.is-default{border-color:rgba(var(--accent-rgb),.45);
  box-shadow:0 0 22px rgba(var(--accent-rgb),.1)}
.choice-card.is-default::after{content:"倒计时默认";position:absolute;
  top:-11px;right:14px;z-index:2;font-family:var(--mono);font-size:10px;
  letter-spacing:.2em;padding:3px 9px;border-radius:3px;
  background:rgba(var(--accent-rgb),.12);
  border:1px solid rgba(var(--accent-rgb),.5);color:var(--fluor)}
.choice-card.is-picked{border-color:var(--fluor);
  box-shadow:0 0 0 1px var(--fluor),0 0 44px rgba(var(--accent-rgb),.35);
  transform:translateY(-8px) scale(1.03)}
.choice-card.is-faded{opacity:.25;filter:saturate(.3);
  pointer-events:none;transform:translateY(6px)}

/* ---------- gate (tap-to-start / missing asset / ending) ---------- */
#gate{position:fixed;inset:0;z-index:60;display:flex;
  flex-direction:column;align-items:center;justify-content:center;gap:18px;
  background:rgba(2,3,5,.82);text-align:center;padding:0 24px;
  backdrop-filter:blur(3px)}
#gate.hidden{display:none}
#gate-title{font-family:var(--serif-cjk);font-weight:900;font-size:22px;
  letter-spacing:.18em;color:var(--paper);
  text-shadow:0 0 24px rgba(184,255,46,.25)}
#gate.gate--end #gate-title{text-shadow:0 0 24px rgba(255,51,85,.45)}
#gate-message{font-size:13px;color:var(--fog);max-width:460px;
  line-height:1.8;letter-spacing:.06em}
#gate-actions{display:flex;gap:12px;flex-wrap:wrap;justify-content:center}
.gate-btn{padding:13px 30px;border:1px solid rgba(184,255,46,.55);
  border-radius:999px;background:rgba(184,255,46,.08);color:var(--paper);
  font-size:15px;font-weight:700;letter-spacing:.14em;transition:all .18s}
.gate-btn:hover{background:rgba(184,255,46,.28);transform:scale(1.05);
  box-shadow:0 0 26px rgba(184,255,46,.25)}
.gate-btn--dim{border-color:var(--ink-3);background:var(--ink-2);
  color:var(--fog)}
.gate-btn--dim:hover{background:var(--ink-3);box-shadow:none}

/* ---------- toast ---------- */
.toast{position:fixed;top:76px;left:50%;
  transform:translate(-50%,-16px);z-index:90;display:flex;
  align-items:center;gap:11px;padding:12px 22px;border-radius:4px;
  border:1px solid rgba(184,255,46,.55);background:rgba(8,11,14,.95);
  color:var(--paper);font-size:13px;letter-spacing:.22em;
  box-shadow:0 10px 34px rgba(0,0,0,.6),0 0 26px rgba(184,255,46,.18);
  opacity:0;pointer-events:none;transition:opacity .3s,transform .3s}
.toast.is-on{opacity:1;transform:translate(-50%,0)}
.toast::before{content:"◈";color:var(--fluor);letter-spacing:0}

/* ---------- reduced motion / small screens ---------- */
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;
    animation-iteration-count:1!important;
    transition-duration:.01ms!important}
  .atmos--rain,.atmos--grain{display:none}
}
@media (max-width:860px){
  .map-side{display:none}
  .choice-row{flex-direction:column;align-items:center}
  .choice-card{width:min(340px,90vw);max-width:none}
}
</style>
</head>
<body>

<div class="atmos atmos--fog"></div>
<div class="atmos atmos--rain"></div>
<div class="atmos atmos--grain"></div>
<div class="atmos atmos--scan"></div>
<div class="atmos atmos--vig"></div>

<!-- screen 1: title -->
<section class="screen is-active" id="scr-title" aria-label="标题屏">
  <video id="cover" muted playsinline preload="auto" aria-hidden="true">
  </video>
  <div class="fluor-tube" aria-hidden="true"></div>
  <div class="neon-open" aria-hidden="true">ON AIR</div>
  <div class="shelf-sil" aria-hidden="true"></div>
  <div class="title-stack">
    <p class="title-kicker">QWENPAW · INTERACTIVE FILM</p>
    <h1 class="title-main" id="title-main"></h1>
    <p class="title-sub" id="title-sub"></p>
    <p class="title-syn" id="title-syn"></p>
    <div class="title-rule"></div>
    <nav class="menu" aria-label="主菜单">
      <button class="menu-btn" id="btn-start">开始故事
        <span class="arrow">►</span></button>
      <button class="menu-btn is-hidden" id="btn-resume">继续上次
        <span class="arrow" id="resume-label"></span></button>
      <button class="menu-btn" id="btn-map">剧情地图
        <span class="arrow">►</span></button>
      <button class="menu-btn" id="btn-endings">结局图鉴
        <span class="badge" id="endings-badge"></span></button>
      <button class="menu-btn menu-btn--reset is-hidden" id="btn-reset">
        重新开始<span class="arrow">⟲</span></button>
    </nav>
  </div>
</section>

<!-- screen 2: story map -->
<section class="screen" id="scr-map" aria-label="剧情地图">
  <header class="topbar">
    <span class="brand" id="map-brand"></span>
    <span class="crumb">/ 剧情地图 · STORY MAP</span>
    <span class="spacer"></span>
    <button class="chip-btn" id="btn-map-continue">▶ 继续播放</button>
    <button class="chip-btn chip-btn--red" id="btn-map-home">✕ 返回主页
    </button>
  </header>
  <div class="map-body">
    <div class="map-stage" id="mapStage">
      <span class="map-floor-tag" style="left:3%;top:4%">STORY MAP ·
        已探索的剧情才会点亮</span>
      <span class="map-floor-tag" style="right:3%;bottom:4%">? = 未探索的
        分支</span>
      <svg class="map-svg" id="mapSvg" viewBox="0 0 1000 560"
        preserveAspectRatio="none" aria-hidden="true"></svg>
      <div class="tip" id="mapTip" role="tooltip">
        <h4></h4><p></p><div class="tip-act"></div>
      </div>
    </div>
    <aside class="map-side">
      <div class="side-card">
        <h3>图例 LEGEND</h3>
        <div class="lg-row"><span class="lg-dot lg-dot--seen"></span>
          已看过 · 可重看</div>
        <div class="lg-row"><span class="lg-dot lg-dot--cur"></span>
          当前进度</div>
        <div class="lg-row"><span class="lg-dot lg-dot--lock"></span>
          未探索（?）</div>
        <div class="lg-row"><span class="lg-dot lg-dot--end"></span>
          ★ 已达成的结局</div>
      </div>
      <div class="side-card">
        <h3>剧情覆盖率</h3>
        <div class="cov-num"><span id="cov-num">0</span><small> %</small>
        </div>
        <div class="cov-bar"><i id="cov-fill"></i></div>
        <div class="cov-meta"><span id="cov-nodes"></span>
          <span id="cov-endings"></span></div>
      </div>
      <div class="side-card">
        <h3 id="endings-title">结局图鉴</h3>
        <div class="side-endings" id="ending-slots"></div>
      </div>
    </aside>
  </div>
</section>

<!-- screen 3: playback -->
<section class="screen" id="scr-play" aria-label="播放屏">
  <header class="topbar">
    <span class="brand" id="play-brand"></span>
    <span class="crumb" id="play-crumb"></span>
    <span class="spacer"></span>
    <button class="chip-btn" id="btn-play-map">◈ 剧情地图</button>
    <button class="chip-btn chip-btn--red" id="btn-play-home">✕ 退出
    </button>
  </header>
  <div class="play-stage">
    <video id="video" playsinline></video>
    <div class="hud" aria-hidden="true">
      <span class="rec"><i></i>REC</span>
      <span class="cam" id="hud-cam"></span>
      <span class="tc" id="timecode">00:00:00:00</span>
      <span class="chapter" id="hud-chapter"></span>
      <span class="corner tl"></span><span class="corner tr"></span>
      <span class="corner bl"></span><span class="corner br"></span>
    </div>
    <div class="choice-layer" id="choiceLayer">
      <div class="choice-head">
        <h2 class="choice-title" id="question"></h2>
        <div class="cd-ring" id="cdRing" aria-label="选择倒计时">
          <svg viewBox="0 0 58 58">
            <circle class="track" cx="29" cy="29" r="25"/>
            <circle class="prog" id="cdProg" cx="29" cy="29" r="25"/>
          </svg>
          <span class="cd-num" id="cdNum"></span>
        </div>
      </div>
      <div class="choice-row" id="options"></div>
    </div>
  </div>
</section>

<div id="gate" class="hidden">
  <div id="gate-title"></div>
  <div id="gate-message"></div>
  <div id="gate-actions"></div>
</div>

<div class="toast" id="toast" role="status"></div>

<script id="if-manifest" type="application/json">__MANIFEST_JSON__</script>
<script>
(function () {
  "use strict";
  // Manifest is inlined so the bundle works when index.html is opened
  // directly from disk (file:// blocks fetch of sibling files).
  const manifest = JSON.parse(
    document.getElementById("if-manifest").textContent,
  );
  const reduced =
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const byId = (id) => document.getElementById(id);

  // ---------- data (with fallbacks for pre-game-shell manifests) ----------
  const entryId = manifest.entry_timeline_id;
  const segments = manifest.segments || {};
  const edgeIndex = manifest.edge_index || {};
  const interactions = manifest.interactions || [];

  function fallbackNodes() {
    // Old manifests carry only `titles`; derive the adjacency the story
    // map needs from interactions + edge_index so they keep working.
    const titles = manifest.titles || {};
    const kids = {};
    Object.keys(segments).forEach((id) => { kids[id] = []; });
    interactions.forEach((point) => {
      (point.options || []).forEach((option) => {
        const edge = edgeIndex[option.edge_ref];
        if (!edge) { return; }
        const list = kids[point.source_timeline_id];
        const target = edge.target_timeline_id;
        if (list && kids[target] && list.indexOf(target) < 0) {
          list.push(target);
        }
      });
    });
    const out = {};
    Object.keys(segments).forEach((id) => {
      out[id] = {
        title: titles[id] || id,
        synopsis: "",
        children: kids[id],
        is_ending: kids[id].length === 0,
      };
    });
    return out;
  }
  const nodes = manifest.nodes || fallbackNodes();
  const meta = manifest.meta || {
    bundle_id: "",
    title: (nodes[entryId] || {}).title || "互动故事",
    tagline: "",
    synopsis: "",
    accent: "",
  };
  // Theme: derive a small palette from meta.accent (base, dimmed shade,
  // and an rgb triplet for glow / border / countdown-ring alphas) so
  // different projects get visibly different cards.
  (function applyAccent() {
    const match = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(meta.accent || "");
    if (!match) { return; }
    let hex = match[1];
    if (hex.length === 3) {
      hex = hex.replace(/./g, (glyph) => glyph + glyph);
    }
    const value = parseInt(hex, 16);
    const rgb = [(value >> 16) & 255, (value >> 8) & 255, value & 255];
    const style = document.documentElement.style;
    style.setProperty("--fluor", "rgb(" + rgb.join(",") + ")");
    style.setProperty("--accent-rgb", rgb.join(","));
    style.setProperty("--fluor-dim", "rgb(" +
      rgb.map((channel) => Math.round(channel * 0.55)).join(",") + ")");
  })();
  document.title = meta.title || "互动故事";

  const allIds = Object.keys(nodes);
  const endingIds = allIds.filter((id) => nodes[id].is_ending);

  function nodeTitle(id) {
    if (nodes[id] && nodes[id].title) { return nodes[id].title; }
    return (manifest.titles || {})[id] || id;
  }

  // ---------- persisted progress (fog-of-war state) ----------
  const storageKey = "qwenpaw-if:" + (meta.bundle_id || entryId);
  function freshState() {
    return { visited: [], last: null, endings: [] };
  }
  function loadState() {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const parsed = JSON.parse(raw);
        return {
          visited: (parsed.visited || []).filter((id) => nodes[id]),
          last: nodes[parsed.last] ? parsed.last : null,
          endings: (parsed.endings || []).filter((id) => nodes[id]),
        };
      }
    } catch (error) { /* storage unavailable: session-only progress */ }
    return freshState();
  }
  function saveState() {
    try {
      localStorage.setItem(storageKey, JSON.stringify(state));
    } catch (error) { /* storage unavailable: session-only progress */ }
  }
  let state = loadState();
  function hasProgress() {
    return state.visited.length > 0 || state.endings.length > 0;
  }
  function markVisited(id) {
    if (state.visited.indexOf(id) < 0) { state.visited.push(id); }
    state.last = id;
    saveState();
  }
  function unlockEnding(id) {
    if (state.endings.indexOf(id) < 0) {
      state.endings.push(id);
      saveState();
      toast("解锁结局 ·「" + nodeTitle(id) + "」");
    }
  }

  // ---------- screens ----------
  const screens = {
    title: byId("scr-title"),
    map: byId("scr-map"),
    play: byId("scr-play"),
  };
  let current = "title";
  function go(name) {
    if (!screens[name]) { return; }
    Object.keys(screens).forEach((key) => {
      screens[key].classList.remove("is-active");
    });
    screens[name].classList.add("is-active");
    current = name;
    if (name !== "play") {
      clearTimer();
      video.pause();
      overlayOff();
      hideGate();
    }
    if (name === "map") { renderMap(); }
    if (name === "title") { refreshTitle(); }
  }

  // ---------- toast ----------
  const toastEl = byId("toast");
  let toastTimer = null;
  function toast(message) {
    toastEl.textContent = message;
    toastEl.classList.add("is-on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toastEl.classList.remove("is-on");
    }, 2400);
  }

  // ---------- gate (tap-to-start / missing asset / ending) ----------
  const gate = byId("gate");
  const gateTitle = byId("gate-title");
  const gateMessage = byId("gate-message");
  const gateActions = byId("gate-actions");
  function showGate(title, message, actions, isEnding) {
    gateTitle.textContent = title;
    gateMessage.textContent = message;
    gate.classList.toggle("gate--end", Boolean(isEnding));
    gateActions.innerHTML = "";
    (actions || []).forEach((action) => {
      const button = document.createElement("button");
      button.className = "gate-btn" + (action.dim ? " gate-btn--dim" : "");
      button.textContent = action.label;
      button.onclick = () => { hideGate(); action.onClick(); };
      gateActions.appendChild(button);
    });
    gate.classList.remove("hidden");
  }
  function hideGate() { gate.classList.add("hidden"); }

  // ---------- title screen ----------
  const cover = byId("cover");
  function applyMeta() {
    const titleEl = byId("title-main");
    titleEl.innerHTML = "";
    const text = meta.title || "互动故事";
    // Accent the second glyph like the approved design (data-driven copy).
    Array.from(text).forEach((glyph, index) => {
      const span = document.createElement("span");
      if (index === 1) { span.className = "tick"; }
      span.textContent = glyph;
      titleEl.appendChild(span);
    });
    byId("title-sub").textContent =
      meta.tagline || "互动故事 · 你的选择决定剧情走向";
    byId("title-syn").textContent = meta.synopsis || "";
    byId("map-brand").textContent = text;
    byId("play-brand").textContent = text;
    // Cover: the entry segment's first frame, painted by the <video>
    // element itself (no pre-rendered thumbnail exists in the bundle).
    if (segments[entryId]) {
      cover.src = segments[entryId];
      cover.onerror = () => { cover.style.display = "none"; };
    }
  }
  function refreshTitle() {
    const resume = byId("btn-resume");
    const reset = byId("btn-reset");
    if (state.last && hasProgress()) {
      resume.classList.remove("is-hidden");
      byId("resume-label").textContent = nodeTitle(state.last);
    } else {
      resume.classList.add("is-hidden");
    }
    reset.classList.toggle("is-hidden", !hasProgress());
    byId("endings-badge").textContent =
      "已解锁 " + state.endings.length + "/" + endingIds.length;
  }
  byId("btn-start").onclick = () => playSegment(entryId);
  byId("btn-resume").onclick = () => {
    if (state.last) {
      toast("继续上次 ·「" + nodeTitle(state.last) + "」");
      playSegment(state.last);
    }
  };
  byId("btn-map").onclick = () => go("map");
  byId("btn-endings").onclick = () => {
    toast("结局图鉴 · 已解锁 " + state.endings.length + "/" +
      endingIds.length + "，未达成的结局以 ? 隐藏");
    go("map");
  };
  byId("btn-reset").onclick = () => {
    const ok = window.confirm(
      "重新开始将清除本机的观看进度与已解锁结局，确定吗？");
    if (!ok) { return; }
    try { localStorage.removeItem(storageKey); } catch (error) { /* noop */ }
    state = freshState();
    refreshTitle();
    toast("进度已清除 · 从头开始探索吧");
  };

  // ---------- story map (progressive reveal / fog of war) ----------
  // BFS depth from the entry over the FULL graph gives each node a stable
  // relative order; on-screen positions are computed per render over the
  // REVEALED subgraph only (no reserved empty slots, no x-scale leaking
  // depth beyond the frontier).
  const depths = {};
  (function computeDepths() {
    depths[entryId] = 0;
    let frontier = [entryId];
    while (frontier.length) {
      const next = [];
      frontier.forEach((id) => {
        ((nodes[id] || {}).children || []).forEach((kid) => {
          if (nodes[kid] && depths[kid] === undefined) {
            depths[kid] = depths[id] + 1;
            next.push(kid);
          }
        });
      });
      frontier = next;
    }
    allIds.forEach((id) => {
      if (depths[id] === undefined) { depths[id] = 0; }
    });
  })();
  let positions = {};
  function layoutPositions(reveal) {
    const shown = allIds.filter((id) => reveal[id]);
    const maxDepth = shown.reduce(
      (acc, id) => Math.max(acc, depths[id]), 0);
    const columns = {};
    shown.forEach((id) => {
      (columns[depths[id]] = columns[depths[id]] || []).push(id);
    });
    const out = {};
    shown.forEach((id) => {
      const column = columns[depths[id]];
      const index = column.indexOf(id);
      out[id] = {
        x: maxDepth ? 12 + depths[id] * (76 / maxDepth) : 50,
        y: 14 + (index + 0.5) * (72 / column.length),
      };
    });
    return out;
  }

  // Fog-of-war rule: render ONLY visited nodes (entry always counts as
  // discovered) plus the direct children of visited nodes as "?"
  // silhouettes. Deeper nodes are not rendered at all.
  function revealMap() {
    const seen = {};
    state.visited.forEach((id) => { if (nodes[id]) { seen[id] = true; } });
    seen[entryId] = true;
    const reveal = {};
    Object.keys(seen).forEach((id) => { reveal[id] = "visited"; });
    Object.keys(seen).forEach((id) => {
      (nodes[id].children || []).forEach((kid) => {
        if (nodes[kid] && !reveal[kid]) { reveal[kid] = "locked"; }
      });
    });
    return reveal;
  }

  const mapStage = byId("mapStage");
  const mapSvg = byId("mapSvg");
  const mapTip = byId("mapTip");
  function edgePath(from, to) {
    const x1 = positions[from].x * 10;
    const y1 = positions[from].y * 5.6;
    const x2 = positions[to].x * 10;
    const y2 = positions[to].y * 5.6;
    const dx = (x2 - x1) * 0.45;
    return "M" + x1 + "," + y1 + " C" + (x1 + dx) + "," + y1 + " " +
      (x2 - dx) + "," + y2 + " " + x2 + "," + y2;
  }
  function showTip(node, title, body, act, locked) {
    mapTip.querySelector("h4").textContent = title;
    mapTip.querySelector("p").textContent = body;
    const actEl = mapTip.querySelector(".tip-act");
    actEl.textContent = act;
    actEl.className = "tip-act" + (locked ? " is-lock" : "");
    mapTip.style.left = node.x + "%";
    mapTip.style.top = "calc(" + node.y + "% - 42px)";
    mapTip.classList.add("is-on");
  }
  function hideTip() { mapTip.classList.remove("is-on"); }

  function renderMap() {
    const reveal = revealMap();
    positions = layoutPositions(reveal);
    mapStage.querySelectorAll(".node").forEach((el) => el.remove());
    mapSvg.innerHTML = "";
    // Edges first: only from visited sources to rendered targets.
    Object.keys(reveal).forEach((id) => {
      if (reveal[id] !== "visited") { return; }
      (nodes[id].children || []).forEach((kid) => {
        if (!reveal[kid]) { return; }
        const path = document.createElementNS(
          "http://www.w3.org/2000/svg", "path");
        let cls = "edge edge--locked";
        if (reveal[kid] === "visited") {
          cls = kid === state.last && state.visited.indexOf(kid) >= 0
            ? "edge edge--live" : "edge edge--seen";
        }
        path.setAttribute("class", cls);
        path.setAttribute("d", edgePath(id, kid));
        mapSvg.appendChild(path);
      });
    });
    // Nodes. Uniform fog: EVERY unvisited frontier node renders as the
    // same anonymous "?" circle — identical shape / color / tag / name —
    // whether or not it is an ending. Ending identity (★, red pill,
    // title) appears only after the node was actually reached.
    allIds.forEach((id) => {
      if (!reveal[id]) { return; }
      const info = nodes[id];
      const point = positions[id];
      const visited = reveal[id] === "visited";
      const isCurrent = visited && id === state.last;
      const el = document.createElement("button");
      el.className = "node " + (isCurrent ? "node--current" :
        (visited ? "node--seen" : "node--locked"));
      el.style.left = point.x + "%";
      el.style.top = point.y + "%";
      let tag = "?";
      let icon = "?";
      let name = "？？？";
      if (visited) {
        if (info.is_ending) { el.classList.add("node--end"); }
        tag = id === entryId ? "START" :
          (info.is_ending ? "ENDING" : "NODE " + (depths[id] + 1));
        icon = info.is_ending ? "★" : (id === entryId ? "⛯" : "●");
        if (isCurrent) { icon = "▶"; }
        name = info.title;
      }
      const tagEl = document.createElement("span");
      tagEl.className = "n-tag";
      tagEl.textContent = tag;
      const icoEl = document.createElement("span");
      icoEl.className = "n-ico";
      icoEl.textContent = icon;
      const nameEl = document.createElement("span");
      nameEl.className = "n-name";
      nameEl.textContent = name;
      el.appendChild(tagEl);
      el.appendChild(icoEl);
      el.appendChild(nameEl);
      el.setAttribute("aria-label",
        visited ? info.title : "未探索的剧情点");
      el.addEventListener("mouseenter", () => {
        if (visited) {
          showTip(point, info.title,
            info.synopsis || "（暂无本节点提要）",
            isCurrent ? "▶ 回到播放" : "▶ 从此处重看", false);
        } else {
          // One shared tooltip for every fogged node: it must not hint
          // whether this branch is an ending.
          showTip(point, "未探索的剧情点",
            "继续做出不同的选择即可解锁。", "🔒 尚未解锁", true);
        }
      });
      el.addEventListener("mouseleave", hideTip);
      el.addEventListener("blur", hideTip);
      el.addEventListener("click", () => {
        if (!visited) {
          toast("该剧情点尚未解锁 · 试试其他分支");
          return;
        }
        toast(info.is_ending
          ? "重看结局 ·「" + info.title + "」"
          : "从「" + info.title + "」重看剧情");
        playSegment(id);
      });
      mapStage.appendChild(el);
    });
    // Sidebar: coverage over revealed story points vs the full graph.
    const visitedCount =
      state.visited.filter((id) => nodes[id]).length;
    const percent = allIds.length
      ? Math.round((visitedCount / allIds.length) * 100) : 0;
    byId("cov-num").textContent = String(percent);
    byId("cov-fill").style.width = percent + "%";
    byId("cov-nodes").textContent =
      "剧情点 " + visitedCount + "/" + allIds.length;
    byId("cov-endings").textContent =
      "结局 " + state.endings.length + "/" + endingIds.length;
    byId("endings-title").textContent =
      "结局图鉴 " + state.endings.length + "/" + endingIds.length;
    const slots = byId("ending-slots");
    slots.innerHTML = "";
    endingIds.forEach((id) => {
      const got = state.endings.indexOf(id) >= 0;
      const slot = document.createElement("span");
      slot.className = "end-slot" + (got ? " is-got" : "");
      slot.textContent = got ? "★" : "?";
      slot.title = got ? nodeTitle(id) : "未解锁";
      slots.appendChild(slot);
    });
  }
  byId("btn-map-continue").onclick = () => {
    playSegment(state.last || entryId);
  };
  byId("btn-map-home").onclick = () => go("title");

  // ---------- playback ----------
  const video = byId("video");
  const choiceLayer = byId("choiceLayer");
  const questionEl = byId("question");
  const optionsEl = byId("options");
  const cdRing = byId("cdRing");
  const cdProg = byId("cdProg");
  const cdNum = byId("cdNum");
  const CIRC = 2 * Math.PI * 25;
  cdProg.style.strokeDasharray = CIRC;
  let timer = null;
  let currentNodeId = null;

  function interactionsFor(timelineId) {
    return interactions.filter(
      (item) => item.source_timeline_id === timelineId,
    );
  }
  function clearTimer() {
    if (timer) { clearInterval(timer); timer = null; }
    cdRing.classList.remove("is-shown", "is-urgent");
  }
  function overlayOff() {
    choiceLayer.classList.remove("is-on");
  }
  function segmentTitle(timelineId) { return nodeTitle(timelineId); }

  function showMissingSegment(timelineId) {
    clearTimer();
    overlayOff();
    showGate(
      "素材未就绪",
      "分支「" + segmentTitle(timelineId) + "」的视频缺失或无法加载，" +
        "请回到创作工具重新渲染后再导出互动包。",
      [
        { label: "从头重新播放", onClick: () => playSegment(entryId) },
        { label: "返回主页", dim: true, onClick: () => go("title") },
      ],
    );
  }

  function updateHud(timelineId) {
    const index = allIds.indexOf(timelineId);
    const pad = (value) => (value < 10 ? "0" : "") + value;
    byId("hud-cam").textContent =
      "CAM-" + pad(index + 1) + " · " + segmentTitle(timelineId);
    byId("hud-chapter").textContent =
      "NODE " + (index + 1) + "/" + allIds.length;
    byId("play-crumb").textContent = "/ " + segmentTitle(timelineId);
  }
  video.addEventListener("timeupdate", () => {
    const t = video.currentTime || 0;
    const pad = (value) => (value < 10 ? "0" : "") + value;
    byId("timecode").textContent = "00:" +
      pad(Math.floor(t / 60)) + ":" + pad(Math.floor(t % 60)) + ":" +
      pad(Math.floor((t % 1) * 25));
  });

  function segmentEnded(timelineId) {
    const points = interactionsFor(timelineId);
    if (points.length) {
      showChoice(points[points.length - 1]);
      return;
    }
    const info = nodes[timelineId] || { children: [] };
    if (!info.children || !info.children.length) {
      // A leaf node = one ending reached.
      unlockEnding(timelineId);
      showGate(
        "结局达成 ·「" + segmentTitle(timelineId) + "」",
        (info.synopsis || "这条支线走到了尽头。") +
          "换一个选择，也许会看到另一个夜晚。",
        [
          { label: "查看剧情地图", onClick: () => go("map") },
          { label: "从头再看", onClick: () => playSegment(entryId) },
          { label: "返回主页", dim: true, onClick: () => go("title") },
        ],
        true,
      );
      return;
    }
    // No choice point but the story continues (linear chain / default
    // edge): advance automatically.
    playSegment(info.children[0]);
  }

  function playSegment(timelineId) {
    clearTimer();
    overlayOff();
    hideGate();
    const src = segments[timelineId];
    if (!src) {
      go("play");
      showMissingSegment(timelineId);
      return;
    }
    currentNodeId = timelineId;
    markVisited(timelineId);
    go("play");
    updateHud(timelineId);
    video.onerror = () => showMissingSegment(timelineId);
    video.src = src;
    video.onended = () => {
      // Ignore an end event racing a user navigation away from the
      // play screen (it would pop a gate over the map / title).
      if (current !== "play") { return; }
      segmentEnded(timelineId);
    };
    video.play().catch(() => {
      // Autoplay without a user gesture is blocked (NotAllowedError);
      // surface a tap-to-play gate instead of a dead page.
      showGate(
        segmentTitle(timelineId),
        "浏览器阻止了自动播放，点击下方按钮开始。",
        [{
          label: "▶ 点击播放",
          onClick: () => { video.play().catch(() => {}); },
        }],
      );
    });
  }

  // ---------- choice-card faces (first frame of the target segment) ----
  // Captured at runtime through an offscreen <video> + <canvas>. Under
  // file:// the canvas is usually tainted (file media count as
  // cross-origin), so toDataURL() may throw SecurityError: we then keep
  // the drawn canvas itself as the face, and fall back to the stylized
  // accent-gradient face when even loading/drawing fails. Unvisited
  // targets stay fogged by CSS (blur + darkening) so the captured frame
  // leaks no plot or visuals before the branch is reached.
  const faceCache = {};
  function captureFrame(target, done) {
    if (Object.prototype.hasOwnProperty.call(faceCache, target)) {
      done(faceCache[target]);
      return;
    }
    const src = segments[target];
    if (!src) { faceCache[target] = null; done(null); return; }
    const probe = document.createElement("video");
    probe.muted = true;
    probe.playsInline = true;
    probe.preload = "auto";
    let settled = false;
    const finish = (frame) => {
      if (settled) { return; }
      settled = true;
      faceCache[target] = frame;
      probe.removeAttribute("src");
      try { probe.load(); } catch (error) { /* release best-effort */ }
      done(frame);
    };
    probe.onerror = () => finish(null);
    probe.onloadeddata = () => {
      const canvas = document.createElement("canvas");
      canvas.width = probe.videoWidth || 320;
      canvas.height = probe.videoHeight || 180;
      try {
        canvas.getContext("2d").drawImage(
          probe, 0, 0, canvas.width, canvas.height);
      } catch (error) { finish(null); return; }
      let url = null;
      try {
        url = canvas.toDataURL("image/jpeg", 0.72);
      } catch (error) { url = null; /* tainted: keep the canvas */ }
      finish({ url: url, canvas: canvas });
    };
    probe.src = src;
  }
  function faceInto(faceEl, target) {
    captureFrame(target, (frame) => {
      if (!frame || !faceEl.isConnected) { return; }
      let shot;
      if (frame.url) {
        shot = document.createElement("img");
        shot.src = frame.url;
        shot.alt = "";
      } else {
        // Copy the cached (possibly tainted) master canvas so the same
        // frame can back several cards; taint only blocks readback,
        // never display.
        shot = document.createElement("canvas");
        shot.width = frame.canvas.width;
        shot.height = frame.canvas.height;
        shot.getContext("2d").drawImage(frame.canvas, 0, 0);
      }
      shot.className = "f-shot";
      faceEl.insertBefore(shot, faceEl.firstChild);
    });
  }

  // Optional manifest `tone` (per option, else per edge): subtle card
  // treatment. Unknown / absent tones = neutral card, fully backward
  // compatible with manifests that never carry the field.
  const TONE_LABELS = { risky: "△ 冒险", danger: "▲ 危险", safe: "○ 稳妥" };

  function showChoice(point) {
    questionEl.textContent = point.question || "此刻，你决定——";
    optionsEl.innerHTML = "";
    point.options.forEach((option, index) => {
      const edge = edgeIndex[option.edge_ref] || {};
      const target = edge.target_timeline_id;
      const isDefault = point.default_edge_ref &&
        option.edge_ref === point.default_edge_ref;
      const visitedTarget =
        target && state.visited.indexOf(target) >= 0;
      const card = document.createElement("button");
      card.className = "choice-card" + (isDefault ? " is-default" : "");
      const tone = String(option.tone || edge.tone || "").toLowerCase();
      if (TONE_LABELS[tone]) { card.setAttribute("data-tone", tone); }
      // Face: fog-of-war applies to the thumbnail too — an unvisited
      // target is blurred into an unrecognizable silhouette.
      const face = document.createElement("span");
      face.className = "c-face" + (visitedTarget ? "" : " is-fogged");
      if (!visitedTarget) {
        const mark = document.createElement("i");
        mark.className = "f-mark";
        mark.textContent = "?";
        face.appendChild(mark);
      }
      const key = document.createElement("span");
      key.className = "c-key";
      key.textContent = String.fromCharCode(65 + index) +
        (isDefault ? " · 默认" : "");
      const name = document.createElement("span");
      name.className = "c-name";
      name.textContent = edge.label || option.edge_ref;
      const desc = document.createElement("p");
      desc.className = "c-desc";
      desc.textContent = visitedTarget
        ? ((nodes[target] || {}).synopsis || nodeTitle(target))
        : "未知走向 · 由你的选择揭晓。";
      const risk = document.createElement("span");
      risk.className = "c-risk" + (visitedTarget ? "" : " is-hi");
      const bar = document.createElement("i");
      risk.appendChild(bar);
      risk.appendChild(document.createTextNode(
        visitedTarget ? "已探索 ·「" + nodeTitle(target) + "」"
          : "未探索分支"));
      if (TONE_LABELS[tone]) {
        const toneEl = document.createElement("i");
        toneEl.className = "c-tone";
        toneEl.textContent = TONE_LABELS[tone];
        risk.appendChild(toneEl);
      }
      card.appendChild(face);
      card.appendChild(key);
      card.appendChild(name);
      card.appendChild(desc);
      card.appendChild(risk);
      card.onclick = () => pickCard(card, target, false);
      optionsEl.appendChild(card);
      // After DOM attachment: cached captures call back synchronously
      // and must pass the isConnected guard.
      if (target) { faceInto(face, target); }
    });
    choiceLayer.classList.add("is-on");
    if (point.countdown_seconds && point.default_edge_ref) {
      let remaining = Math.ceil(point.countdown_seconds);
      const total = remaining;
      cdRing.classList.add("is-shown");
      cdRing.classList.remove("is-urgent");
      cdNum.textContent = String(remaining);
      cdProg.style.strokeDashoffset = 0;
      timer = setInterval(() => {
        remaining -= 1;
        cdNum.textContent = String(Math.max(remaining, 0));
        cdProg.style.strokeDashoffset =
          CIRC * (1 - Math.max(remaining, 0) / total);
        cdRing.classList.toggle("is-urgent", remaining <= 3);
        if (remaining <= 0) {
          const edge = edgeIndex[point.default_edge_ref] || {};
          const fallback = optionsEl.querySelector(".is-default");
          pickCard(fallback, edge.target_timeline_id, true);
        }
      }, 1000);
    }
  }

  function pickCard(card, target, isAuto) {
    clearTimer();
    if (card) {
      Array.prototype.forEach.call(optionsEl.children, (child) => {
        child.classList.add(child === card ? "is-picked" : "is-faded");
      });
      if (isAuto) { toast("倒计时结束 · 已自动选择默认走向"); }
    }
    setTimeout(() => playSegment(target), reduced ? 0 : 650);
  }

  byId("btn-play-map").onclick = () => go("map");
  byId("btn-play-home").onclick = () => go("title");
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (!gate.classList.contains("hidden")) { return; }
      if (current === "play") { go("map"); }
      else if (current === "map") { go("title"); }
    }
  });

  // Browsers (Chrome/Safari, including file://) refuse unmuted play()
  // before the first user gesture, so the story always starts behind the
  // title menu ("开始故事" / "继续上次") — every playback is click-driven.
  applyMeta();
  refreshTitle();
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
