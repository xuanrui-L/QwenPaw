# -*- coding: utf-8 -*-
from __future__ import annotations

import copy

import pytest

from services.project_files.auto_snapshot import (
    _next_snapshot_id,
    _timeline_element_changes,
    auto_snapshot_timelines,
)

pytestmark = pytest.mark.unit

TL = "timeline:main"
ELEM = "elem:1"


def _minimal_project(
    *,
    timeline_id: str = TL,
    name: str = "主时间轴",
    elements: dict | None = None,
) -> dict:
    return {
        "timelines": {
            "items": {
                timeline_id: {
                    "timeline_id": timeline_id,
                    "name": name,
                    "description": "",
                    "ticks_per_second": 30,
                    "elements_by_id": elements or {},
                    "order": list((elements or {}).keys()),
                },
            },
            "order": [timeline_id],
        },
    }


def _element(elem_id: str, label: str = "Shot 1") -> dict:
    return {
        "element_id": elem_id,
        "label": label,
        "kind": "shot",
        "span": {"start_tick": 0, "end_tick": 30},
    }


def _elems(doc: dict, tid: str = TL) -> dict:
    return doc["timelines"]["items"][tid]["elements_by_id"]


class TestTimelineElementChanges:
    def test_no_changes(self):
        base = _minimal_project()
        candidate = copy.deepcopy(base)
        assert _timeline_element_changes(base, candidate) == set()

    def test_element_added(self):
        base = _minimal_project()
        candidate = copy.deepcopy(base)
        _elems(candidate)[ELEM] = _element(ELEM)
        assert _timeline_element_changes(base, candidate) == {TL}

    def test_element_removed(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})
        candidate = copy.deepcopy(base)
        del _elems(candidate)[ELEM]
        assert _timeline_element_changes(base, candidate) == {TL}

    def test_element_field_changed(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})
        candidate = copy.deepcopy(base)
        _elems(candidate)[ELEM]["label"] = "Updated Shot"
        assert _timeline_element_changes(base, candidate) == {TL}

    def test_name_only_change_not_detected(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})
        candidate = copy.deepcopy(base)
        candidate["timelines"]["items"][TL]["name"] = "Renamed"
        changes = _timeline_element_changes(base, candidate)
        assert TL not in changes

    def test_multiple_timelines_changed(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})
        base["timelines"]["items"]["timeline:b"] = {
            "timeline_id": "timeline:b",
            "name": "B",
            "description": "",
            "ticks_per_second": 30,
            "elements_by_id": {"elem:b1": _element("elem:b1")},
        }
        base["timelines"]["order"].append("timeline:b")

        candidate = copy.deepcopy(base)
        _elems(candidate)[ELEM]["label"] = "Changed"
        _elems(candidate, "timeline:b")["elem:b1"]["label"] = "Changed B"

        result = _timeline_element_changes(base, candidate)
        assert result == {TL, "timeline:b"}


class TestNextSnapshotId:
    def test_first_snapshot(self):
        items = {TL: {}}
        result = _next_snapshot_id(items, TL)
        assert result == "snapshot:timeline:main:1"

    def test_second_snapshot(self):
        items = {
            TL: {},
            "snapshot:timeline:main:1": {},
        }
        result = _next_snapshot_id(items, TL)
        assert result == "snapshot:timeline:main:2"

    def test_independent_per_timeline(self):
        items = {
            TL: {},
            "snapshot:timeline:main:1": {},
            "snapshot:timeline:main:2": {},
        }
        result = _next_snapshot_id(items, "timeline:b")
        assert result == "snapshot:timeline:b:1"


class TestAutoSnapshotTimelines:
    def test_no_change_no_snapshot(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})
        candidate = copy.deepcopy(base)
        auto_snapshot_timelines(base, candidate)
        assert len(candidate["timelines"]["items"]) == 1

    def test_empty_timeline_not_snapshotted(self):
        base = _minimal_project()
        candidate = copy.deepcopy(base)
        candidate["timelines"]["items"][TL]["name"] = "Changed"
        auto_snapshot_timelines(base, candidate)
        assert len(candidate["timelines"]["items"]) == 1

    def test_element_change_creates_snapshot(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})
        candidate = copy.deepcopy(base)
        _elems(candidate)[ELEM]["label"] = "Modified"

        auto_snapshot_timelines(base, candidate)

        items = candidate["timelines"]["items"]
        assert len(items) == 2
        sid = "snapshot:timeline:main:1"
        assert sid in items
        snapshot = items[sid]
        assert snapshot["timeline_id"] == sid
        assert "快照" in snapshot["name"]
        assert "主时间轴" in snapshot["name"]
        remapped = f"{sid}:{ELEM}"
        assert remapped in snapshot["elements_by_id"]
        assert snapshot["elements_by_id"][remapped]["label"] == "Modified"
        assert sid in candidate["timelines"]["order"]

    def test_snapshot_preserves_candidate_not_base(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})
        candidate = copy.deepcopy(base)
        _elems(candidate)[ELEM]["label"] = "Modified"

        auto_snapshot_timelines(base, candidate)

        sid = "snapshot:timeline:main:1"
        snapshot = candidate["timelines"]["items"][sid]
        remapped = f"{sid}:{ELEM}"
        assert snapshot["elements_by_id"][remapped]["label"] == "Modified"

    def test_original_timeline_reverted_to_base(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})
        candidate = copy.deepcopy(base)
        _elems(candidate)[ELEM]["label"] = "Modified"

        auto_snapshot_timelines(base, candidate)

        assert _elems(candidate)[ELEM]["label"] == "Shot 1"
        sid = "snapshot:timeline:main:1"
        snapshot = candidate["timelines"]["items"][sid]
        remapped = f"{sid}:{ELEM}"
        assert snapshot["elements_by_id"][remapped]["label"] == "Modified"

    def test_multiple_snapshots_increment(self):
        base = _minimal_project(elements={ELEM: _element(ELEM)})

        c1 = copy.deepcopy(base)
        _elems(c1)[ELEM]["label"] = "V2"
        auto_snapshot_timelines(base, c1)
        assert "snapshot:timeline:main:1" in c1["timelines"]["items"]

        base2 = copy.deepcopy(c1)
        c2 = copy.deepcopy(base2)
        _elems(c2)[ELEM]["label"] = "V3"
        auto_snapshot_timelines(base2, c2)
        items = c2["timelines"]["items"]
        assert "snapshot:timeline:main:1" in items
        assert "snapshot:timeline:main:2" in items
