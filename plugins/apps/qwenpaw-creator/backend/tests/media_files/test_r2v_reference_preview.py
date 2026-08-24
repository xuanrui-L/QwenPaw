# -*- coding: utf-8 -*-
"""Authoritative [Image N] reference-order preview for r2v Elements."""
from __future__ import annotations

import pytest

from services.media_files.visual_reference_resolution import (
    preview_r2v_reference_order,
)
from services.project_files.models import (
    ArtifactSlot,
    ArtifactVersion,
    ElementLocation,
    ElementOutput,
    Project,
    R2VCreation,
    SourceAssetVersion,
    TimelineElement,
    TimelineSpan,
    VisualEntity,
    VisualVariant,
)


pytestmark = pytest.mark.unit

_NOW = "2026-08-05T00:00:00Z"


def _artifact(version_id: str, *, slot_id: str, name: str) -> ArtifactVersion:
    return ArtifactVersion(
        version_id=version_id,
        slot_id=slot_id,
        kind="r2v_storyboard_image",
        owner_ref="element:elem:1",
        name=name,
        file_id=f"file-{version_id}",
        checksum="0" * 64,
        based_on_generation=1,
        created_at=_NOW,
    )


def _project() -> Project:
    project = Project.new(project_id="p-preview", name="Preview")
    project.visual.entities.items["char:a"] = VisualEntity(
        entity_id="char:a",
        kind="character",
        name="阿珍",
        required_variant_ids=["var:x"],
        variants={
            "items": {
                "var:x": VisualVariant(
                    variant_id="var:x",
                    selected_artifact_version_id="art:a-main",
                ),
            },
            "order": ["var:x"],
        },
    )
    project.visual.entities.order.append("char:a")
    for entity_id, kind, variant_id, version_id, name in (
        ("prop:bird", "prop", "var:bird", "art:bird-main", "纸鹤道具图"),
        ("scene:rain", "scene", "var:rain", "art:rain-main", "雨夜场景图"),
    ):
        project.visual.entities.items[entity_id] = VisualEntity(
            entity_id=entity_id,
            kind=kind,
            name=name,
            required_variant_ids=[variant_id],
            variants={
                "items": {
                    variant_id: VisualVariant(
                        variant_id=variant_id,
                        selected_artifact_version_id=version_id,
                    ),
                },
                "order": [variant_id],
            },
        )
        project.visual.entities.order.append(entity_id)
        project.assets.artifact_versions_by_id[version_id] = _artifact(
            version_id,
            slot_id=f"slot:{entity_id}",
            name=name,
        )
    project.assets.artifact_versions_by_id["art:a-main"] = _artifact(
        "art:a-main",
        slot_id="slot:char-a",
        name="阿珍 定妆图",
    )
    project.assets.artifact_versions_by_id["art:extra"] = _artifact(
        "art:extra",
        slot_id="slot:extra",
        name="氛围参考图",
    )
    project.assets.artifact_versions_by_id["art:sb-1"] = _artifact(
        "art:sb-1",
        slot_id="element:elem:1:storyboard",
        name="第一镜 分镜图",
    )
    project.assets.source_versions_by_id["src:upload-1"] = SourceAssetVersion(
        version_id="src:upload-1",
        logical_asset_id="asset:upload-1",
        name="用户上传参考",
        file_id="file-src-upload-1",
        checksum="1" * 64,
        media_kind="image",
        media_type="image/png",
        created_at=_NOW,
    )
    project.assets.artifact_slots_by_id[
        "element:elem:1:storyboard"
    ] = ArtifactSlot(
        slot_id="element:elem:1:storyboard",
        kind="r2v_storyboard_image",
        owner_ref="element:elem:1",
        version_ids=["art:sb-1"],
        selected_version_id="art:sb-1",
    )
    project.timelines.items["timeline:main"].elements_by_id[
        "elem:1"
    ] = TimelineElement(
        element_id="elem:1",
        span=TimelineSpan(start_tick=0, duration_tick=4_000),
        location=ElementLocation(),
        creation=R2VCreation(
            character_refs=["char:a"],
            prop_refs=["prop:bird"],
            scene_ref="scene:rain",
            visual_variant_refs={
                "char:a": "var:x",
                "prop:bird": "var:bird",
                "scene:rain": "var:rain",
            },
            video_reference_version_ids=["src:upload-1", "art:extra"],
        ),
        outputs={
            "storyboard": ElementOutput(
                slot_id="element:elem:1:storyboard",
            ),
        },
    )
    return project


def test_preview_mirrors_submit_order_and_shifts_without_storyboard() -> None:
    project = _project()

    preview = preview_r2v_reference_order(project, "elem:1")

    assert preview["elementId"] == "elem:1"
    assert preview["storyboardSelected"] is True
    assert [
        (item["index"], item["versionId"], item["kind"])
        for item in preview["references"]
    ] == [
        (1, "art:sb-1", "storyboard"),
        (2, "art:a-main", "artifact"),
        (3, "art:rain-main", "artifact"),
        (4, "art:bird-main", "artifact"),
        (5, "src:upload-1", "source"),
        (6, "art:extra", "artifact"),
    ]
    assert preview["references"][0]["name"] == "第一镜 分镜图"
    assert preview["references"][4]["name"] == "用户上传参考"

    # Without a selected storyboard the chain starts at [Image 1] with the
    # first resolved visual reference.
    slot = project.assets.artifact_slots_by_id["element:elem:1:storyboard"]
    slot.selected_version_id = None
    shifted = preview_r2v_reference_order(project, "elem:1")
    assert shifted["storyboardSelected"] is False
    assert [
        (item["index"], item["versionId"]) for item in shifted["references"]
    ] == [
        (1, "art:a-main"),
        (2, "art:rain-main"),
        (3, "art:bird-main"),
        (4, "src:upload-1"),
        (5, "art:extra"),
    ]
