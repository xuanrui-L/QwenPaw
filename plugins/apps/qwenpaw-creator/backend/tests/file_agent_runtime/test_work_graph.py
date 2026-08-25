# -*- coding: utf-8 -*-
"""Work graph derivation: the production DAG projected from durable facts.

Every status is recomputed from project.json plus task records — the
graph is a view, never a second authority. These tests pin the node
identities, dependency edges and all seven states.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from domain.enums import TaskStatus
from services.file_agent_runtime.work_graph import (
    WorkNodeStatus,
    derive_work_graph,
)
from services.project_files.models import (
    ArtifactSlot,
    ArtifactVersion,
    ElementLocation,
    IndexedFile,
    Project,
    R2VCreation,
    Shot,
    TimelineElement,
    TimelineSpan,
    VisualCastLineup,
    VisualEntity,
    VisualVariant,
)


pytestmark = pytest.mark.unit


def _entity(entity_id: str, variants: dict[str, str | None]) -> VisualEntity:
    return VisualEntity(
        entity_id=entity_id,
        kind="character",
        name=entity_id.removeprefix("char:"),
        required_variant_ids=list(variants),
        variants={
            "items": {
                variant_id: VisualVariant(
                    variant_id=variant_id,
                    selected_artifact_version_id=selected,
                )
                for variant_id, selected in variants.items()
            },
            "order": list(variants),
        },
    )


def _element(element_id: str, **creation_kwargs) -> TimelineElement:
    shot = Shot(
        shot_id=f"{element_id}-shot",
        description="镜头",
        camera="⊙ 静止",
        framing="全景",
        duration_seconds=4,
    )
    defaults = {
        "narrative": "叙事",
        "storyboard_prompt": "分镜 prompt",
        "video_prompt": "视频 prompt",
        "shots": {"items": {shot.shot_id: shot}, "order": [shot.shot_id]},
    }
    defaults.update(creation_kwargs)
    return TimelineElement(
        element_id=element_id,
        label=element_id,
        span=TimelineSpan(start_tick=0, duration_tick=4_000),
        location=ElementLocation(),
        creation=R2VCreation(**defaults),
    )


def _project() -> Project:
    return Project.new(project_id="p-graph", name="Graph")


def _add_element(project: Project, element: TimelineElement) -> None:
    project.timelines.items["timeline:main"].elements_by_id[
        element.element_id
    ] = element


def _select_slot(
    project: Project,
    *,
    slot_id: str,
    kind: str,
    owner_ref: str,
    version_id: str,
    provenance: list[str] | None = None,
) -> None:
    project.assets.artifact_slots_by_id[slot_id] = ArtifactSlot(
        slot_id=slot_id,
        kind=kind,
        owner_ref=owner_ref,
        version_ids=[version_id],
        selected_version_id=version_id,
    )
    project.assets.files_by_id[f"file-{version_id}"] = IndexedFile(
        file_id=f"file-{version_id}",
        kind="artifact_payload",
        media_type="image/png",
        relative_uri=f"assets/artifacts/file-{version_id}.png",
        sha256="0" * 64,
        size_bytes=1,
        created_at="2026-08-05T00:00:00Z",
    )
    project.assets.artifact_versions_by_id[version_id] = ArtifactVersion(
        version_id=version_id,
        name=version_id,
        slot_id=slot_id,
        kind=kind,
        owner_ref=owner_ref,
        file_id=f"file-{version_id}",
        checksum="0" * 64,
        input_fingerprint="sha256:" + "1" * 64,
        based_on_generation=1,
        provenance_refs=provenance or [],
        created_at="2026-08-05T00:00:00Z",
    )


def _task(kind: str, target: str, status: TaskStatus, **extra):
    return SimpleNamespace(
        task_id=f"task-{kind}-{target}",
        kind=kind,
        status=status,
        input_refs=[target],
        metadata={"targetRef": target, **extra.pop("metadata", {})},
        progress=extra.pop("progress", None),
        error=extra.pop("error", None),
        updated_at=extra.pop("updated_at", "2026-08-05T00:00:00Z"),
        idempotency_key=extra.pop("idempotency_key", None),
    )


def test_variant_nodes_cover_ready_running_failed_done() -> None:
    project = _project()
    project.visual.entities.items["char:a"] = _entity(
        "char:a",
        {"var:x": "art:x", "var:y": None},
    )
    project.visual.entities.order.append("char:a")
    project.visual.entities.items["char:b"] = _entity(
        "char:b",
        {"var:z": None},
    )
    project.visual.entities.order.append("char:b")

    graph = derive_work_graph(
        project,
        tasks=[
            _task(
                "image_generation",
                "asset:char:b",
                TaskStatus.RUNNING,
                metadata={"variantId": "var:z"},
                progress=0.4,
            ),
        ],
    )
    by_id = graph.by_id
    assert by_id["visual:char:a:var:x"].status is WorkNodeStatus.DONE
    assert by_id["visual:char:a:var:y"].status is WorkNodeStatus.READY
    running = by_id["visual:char:b:var:z"]
    assert running.status is WorkNodeStatus.RUNNING
    assert running.progress == 0.4


def test_prompt_rewrite_reopens_a_deterministically_failed_node() -> None:
    """FAILED parks a node only while its inputs are unchanged.

    Field run 2026-08-11: three safety-rejected character anchors kept
    their nodes FAILED after the agent rewrote the prompts, so the
    scheduler never retried and the project stalled. The dag idempotency
    key carries the dispatch fingerprint: an input change re-derives
    READY, while same-input failures (including transient retry slots)
    and agent-dispatched failures without a dag key stay parked.
    """

    project = _project()
    project.visual.entities.items["char:a"] = _entity(
        "char:a",
        {"var:x": None},
    )
    project.visual.entities.order.append("char:a")
    node_id = "visual:char:a:var:x"
    current = derive_work_graph(project).by_id[node_id].dispatch_fingerprint

    def status_with_failed_key(key: str | None) -> WorkNodeStatus:
        graph = derive_work_graph(
            project,
            tasks=[
                _task(
                    "image_generation",
                    "asset:char:a",
                    TaskStatus.FAILED,
                    metadata={"variantId": "var:x"},
                    error={"message": "safety rejected"},
                    idempotency_key=key,
                ),
            ],
        )
        return graph.by_id[node_id].status

    # The prompt (and thus the fingerprint) moved on: reopen.
    assert (
        status_with_failed_key(f"dag-{node_id}-stale-fingerprint")
        is WorkNodeStatus.READY
    )
    # Same inputs: stay parked.
    assert (
        status_with_failed_key(f"dag-{node_id}-{current}")
        is WorkNodeStatus.FAILED
    )
    # Agent-dispatched failures carry no graph identity: stay parked.
    assert status_with_failed_key(None) is WorkNodeStatus.FAILED


def test_lineup_gates_until_members_have_artwork() -> None:
    project = _project()
    project.visual.entities.items["char:a"] = _entity(
        "char:a",
        {"var:x": "art:a"},
    )
    project.visual.entities.order.append("char:a")
    project.visual.entities.items["char:b"] = _entity(
        "char:b",
        {"var:y": None},
    )
    project.visual.entities.order.append("char:b")
    project.visual.cast_lineups.items["lineup:main"] = VisualCastLineup(
        lineup_id="lineup:main",
        name="主阵容",
        character_refs=["char:a", "char:b"],
    )
    project.visual.cast_lineups.order.append("lineup:main")

    graph = derive_work_graph(project)
    node = graph.by_id["lineup:lineup:main"]
    assert node.status is WorkNodeStatus.GATED
    assert node.missing == ("visual:char:b:var:y",)

    # Give char:b artwork: the lineup becomes READY (dispatchable).
    project.visual.entities.items["char:b"].variants.items[
        "var:y"
    ].selected_artifact_version_id = "art:b"
    graph = derive_work_graph(project)
    node = graph.by_id["lineup:lineup:main"]
    assert node.status is WorkNodeStatus.READY
    assert node.command == "GENERATE_CAST_LINEUP_IMAGE"


def test_element_lane_storyboard_then_video() -> None:
    project = _project()
    project.visual.entities.items["char:a"] = _entity(
        "char:a",
        {"var:x": "art:a"},
    )
    project.visual.entities.order.append("char:a")
    _add_element(
        project,
        _element(
            "elem:one",
            character_refs=["char:a"],
            visual_variant_refs={"char:a": "var:x"},
        ),
    )

    graph = derive_work_graph(project)
    storyboard = graph.by_id["storyboard:elem:one"]
    video = graph.by_id["video:elem:one"]
    assert storyboard.status is WorkNodeStatus.READY
    assert video.status is WorkNodeStatus.GATED
    assert video.missing == ("storyboard:elem:one",)
    assert graph.by_id["compose:final"].status is WorkNodeStatus.GATED

    # Storyboard lands: video becomes READY.
    _select_slot(
        project,
        slot_id="element:elem:one:storyboard",
        kind="r2v_storyboard_image",
        owner_ref="element:elem:one",
        version_id="art:sb",
    )
    graph = derive_work_graph(project)
    assert graph.by_id["storyboard:elem:one"].status is WorkNodeStatus.DONE
    assert graph.by_id["video:elem:one"].status is WorkNodeStatus.READY

    # Video lands: compose becomes READY.
    _select_slot(
        project,
        slot_id="element:elem:one:main",
        kind="element_video",
        owner_ref="element:elem:one",
        version_id="art:vid",
    )
    graph = derive_work_graph(project)
    assert graph.by_id["video:elem:one"].status is WorkNodeStatus.DONE
    assert graph.by_id["compose:final"].status is WorkNodeStatus.READY


def test_missing_storyboard_prompt_is_a_model_required_gap() -> None:
    project = _project()
    _add_element(project, _element("elem:one", storyboard_prompt=""))

    graph = derive_work_graph(project)
    storyboard = graph.by_id["storyboard:elem:one"]
    assert storyboard.status is WorkNodeStatus.GATED
    assert storyboard.missing == ("storyboard_prompt 缺失",)
    # The scheduler cannot solve this: it needs a model turn.
    assert storyboard in graph.model_required_nodes()


def _element_with_landed_storyboard(
    project: Project,
    **creation_kwargs,
) -> None:
    _add_element(project, _element("elem:one", **creation_kwargs))
    _select_slot(
        project,
        slot_id="element:elem:one:storyboard",
        kind="r2v_storyboard_image",
        owner_ref="element:elem:one",
        version_id="art:sb",
    )


def test_video_gates_until_prompt_quotes_planned_dialogue() -> None:
    """Field run 2026-08-12 (f5ac): planned dialogue never reached veo3.

    The mainline wrote per-shot dialogue, committed a mood summary as
    video_prompt, and the scheduler dispatched the summary verbatim — the
    finished film was silent. The graph now refuses to dispatch a video
    whose prompt drops any planned line.
    """
    project = _project()
    spoken = Shot(
        shot_id="shot:reunion-2",
        description="重逢对话",
        camera="⊙ 静止",
        framing="近景",
        dialogue="林薇，这么多年了，有句话我一直想对你说。",
        duration_seconds=3,
    )
    silent = Shot(
        shot_id="shot:reunion-1",
        description="环境建立",
        camera="↑ 推近",
        framing="全景",
        duration_seconds=2,
    )
    _element_with_landed_storyboard(
        project,
        shots={
            "items": {s.shot_id: s for s in (silent, spoken)},
            "order": [silent.shot_id, spoken.shot_id],
        },
        video_prompt="Emotional reunion, intimate conversation.",
    )

    graph = derive_work_graph(project)
    video = graph.by_id["video:elem:one"]
    assert video.status is WorkNodeStatus.GATED
    assert video.missing == ("video_prompt 缺台词原文：shot:reunion-2",)
    assert video in graph.model_required_nodes()

    # Quoting the line verbatim releases the gate; line wrapping inside
    # the prompt must not re-trigger it.
    element = project.timelines.items["timeline:main"].elements_by_id[
        "elem:one"
    ]
    element.creation.video_prompt = (
        "镜头二：他凝视她，轻声说：“林薇，这么多年了，\n" + "有句话我一直想对你说。”语气哽咽而坚定。"
    )
    graph = derive_work_graph(project)
    assert graph.by_id["video:elem:one"].status is WorkNodeStatus.READY


def test_declared_pending_lineup_gates_every_storyboard() -> None:
    """Field run 2026-08-12 (27dc): a single-character closing scene
    derived READY while another element's declared lineup was pending;
    the executor's project-wide gate rejected the dispatch and the node
    stalled READY-but-undispatchable until a restart."""

    project = _project()
    for ref in ("char:a", "char:b"):
        project.visual.entities.items[ref] = _entity(
            ref,
            {f"var:{ref[-1]}": f"art:{ref[-1]}"},
        )
        project.visual.entities.order.append(ref)
    project.visual.cast_lineups.items["lineup:duo"] = VisualCastLineup(
        lineup_id="lineup:duo",
        name="双人阵容",
        character_refs=["char:a", "char:b"],
    )
    project.visual.cast_lineups.order.append("lineup:duo")
    _add_element(
        project,
        _element(
            "elem:pair",
            character_refs=["char:a", "char:b"],
            visual_variant_refs={"char:a": "var:a", "char:b": "var:b"},
            cast_lineup_refs=["lineup:duo"],
        ),
    )
    # The closing scene has one character and never references the lineup.
    _add_element(
        project,
        _element(
            "elem:solo",
            character_refs=["char:a"],
            visual_variant_refs={"char:a": "var:a"},
        ),
    )

    graph = derive_work_graph(project)
    solo = graph.by_id["storyboard:elem:solo"]
    assert solo.status is WorkNodeStatus.GATED
    assert "lineup:lineup:duo" in solo.missing

    # The lineup lands: every storyboard unblocks together.
    project.visual.cast_lineups.items[
        "lineup:duo"
    ].selected_artifact_version_id = "art:lineup"
    graph = derive_work_graph(project)
    assert graph.by_id["storyboard:elem:solo"].status is WorkNodeStatus.READY
    assert graph.by_id["storyboard:elem:pair"].status is WorkNodeStatus.READY


def test_stale_marks_but_does_not_regenerate() -> None:
    project = _project()
    project.visual.entities.items["char:a"] = _entity(
        "char:a",
        {"var:x": "art:new"},
    )
    project.visual.entities.order.append("char:a")
    _add_element(
        project,
        _element(
            "elem:one",
            character_refs=["char:a"],
            visual_variant_refs={"char:a": "var:x"},
        ),
    )
    # Storyboard was drawn from art:old; the variant now selects art:new.
    _select_slot(
        project,
        slot_id="element:elem:one:storyboard",
        kind="r2v_storyboard_image",
        owner_ref="element:elem:one",
        version_id="art:sb",
        provenance=["artifact-version:art:old"],
    )

    graph = derive_work_graph(project)
    node = graph.by_id["storyboard:elem:one"]
    assert node.status is WorkNodeStatus.STALE
    # STALE is terminal for the scheduler: not READY, not dispatched.
    assert node not in graph.ready_media_nodes()


def test_storyboard_waits_for_all_referenced_entities_not_just_bindings() -> (
    None
):
    """Field run 2026-08-06: the graph said READY, the execution gate said
    no — shots referenced scene entities that had no artwork yet. The
    graph's dependency set must mirror visual_design_readiness."""
    project = _project()
    project.visual.entities.items["char:a"] = _entity(
        "char:a",
        {"var:x": "art:a"},
    )
    project.visual.entities.order.append("char:a")
    # Scene without required variants and without artwork: entity-level
    # readiness, machine-solvable through its single variant.
    scene = _entity("scene:home", {"var:day": None})
    project.visual.entities.items["scene:home"] = scene
    project.visual.entities.order.append("scene:home")
    _add_element(
        project,
        _element(
            "elem:one",
            character_refs=["char:a"],
            scene_ref="scene:home",
            visual_variant_refs={"char:a": "var:x"},
        ),
    )

    graph = derive_work_graph(project)
    storyboard = graph.by_id["storyboard:elem:one"]
    assert storyboard.status is WorkNodeStatus.GATED
    assert "visual:scene:home:var:day" in storyboard.missing
    # The gap is a media node: the scheduler can solve it, no model turn.
    assert storyboard not in graph.model_required_nodes()

    # Scene artwork lands (single-variant entities auto-select at entity
    # level on write-back, mirrored here): the storyboard opens up.
    scene.variants.items["var:day"].selected_artifact_version_id = "art:s"
    scene.selected_artifact_version_id = "art:s"
    graph = derive_work_graph(project)
    assert graph.by_id["storyboard:elem:one"].status is WorkNodeStatus.READY


