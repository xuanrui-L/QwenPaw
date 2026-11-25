# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,unused-argument
"""Review policy switches and their effects at agent commit boundaries."""

from __future__ import annotations

import json
import threading

import pytest

from models import text_model
from models.config import forced_review_env_overrides
from services.project_files.agent_tools import (
    AgentProjectToolContext,
    AgentProjectTools,
)
from services.project_files.models import Project
from services.project_files.store import ProjectStore
from services.run_review import admission, operator_registry
from services.run_review.operator_registry import (
    REVIEW_OPERATORS,
    is_operator_enabled,
    operator_keys,
    operator_status_report,
)

pytestmark = pytest.mark.unit


def test_registry_integrity() -> None:
    keys = operator_keys()
    assert len(keys) == len(set(keys))
    tiers = {operator.tier for operator in REVIEW_OPERATORS}
    assert tiers == {0, 1, 2, 3}
    # Every APE-imported switchable capability is present.
    assert set(keys) >= {
        "video_index",
        "av_sync",
        "audio_content",
        "machine_params",
        "light_metrics",
        "cross_shot_consistency",
        "camera_motion",
        "ocr_text",
        "script_check",
        "defect_bank",
        "faithfulness",
        "focused_frames",
        "challenge",
    }


def test_auto_resolution_follows_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        operator_registry,
        "_configured_operators",
        dict,
    )
    # Dependency-free operators are on by default (能开尽开).
    assert is_operator_enabled("video_index") is True
    assert is_operator_enabled("defect_bank") is True
    assert is_operator_enabled("challenge") is True
    # ASR-backed operator follows the configured key.
    from models import config as model_config

    monkeypatch.setattr(model_config, "get_asr_api_key", lambda: "")
    assert is_operator_enabled("av_sync") is False
    monkeypatch.setattr(model_config, "get_asr_api_key", lambda: "key")
    assert is_operator_enabled("av_sync") is True
    # Self-degrading operator stays on even without its optional lib.
    monkeypatch.setattr(
        operator_registry,
        "_dependency_available",
        lambda dependency: False,
    )
    assert is_operator_enabled("camera_motion") is True
    # Unknown keys never enable a code path.
    assert is_operator_enabled("no_such_operator") is False


def test_explicit_user_choice_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        operator_registry,
        "_configured_operators",
        lambda: {"video_index": False, "av_sync": True},
    )
    monkeypatch.setattr(
        operator_registry,
        "_dependency_available",
        lambda dependency: False,
    )
    assert is_operator_enabled("video_index") is False
    assert is_operator_enabled("av_sync") is True


def test_status_report_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        operator_registry,
        "_configured_operators",
        lambda: {"defect_bank": False},
    )
    rows = {row["key"]: row for row in operator_status_report()}
    assert rows["defect_bank"]["source"] == "user"
    assert rows["defect_bank"]["enabled"] is False
    assert rows["video_index"]["source"] == "auto"
    assert rows["video_index"]["enabled"] is True
    assert {"key", "tier", "dependency", "capability_ok", "enabled"} <= set(
        rows["av_sync"],
    )


