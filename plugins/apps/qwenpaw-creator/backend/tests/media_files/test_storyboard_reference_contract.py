# -*- coding: utf-8 -*-
# Pytest fixtures and contract probes retain exact types and private seams.
# pylint: disable=protected-access
# pylint: disable=unused-argument
# pylint: disable=use-implicit-booleaness-not-comparison
"""Canonical storyboard references: preview, resolution and payload agree.

All references below are synthetic. No network or real provider is used.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from domain.enums import CreatorCommandType
from domain.errors import NotFoundError, ValidationError
from models.image import openai_provider
from models.image.base import image_model_prompt_guidance
from models.image.dashscope_provider import DashScopeImageModel
from services.media_files.image_execution import (
    ImageReferenceBudgetError,
    _resolve_request,
)
from services.media_files.visual_reference_resolution import (
    preview_r2v_reference_order,
)
from services.project_files.models import Project, SourceAssetVersion
from services.project_files.store import ProjectSnapshot
from utils.exceptions import ModelError

from .conftest import make_r2v_element

pytestmark = pytest.mark.unit


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
    ("model", "word"),
    [
        ("qwen-image-3.0-pro", "图2"),
        ("qwen-image-edit-plus", "图2"),
        ("gpt-image-2", "第2张参考图"),
        ("gemini-3-pro-image", "image 2"),
        ("gemini-2.5-flash-image", "image 2"),
        ("flux-2-pro", "image 2"),
        ("doubao-seedream-4-5", "第2张参考图"),
    ],
)
def test_actual_resolver_matches_phase_preview_and_provider_wording(
    tmp_path,
    model,
    word,
):
    snapshot = _snapshot()
    before = snapshot.project.model_dump_json()
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
    assert [ref["index"] for ref in preview["references"]] == [1, 2, 3]
    assert all(ref["available"] for ref in preview["references"])
    assert len(request.reference_image_urls) == 3
    assert request.reference_image_urls[0] == request.reference_image_urls[1]
    assert f"{word} 的人物" in request.prompt
    assert "[Image" not in request.prompt
    assert snapshot.project.model_dump_json() == before


@pytest.mark.parametrize("index", [0, 4, 99])
def test_invalid_indices_are_visible_and_rejected_before_provider(
    tmp_path,
    index,
):
    snapshot = _snapshot(f"采用 [Image {index}] 的人物。")
    preview = preview_r2v_reference_order(
        snapshot.project,
        "shot:one",
        stage="storyboard",
        image_model_name="qwen-image-3.0-pro",
        project_root=tmp_path,
    )
    assert preview["invalidMarkerIndices"] == [index]
    assert preview["ready"] is False
    with pytest.raises(ValidationError, match="参考图编号"):
        _resolve(snapshot, tmp_path)


def test_missing_middle_version_stays_in_position_and_cannot_submit(tmp_path):
    snapshot = _snapshot()
    del snapshot.project.assets.source_versions_by_id["src:a-person"]
    preview = preview_r2v_reference_order(
        snapshot.project,
        "shot:one",
        stage="storyboard",
        image_model_name="qwen-image-3.0-pro",
        project_root=tmp_path,
    )
    assert [
        (item["index"], item["available"]) for item in preview["references"]
    ] == [(1, True), (2, False), (3, True)]
    assert preview["references"][1]["versionId"] == "src:a-person"
    assert not preview["ready"]
    with pytest.raises(NotFoundError):
        _resolve(snapshot, tmp_path)


def test_explicit_storyboard_references_do_not_get_auto_trimmed(tmp_path):
    snapshot = _snapshot("历史自然语言：第一张参考图的背景。")
    # A one-reference model cannot silently discard the final two images.
    preview = preview_r2v_reference_order(
        snapshot.project,
        "shot:one",
        stage="storyboard",
        image_model_name="ideogram-v3",
        project_root=tmp_path,
    )
    assert len(preview["references"]) == 3 and not preview["ready"]
    with pytest.raises(ImageReferenceBudgetError):
        _resolve(snapshot, tmp_path, "ideogram-v3")


def test_legacy_prose_and_stored_prompt_are_not_rewritten(tmp_path):
    prompt = "第一张参考图的背景，第二张参考图的人物。"
    snapshot = _snapshot(prompt)
    assert _resolve(snapshot, tmp_path).prompt.startswith(prompt)
    assert (
        snapshot.project.timelines.items["timeline:main"]
        .elements_by_id["shot:one"]
        .creation.storyboard_prompt
        == prompt
    )


@pytest.mark.parametrize("mode", ["generate", "edit"])
def test_dashscope_missing_middle_input_never_degrades_or_shifts(
    monkeypatch,
    mode,
):
    model = DashScopeImageModel(
        model_name="qwen-image-3.0-pro",
        api_key="fake",
        base_url="https://provider.invalid",
        timeout=5,
    )
    seen = []

    async def public(url):
        seen.append(url)
        return None if url == "missing" else url

    monkeypatch.setattr(model, "_public_reference_url", public)
    with pytest.raises(ModelError, match="reference cannot be read"):
        asyncio.run(
            model._build_body("图3", "9:16", ["a", "missing", "c"], mode=mode),
        )
    assert seen == ["a", "missing"]


def test_dashscope_payload_preserves_distinct_positions_sharing_url():
    model = DashScopeImageModel(
        model_name="qwen-image-3.0-pro",
        api_key="fake",
        base_url="https://provider.invalid",
        timeout=5,
    )
    url = "https://images.example/shared.png"
    body = asyncio.run(model._build_body("图1 与 图2", "9:16", [url, url]))
    assert body["input"]["messages"][0]["content"] == [
        {"image": url},
        {"image": url},
        {"text": "图1 与 图2"},
    ]


@pytest.mark.parametrize(
    "builder",
    ["build_reference_image_files", "build_reference_image_data_urls"],
)
def test_openai_both_payload_paths_preserve_order_and_duplicate_url(
    monkeypatch,
    builder,
):
    seen = []

    async def read(url):
        seen.append(url)
        return b"synthetic fixture", "image.png"

    monkeypatch.setattr(openai_provider, "read_reference_media", read)
    result = asyncio.run(
        getattr(openai_provider, builder)(["shared", "shared", "last"]),
    )
    assert len(result) == 3 and seen == ["shared", "shared", "last"]
    with pytest.raises(ModelError):
        asyncio.run(getattr(openai_provider, builder)(["url"] * 17))
    with pytest.raises(ModelError):
        asyncio.run(getattr(openai_provider, builder)(["first", "", "third"]))


def test_model_guidance_authors_canonical_markers_for_storyboards():
    for model in [
        "qwen-image-3.0-pro",
        "gpt-image-2",
        "gemini-3-pro-image",
        "flux-2-pro",
        "doubao-seedream-4-5",
        "ideogram-v3",
    ]:
        guidance = image_model_prompt_guidance(model)
        assert "[Image 1]" in guidance and "分镜图自身" in guidance


def test_base_image_call_rejects_empty_position_before_request():
    model = DashScopeImageModel(
        model_name="qwen-image-3.0-pro",
        api_key="fake",
        base_url="https://provider.invalid",
        timeout=5,
    )
    with pytest.raises(ModelError, match="empty input"):
        asyncio.run(
            model.generate("图3", reference_image_urls=["first", "", "third"]),
        )


def test_http_stage_selects_storyboard_without_mutating_project(
    tmp_path,
    monkeypatch,
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import project_file_routes
    from services.project_files.facade import CreatorFileServices

    services = CreatorFileServices.create(tmp_path)
    snapshot = services.projects.create(_snapshot().project)
    monkeypatch.setattr(
        project_file_routes,
        "get_image_model_name",
        lambda: "qwen-image-3.0-pro",
    )
    app = FastAPI()
    app.include_router(project_file_routes.router)
    app.dependency_overrides[
        project_file_routes.project_file_services
    ] = lambda: services
    path = next(
        route.path
        for route in project_file_routes.router.routes
        if route.path.endswith("/r2v-references")
    )
    path = path.replace("{project_id}", snapshot.project.project_id).replace(
        "{element_id}",
        "shot:one",
    )
    with TestClient(app) as client:
        response = client.get(path, params={"stage": "storyboard"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["stage"] == "storyboard"
        assert [ref["versionId"] for ref in result["references"]] == [
            "src:z-scene",
            "src:a-person",
            "src:m-prop",
        ]
        assert result["ready"] is True
        assert client.get(path, params={"stage": "unknown"}).status_code == 422
        assert client.get(path).json()["stage"] == "video"
    assert (
        services.projects.read(snapshot.project.project_id).etag
        == snapshot.etag
    )


def test_storyboard_legacy_auto_budget_plan_matches_preview(tmp_path):
    from services.project_files.models import VisualEntity, VisualVariant

    snapshot = _snapshot("第一张参考图的场景。")
    creation = (
        snapshot.project.timelines.items["timeline:main"]
        .elements_by_id["shot:one"]
        .creation
    )
    ids = creation.storyboard_reference_version_ids
    creation.storyboard_reference_version_ids = []
    for index, version_id in enumerate(ids):
        entity_id = f"character:{index}"
        # Only the source-independent planner is exercised here; the
        # selection is intentionally a synthetic opaque version identity.
        snapshot.project.visual.entities.items[entity_id] = VisualEntity(
            entity_id=entity_id,
            kind="character",
            name=f"人物{index}",
            required_variant_ids=["default"],
            variants={
                "order": ["default"],
                "items": {
                    "default": VisualVariant(
                        variant_id="default",
                        selected_artifact_version_id=version_id,
                    ),
                },
            },
        )
        snapshot.project.visual.entities.order.append(entity_id)
        creation.character_refs.append(entity_id)
        creation.visual_variant_refs[entity_id] = "default"
    preview = preview_r2v_reference_order(
        snapshot.project,
        "shot:one",
        stage="storyboard",
        image_model_name="ideogram-v3",
    )
    assert [ref["versionId"] for ref in preview["references"]] == ids[:1]
    assert preview["budgetDroppedVersionIds"] == ids[1:]
    # Authoring an explicit index pins the full list, even if that index is
    # within the smaller model budget. No automatic renumbering is allowed.
    creation.storyboard_prompt = "采用 [Image 1] 的场景。"
    pinned = preview_r2v_reference_order(
        snapshot.project,
        "shot:one",
        stage="storyboard",
        image_model_name="ideogram-v3",
    )
    assert [ref["versionId"] for ref in pinned["references"]] == ids
    assert pinned["budgetDroppedVersionIds"] == [] and not pinned["ready"]