def test_stale_final_render_reopens_compose() -> None:
    # Render review revises an overlay after the master render: edit
    # impact marks the final_video version stale. The compose node must
    # drop back to READY so the unattended loop re-renders the corrected
    # cut instead of reporting DONE one compose short.
    project = _project()
    _add_element(project, _element("elem:one"))
    _select_slot(
        project,
        slot_id="element:elem:one:storyboard",
        kind="r2v_storyboard_image",
        owner_ref="element:elem:one",
        version_id="art:sb",
    )
    _select_slot(
        project,
        slot_id="element:elem:one:main",
        kind="element_video",
        owner_ref="element:elem:one",
        version_id="art:vid",
    )
    _select_slot(
        project,
        slot_id="timeline:timeline:main:render",
        kind="final_video",
        owner_ref="timeline:timeline:main",
        version_id="art:final",
    )

    graph = derive_work_graph(project)
    assert graph.by_id["compose:final"].status is WorkNodeStatus.DONE

    project.assets.artifact_versions_by_id["art:final"].stale = True
    graph = derive_work_graph(project)
    compose = graph.by_id["compose:final"]
    assert compose.status is WorkNodeStatus.READY
    assert compose in graph.ready_media_nodes()


def test_superseded_render_source_reopens_compose_without_stale_flag() -> None:
    """Frozen selections are authoritative even if impact missed stale."""

    project = _project()
    _add_element(project, _element("elem:one"))
    _select_slot(
        project,
        slot_id="element:elem:one:storyboard",
        kind="r2v_storyboard_image",
        owner_ref="element:elem:one",
        version_id="art:sb",
    )
    _select_slot(
        project,
        slot_id="element:elem:one:main",
        kind="element_video",
        owner_ref="element:elem:one",
        version_id="art:vid-old",
    )
    _select_slot(
        project,
        slot_id="timeline:timeline:main:render",
        kind="final_video",
        owner_ref="timeline:timeline:main",
        version_id="art:final",
    )
    project.assets.artifact_versions_by_id["art:final"].metadata = {
        "sourceSelections": [
            {
                "sourceRef": "element:elem:one",
                "versionId": "art:vid-old",
            },
        ],
    }
    assert (
        derive_work_graph(project).by_id["compose:final"].status
        is WorkNodeStatus.DONE
    )

    _select_slot(
        project,
        slot_id="element:elem:one:main",
        kind="element_video",
        owner_ref="element:elem:one",
        version_id="art:vid-new",
    )
    # The lifecycle bug observed in the real run left this false.
    assert not project.assets.artifact_versions_by_id["art:final"].stale
    graph = derive_work_graph(project)
    compose = graph.by_id["compose:final"]
    assert compose.status is WorkNodeStatus.READY
    assert compose in graph.ready_media_nodes()


