# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Pending WorkGraph approvals retire once their recorded terms drift.

A decision card binds exact billing terms. When the project (or model
config) moves after the card was created, executing the node today would
bill something the user never approved, so the sweep expires the card
instead of letting every approve click fail with a generic conflict.
"""

import asyncio
import copy

from services.file_agent_runtime import workgraph_execution as wg
from services.file_agent_runtime.driver import _execution_provider_model
from services.file_agent_runtime.work_graph import derive_work_graph
from services.file_agent_runtime.workgraph_execution import (
    WORKGRAPH_DRIFT_RETIRE_REASON,
    requested_work_node,
    retire_drifted_workgraph_authorizations,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project, VisualEntity, VisualVariant
from services.runtime_files.execution_models import (
    ExecutionAuthorizationRecord,
    ExecutionAuthorizationStatus,
)
from services.runtime_files.execution_store import ProjectExecutionStore


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
