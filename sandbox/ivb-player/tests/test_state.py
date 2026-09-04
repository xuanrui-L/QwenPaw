# -*- coding: utf-8 -*-
"""状态层:四张表都要真的能用,清库必须真删。"""

from __future__ import annotations

import sqlite3

import pytest

from ivb_player.state.store import ANONYMOUS_USER_ID, ProgressStore

BUNDLE = "project-smoke-0001"


@pytest.fixture
def store(tmp_path):
    return ProgressStore(tmp_path / "state.db")


def tables(db_path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        ).fetchall()
    return {row[0] for row in rows}


def test_only_the_four_live_tables_exist(tmp_path):
    ProgressStore(tmp_path / "state.db")
    found = tables(tmp_path / "state.db")
    assert {"progress", "visits", "endings", "choice_stats"} <= found
    # v1 不做鉴权,也不建无消费方的死表。
    assert not {"users", "tokens", "variables"} & found


def test_visit_records_the_edge_it_came_in_by(store):
    store.record_visit(BUNDLE, "timeline:open")
    store.record_visit(
        BUNDLE, "timeline:counter", choice_edge="edge:go_counter"
    )
    trail = store.trail(BUNDLE)
    assert [row["timeline_id"] for row in trail] == [
        "timeline:open",
        "timeline:counter",
    ]
    assert trail[0]["choice_edge"] is None
    assert trail[1]["choice_edge"] == "edge:go_counter"


def test_visited_is_first_visit_order(store):
    # entered_at 是秒级,同秒内的多次访问必须按插入序(id)定序。
    for timeline in ("timeline:open", "timeline:counter", "timeline:open"):
        store.record_visit(BUNDLE, timeline)
    assert store.visited(BUNDLE) == ["timeline:open", "timeline:counter"]


def test_watch_seconds_accumulate_on_latest_row(store):
    store.record_visit(BUNDLE, "timeline:open")
    store.commit_watch_time(BUNDLE, "timeline:open", 6.5)
    store.record_visit(BUNDLE, "timeline:open")
    store.commit_watch_time(BUNDLE, "timeline:open", 3.25)
    trail = store.trail(BUNDLE)
    assert trail[0]["watched_seconds"] == pytest.approx(6.5)
    assert trail[1]["watched_seconds"] == pytest.approx(3.25)
    assert store.stats(BUNDLE)["watched_seconds"] == pytest.approx(9.75)


def test_zero_watch_time_is_a_noop(store):
    store.record_visit(BUNDLE, "timeline:open")
    assert store.commit_watch_time(BUNDLE, "timeline:open", 0) == 0


def test_unlock_ending_reports_first_time_only(store):
    assert store.unlock_ending(BUNDLE, "timeline:good_end") is True
    assert store.unlock_ending(BUNDLE, "timeline:good_end") is False
    assert store.endings(BUNDLE) == ["timeline:good_end"]


def test_choice_stats_count_up(store):
    store.record_choice(BUNDLE, "timeline:open", "edge:go_counter")
    store.record_choice(BUNDLE, "timeline:open", "edge:go_counter")
    store.record_choice(BUNDLE, "timeline:open", "edge:go_storage")
    rows = store.choice_stats(BUNDLE)
    by_edge = {row["edge_ref"]: row["count"] for row in rows}
    assert by_edge == {"edge:go_counter": 2, "edge:go_storage": 1}


def test_current_timeline_follows_the_last_write(store):
    store.record_visit(BUNDLE, "timeline:open")
    store.record_visit(
        BUNDLE, "timeline:counter", choice_edge="edge:go_counter"
    )
    assert store.current_timeline(BUNDLE) == "timeline:counter"


def test_started_at_survives_updates(store):
    store.touch_progress(BUNDLE, "timeline:open")
    first = store.progress(BUNDLE).started_at
    store.touch_progress(BUNDLE, "timeline:counter")
    assert store.progress(BUNDLE).started_at == first


def test_clear_actually_deletes_everything(store):
    store.record_visit(BUNDLE, "timeline:open")
    store.commit_watch_time(BUNDLE, "timeline:open", 5)
    store.record_choice(BUNDLE, "timeline:open", "edge:go_counter")
    store.unlock_ending(BUNDLE, "timeline:good_end")
    deleted = store.clear(BUNDLE)
    assert deleted == {
        "progress": 1,
        "visits": 1,
        "endings": 1,
        "choice_stats": 1,
        "user_id": ANONYMOUS_USER_ID,
        "bundle_id": BUNDLE,
    }
    assert store.visited(BUNDLE) == []
    assert store.endings(BUNDLE) == []
    assert store.choice_stats(BUNDLE) == []
    assert store.current_timeline(BUNDLE) == ""


def test_bundles_are_isolated(store):
    store.record_visit(BUNDLE, "timeline:open")
    store.record_visit("other-project", "timeline:x")
    assert store.visited(BUNDLE) == ["timeline:open"]
    assert store.visited("other-project") == ["timeline:x"]
    store.clear(BUNDLE)
    assert store.visited("other-project") == ["timeline:x"]


def test_store_is_reusable_across_connections(tmp_path):
    first = ProgressStore(tmp_path / "state.db")
    first.record_visit(BUNDLE, "timeline:open")
    second = ProgressStore(tmp_path / "state.db")
    assert second.visited(BUNDLE) == ["timeline:open"]
    assert second.stats(BUNDLE)["visits"] == 1
    # user_id 不外露:v1 恒为 1,但列保留以便日后加鉴权不迁库。
    assert second.user_id == ANONYMOUS_USER_ID
