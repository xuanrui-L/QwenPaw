# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Reference resolution and provider ordering for storyboards and video."""

from __future__ import annotations

import hashlib
import re

import pytest

from domain.enums import CreatorCommandType
from domain.errors import ValidationError
from models.reference_markers import (
    ReferenceMarkerSpec,
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
        "versionId": "",
        "kind": "storyboard",
        "available": False,
        "name": "分镜图（待生成）",
    }
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


def test_explicit_reference_from_another_variant_is_rejected() -> None:
    """A bound character never consumes an ArtifactVersion owned by another
    Variant: instead of silently dropping the conflicting reference, the
    resolver fails loudly so the agent fixes the list or the binding."""
    from domain.errors import ValidationError
    from services.media_files.visual_reference_resolution import (
        resolve_r2v_visual_reference_version_ids,
    )

    project = _project()
    entity = project.visual.entities.items["char:a"]
    entity.variants.items["var:z"] = VisualVariant(
        variant_id="var:z",
        generated_artifact_version_ids=["art:a-alt"],
        selected_artifact_version_id="art:a-alt",
    )
    entity.variants.order.append("var:z")
    project.assets.artifact_versions_by_id["art:a-alt"] = _artifact(
        "art:a-alt",
        slot_id="slot:char-a-alt",
        name="阿珍 战损造型",
    )
    creation = R2VCreation(
        character_refs=["char:a"],
        visual_variant_refs={"char:a": "var:x"},
    )

    with pytest.raises(ValidationError, match="var:z"):
        resolve_r2v_visual_reference_version_ids(
            project,
            creation,
            ["art:a-alt"],
        )

    # A reference from the bound Variant itself stays authoritative.
    resolved = resolve_r2v_visual_reference_version_ids(
        project,
        creation,
        ["art:a-main", "art:extra"],
    )
    assert resolved == ("art:a-main", "art:extra")


def test_bound_scene_may_reference_sibling_variants() -> None:
    """A scene may carry several of its own Variants into one shot.

    A dusk-to-night lighting transition references two time-of-day states
    of the same location on purpose. Unlike a character costume swap, this
    is authored intent, so the resolver must accept the explicit list even
    though only one Variant is bound.
    """
    from services.media_files.visual_reference_resolution import (
        resolve_r2v_visual_reference_version_ids,
    )

    project = _project()
    scene = project.visual.entities.items["scene:rain"]
    scene.required_variant_ids = ["var:rain", "var:rain-dusk"]
    scene.variants.items["var:rain-dusk"] = VisualVariant(
        variant_id="var:rain-dusk",
        generated_artifact_version_ids=["art:rain-dusk"],
        selected_artifact_version_id="art:rain-dusk",
    )
    scene.variants.order.append("var:rain-dusk")
    project.assets.artifact_versions_by_id["art:rain-dusk"] = _artifact(
        "art:rain-dusk",
        slot_id="slot:scene-rain-dusk",
        name="雨夜黄昏时刻",
    )
    creation = R2VCreation(
        scene_ref="scene:rain",
        visual_variant_refs={"scene:rain": "var:rain"},
    )

    resolved = resolve_r2v_visual_reference_version_ids(
        project,
        creation,
        ["art:rain-main", "art:rain-dusk"],
    )
    assert resolved == ("art:rain-main", "art:rain-dusk")


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


def test_structured_reference_models_get_no_invented_numbering(
    patch_attributes,
) -> None:
    """Inventing an inline index would misdescribe a structured payload."""
    from models import config as model_config

    patch_attributes(
        model_config,
        get_video_model_name=lambda: "some-structured-model",
        get_video_backend=lambda: "unknown-backend",
    )

    prompt = _append_reference_role_mapping(
        "镜头描述。",
        _prompt_project(),
        ["art:sb", "art:ahai"],
        storyboard_id="art:sb",
    )
    assert prompt == "镜头描述。"
    assert _REFERENCE_ROLE_MARKER not in prompt


