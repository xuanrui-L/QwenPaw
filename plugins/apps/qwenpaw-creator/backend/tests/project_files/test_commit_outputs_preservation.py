# -*- coding: utf-8 -*-
"""Runtime-owned element ``outputs`` survive non-runtime commits.

Field run 2026-08-19 (project 05580c2e): the agent replaced a whole
timeline element via ``patch_project`` to edit its creation, wiping the
runtime-written ``outputs`` bindings. The surviving artifact slot then
made the R2V node permanently undispatchable ("尚未选择 storyboard
ArtifactVersion") with no recovery goal — a silent deadlock. The commit
boundary now restores clobbered bindings for every non-RUNTIME_TASK
origin.
"""

from __future__ import annotations

import copy

import pytest

from services.project_files.commit import _preserve_element_outputs

pytestmark = pytest.mark.unit


def _doc(outputs: dict | None, *, element_id: str = "elem-1") -> dict:
    return {
        "timelines": {
            "items": {
                "timeline:main": {
                    "elements_by_id": {
                        element_id: {
                            "element_id": element_id,
                            "outputs": outputs,
                        },
                    },
                },
            },
        },
    }


_BINDINGS = {
    "storyboard": {"slot_id": "element:elem-1:storyboard"},
    "main": {"slot_id": "element:elem-1:main"},
}


def test_clobbered_outputs_are_restored() -> None:
    base = _doc(copy.deepcopy(_BINDINGS))
    candidate = _doc({})

    restored = _preserve_element_outputs(base, candidate)

    assert restored == [
        "/timelines/items/timeline:main/elements_by_id/elem-1/outputs",
    ]
    element = candidate["timelines"]["items"]["timeline:main"][
        "elements_by_id"
    ]["elem-1"]
    assert element["outputs"] == _BINDINGS


def test_partial_drop_is_restored_wholesale() -> None:
    base = _doc(copy.deepcopy(_BINDINGS))
    candidate = _doc(
        {"storyboard": {"slot_id": "element:elem-1:storyboard"}},
    )

    restored = _preserve_element_outputs(base, candidate)

    assert len(restored) == 1
    element = candidate["timelines"]["items"]["timeline:main"][
        "elements_by_id"
    ]["elem-1"]
    assert element["outputs"] == _BINDINGS


def test_identical_outputs_record_nothing() -> None:
    base = _doc(copy.deepcopy(_BINDINGS))
    candidate = _doc(copy.deepcopy(_BINDINGS))

    assert not _preserve_element_outputs(base, candidate)


def test_empty_base_stays_writable_for_repairs() -> None:
    """A lost binding must be re-addable (health monitor / manual fix)."""

    base = _doc({})
    repair = {"storyboard": {"slot_id": "element:elem-1:storyboard"}}
    candidate = _doc(copy.deepcopy(repair))

    assert not _preserve_element_outputs(base, candidate)
    element = candidate["timelines"]["items"]["timeline:main"][
        "elements_by_id"
    ]["elem-1"]
    assert element["outputs"] == repair


def test_new_element_is_untouched() -> None:
    base = {"timelines": {"items": {"timeline:main": {"elements_by_id": {}}}}}
    candidate = _doc({"main": {"slot_id": "element:elem-1:main"}})

    assert not _preserve_element_outputs(base, candidate)


def test_deleted_element_is_ignored() -> None:
    base = _doc(copy.deepcopy(_BINDINGS))
    candidate = {
        "timelines": {"items": {"timeline:main": {"elements_by_id": {}}}},
    }

    assert not _preserve_element_outputs(base, candidate)


def test_null_outputs_treated_as_clobbered() -> None:
    base = _doc(copy.deepcopy(_BINDINGS))
    candidate = _doc(None)

    restored = _preserve_element_outputs(base, candidate)

    assert len(restored) == 1
    element = candidate["timelines"]["items"]["timeline:main"][
        "elements_by_id"
    ]["elem-1"]
    assert element["outputs"] == _BINDINGS