# ---- Blueprint script lane（方案 3.2/3.3）--------------------------------


def _add_second_timeline(project: Project) -> None:
    from services.project_files.models import Timeline

    project.timelines.items["timeline:ep2"] = Timeline(
        timeline_id="timeline:ep2",
        title="第二集 · 旧宅疑云",
        synopsis="林晚发现母亲遗物的秘密。",
    )
    project.timelines.order.append("timeline:ep2")


def test_legacy_single_timeline_project_has_no_script_node() -> None:
    """旧项目（单 timeline 且无 script slot）零回退：不生成 script 节点。"""

    project = _project()
    _add_element(project, _element("elem:one"))
    graph = derive_work_graph(project)
    assert not [node for node in graph.nodes if node.kind == "script"]
    storyboard = graph.by_id["storyboard:elem:one"]
    assert "script:timeline:main" not in storyboard.deps


def test_multi_timeline_project_derives_script_nodes_gating_elements() -> (
    None
):
    project = _project()
    _add_second_timeline(project)
    _add_element(project, _element("elem:one"))

    graph = derive_work_graph(project)
    main_script = graph.by_id["script:timeline:main"]
    ep2_script = graph.by_id["script:timeline:ep2"]
    # slot 无版本 → READY，可被调度器直接派发。
    assert main_script.status is WorkNodeStatus.READY
    assert ep2_script.status is WorkNodeStatus.READY
    assert main_script.command == "GENERATE_TIMELINE_SCRIPT"
    assert main_script.timeline_id == "timeline:main"
    assert ep2_script.lane == "第二集 · 旧宅疑云"
    assert ep2_script.locator == {
        "page": "blueprint",
        "timelineId": "timeline:ep2",
    }
    assert main_script in graph.ready_media_nodes()

    # 该 timeline 的 storyboard/video 等待剧本节点。
    storyboard = graph.by_id["storyboard:elem:one"]
    video = graph.by_id["video:elem:one"]
    assert "script:timeline:main" in storyboard.deps
    assert storyboard.status is WorkNodeStatus.GATED
    assert "script:timeline:main" in storyboard.missing
    assert video.deps == ("script:timeline:main", "storyboard:elem:one")
    assert storyboard.timeline_id == "timeline:main"

    # 剧本版本选定后分镜解除门禁。
    _select_slot(
        project,
        slot_id="script:timeline:main",
        kind="timeline_script",
        owner_ref="timeline:timeline:main",
        version_id="art:script-main",
    )
    graph = derive_work_graph(project)
    assert (
        graph.by_id["script:timeline:main"].status is WorkNodeStatus.DONE
    )
    assert (
        graph.by_id["storyboard:elem:one"].status is WorkNodeStatus.READY
    )