def test_no_references_leaves_prompt_untouched(monkeypatch) -> None:
    from models import config as model_config

    monkeypatch.setattr(
        model_config,
        "get_video_model_name",
        lambda: "wan3.0-video",
    )
    monkeypatch.setattr(model_config, "get_video_backend", lambda: "")

    assert (
        _append_reference_role_mapping(
            "纯文本。",
            _prompt_project(),
            [],
            storyboard_id="",
        )
        == "纯文本。"
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


def test_actual_resolver_matches_phase_preview_and_provider_wording(
    tmp_path,
):
    snapshot = _snapshot()
    model = "qwen-image-3.0-pro"
    preview = preview_r2v_reference_order(
        snapshot.project,
        "shot:one",
        stage="storyboard",
        image_model_name=model,
        project_root=tmp_path,
    )
    request = _resolve(snapshot, tmp_path, model)
    assert preview["ready"]
    assert [ref["versionId"] for ref in preview["references"]] == list(
        request.reference_version_ids,
    )
    assert len(request.reference_image_urls) == 3
    assert request.reference_image_urls[0] == request.reference_image_urls[1]
    assert "图2 的人物" in request.prompt
    assert "[Image" not in request.prompt


def test_invalid_indices_are_visible_and_rejected_before_provider(
    tmp_path,
):
    snapshot = _snapshot("采用 [Image 4] 的人物。")
    preview = preview_r2v_reference_order(
        snapshot.project,
        "shot:one",
        stage="storyboard",
        image_model_name="qwen-image-3.0-pro",
        project_root=tmp_path,
    )
    assert preview["invalidMarkerIndices"] == [4]
    assert preview["ready"] is False
    with pytest.raises(ValidationError, match="参考图编号"):
        _resolve(snapshot, tmp_path)


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


_AUTHORED = "[Image 1] 的护士靠窗，[Image 2] 的司机看后视镜，背景 [Image 3]。"


@pytest.mark.parametrize(
    ("model_name", "backend", "expected"),
    [
        # wan3 documents 图N: "数组中的第 1 个 reference_image 对应 图1".
        ("wan3.0-video", "", "图1 的护士靠窗，图2 的司机看后视镜，背景 图3。"),
        (
            "happyhorse-video",
            "",
            "[Image 1] 的护士靠窗，[Image 2] 的司机看后视镜，背景 [Image 3]。",
        ),
        (
            "doubao-seedance-2.0",
            "seedance2",
            "图片1 的护士靠窗，图片2 的司机看后视镜，背景 图片3。",
        ),
        (
            "provider/kling-v2",
            "kling",
            "<<<image_1>>> 的护士靠窗，<<<image_2>>> 的司机看后视镜，背景 <<<image_3>>>。",
        ),
        (
            "kling-v2",
            "kling",
            "@image_1 的护士靠窗，@image_2 的司机看后视镜，背景 @image_3。",
        ),
        (
            "wan2.6-i2v",
            "wan",
            "character1 的护士靠窗，character2 的司机看后视镜，背景 character3。",
        ),
    ],
)
def test_one_authored_form_renders_to_each_provider_dialect(
    model_name: str,
    backend: str,
    expected: str,
) -> None:
    spec = video_reference_marker_spec(model_name, backend)
    assert render_reference_markers(_AUTHORED, spec) == expected


def test_provider_without_documented_addressing_gets_prose() -> None:
    """OpenAI's image family documents array order only, no per-image syntax.

    Passing "[Image 1]" through would send literal text the provider has no
    contract for, so it becomes ordinal prose instead.
    """
    rendered = render_reference_markers(_AUTHORED, None)
    assert "[Image" not in rendered
    assert "第1张参考图" in rendered and "第3张参考图" in rendered
    english = render_reference_markers(
        "[Image 2] shows the bus",
        None,
        language="en",
    )
    assert english == "reference image 2 shows the bus"


def test_prompts_stored_before_this_layer_are_untouched() -> None:
    """A legacy prompt already holds provider-native markers and no canonical
    ones, so rendering must be a no-op — and stable if applied twice.
    """
    spec = video_reference_marker_spec("wan3.0-video", "")
    legacy = "图1 的护士靠窗，图2 的司机看后视镜。"
    once = render_reference_markers(legacy, spec)
    assert once == legacy
    assert render_reference_markers(once, spec) == once


def test_authoring_tolerates_case_and_spacing() -> None:
    spec = video_reference_marker_spec("wan3.0-video", "")
    assert (
        render_reference_markers("[image  2] 与 [IMAGE 3]", spec) == "图2 与 图3"
    )


def test_marker_helpers_round_trip() -> None:
    assert canonical_marker(4) == "[Image 4]"
    assert canonical_marker_indices("[Image 2] then [Image 1]") == (2, 1)
    assert not canonical_marker_indices("no markers here")


def test_spec_records_where_the_syntax_came_from() -> None:
    """The marker table used to assert a syntax with no citation."""
    spec = video_reference_marker_spec("wan3.0-video", "")
    assert spec is not None
    assert "wan3-video-generation-api-reference" in spec.documentation_url


@pytest.mark.parametrize(
    ("model_name", "backend", "expected"),
    [
        ("wan3.0-video", "", "图12"),
        ("happyhorse-video", "", "[Image 12]"),
        ("doubao-seedance-2.0", "seedance2", "图片12"),
        ("provider/kling-v2", "kling", "<<<image_12>>>"),
        ("kling-v2", "kling", "@image_12"),
        ("wan2.6-i2v", "wan", "character12"),
    ],
)
def test_multi_digit_indices_render_intact(
    model_name: str,
    backend: str,
    expected: str,
) -> None:
    """Index substitution must be templated, not a digit string-replace.

    The earlier implementation built marker N by replacing "1" inside the
    literal for image 1, which turns index 12 into nonsense and breaks any
    dialect whose literal carries another digit.
    """
    spec = video_reference_marker_spec(model_name, backend)
    assert spec is not None
    assert spec.render_index(12) == expected
    assert render_reference_markers("看 [Image 12]", spec) == f"看 {expected}"


def test_render_index_uses_the_template() -> None:
    spec = ReferenceMarkerSpec(
        template="图{index}",
        pattern=re.compile(r"图(\d+)"),
        documentation_url="https://example.invalid",
    )
    assert spec.render_index(7) == "图7"
