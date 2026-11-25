# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Role and target scope for specialist tools and provider capabilities."""

from __future__ import annotations

import pytest

from domain.enums import SpecialistRole
from domain.errors import PermissionDeniedError
from services.project_files.facade import CreatorFileServices
from services.specialist_tools import FileSpecialistToolRegistry

import asyncio
import io
from PIL import Image
from services import object_grounding


def _names(manifest) -> set[str]:
    return {item["function"]["name"] for item in manifest}


def test_specialist_registry_owns_role_specific_media_tools(tmp_path) -> None:
    registry = FileSpecialistToolRegistry(
        CreatorFileServices.create(tmp_path.resolve()),
    )

    visual = _names(
        registry.manifest_for(
            SpecialistRole.VISUAL_DEVELOPMENT,
            admitted_target_refs=["asset:hero"],
        ),
    )
    # Direct retired-role manifests are durable checkpoint compatibility only;
    # subagents.py rejects every new delegation to this role.
    legacy_r2v = _names(
        registry.manifest_for(
            SpecialistRole.R2V_GENERATION_DIRECTOR,
            admitted_target_refs=["element:r2v-1"],
        ),
    )
    editing = _names(
        registry.manifest_for(
            SpecialistRole.AI_EDITING_DIRECTOR,
            admitted_target_refs=["timeline:timeline:main"],
        ),
    )
    source = _names(
        registry.manifest_for(
            SpecialistRole.SOURCE_INTELLIGENCE,
            admitted_target_refs=["asset:source-1"],
        ),
    )

    assert "image_generation" not in visual
    assert {"image_generation", "r2v_generation"} <= legacy_r2v
    # The editing director is a pure orchestration role: composition/export
    # is triggered directly by the user via HTTP endpoints.
    assert "ai_edit" not in editing
    assert "compose_final_video" not in editing
    assert {"read_project", "jq_project"} <= editing
    assert "transcribe_source_audio" in source
    assert "commit_source_intelligence" in source
    assert {"read_project", "read_project_file"} <= source
    assert "jq_project" not in source
    assert "elements_at" not in source
    assert "r2v_generation" not in visual
    assert "image_generation" not in editing

    # Verified-identity delegations drop the reader tools entirely: locating
    # the source via read_project burns a whole VLM turn for facts the
    # delegation message already carries.
    verified_source = _names(
        registry.manifest_for(
            SpecialistRole.SOURCE_INTELLIGENCE,
            admitted_target_refs=["asset:source-1"],
            include_project_readers=False,
        ),
    )
    assert not {"read_project", "read_project_file"} & verified_source
    assert {"transcribe_source_audio", "commit_source_intelligence"} <= (
        verified_source
    )
    editing_unaffected = _names(
        registry.manifest_for(
            SpecialistRole.AI_EDITING_DIRECTOR,
            admitted_target_refs=["timeline:timeline:main"],
            include_project_readers=False,
        ),
    )
    assert {"read_project", "jq_project"} <= editing_unaffected


def test_visual_role_cannot_bypass_work_graph_with_image_generation(
    tmp_path,
) -> None:
    registry = FileSpecialistToolRegistry(
        CreatorFileServices.create(tmp_path.resolve()),
    )
    manifest = registry.manifest_for(
        SpecialistRole.VISUAL_DEVELOPMENT,
        admitted_target_refs=["project:assets"],
    )
    assert "image_generation" not in _names(manifest)
    with pytest.raises(PermissionDeniedError, match="无权调用"):
        registry.spec_for(
            SpecialistRole.VISUAL_DEVELOPMENT,
            "image_generation",
        )


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        ("qwen-image-3.0-pro", 3),
        ("qwen-mt-image", 1),
        ("gpt-image-2", 16),
        ("qwen-image-max", 0),
        ("private-gateway-alias", 0),
    ],
)
def test_image_manifest_uses_official_model_reference_limit(
    tmp_path,
    monkeypatch,
    model_name,
    expected,
) -> None:
    monkeypatch.setattr(
        "services.specialist_tools.get_image_model_name",
        lambda: model_name,
    )
    manifest = FileSpecialistToolRegistry(
        CreatorFileServices.create(tmp_path.resolve()),
    ).manifest_for(
        SpecialistRole.R2V_GENERATION_DIRECTOR,
        admitted_target_refs=["element:hero"],
    )
    image_tool = next(
        item
        for item in manifest
        if item["function"]["name"] == "image_generation"
    )
    references = image_tool["function"]["parameters"]["properties"][
        "arguments"
    ]["properties"]["referenceVersionIds"]

    assert references["maxItems"] == expected