def test_stale_script_version_marks_script_node_stale() -> None:
    project = _project()
    _add_second_timeline(project)
    _select_slot(
        project,
        slot_id="script:timeline:ep2",
        kind="timeline_script",
        owner_ref="timeline:timeline:ep2",
        version_id="art:script-ep2",
    )
    project.assets.artifact_versions_by_id["art:script-ep2"].stale = True

    graph = derive_work_graph(project)
    node = graph.by_id["script:timeline:ep2"]
    assert node.status is WorkNodeStatus.STALE
    # STALE is terminal for the scheduler: not READY, not dispatched.
    assert node not in graph.ready_media_nodes()


def test_single_timeline_with_script_slot_opts_into_script_flow() -> None:
    """存在 timeline_script slot 的单 timeline 项目也进入剧本流。"""

    project = _project()
    _select_slot(
        project,
        slot_id="script:timeline:main",
        kind="timeline_script",
        owner_ref="timeline:timeline:main",
        version_id="art:script-main",
    )
    _add_element(project, _element("elem:one"))

    graph = derive_work_graph(project)
    script = graph.by_id["script:timeline:main"]
    assert script.status is WorkNodeStatus.DONE
    assert "script:timeline:main" in graph.by_id["storyboard:elem:one"].deps


def test_running_script_task_projects_running_status() -> None:
    project = _project()
    _add_second_timeline(project)
    graph = derive_work_graph(
        project,
        tasks=[
            _task(
                "script_draft",
                "timeline:timeline:ep2",
                TaskStatus.RUNNING,
                progress=0.5,
            ),
        ],
    )
    node = graph.by_id["script:timeline:ep2"]
    assert node.status is WorkNodeStatus.RUNNING
    assert node.progress == 0.5


