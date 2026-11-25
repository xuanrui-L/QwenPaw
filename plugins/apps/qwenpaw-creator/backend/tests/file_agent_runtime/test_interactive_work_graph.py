# -*- coding: utf-8 -*-
"""Interactive graph nodes and mode-specific dispatch."""

from __future__ import annotations

import pytest
from domain.enums import TaskStatus
from services.file_agent_runtime.work_graph import (
    WorkNodeStatus,
    derive_work_graph,
)
from services.project_files.models import (
    Project,
    TimelineElement,
    TimelineSpan,
)
from test_work_graph import (
    _add_element,
    _add_second_timeline,
    _element,
    _project,
    _select_slot,
    _task,
)

import asyncio
import copy
from services.file_agent_runtime import workgraph_execution as wg
from services.file_agent_runtime.driver import _execution_provider_model
from services.file_agent_runtime.workgraph_execution import requested_work_node
from services.runtime_files.execution_models import (
    ExecutionAuthorizationRecord,
)
from services.runtime_files.execution_store import ProjectExecutionStore
from test_workgraph_execution import create

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("delegated", [False, True])
def test_stale_final_render_recompose_follows_mode(
    monkeypatch,
    delegated,
) -> None:
    # Render review revises an overlay after the master render: edit impact
    # marks the final_video version stale. Delegated (YOLO) runs drop back to
    # READY so the unattended loop re-renders the corrected cut — unattended
    # delivery is the point of that mode. Every other mode marks it STALE and
    # waits for someone to press 成片: auto-composing under a stream of edits
    # produced five renders inside five minutes on 2026-11-05, four discarded
    # as PROJECT_INPUT_SNAPSHOT_STALE because the timeline moved again before
    # the render landed.
    from models import config as models_config

    if delegated:
        monkeypatch.setattr(
            models_config,
            "get_execution_mode",
            lambda: models_config.EXECUTION_MODE_DELEGATED,
        )
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
    assert graph.by_id["compose:timeline:main"].status is WorkNodeStatus.DONE

    project.assets.artifact_versions_by_id["art:final"].stale = True
    graph = derive_work_graph(project)
    compose = graph.by_id["compose:timeline:main"]
    expected = WorkNodeStatus.READY if delegated else WorkNodeStatus.STALE
    assert compose.status is expected
    assert (compose in graph.ready_media_nodes()) is delegated
    # STALE must not leak back through the regeneration path either: that one
    # additionally requires a regeneration_of marker a compose node never has.
    assert compose not in graph.regeneration_nodes()


@pytest.mark.parametrize(
    "delegated,existing,automatic,accepted",
    [
        (False, False, True, True),
        (False, True, True, False),
        (False, True, False, True),
        (True, True, True, True),
    ],
)
def test_render_admission_follows_saved_compose_state(
    tmp_path,
    monkeypatch,
    delegated,
    existing,
    automatic,
    accepted,
):
    import asyncio
    from api.file_execution_routes import render_timeline
    from domain.errors import ConflictError
    from models import config
    from services.media_files import local_execution
    from services.project_files.facade import CreatorFileServices

    mode = (
        config.EXECUTION_MODE_DELEGATED
        if delegated
        else config.EXECUTION_MODE_CO_CREATION
    )
    monkeypatch.setattr(config, "get_execution_mode", lambda: mode)
    project = _project()
    _add_element(project, _element("elem:one"))
    for output, kind in (
        ("storyboard", "r2v_storyboard_image"),
        ("main", "element_video"),
    ):
        _select_slot(
            project,
            slot_id=f"element:elem:one:{output}",
            kind=kind,
            owner_ref="element:elem:one",
            version_id=f"art:{output}",
        )
    if existing:
        _select_slot(
            project,
            slot_id="timeline:timeline:main:render",
            kind="final_video",
            owner_ref="timeline:timeline:main",
            version_id="art:final",
        )
        project.assets.artifact_versions_by_id["art:final"].stale = True
    services = CreatorFileServices.create(tmp_path.resolve())
    services.projects.create(project)
    calls = []

    async def compose(*_args, **_kwargs):
        calls.append("compose")

    monkeypatch.setattr(
        local_execution,
        "validate_local_media_execution",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        local_execution, "execute_file_local_media_command", compose
    )

    async def dispatch():
        result = await render_timeline(
            project.project_id,
            "timeline:main",
            idempotency_key="render-1",
            services=services,
            automatic=automatic,
        )
        await asyncio.sleep(0)
        return result

    if accepted:
        assert asyncio.run(dispatch())["replayed"] is False
        assert calls == ["compose"]
    else:
        with pytest.raises(ConflictError) as blocked:
            asyncio.run(dispatch())
        assert blocked.value.details == {
            "code": "AUTOMATIC_COMPOSE_NOT_READY",
            "nodeId": "compose:timeline:main",
            "status": "stale",
        }
        assert not calls