def test_disabled_operator_records_visible_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-disabled operator leaves a self-explaining marker.

    Exercises the ``_safe`` gate directly (the ffmpeg end-to-end path is
    already covered by test_objective_facts).
    """
    from services.run_review.objective import facts as facts_mod

    monkeypatch.setattr(
        operator_registry,
        "_configured_operators",
        lambda: {"ocr_text": False, "cross_shot_consistency": False},
    )
    collected: dict = {}
    facts_mod._safe(  # pylint: disable=protected-access
        collected,
        "text_render",
        lambda: {"measured": True},
        switch="ocr_text",
    )
    facts_mod._safe(  # pylint: disable=protected-access
        collected,
        "cross_shot_consistency",
        lambda: {"measured": True},
    )
    facts_mod._safe(  # pylint: disable=protected-access
        collected,
        "video_index",
        lambda: {"cut_count": 1},
    )
    assert collected["text_render"]["status"] == "disabled"
    assert collected["cross_shot_consistency"]["status"] == "disabled"
    # Untouched operators keep running.
    assert collected["video_index"]["cut_count"] == 1


def test_render_challenge_switch_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from models import config as model_config

    monkeypatch.delenv("CREATOR_RENDER_CHALLENGE_ENABLED", raising=False)
    monkeypatch.setattr(
        operator_registry,
        "_configured_operators",
        lambda: {"challenge": False},
    )
    assert model_config.is_render_challenge_enabled() is False
    monkeypatch.setattr(operator_registry, "_configured_operators", dict)
    assert model_config.is_render_challenge_enabled() is True
    # An explicitly set environment variable keeps full control.
    monkeypatch.setenv("CREATOR_RENDER_CHALLENGE_ENABLED", "0")
    assert model_config.is_render_challenge_enabled() is False


def test_explicit_values_are_reported_even_when_falsy(monkeypatch) -> None:
    """``0`` is still an override: it shadows a UI-enabled tier."""
    for name in (
        "CREATOR_SYNC_REVIEW_ENABLED",
        "CREATOR_MEDIA_REVIEW_ENABLED",
        "CREATOR_SELF_REVIEW_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    assert forced_review_env_overrides() == {}
    # Blank values never count as overrides.
    monkeypatch.setenv("CREATOR_SYNC_REVIEW_ENABLED", "  ")
    assert forced_review_env_overrides() == {}
    monkeypatch.setenv("CREATOR_MEDIA_REVIEW_ENABLED", "0")
    monkeypatch.setenv("CREATOR_SELF_REVIEW_ENABLED", "1")
    assert forced_review_env_overrides() == {
        "CREATOR_MEDIA_REVIEW_ENABLED": "0",
        "CREATOR_SELF_REVIEW_ENABLED": "1",
    }


PROJECT_ID = "project-commit-tools"


def _advisory_response() -> str:
    scores = [
        {"row_key": k, "score": 8, "ok": True, "finding": "", "suggestion": ""}
        for k in ("concept", "contract", "rhythm")
    ]
    scores[0] |= {
        "score": 3,
        "ok": False,
        "finding": "/strategy/creative_brief 是流水账",
        "suggestion": "提炼一句话概念",
    }
    return json.dumps(
        {"scores": scores, "summary": "需要补概念"},
        ensure_ascii=False,
    )


def _boundary(tmp_path) -> AgentProjectTools:
    store = ProjectStore(tmp_path.resolve())
    store.create(Project.new(project_id=PROJECT_ID, name="Initial"))
    boundary = AgentProjectTools(
        store,
        context=AgentProjectToolContext(
            origin="runtime_task",
            caused_by_request_id="request-1",
            caused_by_message_seq=1,
            round_id="agent-round-run-1",
        ),
    )
    boundary.invoke("read_project", {"projectId": PROJECT_ID})
    return boundary


def _patch(boundary: AgentProjectTools, value: str = "剪一剪加音乐") -> dict:
    return boundary.invoke(
        "patch_project",
        {
            "projectId": PROJECT_ID,
            "ops": [
                {
                    "op": "replace",
                    "path": "/strategy/creative_brief",
                    "value": value,
                },
            ],
        },
    )


@pytest.fixture()
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("CREATOR_SYNC_REVIEW_ENABLED", "1")

    async def fake_chat_completion(prompt, **kwargs):
        return _advisory_response()

    monkeypatch.setattr(text_model, "chat_completion", fake_chat_completion)
    return _boundary(tmp_path)


def test_both_commit_tools_attach_review_advisory(tools) -> None:
    """jq_project and patch_project share the commit pipeline.

    Regression: patch_project initially bypassed the sync-review
    attachment, silently losing the advisory for agent runs.
    """
    result = tools.invoke(
        "jq_project",
        {
            "projectId": PROJECT_ID,
            "program": '.strategy.creative_brief = "剪一剪加音乐"',
        },
    )
    advisory = result.get("reviewAdvisory")
    assert advisory["round"] == 1
    advisory = _patch(tools, "换个节奏再剪一版").get("reviewAdvisory")
    assert advisory["round"] == 2


def test_commit_tools_skip_review_when_off(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CREATOR_SYNC_REVIEW_ENABLED", raising=False)
    assert _patch(_boundary(tmp_path)).get("reviewAdvisory") is None


def test_parent_commit_registers_fence_until_inline_review_finishes(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CREATOR_SYNC_REVIEW_ENABLED", "1")
    started = threading.Event()
    release = threading.Event()

    async def blocked_review(_prompt, **_kwargs):
        started.set()
        assert release.wait(5)
        return _advisory_response()

    monkeypatch.setattr(text_model, "chat_completion", blocked_review)
    boundary = _boundary(tmp_path)
    outcome: dict[str, object] = {}

    def commit() -> None:
        outcome.update(
            boundary.invoke(
                "patch_project",
                {
                    "projectId": PROJECT_ID,
                    "ops": [
                        {
                            "op": "replace",
                            "path": "/strategy",
                            "value": {
                                "creative_brief": "纸船驶向晨雾",
                                "audience": "纪录片观众",
                                "creative_direction": "柔和电影感",
                                "constraints": "单镜头",
                                "success_criteria": "主体清晰",
                            },
                        },
                    ],
                },
            ),
        )

    worker = threading.Thread(target=commit, daemon=True)
    worker.start()
    assert started.wait(5)
    reports_root = (
        boundary.store.project_root(PROJECT_ID) / "runtime" / "run-review"
    )
    assert admission.active_sync_fences(reports_root)
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert outcome.get("reviewAdvisory") is not None
    # Strategy is reviewed inline but is not itself a generation input, so it
    # must not leave a cross-turn media blocker behind.
    assert not admission.active_sync_fences(reports_root)