# ---- Phase 3: interaction motions & interactive bundle gate -----------


def _make_branching(project: Project) -> None:
    """timeline:main --edge:a/b--> ep4a / ep4b，主线末尾挂抉择 element。"""

    from services.project_files.models import (
        InteractionCreation,
        InteractionOption,
        NarrativeEdge,
        Timeline,
    )

    project.timelines.items["timeline:main"].title = "第3集 · 双重身份"
    for timeline_id, title in (
        ("timeline:ep4a", "第4集A · 真相大白"),
        ("timeline:ep4b", "第4集B · 沉默代价"),
    ):
        project.timelines.items[timeline_id] = Timeline(
            timeline_id=timeline_id,
            title=title,
        )
        project.timelines.order.append(timeline_id)
    project.narrative_edges = [
        NarrativeEdge(
            edge_id="edge:a",
            source_timeline_id="timeline:main",
            target_timeline_id="timeline:ep4a",
            label="选择A · 揭发真相",
        ),
        NarrativeEdge(
            edge_id="edge:b",
            source_timeline_id="timeline:main",
            target_timeline_id="timeline:ep4b",
            label="选择B · 保持沉默",
        ),
    ]
    project.timelines.items["timeline:main"].elements_by_id["el:choice"] = (
        TimelineElement(
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
        )
    )