@pytest.mark.parametrize("delegated", [False, True])
def test_superseded_render_source_recompose_follows_mode(
    monkeypatch,
    delegated,
) -> None:
    """Frozen selections are authoritative even if impact missed stale.

    Whether that reopens the compose follows the same mode split as the
    stale-flag path: READY for delegated runs, STALE-and-wait otherwise.
    """
    from models import config as models_config

    if delegated:
        monkeypatch.setattr(
            models_config,
            "get_execution_mode",
            lambda: models_config.EXECUTION_MODE_DELEGATED,
        )
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
        derive_work_graph(project).by_id["compose:timeline:main"].status
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
    compose = graph.by_id["compose:timeline:main"]
    expected = WorkNodeStatus.READY if delegated else WorkNodeStatus.STALE
    assert compose.status is expected
    assert (compose in graph.ready_media_nodes()) is delegated


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
    project.timelines.items["timeline:main"].elements_by_id[
        "el:choice"
    ] = TimelineElement(
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


def _draft_choice_motion(project: Project) -> None:
    from services.project_files.models import MotionGraphic

    element = project.timelines.items["timeline:main"].elements_by_id[
        "el:choice"
    ]
    element.creation.motion = MotionGraphic(
        format="html_css",
        html=(
            "<!DOCTYPE html><html><body>"
            '<button data-edge-ref="edge:a">A</button>'
            '<button data-edge-ref="edge:b">B</button>'
            "</body></html>"
        ),
    )


def _draft_presentation(project: Project) -> None:
    """Use the shared HTML fixture without importing another test module."""
    from pathlib import Path

    from services.media_files.presentation_authoring import (
        presentation_fingerprint,
    )
    from services.project_files.models import (
        MotionGraphic,
        narrative_timeline_ids,
    )

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


def _make_cover_current(project: Project) -> None:
    """Mark the whole-piece cover as generated and current for the graph."""

    from services.media_files.cover_generation import cover_input_fingerprint

    fingerprint = cover_input_fingerprint(project)
    project.interactive_presentation = (
        project.interactive_presentation.model_copy(
            update={
                "cover_file_id": "file-cover-test",
                "cover_checksum": "a" * 64,
                "cover_fingerprint": f"input_fingerprint={fingerprint}",
            },
        )
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


def test_cover_node_becomes_dispatchable_when_script_is_final() -> None:
    """Regression: cover must be dispatchable or it strands at 待开始.

    The whole-piece cover shares interaction:project's deps, so once the
    script is selected it turns READY; it only leaves 待开始 if its kind is
    in DISPATCHABLE_KINDS and the scheduler actually picks it up.
    """

    project = _project()
    _make_branching(project)
    # cover 依赖全部 timeline 的 script 节点，三条都定稿后才转 READY。
    for timeline_id in ("timeline:main", "timeline:ep4a", "timeline:ep4b"):
        _select_slot(
            project,
            slot_id=f"script:{timeline_id}",
            kind="timeline_script",
            owner_ref=f"timeline:{timeline_id}",
            version_id=f"art:script-{timeline_id.rsplit(':', 1)[-1]}",
        )

    graph = derive_work_graph(project)
    node = graph.by_id["cover:project"]
    assert node.kind == "cover"
    assert node.command == "GENERATE_COVER"
    assert node.status is WorkNodeStatus.READY
    assert node in graph.ready_media_nodes()


def test_interaction_node_done_when_motion_is_drafted() -> None:
    project = _project()
    _make_branching(project)
    _draft_choice_motion(project)
    _draft_presentation(project)

    graph = derive_work_graph(project)
    assert graph.by_id["interaction:el:choice"].status is WorkNodeStatus.DONE


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
    _draft_presentation(project)
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
        project,
    )
    element.creation.motion.design_notes = (
        f"抉择动效\n{FINGERPRINT_MARKER}{fingerprint}"
    )
    graph = derive_work_graph(project)
    assert graph.by_id["interaction:el:choice"].status is WorkNodeStatus.DONE

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
    assert graph.by_id["interaction:el:choice"].status is WorkNodeStatus.READY


@pytest.mark.parametrize("scope", ["element", "project"])
def test_running_interaction_task_projects_running_status(scope) -> None:
    project = _project()
    _make_branching(project)
    target = (
        "element:el:choice"
        if scope == "element"
        else f"project:{project.project_id}"
    )
    graph = derive_work_graph(
        project,
        tasks=[
            _task(
                "interaction_draft",
                target,
                TaskStatus.RUNNING,
                progress=0.3,
            ),
        ],
    )
    node = graph.by_id[
        "interaction:el:choice"
        if scope == "element"
        else "interaction:project"
    ]
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
    _draft_presentation(project)
    for timeline_id in ("timeline:main", "timeline:ep4a", "timeline:ep4b"):
        _select_final_video(project, timeline_id)
    graph = derive_work_graph(project)
    assert graph.by_id["bundle:project"].status is WorkNodeStatus.DONE


def test_bundle_node_goes_stale_when_a_segment_final_is_stale() -> None:
    project = _project()
    _make_branching(project)
    _draft_choice_motion(project)
    _draft_presentation(project)
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


def test_completed_branching_graph_has_no_permanent_ready_bundle():
    project = _project()
    _make_branching(project)
    for timeline_id in project.timelines.order:
        project.timelines.items[
            timeline_id
        ].description = "Published complete scene."
        _select_final_video(project, timeline_id)
    _draft_choice_motion(project)
    _draft_presentation(project)
    _make_cover_current(project)
    graph = derive_work_graph(project)
    assert not graph.unfinished(), [
        (n.node_id, n.status) for n in graph.unfinished()
    ]
    assert not graph.model_required_nodes()


def test_stale_bundle_waits_for_machine_dependencies():
    from services.file_agent_runtime.work_graph import WorkGraph, WorkNode

    graph = WorkGraph(
        nodes=(
            WorkNode(
                node_id="compose:main",
                kind="compose",
                label="compose",
                status=WorkNodeStatus.RUNNING,
            ),
            WorkNode(
                node_id="bundle:project",
                kind="bundle",
                label="bundle",
                status=WorkNodeStatus.STALE,
                deps=("compose:main",),
                missing=("compose:main",),
            ),
        ),
        generation=1,
    )
    assert not graph.model_required_nodes(automatic_regeneration=True)


def _branching_candidate(snapshot):
    """Return ``drift-project`` with a second timeline and a branch edge.

    interactive-video derives a project-level ``interaction`` node whenever
    ``narrative_edges`` is non-empty; creator has no such node kind. The
    extra timeline keeps both edge endpoints real so the commit survives the
    blueprint's branch validation.
    """
    candidate = snapshot.project.model_dump(mode="json")
    tail = copy.deepcopy(candidate["timelines"]["items"]["timeline:main"])
    tail["timeline_id"] = "timeline:ep4b"
    tail["title"] = "Ending B"
    tail["elements_by_id"] = {}
    candidate["timelines"]["items"]["timeline:ep4b"] = tail
    candidate["timelines"]["order"].append("timeline:ep4b")
    candidate["narrative_edges"] = [
        {
            "edge_id": "edge:b",
            "source_timeline_id": "timeline:main",
            "target_timeline_id": "timeline:ep4b",
            "label": "Look for more evidence",
        },
    ]
    return candidate


def test_interaction_approval_is_not_retired_as_target_removed(tmp_path):
    """C1 regression: an ``interaction`` approval survives an untouched check.

    interactive-video adds an ``interaction`` media node kind that creator
    lacks. The drift guard once matched a hardcoded ``{visual, lineup,
    storyboard, video}`` literal that omitted it, so every interaction card
    was retired as ``workgraph_target_removed`` and the node could never be
    approved. Deriving the authorizable set from ``MEDIA_NODE_KINDS`` keeps
    interaction covered, so a card whose terms still match reports no drift.
    """

    async def scenario():
        services = create(tmp_path)
        store = ProjectExecutionStore(services.root)

        base = services.projects.read("drift-project")
        await services.commit_candidate(
            base=base,
            candidate=_branching_candidate(base),
            origin="agentdock_idle_goal",
        )

        snapshot = services.projects.read("drift-project")
        graph = derive_work_graph(snapshot.project)
        node = next(item for item in graph.nodes if item.kind == "interaction")
        plan = requested_work_node(snapshot, node)
        provider, model = _execution_provider_model(plan.spec, plan.parameters)
        store.create_execution_authorization(
            ExecutionAuthorizationRecord(
                authorization_id="auth-interaction",
                project_id="drift-project",
                round_id="round-interaction",
                run_id="run-interaction",
                execution_request_id="request-interaction",
                operation=plan.spec.name,
                target_scope=[node.target_ref],
                authorization_token="token-interaction",
                summary="起草抉择动效",
                scope={
                    "operation": plan.spec.name,
                    "targetRefs": [node.target_ref],
                    "parameters": plan.parameters,
                    "workGraph": {
                        "nodeId": node.node_id,
                        "fingerprint": plan.fingerprint,
                        "provider": provider,
                        "model": model,
                    },
                },
                requested_provider=provider,
                requested_model=model,
            ),
        )

        # Terms match the live interaction node exactly, so the drift reason
        # is None -- never the target-removed retirement the hardcoded kind
        # list once produced for this node kind.
        assert (
            await wg.workgraph_authorization_drift_reason(
                services,
                "drift-project",
                snapshot,
                graph,
                store.get_execution_authorization(
                    "drift-project",
                    "auth-interaction",
                ),
            )
            is None
        )

    asyncio.run(scenario())