def test_project_assets_scope_does_not_expand_for_r2v_image_tool(
    tmp_path,
) -> None:
    registry = FileSpecialistToolRegistry(
        CreatorFileServices.create(tmp_path.resolve()),
    )
    spec = registry.spec_for(
        SpecialistRole.R2V_GENERATION_DIRECTOR,
        "image_generation",
    )

    assert spec is not None
    assert not spec.admits_target_ref(
        role=SpecialistRole.R2V_GENERATION_DIRECTOR,
        target_ref="asset:char-cat",
        admitted_target_refs=["project:assets"],
    )


def test_unique_prefix_correction_recovers_id_transcription_slips() -> None:
    # Field run 2026-08-09: the model flipped one hex run inside a
    # content-addressed asset id and the whole 18-asset delegation
    # burned on PERMISSION_DENIED. A unique long-prefix match is
    # deterministic evidence of the intended target.
    from services.specialist_tools import _unique_prefix_correction

    admitted = [
        "asset:asset-0636e4ce478c58b683df30a9966b6c34",
        "asset:asset-b0d373c89a3e54ea8344ae2ee4527e27",
        "asset:asset-f683a4e5799e56949482f6ac52a94da1",
    ]
    # The real slip observed in the run: head intact, tail corrupted.
    assert (
        _unique_prefix_correction(
            "asset:asset-0636e4ce478c58b0ab03802f6cac5204",
            admitted,
        )
        == admitted[0]
    )
    # Exact members need no correction path but resolve to themselves.
    assert _unique_prefix_correction(admitted[1], admitted) == admitted[1]
    # A short or shared-structural-prefix-only ref stays rejected: the
    # 8-extra-character requirement is measured beyond "asset:asset-".
    assert _unique_prefix_correction("asset:asset-0636", admitted) is None
    assert _unique_prefix_correction("", admitted) is None
    # Ambiguity fails closed: two admitted refs sharing the same long
    # head must never be silently disambiguated.
    twins = [
        "asset:asset-aaaaaaaabbbbbbbb1111",
        "asset:asset-aaaaaaaabbbbbbbb2222",
    ]
    assert (
        _unique_prefix_correction("asset:asset-aaaaaaaabbbbbbbb3333", twins)
        is None
    )


def _registry() -> FileSpecialistToolRegistry:
    return FileSpecialistToolRegistry.__new__(FileSpecialistToolRegistry)


def _tool_names(registry, role, refs) -> list[str]:
    return [
        item["function"]["name"]
        for item in registry.manifest_for(role, admitted_target_refs=refs)
    ]


def test_tts_tools_absent_without_configuration(monkeypatch) -> None:
    monkeypatch.delenv("TTS_API_KEY", raising=False)
    registry = _registry()
    names = _tool_names(
        registry,
        SpecialistRole.VISUAL_DEVELOPMENT,
        ("project:assets",),
    )
    assert "tts_generation" not in names
    assert "create_character_voice" not in names
    # Unregistered tools resolve as unknown, not as a permission error.
    assert (
        registry.spec_for(SpecialistRole.VISUAL_DEVELOPMENT, "tts_generation")
        is None
    )