def _draft_choice_motion(project: Project) -> None:
    from services.project_files.models import MotionGraphic

    element = project.timelines.items["timeline:main"].elements_by_id[
        "el:choice"
    ]
    element.creation.motion = MotionGraphic(
        format="html_css",
        html=(
            '<!DOCTYPE html><html><body>'
            '<button data-edge-ref="edge:a">A</button>'
            '<button data-edge-ref="edge:b">B</button>'
            "</body></html>"
        ),
    )


def _select_final_video(project: Project, timeline_id: str) -> None:
    _select_slot(
        project,
        slot_id=f"timeline:{timeline_id}:render",
        kind="final_video",
        owner_ref=f"timeline:{timeline_id}",
        version_id=f"art:final-{timeline_id}",
    )


def test_interaction_node_gates_on_script_then_becomes_dispatchable() -> None:
    project = _project()
    _make_branching(project)

    graph = derive_work_graph(project)
    node = graph.by_id["interaction:el:choice"]
    assert node.kind == "interaction"
    assert node.timeline_id == "timeline:main"
    assert node.lane == "第3集 · 双重身份"
    assert node.deps == ("script:timeline:main",)
    # 剧本未定稿：抉择动效等剧本节点。
    assert node.status is WorkNodeStatus.GATED
    assert "script:timeline:main" in node.missing
    assert node.command == "GENERATE_INTERACTION_MOTION"
    assert node.target_ref == "element:el:choice"
    assert node.locator == {
        "page": "blueprint",
        "timelineId": "timeline:main",
        "elementId": "el:choice",
    }

    _select_slot(
        project,
        slot_id="script:timeline:main",
        kind="timeline_script",
        owner_ref="timeline:timeline:main",
        version_id="art:script-main",
    )
    graph = derive_work_graph(project)
    node = graph.by_id["interaction:el:choice"]
    assert node.status is WorkNodeStatus.READY
    # interaction 在 DISPATCHABLE_KINDS 中：调度器可直接派发。
    assert node in graph.ready_media_nodes()


