# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name
"""状态层:四张表都要真的能用,清库必须真删。"""

from __future__ import annotations

import sqlite3

import pytest

from ivb_player.state.store import (
    ANONYMOUS_USER_ID,
    ProgressStore,
    ProjectRecord,
    StateStore,
)

PROJECT = "project-smoke-0001"


@pytest.fixture
def store(tmp_path):
    return ProgressStore(tmp_path / "state.db")


def tables(db_path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        ).fetchall()
    return {row[0] for row in rows}


def test_expected_tables_exist(tmp_path):
    ProgressStore(tmp_path / "state.db")
    found = tables(tmp_path / "state.db")
    assert {
        "progress",
        "visits",
        "endings",
        "choice_stats",
        "projects",
    } <= found
    # v1 不做鉴权,也不建无消费方的死表。
    assert not {"users", "tokens", "variables"} & found


def test_visit_records_the_edge_it_came_in_by(store):
    store.record_visit(PROJECT, "timeline:open")
    store.record_visit(
        PROJECT,
        "timeline:counter",
        choice_edge="edge:go_counter",
    )
    trail = store.trail(PROJECT)
    assert [row["timeline_id"] for row in trail] == [
        "timeline:open",
        "timeline:counter",
    ]
    assert trail[0]["choice_edge"] is None
    assert trail[1]["choice_edge"] == "edge:go_counter"


def test_visited_is_first_visit_order(store):
    # entered_at 是秒级,同秒内的多次访问必须按插入序(id)定序。
    for timeline in ("timeline:open", "timeline:counter", "timeline:open"):
        store.record_visit(PROJECT, timeline)
    assert store.visited(PROJECT) == ["timeline:open", "timeline:counter"]


def test_watch_seconds_accumulate_on_latest_row(store):
    store.record_visit(PROJECT, "timeline:open")
    store.commit_watch_time(PROJECT, "timeline:open", 6.5)
    store.record_visit(PROJECT, "timeline:open")
    store.commit_watch_time(PROJECT, "timeline:open", 3.25)
    trail = store.trail(PROJECT)
    assert trail[0]["watched_seconds"] == pytest.approx(6.5)
    assert trail[1]["watched_seconds"] == pytest.approx(3.25)
    assert store.stats(PROJECT)["watched_seconds"] == pytest.approx(9.75)


def test_zero_watch_time_is_a_noop(store):
    store.record_visit(PROJECT, "timeline:open")
    assert store.commit_watch_time(PROJECT, "timeline:open", 0) == 0


def test_unlock_ending_reports_first_time_only(store):
    assert store.unlock_ending(PROJECT, "timeline:good_end") is True
    assert store.unlock_ending(PROJECT, "timeline:good_end") is False
    assert store.endings(PROJECT) == ["timeline:good_end"]


def test_choice_stats_count_up(store):
    store.record_choice(PROJECT, "timeline:open", "edge:go_counter")
    store.record_choice(PROJECT, "timeline:open", "edge:go_counter")
    store.record_choice(PROJECT, "timeline:open", "edge:go_storage")
    rows = store.choice_stats(PROJECT)
    by_edge = {row["edge_ref"]: row["count"] for row in rows}
    assert by_edge == {"edge:go_counter": 2, "edge:go_storage": 1}


def test_current_timeline_follows_the_last_write(store):
    store.record_visit(PROJECT, "timeline:open")
    store.record_visit(
        PROJECT,
        "timeline:counter",
        choice_edge="edge:go_counter",
    )
    assert store.current_timeline(PROJECT) == "timeline:counter"


def test_started_at_survives_updates(store):
    store.touch_progress(PROJECT, "timeline:open")
    first = store.progress(PROJECT).started_at
    store.touch_progress(PROJECT, "timeline:counter")
    assert store.progress(PROJECT).started_at == first


def test_clear_actually_deletes_everything(store):
    store.record_visit(PROJECT, "timeline:open")
    store.commit_watch_time(PROJECT, "timeline:open", 5)
    store.record_choice(PROJECT, "timeline:open", "edge:go_counter")
    store.unlock_ending(PROJECT, "timeline:good_end")
    deleted = store.clear(PROJECT)
    assert deleted == {
        "progress": 1,
        "visits": 1,
        "endings": 1,
        "choice_stats": 1,
        "user_id": ANONYMOUS_USER_ID,
        "project_id": PROJECT,
    }
    assert store.visited(PROJECT) == []
    assert store.endings(PROJECT) == []
    assert store.choice_stats(PROJECT) == []
    assert store.current_timeline(PROJECT) == ""


def test_projects_are_isolated(store):
    store.record_visit(PROJECT, "timeline:open")
    store.record_visit("other-project", "timeline:x")
    assert store.visited(PROJECT) == ["timeline:open"]
    assert store.visited("other-project") == ["timeline:x"]
    store.clear(PROJECT)
    assert store.visited("other-project") == ["timeline:x"]


def test_store_is_reusable_across_connections(tmp_path):
    first = ProgressStore(tmp_path / "state.db")
    first.record_visit(PROJECT, "timeline:open")
    second = ProgressStore(tmp_path / "state.db")
    assert second.visited(PROJECT) == ["timeline:open"]
    assert second.stats(PROJECT)["visits"] == 1
    # user_id 现在是 TEXT 哨兵值;真实 user_id 由上游传入(见 service-design §1)。
    assert second.user_id == ANONYMOUS_USER_ID


def test_progressstore_satisfies_statestore_protocol(tmp_path):
    # 接缝契约:SQLite 实现必须满足 StateStore 协议(切 PG 时新增同协议实现)。
    assert isinstance(ProgressStore(tmp_path / "state.db"), StateStore)


def test_upsert_project_registers_then_updates_keeping_owner(store):
    store.upsert_project(
        ProjectRecord(
            project_id=PROJECT,
            owner_user_id="alice",
            title="深夜便利店",
            synopsis="四名夜班店员",
            node_count=5,
            ending_count=2,
            interaction_count=1,
            storage_path=f"bundles/{PROJECT}",
        ),
    )
    record = store.get_project(PROJECT)
    assert record is not None
    assert record.owner_user_id == "alice"
    assert record.title == "深夜便利店"
    assert record.node_count == 5
    assert record.created_at > 0
    first_created = record.created_at

    # 重复上传:改标题/计数,owner 与 created_at 保持不变(service-design §1.5)。
    store.upsert_project(
        ProjectRecord(
            project_id=PROJECT,
            owner_user_id="bob",
            title="改名了",
            node_count=7,
            ending_count=3,
            interaction_count=2,
            storage_path=f"bundles/{PROJECT}",
        ),
    )
    updated = store.get_project(PROJECT)
    assert updated is not None
    assert updated.owner_user_id == "alice"
    assert updated.title == "改名了"
    assert updated.node_count == 7
    assert updated.created_at == first_created
    assert updated.updated_at >= first_created


def test_list_projects_scopes_mine_versus_all(store):
    store.upsert_project(ProjectRecord(project_id="p1", owner_user_id="alice"))
    store.upsert_project(ProjectRecord(project_id="p2", owner_user_id="bob"))
    everything = store.list_projects()
    assert {row.project_id for row in everything} == {"p1", "p2"}
    mine = store.list_projects(owner_user_id="alice")
    assert [row.project_id for row in mine] == ["p1"]
    assert store.get_project("ghost") is None