def test_tts_tools_registered_per_role_with_key(monkeypatch) -> None:
    monkeypatch.setenv("TTS_API_KEY", "sk-test")
    registry = _registry()
    visual = _tool_names(
        registry,
        SpecialistRole.VISUAL_DEVELOPMENT,
        ("project:assets",),
    )
    assert "tts_generation" in visual
    assert "create_character_voice" in visual

    editing = _tool_names(
        registry,
        SpecialistRole.AI_EDITING_DIRECTOR,
        ("timeline:t1",),
    )
    assert "tts_generation" in editing
    assert "create_character_voice" not in editing

    spec = registry.spec_for(
        SpecialistRole.VISUAL_DEVELOPMENT,
        "tts_generation",
    )
    assert spec is not None
    assert spec.requires_execution_authorization is True
    assert spec.provider_kind == "tts"


def test_tts_tools_stay_role_scoped(monkeypatch) -> None:
    monkeypatch.setenv("TTS_API_KEY", "sk-test")
    registry = _registry()
    names = _tool_names(
        registry,
        SpecialistRole.SOURCE_INTELLIGENCE,
        ("asset:a1",),
    )
    assert "tts_generation" not in names
    with pytest.raises(PermissionDeniedError):
        registry.spec_for(
            SpecialistRole.SOURCE_INTELLIGENCE,
            "create_character_voice",
        )


def test_voice_enrollment_admits_asset_children_of_project_assets(
    monkeypatch,
) -> None:
    """A project:assets-scoped visual run must reach asset:<char> targets."""

    monkeypatch.setenv("TTS_API_KEY", "sk-test")
    registry = _registry()
    spec = registry.spec_for(
        SpecialistRole.VISUAL_DEVELOPMENT,
        "create_character_voice",
    )
    assert spec is not None
    assert spec.admits_target_ref(
        role=SpecialistRole.VISUAL_DEVELOPMENT,
        target_ref="asset:char:crow",
        admitted_target_refs=("project:assets",),
    )
    assert not spec.admits_target_ref(
        role=SpecialistRole.VISUAL_DEVELOPMENT,
        target_ref="project:assets",
        admitted_target_refs=("project:assets",),
    )
    params = spec.parameters["properties"]["arguments"]["properties"]
    assert "characterRef" in params


# ---------------------------------------------------------------------------
# S2V specialist tool registration
# ---------------------------------------------------------------------------


def _clear_keys(monkeypatch) -> None:
    monkeypatch.delenv("S2V_API_KEY", raising=False)
    monkeypatch.delenv("TEXT_API_KEY", raising=False)
    monkeypatch.delenv("CREATOR_DATA_ROOT", raising=False)
    monkeypatch.delenv("CREATOR_MODEL_CONFIG_PATH", raising=False)


def test_s2v_tool_absent_without_configuration(monkeypatch) -> None:
    _clear_keys(monkeypatch)
    registry = _registry()
    names = _tool_names(
        registry,
        SpecialistRole.R2V_GENERATION_DIRECTOR,
        ("element:r2v-1",),
    )
    assert "s2v_generation" not in names
    # Unregistered tools resolve as unknown, not as a permission error.
    assert (
        registry.spec_for(
            SpecialistRole.R2V_GENERATION_DIRECTOR,
            "s2v_generation",
        )
        is None
    )


def test_s2v_tool_registered_with_key(monkeypatch) -> None:
    _clear_keys(monkeypatch)
    monkeypatch.setenv("S2V_API_KEY", "sk-test")
    registry = _registry()
    names = _tool_names(
        registry,
        SpecialistRole.R2V_GENERATION_DIRECTOR,
        ("element:r2v-1",),
    )
    assert "s2v_generation" in names

    spec = registry.spec_for(
        SpecialistRole.R2V_GENERATION_DIRECTOR,
        "s2v_generation",
    )
    assert spec is not None
    assert spec.requires_execution_authorization is True
    assert spec.long_running is True
    assert spec.wait.value == "TASK"
    assert spec.provider_kind == "s2v"
    arguments = spec.parameters["properties"]["arguments"]
    # Refs default from the s2v element's declared portrait/audio, so the
    # tool call itself has no required arguments.
    assert set(arguments["required"]) == set()
    assert arguments["properties"]["resolution"]["enum"] == ["480P", "720P"]