def test_interaction_node_done_when_motion_is_drafted() -> None:
    project = _project()
    _make_branching(project)
    _draft_choice_motion(project)

    graph = derive_work_graph(project)
    assert (
        graph.by_id["interaction:el:choice"].status is WorkNodeStatus.DONE
    )


def test_interaction_node_reopens_when_options_change_after_draft() -> None:
    """加选项/改边文案后旧动效必须失效（stale 收窄，方案 2.7a）。"""

    from services.media_files.interaction_fingerprint import (
        FINGERPRINT_MARKER,
        interaction_request_fingerprint,
    )
    from services.project_files.models import InteractionOption, NarrativeEdge

    project = _project()
    _make_branching(project)
    _draft_choice_motion(project)
    _select_slot(
        project,
        slot_id="script:timeline:main",
        kind="timeline_script",
        owner_ref="timeline:timeline:main",
        version_id="art:script-main",
    )
    element = project.timelines.items["timeline:main"].elements_by_id[
        "el:choice"
    ]
    edges_by_id = {edge.edge_id: edge for edge in project.narrative_edges}
    fingerprint = interaction_request_fingerprint(
        element.creation,
        edges_by_id,
    )
    element.creation.motion.design_notes = (
        f"抉择动效\n{FINGERPRINT_MARKER}{fingerprint}"
    )
    graph = derive_work_graph(project)
    assert (
        graph.by_id["interaction:el:choice"].status is WorkNodeStatus.DONE
    )

    project.narrative_edges.append(
        NarrativeEdge(
            edge_id="edge:c",
            source_timeline_id="timeline:main",
            target_timeline_id="timeline:ep4a",
            label="选择C · 报警",
        ),
    )
    element.creation.options.append(InteractionOption(edge_ref="edge:c"))
    graph = derive_work_graph(project)
    assert (
        graph.by_id["interaction:el:choice"].status is WorkNodeStatus.READY
    )


