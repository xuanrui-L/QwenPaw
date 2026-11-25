# -*- coding: utf-8 -*-
# pylint: disable=protected-access,redefined-outer-name,use-implicit-booleaness-not-comparison
"""Work-graph gate reporting, approval drift, and creation checkpoints."""

from __future__ import annotations

import asyncio

import pytest

from domain.enums import SpecialistRole
from services.file_agent_runtime import workgraph_execution as wg
from services.file_agent_runtime.checkpoints import (
    CHECKPOINT_DESIGN,
    CHECKPOINT_SCRIPT,
    CHECKPOINT_STRUCTURE,
    required_checkpoint_phases,
)
from services.file_agent_runtime.driver import _execution_provider_model
from services.file_agent_runtime.work_graph import derive_work_graph
from services.file_agent_runtime.workgraph_execution import (
    WORKGRAPH_DRIFT_RETIRE_REASON,
    requested_work_node,
    retire_drifted_workgraph_authorizations,
    summarize_workgraph_results,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project, VisualEntity, VisualVariant
from services.project_files.production_stage import SCRIPT_ONLY_MESSAGE
from services.runtime_files.execution_models import (
    ExecutionAuthorizationRecord,
    ExecutionAuthorizationStatus,
)
from services.runtime_files.execution_store import ProjectExecutionStore


def _gated(prompt_sync: bool, missing: list[str]) -> dict:
    item = {
        "nodeId": "storyboard:e1",
        "targetRef": "element:e1",
        "status": "BLOCKED",
        "reason": "GATED",
        "missing": missing,
    }
    if prompt_sync:
        item["promptSyncRequired"] = True
    return item


def test_summary_names_script_gate_with_next_action() -> None:
    summary = summarize_workgraph_results(
        [_gated(False, [SCRIPT_ONLY_MESSAGE])],
    )
    assert "1 项等待确认剧本" in summary
    # The script gate must not fall into the generic bucket.
    assert "制作条件尚未满足" not in summary


def test_summary_splits_review_prompt_sync_and_other() -> None:
    summary = summarize_workgraph_results(
        [
            {"status": "BLOCKED", "reason": "WAITING_REVIEW"},
            _gated(True, ["visual:scene:home:var:day"]),
            _gated(False, ["visual:scene:x"]),
        ],
    )
    assert "1 项需要先完成现有审阅" in summary
    assert "1 项需要先同步分镜/提示词内容" in summary
    assert "1 项制作条件尚未满足" in summary
    assert "visual:scene:home:var:day" not in summary
    assert "visual:scene:x" not in summary


@pytest.mark.parametrize("completed", [False, True], ids=["blocked", "mixed"])
@pytest.mark.parametrize(
    ("item", "winner", "excluded"),
    [
        (
            {
                "status": "BLOCKED",
                "reason": "WAITING_REVIEW",
                "promptSyncRequired": True,
            },
            "现有审阅",
            "同步分镜/提示词",
        ),
        (_gated(True, [SCRIPT_ONLY_MESSAGE]), "同步分镜/提示词", "确认剧本"),
    ],
    ids=["review-over-sync", "sync-over-script"],
)
def test_summary_counts_overlapping_gates_once(
    item, winner, excluded, completed
):
    items = [item]
    if completed:
        items.append({"status": "SUCCEEDED", "taskId": "task-1"})

    summary = summarize_workgraph_results(items)

    assert winner in summary
    assert excluded not in summary
    assert "制作条件尚未满足" not in summary
    assert "-1" not in summary
    assert ("已完成制作 1 项" in summary) is completed


def create(temporary):
    services = CreatorFileServices.create(temporary.resolve())
    project = Project.new(project_id="drift-project", name="Drift probe")
    project.visual.entities.items["hero"] = VisualEntity(
        entity_id="hero",
        kind="character",
        name="Hero",
        required_variant_ids=["base"],
        variants={
            "items": {
                "base": VisualVariant(
                    variant_id="base",
                    prompt="Adult in a grey coat.",
                ),
            },
            "order": ["base"],
        },
    )
    project.visual.entities.order.append("hero")

    def initialize(staged):
        services.sessions.initialize_staged_project(
            staged,
            "drift-project",
            session_id="drift-session",
            conversation_id="drift-conversation",
            initial_goal="Generate only this character.",
            goal_id="drift-goal",
            initial_message_id="drift-message",
            initial_client_message_id="drift-client",
        )

    snapshot = services.projects.create(
        project,
        initialize_staged_project=initialize,
    )
    services.poller.note_commit(snapshot)
    return services


def seed_pending_approval(services, store):
    """Record a PENDING card whose terms match the live project exactly."""
    snapshot = services.projects.read("drift-project")
    graph = derive_work_graph(snapshot.project)
    node = next(item for item in graph.nodes if item.kind == "visual")
    plan = requested_work_node(snapshot, node)
    provider, model = _execution_provider_model(plan.spec, plan.parameters)
    store.create_execution_authorization(
        ExecutionAuthorizationRecord(
            authorization_id="auth-drift",
            project_id="drift-project",
            round_id="round-drift",
            run_id="run-drift",
            execution_request_id="request-drift",
            operation=plan.spec.name,
            target_scope=[node.target_ref],
            authorization_token="token-drift",
            summary="生成图片",
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


def test_pending_workgraph_approval_expires_when_terms_drift(tmp_path):
    async def scenario():
        services = create(tmp_path)
        store = ProjectExecutionStore(services.root)
        seed_pending_approval(services, store)

        # Untouched project: the recorded terms still match, nothing retires.
        wg._drift_sweep_at.clear()
        assert (
            await retire_drifted_workgraph_authorizations(
                services,
                store,
                "drift-project",
            )
            == 0
        )
        assert (
            store.get_execution_authorization(
                "drift-project",
                "auth-drift",
            ).status
            is ExecutionAuthorizationStatus.PENDING
        )

        # A project edit moves the billed parameters away from the card.
        base = services.projects.read("drift-project")
        candidate = base.project.model_dump(mode="json")
        candidate["settings"]["aspect_ratio"] = "9:16"
        await services.commit_candidate(
            base=base,
            candidate=candidate,
            origin="agentdock_idle_goal",
        )

        wg._drift_sweep_at.clear()
        assert (
            await retire_drifted_workgraph_authorizations(
                services,
                store,
                "drift-project",
            )
            == 1
        )
        retired = store.get_execution_authorization(
            "drift-project",
            "auth-drift",
        )
        assert retired.status is ExecutionAuthorizationStatus.EXPIRED
        assert (
            retired.metadata.get("retiredReason")
            == WORKGRAPH_DRIFT_RETIRE_REASON
        )

        # A second sweep has nothing left to retire.
        wg._drift_sweep_at.clear()
        assert (
            await retire_drifted_workgraph_authorizations(
                services,
                store,
                "drift-project",
            )
            == 0
        )

    asyncio.run(scenario())


def test_drift_check_error_is_isolated_and_never_retires(tmp_path):
    """H2: a drift-check error must not abort the sweep nor expire the card.

    The sweep piggybacks on the tray's list poll; if one card's check raises
    (e.g. an unsupported S2V node), the error is logged and skipped so the
    endpoint still returns and the surviving card is left PENDING.
    """

    async def scenario():
        services = create(tmp_path)
        store = ProjectExecutionStore(services.root)
        seed_pending_approval(services, store)
        wg._drift_sweep_at.clear()

        async def boom(*_args, **_kwargs):
            raise RuntimeError("drift check exploded")

        original = wg.workgraph_authorization_drift_reason
        wg.workgraph_authorization_drift_reason = boom
        try:
            # The per-record error is swallowed; nothing is retired.
            assert (
                await retire_drifted_workgraph_authorizations(
                    services,
                    store,
                    "drift-project",
                )
                == 0
            )
        finally:
            wg.workgraph_authorization_drift_reason = original
        # A failed check must never expire an approval.
        assert (
            store.get_execution_authorization(
                "drift-project",
                "auth-drift",
            ).status
            is ExecutionAuthorizationStatus.PENDING
        )

    asyncio.run(scenario())


# Pytest fixtures and contract probes retain exact types and private seams.


PROJECT_ID = "project-1"


@pytest.mark.unit
def test_design_images_do_not_require_a_generic_plan_confirmation() -> None:
    """Requiring the design checkpoint for design images would deadlock:
    only storyboards and videos wait for it."""
    assert required_checkpoint_phases(
        "image_generation",
        SpecialistRole.VISUAL_DEVELOPMENT,
    ) == (CHECKPOINT_SCRIPT,)
    assert required_checkpoint_phases(
        "image_generation",
        SpecialistRole.R2V_GENERATION_DIRECTOR,
    ) == (CHECKPOINT_SCRIPT, CHECKPOINT_DESIGN)
    assert required_checkpoint_phases(
        "r2v_generation",
        SpecialistRole.R2V_GENERATION_DIRECTOR,
    ) == (CHECKPOINT_SCRIPT, CHECKPOINT_DESIGN)
    # Non-media tools are never gated.
    assert not required_checkpoint_phases(
        "commit_source_intelligence",
        SpecialistRole.SOURCE_INTELLIGENCE,
    )


@pytest.mark.unit
def test_multi_timeline_projects_prepend_structure_and_script() -> None:
    """Blueprint ladder（方案 3.1）：多集/分支项目在生成前先确认
    结构与剧本；设计图也必须等待剧本确认。"""

    assert required_checkpoint_phases(
        "image_generation",
        SpecialistRole.VISUAL_DEVELOPMENT,
        timeline_count=3,
    ) == (CHECKPOINT_STRUCTURE, CHECKPOINT_SCRIPT)
    assert required_checkpoint_phases(
        "image_generation",
        SpecialistRole.R2V_GENERATION_DIRECTOR,
        timeline_count=3,
    ) == (
        CHECKPOINT_STRUCTURE,
        CHECKPOINT_SCRIPT,
        CHECKPOINT_DESIGN,
    )
    assert required_checkpoint_phases(
        "r2v_generation",
        SpecialistRole.R2V_GENERATION_DIRECTOR,
        timeline_count=2,
    ) == (
        CHECKPOINT_STRUCTURE,
        CHECKPOINT_SCRIPT,
        CHECKPOINT_DESIGN,
    )


@pytest.mark.unit
def test_single_timeline_structure_is_always_silent() -> None:
    """单 timeline 不需要结构检查点，但生图仍须确认剧本。"""

    for timeline_count in (1, None):
        assert required_checkpoint_phases(
            "image_generation",
            SpecialistRole.VISUAL_DEVELOPMENT,
            timeline_count=timeline_count,
        ) == (CHECKPOINT_SCRIPT,)
        assert required_checkpoint_phases(
            "r2v_generation",
            SpecialistRole.R2V_GENERATION_DIRECTOR,
            timeline_count=timeline_count,
        ) == (CHECKPOINT_SCRIPT, CHECKPOINT_DESIGN)


@pytest.mark.unit
def test_skip_mode_silences_structure_and_script_too(monkeypatch) -> None:
    """yolo（creation_checkpoints.mode=skip）强制 delegated：
    多集项目的全部检查点同样静默，沿用既有 skip 语义路径。"""

    from models import config as model_config

    monkeypatch.setattr(
        model_config,
        "_get_user_config",
        lambda: {"creation_checkpoints": {"mode": "skip"}},
    )
    assert not required_checkpoint_phases(
        "r2v_generation",
        SpecialistRole.R2V_GENERATION_DIRECTOR,
        timeline_count=5,
    )
    assert not required_checkpoint_phases(
        "image_generation",
        SpecialistRole.VISUAL_DEVELOPMENT,
        timeline_count=5,
    )


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["delegated", "fine_tuning"])
def test_execution_mode_scales_the_checkpoint_ladder(monkeypatch, mode):
    from models import config as model_config

    monkeypatch.setattr(model_config, "get_execution_mode", lambda: mode)
    for tool in ("image_generation", "r2v_generation"):
        assert not required_checkpoint_phases(
            tool,
            SpecialistRole.R2V_GENERATION_DIRECTOR,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "settings, expected",
    [
        ({"mode": "skip", "execution_mode": "co_creation"}, "delegated"),
        ({"mode": "required", "execution_mode": "fine_tuning"}, "fine_tuning"),
        ({"mode": "required"}, "co_creation"),
    ],
    ids=["skip-overrides-mode", "required-keeps-mode", "default-mode"],
)
def test_checkpoint_settings_resolve_execution_mode(
    monkeypatch,
    settings,
    expected,
):
    from models import config as model_config

    monkeypatch.setattr(
        model_config,
        "_get_user_config",
        lambda: {"creation_checkpoints": settings},
    )
    assert model_config.get_execution_mode() == expected
