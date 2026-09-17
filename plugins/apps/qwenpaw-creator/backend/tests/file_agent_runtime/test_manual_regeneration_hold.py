# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Manual regeneration hold: a human re-roll never auto-cascades downstream.

A person clicking 重新生成 on an upstream node issues a node-scoped
instruction. Under the unattended ladder (``execution_authorization=
allow_all``) the scheduler would otherwise mark every downstream node
STALE and auto-cascade a paid regeneration (storyboard -> video ->
compose). These tests pin the registry semantics (release the clicked
node, hold its transitive dependents) and the two scheduler guarantees:
a held downstream is skipped on the automatic tick, and a
scheduler-driven dispatch never records a hold, so a hands-off 成片 run
still cascades end to end.
"""
from __future__ import annotations

import asyncio

import pytest

from services.file_agent_runtime import (
    manual_regeneration_hold,
    work_scheduler,
)
from services.file_agent_runtime.work_graph import (
    WorkGraph,
    WorkNode,
    WorkNodeStatus,
)
from services.file_agent_runtime.work_scheduler import WorkGraphScheduler
from services.project_files import frontend_edit_hold

pytestmark = pytest.mark.unit

PROJECT_ID = "scheduler-project"


@pytest.fixture(autouse=True)
def _clean_holds():
    manual_regeneration_hold.clear()
    frontend_edit_hold.clear()
    yield
    manual_regeneration_hold.clear()
    frontend_edit_hold.clear()


def _chain_nodes() -> tuple[WorkNode, ...]:
    """storyboard -> video -> compose for a single element."""
    return (
        WorkNode(
            node_id="storyboard:elem:a",
            kind="storyboard",
            label="分镜",
            status=WorkNodeStatus.DONE,
            target_ref="element:elem:a",
            command="GENERATE_STORYBOARD_IMAGE",
        ),
        WorkNode(
            node_id="video:elem:a",
            kind="video",
            label="视频",
            status=WorkNodeStatus.STALE,
            deps=("storyboard:elem:a",),
            target_ref="element:elem:a",
            command="GENERATE_R2V_VIDEO",
            regeneration_of="asset:old-video",
        ),
        WorkNode(
            node_id="compose:timeline:main",
            kind="compose",
            label="成片",
            status=WorkNodeStatus.STALE,
            deps=("video:elem:a",),
            target_ref="timeline:main",
            command="COMPOSE_FINAL_VIDEO",
            regeneration_of="asset:old-compose",
        ),
    )


def test_manual_storyboard_holds_whole_downstream_chain() -> None:
    manual_regeneration_hold.note_manual_regeneration(
        PROJECT_ID,
        "storyboard:elem:a",
        _chain_nodes(),
    )
    assert manual_regeneration_hold.held_nodes(PROJECT_ID) == {
        "video:elem:a",
        "compose:timeline:main",
    }
    # The clicked node stays dispatchable; only downstream is suppressed.
    assert not manual_regeneration_hold.is_held(
        PROJECT_ID,
        "storyboard:elem:a",
    )
    assert manual_regeneration_hold.is_held(PROJECT_ID, "video:elem:a")
    assert manual_regeneration_hold.is_held(
        PROJECT_ID,
        "compose:timeline:main",
    )


def test_manual_video_releases_video_and_keeps_compose() -> None:
    nodes = _chain_nodes()
    manual_regeneration_hold.note_manual_regeneration(
        PROJECT_ID,
        "storyboard:elem:a",
        nodes,
    )
    # Operator now re-rolls the video by hand: it is released, and its own
    # downstream (compose) becomes the new held frontier.
    manual_regeneration_hold.note_manual_regeneration(
        PROJECT_ID,
        "video:elem:a",
        nodes,
    )
    assert manual_regeneration_hold.held_nodes(PROJECT_ID) == {
        "compose:timeline:main",
    }
    assert not manual_regeneration_hold.is_held(PROJECT_ID, "video:elem:a")


def test_manual_compose_leaf_holds_nothing() -> None:
    manual_regeneration_hold.note_manual_regeneration(
        PROJECT_ID,
        "compose:timeline:main",
        _chain_nodes(),
    )
    # compose is the sink: no dependents, and the empty bucket is dropped.
    assert manual_regeneration_hold.held_nodes(PROJECT_ID) == set()
    assert PROJECT_ID not in manual_regeneration_hold._holds


def test_repeat_click_is_idempotent() -> None:
    nodes = _chain_nodes()
    for _ in range(3):
        manual_regeneration_hold.note_manual_regeneration(
            PROJECT_ID,
            "storyboard:elem:a",
            nodes,
        )
    assert manual_regeneration_hold.held_nodes(PROJECT_ID) == {
        "video:elem:a",
        "compose:timeline:main",
    }


def test_holds_are_project_scoped() -> None:
    manual_regeneration_hold.note_manual_regeneration(
        "p1",
        "storyboard:elem:a",
        _chain_nodes(),
    )
    assert manual_regeneration_hold.is_held("p1", "video:elem:a")
    assert not manual_regeneration_hold.is_held("p2", "video:elem:a")
    assert not manual_regeneration_hold.is_held(PROJECT_ID, "video:elem:a")


def test_clear_scopes_to_one_project() -> None:
    nodes = _chain_nodes()
    manual_regeneration_hold.note_manual_regeneration(
        "p1",
        "storyboard:elem:a",
        nodes,
    )
    manual_regeneration_hold.note_manual_regeneration(
        "p2",
        "storyboard:elem:a",
        nodes,
    )
    manual_regeneration_hold.clear("p1")
    assert manual_regeneration_hold.held_nodes("p1") == set()
    assert manual_regeneration_hold.is_held("p2", "video:elem:a")


def test_clear_all_drops_every_project() -> None:
    nodes = _chain_nodes()
    manual_regeneration_hold.note_manual_regeneration(
        "p1",
        "storyboard:elem:a",
        nodes,
    )
    manual_regeneration_hold.note_manual_regeneration(
        "p2",
        "storyboard:elem:a",
        nodes,
    )
    manual_regeneration_hold.clear()
    assert manual_regeneration_hold.held_nodes("p1") == set()
    assert manual_regeneration_hold.held_nodes("p2") == set()
    assert not manual_regeneration_hold._holds


def test_blank_ids_are_ignored() -> None:
    nodes = _chain_nodes()
    manual_regeneration_hold.note_manual_regeneration(
        "",
        "storyboard:elem:a",
        nodes,
    )
    manual_regeneration_hold.note_manual_regeneration(PROJECT_ID, "", nodes)
    assert not manual_regeneration_hold._holds


def _element_graph() -> WorkGraph:
    """A single READY storyboard, mirroring the frontend-hold harness."""
    return WorkGraph(
        nodes=(
            WorkNode(
                node_id="storyboard:elem:a",
                kind="storyboard",
                label="分镜",
                status=WorkNodeStatus.READY,
                target_ref="element:elem:a",
                command="GENERATE_STORYBOARD_IMAGE",
            ),
        ),
        generation=1,
    )


def _upstream_graph() -> WorkGraph:
    """script (DONE) -> storyboard (READY).

    A manual re-roll of the upstream script holds the storyboard, so the
    scheduler tick must skip it until the operator regenerates it too.
    """
    return WorkGraph(
        nodes=(
            WorkNode(
                node_id="script:timeline:main",
                kind="script",
                label="剧本",
                status=WorkNodeStatus.DONE,
                target_ref="timeline:main",
                command="GENERATE_SCRIPT",
            ),
            WorkNode(
                node_id="storyboard:elem:a",
                kind="storyboard",
                label="分镜",
                status=WorkNodeStatus.READY,
                deps=("script:timeline:main",),
                target_ref="element:elem:a",
                command="GENERATE_STORYBOARD_IMAGE",
            ),
        ),
        generation=1,
    )


class _RecordingDispatch:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, services, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


def _scheduler_env(
    tmp_path,
    monkeypatch,
    graph: WorkGraph | None = None,
) -> tuple[WorkGraphScheduler, "_RecordingDispatch"]:
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    # Reuse the established scheduler harness: real services + a fabricated
    # element graph (the visual fixtures cannot produce element nodes).
    from services.project_files.facade import CreatorFileServices
    from services.project_files.models import Project

    services = CreatorFileServices.create(tmp_path.resolve())
    services.projects.create(
        Project.new(project_id=PROJECT_ID, name="Hold"),
    )
    monkeypatch.setattr(
        "services.file_agent_runtime.work_scheduler."
        "get_execution_authorization_mode",
        lambda: "allow_all",
    )
    resolved = graph or _element_graph()
    monkeypatch.setattr(
        work_scheduler,
        "derive_work_graph",
        lambda project, tasks=(), *, media_models=None: resolved,
    )
    dispatch = _RecordingDispatch()
    scheduler = WorkGraphScheduler(services, image_dispatch=dispatch)
    return scheduler, dispatch


async def _drain() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


def test_tick_skips_manual_held_downstream_then_dispatches(
    tmp_path,
    monkeypatch,
):
    scheduler, dispatch = _scheduler_env(
        tmp_path,
        monkeypatch,
        _upstream_graph(),
    )

    async def scenario():
        # A person re-rolled the upstream script by hand; the route holds
        # its transitive downstream (the storyboard).
        manual_regeneration_hold.note_manual_regeneration(
            PROJECT_ID,
            "script:timeline:main",
            _upstream_graph().nodes,
        )
        assert manual_regeneration_hold.is_held(
            PROJECT_ID,
            "storyboard:elem:a",
        )
        await scheduler.tick(PROJECT_ID)
        await _drain()
        assert not dispatch.calls
        # Operator regenerates the storyboard by hand -> released -> runs.
        manual_regeneration_hold.clear(PROJECT_ID)
        await scheduler.tick(PROJECT_ID)
        await _drain()
        await scheduler.shutdown()

    asyncio.run(scenario())

    assert len(dispatch.calls) == 1
    assert dispatch.calls[0]["target_ref"] == "element:elem:a"


def test_automatic_tick_records_no_hold(tmp_path, monkeypatch):
    """The unattended cascade must never populate the manual registry."""
    scheduler, dispatch = _scheduler_env(tmp_path, monkeypatch)

    async def scenario():
        await scheduler.tick(PROJECT_ID)
        await _drain()
        await scheduler.shutdown()

    asyncio.run(scenario())

    assert len(dispatch.calls) == 1
    # A scheduler-driven dispatch is not a human click, so nothing is held
    # and a fresh auto project still cascades to the end.
    assert manual_regeneration_hold.held_nodes(PROJECT_ID) == set()