def test_running_interaction_task_projects_running_status() -> None:
    project = _project()
    _make_branching(project)
    graph = derive_work_graph(
        project,
        tasks=[
            _task(
                "interaction_draft",
                "element:el:choice",
                TaskStatus.RUNNING,
                progress=0.3,
            ),
        ],
    )
    node = graph.by_id["interaction:el:choice"]
    assert node.status is WorkNodeStatus.RUNNING
    assert node.progress == 0.3


def test_bundle_node_gates_until_segments_and_interactions_done() -> None:
    project = _project()
    _make_branching(project)

    graph = derive_work_graph(project)
    bundle = graph.by_id["bundle:project"]
    assert bundle.kind == "bundle"
    assert bundle.lane == "compose"
    assert bundle.status is WorkNodeStatus.GATED
    # 门禁点名：抉择动效未就绪 + 全部可达分段缺成片。
    assert "interaction:el:choice" in bundle.deps
    assert "interaction:el:choice" in bundle.missing
    assert "timeline:timeline:main 缺成片" in bundle.missing
    assert "timeline:timeline:ep4a 缺成片" in bundle.missing
    assert "timeline:timeline:ep4b 缺成片" in bundle.missing
    # bundle 不派发媒体任务：经 GET /interactive-bundle 导出。
    assert bundle.command is None
    assert bundle not in graph.ready_media_nodes()
    assert "interactive-bundle" in bundle.label
    assert bundle.locator == {
        "page": "blueprint",
        "export": "interactive-bundle",
    }

    _draft_choice_motion(project)
    for timeline_id in ("timeline:main", "timeline:ep4a", "timeline:ep4b"):
        _select_final_video(project, timeline_id)
    graph = derive_work_graph(project)
    assert graph.by_id["bundle:project"].status is WorkNodeStatus.READY


def test_bundle_node_goes_stale_when_a_segment_final_is_stale() -> None:
    project = _project()
    _make_branching(project)
    _draft_choice_motion(project)
    for timeline_id in ("timeline:main", "timeline:ep4a", "timeline:ep4b"):
        _select_final_video(project, timeline_id)
    project.assets.artifact_versions_by_id[
        "art:final-timeline:ep4a"
    ].stale = True

    graph = derive_work_graph(project)
    assert graph.by_id["bundle:project"].status is WorkNodeStatus.STALE


def test_projects_without_edges_have_no_interaction_or_bundle_nodes() -> None:
    # 旧单 timeline 项目与线性多集项目零回退。
    legacy = _project()
    _add_element(legacy, _element("elem:one"))
    graph = derive_work_graph(legacy)
    assert not [
        node for node in graph.nodes if node.kind in ("interaction", "bundle")
    ]

    linear = _project()
    _add_second_timeline(linear)
    graph = derive_work_graph(linear)
    assert not [
        node for node in graph.nodes if node.kind in ("interaction", "bundle")
    ]
