# -*- coding: utf-8 -*-
# Pytest fixtures and contract probes retain exact types and private seams.
# pylint: disable=protected-access
# pylint: disable=unused-argument
"""A renewed storyboard replaces Image 1 without shifting other references."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from models import config
from services.media_files.r2v_execution import (
    FileR2VExecutionService,
    _resolve_request,
)
from services.media_files.visual_reference_resolution import (
    preview_r2v_reference_order,
    video_reference_plan,
)
from services.project_files.models import (
    ArtifactSlot,
    ArtifactVersion,
    ElementOutput,
    IndexedFile,
)

from .conftest import write_png
from .test_storyboard_reference_contract import _snapshot

pytestmark = pytest.mark.unit


def _renewed(tmp_path, monkeypatch, *, slot_id="element:shot:one:storyboard"):
    monkeypatch.setattr(
        config,
        "get_video_model_name",
        lambda: "wan3.0-video-prime",
    )
    monkeypatch.setattr(config, "get_video_backend", lambda: "wan")
    snapshot = _snapshot()
    project = snapshot.project
    element = project.timelines.items["timeline:main"].elements_by_id[
        "shot:one"
    ]
    anchors = list(element.creation.storyboard_reference_version_ids)
    element.creation.video_prompt = (
        "[Image 1] 是动作分镜，参考 [Image 2] 场景、[Image 3] 人物和 [Image 4] 道具。"
    )
    element.outputs["storyboard"] = ElementOutput(slot_id=slot_id)
    for version_id, own in (
        ("sb-old", True),
        ("sb-new", True),
        ("sb-other", False),
    ):
        file_id = f"file-{version_id}"
        path = tmp_path / "assets" / "artifacts" / f"{file_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_png(path, 8, 8, lambda _x, _y: (40, 70, 90), alpha=False)
        content = path.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        project.assets.files_by_id[file_id] = IndexedFile(
            file_id=file_id,
            relative_uri=path.relative_to(tmp_path).as_posix(),
            kind="artifact_payload",
            media_type="image/png",
            sha256=checksum,
            size_bytes=len(content),
            created_at=project.created_at,
        )
        project.assets.artifact_versions_by_id[version_id] = ArtifactVersion(
            version_id=version_id,
            slot_id=slot_id if own else "element:other:storyboard",
            kind="r2v_storyboard_image",
            owner_ref="element:shot:one" if own else "element:other",
            name=version_id,
            file_id=file_id,
            checksum=checksum,
            based_on_generation=1,
            created_at=project.created_at,
        )
    project.assets.artifact_slots_by_id[slot_id] = ArtifactSlot(
        slot_id=slot_id,
        kind="r2v_storyboard_image",
        owner_ref="element:shot:one",
        version_ids=["sb-old", "sb-new"],
        selected_version_id="sb-new",
    )
    element.creation.video_reference_version_ids = ["sb-old", *anchors]
    return snapshot, element, anchors


def test_new_selection_replaces_own_history_without_shifting_identity_refs(
    tmp_path,
    monkeypatch,
):
    snapshot, element, anchors = _renewed(tmp_path, monkeypatch)
    project = snapshot.project
    before = project.model_dump_json()
    preview = preview_r2v_reference_order(project, element.element_id)
    assert [r["versionId"] for r in preview["references"]] == [
        "sb-new",
        *anchors,
    ]
    assert [r["index"] for r in preview["references"]] == [1, 2, 3, 4]
    assert preview["references"][0]["kind"] == "storyboard"
    assert project.model_dump_json() == before


def test_slot_identity_preserves_other_storyboards_and_unknown_refs(
    tmp_path,
    monkeypatch,
):
    snapshot, element, anchors = _renewed(
        tmp_path,
        monkeypatch,
        slot_id="custom-storyboard-slot",
    )
    element.creation.video_reference_version_ids = [
        "sb-old",
        "sb-other",
        "missing-reference",
        *anchors,
        "sb-new",
        "sb-old",
    ]
    # The missing item is retained for admission to reject, never filtered or
    # relabelled.
    expected = ["sb-new", "sb-other", "missing-reference", *anchors]
    preview = preview_r2v_reference_order(snapshot.project, element.element_id)
    assert [r["versionId"] for r in preview["references"]] == expected
    assert preview["references"][2]["index"] == 3
    assert not preview["references"][2]["available"]
    assert not preview["ready"]


def test_missing_selection_reserves_image_one_and_excludes_old_slot_versions(
    tmp_path,
    monkeypatch,
):
    snapshot, element, anchors = _renewed(tmp_path, monkeypatch)
    slot = snapshot.project.assets.artifact_slots_by_id[
        element.outputs["storyboard"].slot_id
    ]
    slot.selected_version_id = None
    pending = preview_r2v_reference_order(snapshot.project, element.element_id)
    assert [r["versionId"] for r in pending["references"]] == ["", *anchors]
    assert pending["references"][0]["kind"] == "storyboard"
    assert not pending["ready"]
    slot.selected_version_id = "sb-new"
    selected = preview_r2v_reference_order(
        snapshot.project,
        element.element_id,
    )
    assert selected["references"][1:] == pending["references"][1:]


def test_storyboard_only_explicit_plan_does_not_activate_automatic_inputs(
    tmp_path,
    monkeypatch,
):
    from .test_r2v_reference_preview import _project

    project = _project()
    element = project.timelines.items["timeline:main"].elements_by_id["elem:1"]
    element.creation.video_reference_version_ids = ["art:sb-1"]
    assert video_reference_plan(project, element) == ("art:sb-1",)
    element.creation.video_reference_version_ids = []
    assert len(video_reference_plan(project, element)) == 4


def test_storyboard_image_can_still_explicitly_reference_its_old_version(
    tmp_path,
    monkeypatch,
):
    snapshot, element, _ = _renewed(tmp_path, monkeypatch)
    element.creation.storyboard_reference_version_ids = ["sb-old"]
    preview = preview_r2v_reference_order(
        snapshot.project,
        element.element_id,
        stage="storyboard",
        image_model_name="qwen-image-3.0-pro",
    )
    assert [r["versionId"] for r in preview["references"]] == ["sb-old"]


@pytest.mark.parametrize("repeat_version", [False, True])
def test_actual_fresh_request_uses_preview_order_and_four_native_markers(
    tmp_path,
    monkeypatch,
    repeat_version,
):
    snapshot, element, anchors = _renewed(tmp_path, monkeypatch)
    if repeat_version:
        # A repeated version must not create a fifth prompt reference slot.
        element.creation.video_reference_version_ids.append(anchors[0])
    preview = preview_r2v_reference_order(snapshot.project, element.element_id)
    resolved = _resolve_request(
        snapshot=snapshot,
        project_root=tmp_path,
        target_ref=f"element:{element.element_id}",
        arguments={},
    )
    assert resolved.reference_version_ids == ("sb-new", *anchors)
    assert list(resolved.reference_version_ids) == [
        r["versionId"] for r in preview["references"]
    ]
    assert len(resolved.reference_urls) == 4
    assert "图2 场景、图3 人物和 图4 道具" in resolved.prompt
    assert "图5" not in resolved.prompt


@pytest.mark.parametrize("legacy", [False, True])
def test_publication_preserves_frozen_inputs_without_recompilation(
    tmp_path,
    monkeypatch,
    legacy,
):
    snapshot, element, anchors = _renewed(tmp_path, monkeypatch)
    frozen = ["sb-new", *(["sb-old"] if legacy else []), *anchors]
    task = SimpleNamespace(
        metadata={
            "requestSnapshot": {
                "elementId": element.element_id,
                "referenceVersionIds": frozen,
                "prompt": "frozen prompt",
            },
        },
    )
    before = list(frozen)
    assert FileR2VExecutionService._frozen_inputs_still_current(
        snapshot.project,
        task,
    )
    assert task.metadata["requestSnapshot"]["referenceVersionIds"] == before
    assert task.metadata["requestSnapshot"]["prompt"] == "frozen prompt"
    slot = snapshot.project.assets.artifact_slots_by_id[
        element.outputs["storyboard"].slot_id
    ]
    slot.selected_version_id = "sb-old"
    assert not FileR2VExecutionService._frozen_inputs_still_current(
        snapshot.project,
        task,
    )
