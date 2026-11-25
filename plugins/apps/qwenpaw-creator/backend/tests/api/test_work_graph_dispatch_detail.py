# -*- coding: utf-8 -*-
# Empty-dict assertions stay explicit for readability, same as the other
# API test modules in this package.
# pylint: disable=use-implicit-booleaness-not-comparison
"""The manual dispatch gate must tell a real render apart from a stranded one.

Field case 2026-11-06: a node whose task was admitted but never claimed reads
as "running", and the gate answered it with a silent dispatched=false, so the
user saw a queue label for 69 hours while nothing was queued at all.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.work_graph_routes import _task_admission_detail
from domain.enums import CreatorCommandType, TaskStatus
from domain.errors import ValidationError
from services.file_agent_runtime.work_scheduler import (
    _abandoned_queued_task_ids,
)
from services.media_files import local_execution
from services.project_files.models import (
    SceneLedgerRow,
    SourceAssetVersion,
    SourceVersionRenderSource,
)
from services.render_review import scene_review as scene_review_module

from render_review.test_scene_review import _plan, _timeline_with_scene
from test_file_runtime_api import _app
from test_project_file_api import _select_historical_video

pytestmark = pytest.mark.unit

TARGET = "element:elem:s8d"


def _task(
    *,
    status: TaskStatus,
    updated_at: datetime,
    last_attempt_seq: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        task_id="task-71c0c25dc4725316a3d843183c18612e",
        kind="r2v_generation",
        status=status,
        updated_at=updated_at,
        last_attempt_seq=last_attempt_seq,
        metadata={"targetRef": TARGET},
        input_refs=[TARGET],
        error=None,
        result=None,
    )


def test_admitted_but_unclaimed_is_recognised() -> None:
    now = datetime.now(UTC)
    stranded = _task(
        status=TaskStatus.QUEUED,
        updated_at=now - timedelta(minutes=20),
        last_attempt_seq=0,
    )
    assert _abandoned_queued_task_ids([stranded], now=now) == {
        stranded.task_id
    }


def test_claimed_or_fresh_tasks_are_not_rescued() -> None:
    now = datetime.now(UTC)
    running = _task(
        status=TaskStatus.RUNNING,
        updated_at=now,
        last_attempt_seq=3,
    )
    fresh = _task(
        status=TaskStatus.QUEUED,
        updated_at=now - timedelta(minutes=2),
        last_attempt_seq=0,
    )
    assert _abandoned_queued_task_ids([running, fresh], now=now) == set()


def test_admission_detail_reports_the_task_behind_a_running_node() -> None:
    stranded = _task(
        status=TaskStatus.QUEUED,
        updated_at=datetime.now(UTC) - timedelta(minutes=20),
        last_attempt_seq=0,
    )
    detail = _task_admission_detail([stranded], stranded.task_id)
    assert detail["taskId"] == stranded.task_id
    assert detail["taskStatus"] == "QUEUED"
    assert detail["lastAttemptSeq"] == 0
    assert detail["ageSeconds"] is not None
    assert detail["ageSeconds"] >= 1200


def test_admission_detail_stays_quiet_without_a_target() -> None:
    assert _task_admission_detail([], None) == {}
    assert _task_admission_detail([], "element:elem:none") == {}


def test_preflight_scene_review_surfaces_a_failed_repair(
    monkeypatch,
) -> None:
    """A repair that cannot finish must answer the caller, not the log.

    Review #243: the route answered 202 with a Task id that never came into
    existence, because the repair runs later and its failure had nowhere to
    go -- polling that id returns 404 forever.
    """

    # pylint: disable=protected-access
    import api.file_execution_routes as routes
    from services.render_review import scene_review

    calls: list[str] = []

    class _Projects:
        def read(self, _project_id):
            return SimpleNamespace(
                project=SimpleNamespace(
                    timelines=SimpleNamespace(items={"main": object()}),
                ),
            )

    services = SimpleNamespace(projects=_Projects())
    monkeypatch.setattr(
        scene_review,
        "collect_scene_review_targets",
        lambda _timeline: (["scene:hall"], []),
    )

    async def failing(*_args, **_kwargs):
        calls.append("reviewed")
        raise RuntimeError("VLM unavailable")

    monkeypatch.setattr(scene_review, "auto_review_stale_scenes", failing)

    with pytest.raises(RuntimeError, match="VLM unavailable"):
        asyncio.run(
            routes._preflight_scene_review(services, "project-1", "main"),
        )
    assert calls == ["reviewed"]

    # Nothing stale means no repair call: the precheck must not add work to
    # an export that does not need it.
    calls.clear()
    monkeypatch.setattr(
        scene_review,
        "collect_scene_review_targets",
        lambda _timeline: ([], []),
    )
    asyncio.run(routes._preflight_scene_review(services, "project-1", "main"))
    assert not calls

    # An exhausted budget now also refuses admission (review #243 P1-3
    # remaining half): wait_for cancels ``auto_review_stale_scenes``, so
    # the scenes it had not reached stay blocked, the drive's scene gate
    # still rejects the export, and no Task exists to carry that later
    # failure. Admitting anyway reproduced the 202-plus-phantom-Task
    # defect the whole preflight was introduced to close.
    monkeypatch.setattr(
        scene_review,
        "collect_scene_review_targets",
        lambda _timeline: (["scene:hall"], []),
    )

    async def slow(*_args, **_kwargs):
        calls.append("slow")
        await asyncio.sleep(30)

    monkeypatch.setattr(scene_review, "auto_review_stale_scenes", slow)
    monkeypatch.setattr(routes, "ADMISSION_REVIEW_BUDGET_SECONDS", 0.01)
    from domain.errors import ConflictError

    with pytest.raises(ConflictError) as timeout_refusal:
        asyncio.run(
            routes._preflight_scene_review(services, "project-1", "main"),
        )
    assert calls == ["slow"]
    assert timeout_refusal.value.details["code"] == "SCENE_REVIEW_TIMEOUT"
    assert timeout_refusal.value.details["scenes"] == ["scene:hall"]


def test_manual_render_routes_stale_scenes_to_repair_before_dispatch(
    tmp_path,
    monkeypatch,
    run_scenario,
) -> None:
    app, services, snapshot, _bootstrap = _app(tmp_path)
    scene = SceneLedgerRow(
        scene_id="scene-1",
        element_ids=["el-1"],
        status="locked",
        locked_fingerprint="sha256:stale",
    )
    _project, timeline = _timeline_with_scene(edit_plan=_plan([scene]))
    timeline.elements_by_id["el-1"].render_source = SourceVersionRenderSource(
        version_id="source-1",
        source_out_tick=1000,
    )
    source_url = "https://cdn.test/source.mp4"
    project = snapshot.project.model_copy(
        deep=True, update={"generation": snapshot.generation + 1}
    )
    project.timelines.items["timeline:main"] = timeline
    project.assets.source_versions_by_id["source-1"] = SourceAssetVersion(
        version_id="source-1",
        logical_asset_id="source",
        name="source.mp4",
        checksum=hashlib.sha256(source_url.encode()).hexdigest(),
        media_kind="video",
        media_type="video/mp4",
        duration_seconds=1,
        created_at=project.created_at,
        metadata={
            "sourceKind": "remote_url",
            "checksumKind": "source_url_sha256",
            "publicSourceUrl": source_url,
        },
    )
    services.projects.replace("project-1", project, snapshot.etag)
    with pytest.raises(ValidationError, match="需重审的场景: scene-1"):
        local_execution.validate_local_media_execution(
            services,
            project_id="project-1",
            command=CreatorCommandType.COMPOSE_FINAL_VIDEO,
            target_ref="timeline:timeline:main",
        )

    repair = AsyncMock(return_value=["scene-1"])
    dispatch = AsyncMock()
    monkeypatch.setattr(
        scene_review_module,
        "auto_review_stale_scenes",
        repair,
    )
    monkeypatch.setattr(
        local_execution,
        "execute_file_local_media_command",
        dispatch,
    )

    async def scenario(client):
        return await client.post(
            "/projects/project-1/timelines/timeline%3Amain/render",
            headers={"Idempotency-Key": "stale-scene"},
        )

    response = run_scenario(app, scenario)
    assert response.status_code == 409, response.text
    assert response.json()["details"] == {
        "code": "SCENE_REVIEW_INCOMPLETE",
        "scenes": ["scene-1"],
    }
    repair.assert_awaited_once()
    assert repair.await_args.kwargs["timeline_ref"] == "timeline:timeline:main"
    dispatch.assert_not_awaited()


def test_delegated_compose_stays_ready_after_a_superseded_final(
    tmp_path,
    api_request,
    monkeypatch,
) -> None:
    """Delegated (YOLO) keeps auto-compose, only co-creation pauses.

    Same patch as the co-creation case; the difference is the mode.
    Without this second case the STALE branch could silently be applied
    to YOLO runs too, and the whole point of delegated mode --
    unattended delivery to the final cut -- would quietly disappear.
    """

    from models import config as models_config
    from services.file_agent_runtime.work_graph import derive_work_graph

    monkeypatch.setattr(
        models_config,
        "get_creation_checkpoint_mode",
        lambda: models_config.CREATION_CHECKPOINT_SKIP,
    )
    monkeypatch.setattr(
        models_config,
        "get_execution_mode",
        lambda: models_config.EXECUTION_MODE_DELEGATED,
    )

    saved, _impact = _select_historical_video(tmp_path, api_request)
    graph = derive_work_graph(saved)
    # Delegated mode still auto-composes: the superseded final is READY,
    # not STALE, so the scheduler picks it up without a user press.
    assert graph.by_id["compose:timeline:main"].status.value == "ready"
