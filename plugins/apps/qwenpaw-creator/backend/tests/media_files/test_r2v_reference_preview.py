# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Reference resolution and provider ordering for storyboards and video."""

from __future__ import annotations

import hashlib

import pytest

from domain.enums import CreatorCommandType
from domain.errors import ValidationError
from models.reference_markers import (
    canonical_marker,
    canonical_marker_indices,
    render_reference_markers,
)
from models.video_capabilities import video_reference_marker_spec
from services.media_files.image_execution import _resolve_request
from services.media_files.r2v_execution import (
    _REFERENCE_ROLE_MARKER,
    _append_reference_role_mapping,
)
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
    Timeline,
    TimelineElement,
    TimelineSpan,
    VisualCastLineup,
    VisualEntity,
    VisualVariant,
)
from services.project_files.store import ProjectSnapshot
from services.prompt_text import video_prompt_time_error

from .conftest import make_r2v_element

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
    for entity_id, kind, variant_id, version_id, name, artifact_name, slot in (
        (
            "char:a",
            "character",
            "var:x",
            "art:a-main",
            "阿珍",
            "阿珍 定妆图",
            "slot:char-a",
        ),
        (
            "prop:bird",
            "prop",
            "var:bird",
            "art:bird-main",
            "纸鹤道具图",
            "纸鹤道具图",
            "slot:prop:bird",
        ),
        (
            "scene:rain",
            "scene",
            "var:rain",
            "art:rain-main",
            "雨夜场景图",
            "雨夜场景图",
            "slot:scene:rain",
        ),
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
            slot_id=slot,
            name=artifact_name,
        )
    for version_id, slot_id, name in (
        ("art:extra", "slot:extra", "氛围参考图"),
        ("art:sb-1", "element:elem:1:storyboard", "第一镜 分镜图"),
    ):
        project.assets.artifact_versions_by_id[version_id] = _artifact(
            version_id,
            slot_id=slot_id,
            name=name,
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


def test_preview_mirrors_submit_order_and_reserves_missing_storyboard() -> (
    None
):
    project = _project()

    preview = preview_r2v_reference_order(project, "elem:1")

    assert preview["elementId"] == "elem:1"
    assert preview["storyboardSelected"] is True
    # Agent-specified references are authoritative: the explicit list is
    # used verbatim after the storyboard, with no auto-injected anchors.
    assert [
        (item["index"], item["versionId"], item["kind"])
        for item in preview["references"]
    ] == [
        (1, "art:sb-1", "storyboard"),
        (2, "src:upload-1", "source"),
        (3, "art:extra", "artifact"),
    ]
    assert preview["references"][0]["name"] == "第一镜 分镜图"
    assert preview["references"][1]["name"] == "用户上传参考"

    # Before the storyboard exists its semantic slot stays [Image 1]; input
    # identities must not change when the real storyboard becomes selected.
    slot = project.assets.artifact_slots_by_id["element:elem:1:storyboard"]
    slot.selected_version_id = None
    shifted = preview_r2v_reference_order(project, "elem:1")
    assert shifted["storyboardSelected"] is False
    assert shifted["ready"] is False
    assert shifted["references"][0] == {
        "index": 1,
        "imageIndex": 1,
        "videoIndex": None,
        "versionId": "",
        "kind": "storyboard",
        "available": False,
        "name": "分镜图（待生成）",
    }
    # The reserved storyboard owns 图1, so plan images behind it carry
    # the future-real wire ordinals rather than storyboard-less ones.
    assert [item["imageIndex"] for item in shifted["references"][1:]] == [
        2,
        3,
    ]
    assert [
        (item["index"], item["versionId"]) for item in shifted["references"]
    ] == [
        (1, ""),
        (2, "src:upload-1"),
        (3, "art:extra"),
    ]
    slot.selected_version_id = "art:sb-1"
    selected = preview_r2v_reference_order(project, "elem:1")
    assert selected["ready"] is True
    assert selected["references"][1:] == shifted["references"][1:]
    assert (
        selected["references"][0]["index"] == shifted["references"][0]["index"]
    )
    assert selected["references"][0]["versionId"] == "art:sb-1"
    slot.selected_version_id = None

    # An element with no explicit references falls back to the automatic
    # chain, which resolves every bound anchor in character → scene → prop
    # order.
    element = None
    for timeline in project.timelines.items.values():
        element = timeline.elements_by_id.get("elem:1") or element
    element.creation.video_reference_version_ids.clear()
    auto = preview_r2v_reference_order(project, "elem:1")
    assert [
        (item["index"], item["versionId"]) for item in auto["references"]
    ] == [
        (1, ""),
        (2, "art:a-main"),
        (3, "art:rain-main"),
        (4, "art:bird-main"),
    ]


@pytest.mark.parametrize("entity_kind", ["character", "scene"])
def test_explicit_sibling_variants_are_allowed_only_for_scenes(entity_kind):
    from services.media_files.visual_reference_resolution import (
        resolve_r2v_visual_reference_version_ids,
    )

    project = _project()
    is_scene = entity_kind == "scene"
    entity_id, bound, selected = (
        ("scene:rain", "var:rain", "art:rain-main")
        if is_scene
        else ("char:a", "var:x", "art:a-main")
    )
    alt = "var:alternative"
    other_id = "art:alternative"
    entity = project.visual.entities.items[entity_id]
    entity.variants.items[alt] = VisualVariant(
        variant_id=alt,
        generated_artifact_version_ids=[other_id],
        selected_artifact_version_id=other_id,
    )
    entity.variants.order.append(alt)
    if is_scene:
        entity.required_variant_ids.append(alt)
    project.assets.artifact_versions_by_id[other_id] = _artifact(
        other_id,
        slot_id=f"slot:{entity_id}:alternative",
        name="Alternative",
    )
    creation = R2VCreation(
        scene_ref=entity_id if is_scene else None,
        character_refs=[] if is_scene else [entity_id],
        visual_variant_refs={entity_id: bound},
    )

    resolve = resolve_r2v_visual_reference_version_ids
    if is_scene:
        assert resolve(project, creation, [selected, other_id]) == (
            selected,
            other_id,
        )
    else:
        with pytest.raises(ValidationError, match=alt):
            resolve(project, creation, [other_id])
        assert resolve(project, creation, [selected, "art:extra"]) == (
            selected,
            "art:extra",
        )


_PROMPT_NOW = "2026-08-26T00:00:00Z"


def _prompt_project() -> Project:
    project = Project.new(project_id="p-roles", name="Roles")
    project.assets.artifact_versions_by_id["art:sb"] = ArtifactVersion(
        version_id="art:sb",
        slot_id="element:elem:1:storyboard",
        kind="r2v_storyboard_image",
        owner_ref="element:elem:1",
        name="第一镜 分镜图",
        file_id="file-sb",
        checksum="0" * 64,
        based_on_generation=1,
        created_at=_PROMPT_NOW,
    )
    project.assets.artifact_versions_by_id["art:ahai"] = ArtifactVersion(
        version_id="art:ahai",
        slot_id="visual:char:ahai:var:storm",
        kind="visual_asset_image",
        owner_ref="visual:char:ahai",
        name="阿海（ahai:storm）视觉图",
        file_id="file-ahai",
        checksum="1" * 64,
        based_on_generation=1,
        created_at=_PROMPT_NOW,
    )
    project.assets.source_versions_by_id["src:upload"] = SourceAssetVersion(
        version_id="src:upload",
        logical_asset_id="asset:upload",
        name="用户上传参考",
        file_id="file-upload",
        checksum="2" * 64,
        media_kind="image",
        media_type="image/png",
        created_at=_PROMPT_NOW,
    )
    return project


def test_mapping_labels_every_reference_in_payload_order(monkeypatch) -> None:
    """Numbering follows the submitted order, not the authored prompt."""
    from models import config as model_config

    monkeypatch.setattr(
        model_config,
        "get_video_model_name",
        lambda: "wan3.0-video",
    )
    monkeypatch.setattr(model_config, "get_video_backend", lambda: "")

    prompt = _append_reference_role_mapping(
        "阿海登上灯塔。",
        _prompt_project(),
        ["art:sb", "art:ahai", "src:upload"],
        storyboard_id="art:sb",
    )

    assert prompt.startswith("阿海登上灯塔。")
    assert _REFERENCE_ROLE_MARKER in prompt
    # The block is authored in the canonical form; _render_reference_markers
    # rewrites it to the provider's dialect at submit time.
    assert "[Image 1] = 分镜图（第一镜 分镜图）" in prompt
    assert "[Image 2] = 阿海（ahai:storm）视觉图" in prompt
    assert "[Image 3] = 用户上传参考" in prompt

    # Re-appending must not duplicate the block.
    again = _append_reference_role_mapping(
        prompt,
        _prompt_project(),
        ["art:sb", "art:ahai", "src:upload"],
        storyboard_id="art:sb",
    )
    assert again == prompt
    assert again.count(_REFERENCE_ROLE_MARKER) == 1


def test_submit_path_renders_the_block_into_each_provider_dialect(
    patch_attributes,
) -> None:
    """Canonical in the block; submit renders the provider's own form."""
    from models import config as model_config
    from services.media_files.r2v_execution import _render_reference_markers

    for model_name, backend, first in (
        ("happyhorse-video", "", "[Image 1]"),
        ("wan3.0-video", "", "图1"),
        ("doubao-seedance-2.0", "seedance2", "图片1"),
        ("kling-v2", "kling", "@image_1"),
    ):
        patch_attributes(
            model_config,
            get_video_model_name=lambda name=model_name: name,
            get_video_backend=lambda key=backend: key,
        )
        block = _append_reference_role_mapping(
            "镜头描述。",
            _prompt_project(),
            ["art:sb", "art:ahai"],
            storyboard_id="art:sb",
        )
        assert "[Image 1] = 分镜图（第一镜 分镜图）" in block
        rendered = _render_reference_markers(block)
        assert f"{first} = 分镜图（第一镜 分镜图）" in rendered


@pytest.mark.parametrize(
    ("model", "backend", "references"),
    [
        ("some-structured-model", "unknown-backend", ["art:sb", "art:ahai"]),
        ("wan3.0-video", "", []),
    ],
    ids=["structured-payload", "no-references"],
)
def test_role_mapping_is_skipped_when_there_are_no_textual_references(
    patch_attributes,
    model,
    backend,
    references,
):
    from models import config as model_config

    patch_attributes(
        model_config,
        get_video_model_name=lambda: model,
        get_video_backend=lambda: backend,
    )
    assert (
        _append_reference_role_mapping(
            "镜头描述。",
            _prompt_project(),
            references,
            storyboard_id="art:sb" if references else "",
        )
        == "镜头描述。"
    )


@pytest.mark.parametrize(
    "prompt,invalid",
    [
        ("0–2秒站起，2.0s-7s转身", False),
        ("7秒至14秒转身", True),
        ("00:07–00:14 转身", True),
        ("00:00–00:07 转身", False),
        ("3s–2s转身", True),
        ("4-9格，24-30fps，16:9画幅", False),
        ("０～７秒连续动作", False),
    ],
)
def test_local_action_ranges(prompt, invalid):
    assert bool(video_prompt_time_error(prompt, 7)) is invalid


def _snapshot(
    prompt="[Image 2] 的人物站在 [Image 1] 的场景，拿 [Image 3] 的道具。",
):
    project = Project.new(
        project_id="image-ref-contract",
        name="Reference contract",
    )
    element = make_r2v_element("shot:one", storyboard_prompt=prompt)
    project.timelines.items["timeline:main"].elements_by_id[
        element.element_id
    ] = element
    # Exact IDs deliberately do not sort into their semantic/input order.
    ids = ["src:z-scene", "src:a-person", "src:m-prop"]
    for index, version_id in enumerate(ids):
        # Two different exact versions deliberately share a transport URL.
        url = f"https://images.example/{'shared' if index < 2 else 'prop'}.png"
        project.assets.source_versions_by_id[version_id] = SourceAssetVersion(
            version_id=version_id,
            logical_asset_id=f"asset:{version_id}",
            name=["公寓", "女子", "钥匙"][index],
            checksum=hashlib.sha256(url.encode()).hexdigest(),
            media_kind="image",
            media_type="image/png",
            created_at=project.created_at,
            metadata={
                "sourceKind": "remote_url",
                "checksumKind": "source_url_sha256",
                "publicSourceUrl": url,
            },
        )
    element.creation.storyboard_reference_version_ids = ids
    # A different video sequence must never contaminate storyboard preview.
    element.creation.video_reference_version_ids = list(reversed(ids))
    return ProjectSnapshot(project=project, etag="fixture-etag", generation=1)


def _resolve(snapshot, tmp_path, model="qwen-image-3.0-pro"):
    return _resolve_request(
        snapshot=snapshot,
        project_root=tmp_path,
        command=CreatorCommandType.GENERATE_STORYBOARD_IMAGE,
        target_ref="element:shot:one",
        arguments={},
        image_model_name=model,
    )


@pytest.mark.parametrize(
    "valid",
    [True, False],
    ids=["matches-submit", "out-of-range"],
)
def test_storyboard_preview_matches_submit_and_rejects_invalid_references(
    tmp_path,
    valid,
):
    snapshot = _snapshot() if valid else _snapshot("采用 [Image 4] 的人物。")
    model = "qwen-image-3.0-pro"
    preview = preview_r2v_reference_order(
        snapshot.project,
        "shot:one",
        stage="storyboard",
        image_model_name=model,
        project_root=tmp_path,
    )
    assert preview["ready"] is valid
    if not valid:
        assert preview["invalidMarkerIndices"] == [4]
        with pytest.raises(ValidationError, match="参考图编号"):
            _resolve(snapshot, tmp_path, model)
        return

    request = _resolve(snapshot, tmp_path, model)
    assert [ref["versionId"] for ref in preview["references"]] == list(
        request.reference_version_ids,
    )
    assert len(request.reference_image_urls) == 3
    assert request.reference_image_urls[0] == request.reference_image_urls[1]
    assert "图2 的人物" in request.prompt
    assert "[Image" not in request.prompt


def test_ready_episode_resolves_while_later_episode_assets_are_pending(
    tmp_path,
):
    snapshot = _snapshot()
    project = snapshot.project
    later = make_r2v_element("shot:later", storyboard_prompt="两人夜景")
    later.creation.character_refs = ["char:later-a", "char:later-b"]
    later.creation.cast_lineup_refs = ["lineup:later"]
    for ref in later.creation.character_refs:
        project.visual.entities.items[ref] = VisualEntity(
            entity_id=ref,
            kind="character",
            name=ref,
            required_variant_ids=[],
        )
        project.visual.entities.order.append(ref)
    project.visual.cast_lineups.items["lineup:later"] = VisualCastLineup(
        lineup_id="lineup:later",
        name="Later episode pair",
        character_refs=later.creation.character_refs,
    )
    project.visual.cast_lineups.order.append("lineup:later")
    project.timelines.items["timeline:later"] = Timeline(
        timeline_id="timeline:later",
        elements_by_id={later.element_id: later},
    )
    project.timelines.order.append("timeline:later")

    request = _resolve(snapshot, tmp_path)
    assert request.reference_version_ids == tuple(
        project.timelines.items["timeline:main"]
        .elements_by_id["shot:one"]
        .creation.storyboard_reference_version_ids,
    )
    with pytest.raises(ValidationError, match="尚无使用中视觉产物"):
        _resolve_request(
            snapshot=snapshot,
            project_root=tmp_path,
            command=CreatorCommandType.GENERATE_STORYBOARD_IMAGE,
            target_ref="element:shot:later",
            arguments={},
        )


def test_layout_accepts_frame_classifiers_without_guessing_conflicts():
    from services.storyboard_layout import declared_storyboard_panel_count

    assert declared_storyboard_panel_count("1 张关键帧") == 1
    assert declared_storyboard_panel_count("共 8 格分镜面板") == 8
    assert declared_storyboard_panel_count("4个关键帧，9个分镜格") is None


_AUTHORED = "[Image 1] 的护士靠窗，[Image 2] 的司机看后视镜，背景 [Image 12]。"


@pytest.mark.parametrize(
    ("model_name", "backend", "marker"),
    [
        ("wan3.0-video", "", "图{}"),
        ("happyhorse-video", "", "[Image {}]"),
        ("doubao-seedance-2.0", "seedance2", "图片{}"),
        ("provider/kling-v2", "kling", "<<<image_{}>>>"),
        ("kling-v2", "kling", "@image_{}"),
        ("wan2.6-i2v", "wan", "character{}"),
    ],
)
def test_reference_markers_preserve_indices_in_each_provider_dialect(
    model_name,
    backend,
    marker,
):
    spec = video_reference_marker_spec(model_name, backend)
    assert spec is not None
    expected = "{0} 的护士靠窗，{1} 的司机看后视镜，背景 {2}。".format(
        *(marker.format(index) for index in (1, 2, 12)),
    )
    assert render_reference_markers(_AUTHORED, spec) == expected
    assert render_reference_markers(expected, spec) == expected


def test_provider_without_documented_addressing_gets_prose() -> None:
    rendered = render_reference_markers(_AUTHORED, None)
    assert "[Image" not in rendered
    assert "第1张参考图" in rendered and "第12张参考图" in rendered
    assert (
        render_reference_markers(
            "[Image 2] shows the bus",
            None,
            language="en",
        )
        == "reference image 2 shows the bus"
    )


def test_authoring_tolerates_case_and_spacing() -> None:
    spec = video_reference_marker_spec("wan3.0-video", "")
    assert (
        render_reference_markers("[image  2] 与 [IMAGE 3]", spec) == "图2 与 图3"
    )


def test_marker_helpers_round_trip() -> None:
    assert canonical_marker_indices(
        f"{canonical_marker(2)} then {canonical_marker(1)}",
    ) == (2, 1)
    assert not canonical_marker_indices("no markers here")