def test_s2v_tool_registers_via_reused_llm_key(monkeypatch) -> None:
    """The TTS gating pattern: the shared DashScope credential unlocks it."""

    _clear_keys(monkeypatch)
    monkeypatch.setenv("TEXT_API_KEY", "sk-shared")
    registry = _registry()
    names = _tool_names(
        registry,
        SpecialistRole.R2V_GENERATION_DIRECTOR,
        ("element:r2v-1",),
    )
    assert "s2v_generation" in names


def test_s2v_tool_stays_role_scoped(monkeypatch) -> None:
    _clear_keys(monkeypatch)
    monkeypatch.setenv("S2V_API_KEY", "sk-test")
    registry = _registry()
    for role, refs in (
        (SpecialistRole.VISUAL_DEVELOPMENT, ("project:assets",)),
        (SpecialistRole.AI_EDITING_DIRECTOR, ("timeline:t1",)),
        (SpecialistRole.SOURCE_INTELLIGENCE, ("asset:source-1",)),
    ):
        assert "s2v_generation" not in _tool_names(registry, role, refs)
    with pytest.raises(PermissionDeniedError):
        registry.spec_for(SpecialistRole.VISUAL_DEVELOPMENT, "s2v_generation")


def _png_bytes(size=(100, 200), color="white") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color=color).save(output, format="PNG")
    return output.getvalue()


def test_parse_object_grounding_maps_aliases_and_rejects_bad_boxes():
    detections = object_grounding.parse_object_grounding(
        """```json
        {"objects":[
          {"label":"car","bbox_2d":[100,200,900,800]},
          {"name":"cat","bounding_box":[0,0,500,1000]},
          {"name":"invalid","bbox":[900,0,100,1000]},
          {"name":"outside","bbox":[0,0,1001,1000]}
        ]}
        ```""",
        80,
        40,
    )

    assert [d["label"] for d in detections] == ["car", "cat"]
    assert detections[0]["bbox_normalized"] == [100, 200, 900, 800]
    assert detections[0]["bbox_pixel"] == [8, 8, 72, 32]
    assert detections[1]["bbox_pixel"] == [0, 0, 40, 40]


def test_parse_object_grounding_supports_ref_box_fallback():
    detections = object_grounding.parse_object_grounding(
        "<ref>red car</ref><box>(10, 20), (500, 600)</box>",
        1000,
        500,
    )

    assert detections[0]["label"] == "red car"
    assert detections[0]["bbox_pixel"] == [10, 10, 500, 300]


def test_render_object_grounding_annotation_returns_source_sized_png():
    annotated = object_grounding.render_object_grounding_annotation(
        _png_bytes(),
        [
            {
                "label": "car",
                "bbox_normalized": [100, 100, 900, 900],
                "bbox_pixel": [10, 20, 90, 180],
            },
        ],
    )

    with Image.open(io.BytesIO(annotated)) as image:
        assert image.format == "PNG"
        assert image.size == (100, 200)
        assert image.getpixel((10, 20)) != (255, 255, 255)


def test_ground_image_objects_uses_creator_vlm_and_returns_raw_response(
    tmp_path,
    monkeypatch,
):
    captured = {}
    url = "/generated/projects/project-1/task-work/request-1/input.png"
    image_path = tmp_path / "project-1/runtime/task-work/request-1/input.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(_png_bytes())
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path))

    async def fake_chat_completion(content, **kwargs):
        captured["content"] = content
        captured["kwargs"] = kwargs
        return '[{"label":"person","bbox_2d":[100,100,800,900]}]'

    vm, mc = object_grounding.vlm_model, object_grounding.model_config
    monkeypatch.setattr(vm, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(mc, "get_vlm_model_name", lambda: "qwen-test-vl")

    result = asyncio.run(
        object_grounding.ground_image_objects(_png_bytes(), url, "all people"),
    )

    assert captured["content"][0] == {
        "type": "image_url",
        "image_url": {"url": url},
    }
    assert captured["kwargs"]["temperature"] == 0.0
    assert result["model"] == "qwen-test-vl"
    assert result["imageSize"] == {"width": 100, "height": 200}
    assert result["detections"][0]["bbox_pixel"] == [10, 20, 80, 180]
    assert result["rawResponse"].startswith("[")
