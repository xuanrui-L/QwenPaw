# -*- coding: utf-8 -*-
# Pytest fixtures and contract probes retain exact types and private seams.
# pylint: disable=redefined-outer-name
# pylint: disable=use-implicit-booleaness-not-comparison
"""Creation pit-stop checkpoints gate costly generation deterministically."""

from __future__ import annotations

import pytest

from domain.enums import SpecialistRole
from services.file_agent_runtime.checkpoints import (
    CHECKPOINT_DESIGN,
    CHECKPOINT_SCRIPT,
    CHECKPOINT_STRUCTURE,
    retire_legacy_plan_checkpoints,
    required_checkpoint_phases,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project
from services.runtime_files.execution_models import (
    ExecutionAuthorizationRecord,
    ExecutionAuthorizationStatus,
)
from services.runtime_files.execution_store import ProjectExecutionStore

pytestmark = pytest.mark.unit

PROJECT_ID = "project-1"


def test_design_images_do_not_require_a_generic_plan_confirmation() -> None:
    """Requiring the design checkpoint for design images would deadlock:
    only storyboards and videos wait for it."""
    assert (
        required_checkpoint_phases(
            "image_generation",
            SpecialistRole.VISUAL_DEVELOPMENT,
        )
        == ()
    )
    assert required_checkpoint_phases(
        "image_generation",
        SpecialistRole.R2V_GENERATION_DIRECTOR,
    ) == (CHECKPOINT_DESIGN,)
    assert required_checkpoint_phases(
        "r2v_generation",
        SpecialistRole.R2V_GENERATION_DIRECTOR,
    ) == (CHECKPOINT_DESIGN,)
    # Non-media tools are never gated.
    assert not required_checkpoint_phases(
        "commit_source_intelligence",
        SpecialistRole.SOURCE_INTELLIGENCE,
    )


def test_multi_timeline_projects_prepend_structure_and_script() -> None:
    """Blueprint ladder（方案 3.1）：多集/分支项目在生成前先确认
    结构与剧本；设计图只等结构（可与剧本审阅并行）。"""

    assert required_checkpoint_phases(
        "image_generation",
        SpecialistRole.VISUAL_DEVELOPMENT,
        timeline_count=3,
    ) == (CHECKPOINT_STRUCTURE,)
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


def test_single_timeline_structure_is_always_silent() -> None:
    """单 timeline 项目感知不到 structure/script；调用点拿不到
    project（timeline_count=None）时同样按单 timeline 处理。"""

    for timeline_count in (1, None):
        assert (
            required_checkpoint_phases(
                "image_generation",
                SpecialistRole.VISUAL_DEVELOPMENT,
                timeline_count=timeline_count,
            )
            == ()
        )
        assert required_checkpoint_phases(
            "r2v_generation",
            SpecialistRole.R2V_GENERATION_DIRECTOR,
            timeline_count=timeline_count,
        ) == (CHECKPOINT_DESIGN,)


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


@pytest.mark.parametrize("mode", ["delegated", "fine_tuning"])
def test_execution_mode_scales_the_checkpoint_ladder(monkeypatch, mode):
    from models import config as model_config

    monkeypatch.setattr(model_config, "get_execution_mode", lambda: mode)
    for tool in ("image_generation", "r2v_generation"):
        assert not required_checkpoint_phases(
            tool,
            SpecialistRole.R2V_GENERATION_DIRECTOR,
        )


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


@pytest.fixture
def checkpoint_store(tmp_path):
    services = CreatorFileServices.create(tmp_path.resolve())
    services.projects.create(
        Project.new(project_id=PROJECT_ID, name="Initial"),
    )
    return ProjectExecutionStore(services.root)


def _seed_authorization(
    store,
    name,
    operation="creation_checkpoint_plan",
    provider="creator-checkpoint",
):
    return store.create_execution_authorization(
        ExecutionAuthorizationRecord(
            authorization_id=name,
            project_id=PROJECT_ID,
            round_id="round-1",
            run_id="run-1",
            execution_request_id=name,
            operation=operation,
            target_scope=[f"project:{PROJECT_ID}"],
            authorization_token=f"token-{name}",
            summary="test",
            requested_provider=provider,
        ),
    )


def test_only_pending_nonbilling_plan_records_are_retired(checkpoint_store):
    store = checkpoint_store
    plan = _seed_authorization(store, "plan-pending")
    paid = _seed_authorization(
        store,
        "image-pending",
        "image_generation",
        "dashscope",
    )
    design = _seed_authorization(
        store,
        "design-pending",
        "creation_checkpoint_design",
    )
    history = _seed_authorization(store, "plan-approved")
    store.decide_execution_authorization(
        PROJECT_ID,
        history.authorization_id,
        authorization_token=history.authorization_token,
        status=ExecutionAuthorizationStatus.APPROVED,
        decision={
            "provider": "creator-checkpoint",
            "model": "计划确认",
            "maxCandidates": 1,
        },
    )
    assert retire_legacy_plan_checkpoints(store, PROJECT_ID) == 1
    assert retire_legacy_plan_checkpoints(store, PROJECT_ID) == 0
    retired = store.get_execution_authorization(
        PROJECT_ID,
        plan.authorization_id,
    )
    assert (
        retired.status is ExecutionAuthorizationStatus.EXPIRED
        and retired.decision is None
    )
    assert retired.metadata["retiredReason"] == "plan_checkpoint_removed"
    for record, expected in (
        (paid, ExecutionAuthorizationStatus.PENDING),
        (design, ExecutionAuthorizationStatus.PENDING),
        (history, ExecutionAuthorizationStatus.APPROVED),
    ):
        assert (
            store.get_execution_authorization(
                PROJECT_ID,
                record.authorization_id,
            ).status
            is expected
        )


def test_concurrent_user_decision_wins_retirement_cas(
    checkpoint_store,
    monkeypatch,
):
    store = checkpoint_store
    record = _seed_authorization(store, "plan-race")
    decide = store.decide_execution_authorization

    def racing_decision(*args, **kwargs):
        decide(
            PROJECT_ID,
            record.authorization_id,
            authorization_token=record.authorization_token,
            status=ExecutionAuthorizationStatus.REJECTED,
        )
        return decide(*args, **kwargs)

    monkeypatch.setattr(
        store,
        "decide_execution_authorization",
        racing_decision,
    )
    assert retire_legacy_plan_checkpoints(store, PROJECT_ID) == 0
    assert (
        store.get_execution_authorization(
            PROJECT_ID,
            record.authorization_id,
        ).status
        is ExecutionAuthorizationStatus.REJECTED
    )
