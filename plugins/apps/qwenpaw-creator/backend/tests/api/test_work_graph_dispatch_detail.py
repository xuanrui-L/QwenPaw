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

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from api.work_graph_routes import _task_admission_detail
from domain.enums import TaskStatus
from services.file_agent_runtime.work_scheduler import (
    _abandoned_queued_task_ids,
)

import asyncio

import hashlib
from fastapi import FastAPI
from api.dependencies import creator_error_handler, project_file_services
from api.project_file_routes import router
from domain.errors import CreatorError
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project
from test_project_file_api import PROJECT_URL, _patch_payload

from services.project_files.models import SceneLedgerRow
from services.render_review import scene_review as scene_review_module
from render_review.test_scene_review import _plan, _timeline_with_scene

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


def test_preflight_scene_review_refuses_when_scenes_are_still_blocked(
    monkeypatch,
) -> None:
    """``auto_review`` returning a non-empty list is also a refusal.

    The repair gathers per-scene work with ``return_exceptions=True``, so a
    VLM outage that used to escape as an exception now lands in the returned
    ``remaining`` list. The route used to discard that value, and the drive's
    scene gate would still reject the compose -- leaving the user holding a
    202 receipt for a Task id that never materialised (review #243 P1).
    """

    # pylint: disable=protected-access
    import api.file_execution_routes as routes
    from domain.errors import ConflictError
    from services.render_review import scene_review

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

    async def swallows_one(*_args, **_kwargs):
        # The real auto_review gathers with return_exceptions=True and
        # puts the still-broken scene id into its result, exactly like this.
        return ["scene:hall"]

    monkeypatch.setattr(scene_review, "auto_review_stale_scenes", swallows_one)

    with pytest.raises(ConflictError) as captured:
        asyncio.run(
            routes._preflight_scene_review(services, "project-1", "main"),
        )

    assert captured.value.details == {
        "code": "SCENE_REVIEW_INCOMPLETE",
        "scenes": ["scene:hall"],
    }
    assert "scene:hall" in str(captured.value)


def test_delegated_compose_stays_ready_after_a_superseded_final(
    tmp_path,
    run_scenario,
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

    project = Project.new(project_id="project-1", name="Delegated compose")
    document = project.model_dump(mode="json")
    document["timelines"]["items"]["timeline:main"]["elements_by_id"] = {
        "clip": {
            "element_id": "clip",
            "label": "Clip",
            "span": {"start_tick": 0, "duration_tick": 4000},
            "location": {},
            "creation": {"type": "t2v", "video_prompt": "Current prompt"},
            "outputs": {"main": {"slot_id": "element:clip:main"}},
            "render_source": {
                "type": "element_output",
                "element_id": "clip",
                "output_name": "main",
            },
        },
    }
    assets = document["assets"]
    for kind, owner, slot_id, versions, current in (
        (
            "element_video",
            "element:clip",
            "element:clip:main",
            ["old", "new"],
            "new",
        ),
        (
            "final_video",
            "timeline:timeline:main",
            "timeline:timeline:main:render",
            ["final"],
            "final",
        ),
    ):
        assets["artifact_slots_by_id"][slot_id] = {
            "slot_id": slot_id,
            "kind": kind,
            "owner_ref": owner,
            "version_ids": versions,
            "selected_version_id": current,
        }
        for version_id in versions:
            checksum = hashlib.sha256(version_id.encode()).hexdigest()
            assets["files_by_id"][version_id] = {
                "file_id": version_id,
                "kind": "artifact_payload",
                "relative_uri": f"assets/{version_id}.mp4",
                "sha256": checksum,
                "size_bytes": len(version_id),
                "media_type": "video/mp4",
                "created_at": "2026-09-17T00:00:00Z",
            }
            assets["artifact_versions_by_id"][version_id] = {
                "version_id": version_id,
                "name": version_id,
                "slot_id": slot_id,
                "kind": kind,
                "owner_ref": owner,
                "file_id": version_id,
                "checksum": checksum,
                "based_on_generation": 0,
                "stale": version_id == "old",
                "stale_reason": "Old inputs" if version_id == "old" else None,
                "created_at": "2026-09-17T00:00:00Z",
            }

    def initialize(root):
        (root / "assets").mkdir(exist_ok=True)
        for version_id in ("old", "new", "final"):
            (root / f"assets/{version_id}.mp4").write_bytes(
                version_id.encode(),
            )

    services = CreatorFileServices.create(tmp_path.resolve())
    base = services.projects.create(
        Project.model_validate(document),
        initialize_staged_project=initialize,
    )
    app = FastAPI()
    app.add_exception_handler(CreatorError, creator_error_handler)
    app.include_router(router)
    app.dependency_overrides[project_file_services] = lambda: services
    payload = _patch_payload(
        "choose-history",
        "old",
        base,
        path=(
            "/assets/artifact_slots_by_id/element:clip:main/"
            "selected_version_id"
        ),
        expected="new",
    )

    async def scenario(client):
        return await client.patch(PROJECT_URL, json=payload)

    response = run_scenario(app, scenario)
    assert response.status_code == 200, response.text
    saved = services.projects.read("project-1").project
    graph = derive_work_graph(saved)
    # Delegated mode still auto-composes: the superseded final is READY,
    # not STALE, so the scheduler picks it up without a user press.
    assert graph.by_id["compose:timeline:main"].status.value == "ready"


def test_precheck_can_skip_the_gate_that_auto_rereview_repairs(
    monkeypatch,
) -> None:
    """An expired lock may only block the render itself, never the precheck.

    ``auto_review_stale_scenes`` repairs stale locks inside the dispatch
    path; a caller that runs ahead of it must resolve without the gate, or
    every user-initiated export is rejected with 422 before the repair ever
    gets a turn.
    """
    from domain.enums import CreatorCommandType
    from services.media_files import local_execution

    stale = SceneLedgerRow(
        scene_id="scene-1",
        element_ids=["el-1"],
        status="locked",
        locked_fingerprint="sha256:stale",
    )
    project, _timeline = _timeline_with_scene(edit_plan=_plan([stale]))
    gated: list[str] = []
    monkeypatch.setattr(
        scene_review_module,
        "validate_scene_ledger_locked",
        lambda _timeline: gated.append("gate"),
    )
    monkeypatch.setattr(
        local_execution,
        "_timeline_execution",
        lambda **_kwargs: "resolved",
    )

    def _resolve(**overrides):
        return local_execution._resolve_execution(
            snapshot=SimpleNamespace(project=project),
            command=CreatorCommandType.COMPOSE_FINAL_VIDEO,
            target_ref="timeline:timeline:main",
            arguments={},
            **overrides,
        )

    assert _resolve() == "resolved"
    assert gated == ["gate"]
    gated.clear()
    assert _resolve(enforce_scene_gate=False) == "resolved"
    assert not gated
