# -*- coding: utf-8 -*-
"""Readable provider prose must preserve exact references and user literals."""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from models import config as model_config
from services.media_files.image_execution import FileImageExecutionService
from services.media_files.prompt_labels import media_prompt_entity_names
from services.media_files.r2v_execution import FileR2VExecutionService
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project, VisualEntity
from utils.paths import unique_task_work_path

from .conftest import accept_pending_reviews, make_r2v_element, write_png

pytestmark = pytest.mark.unit

_NAMES = {
    "char:woman": ("character", "找钥匙的女子"),
    "scene:apartment_door": ("scene", "公寓门口"),
    "prop:silver_key": ("prop", "银色钥匙"),
}


def _project() -> Project:
    project = Project.new(project_id="prompt-names", name="名称引用测试")
    for entity_id, (kind, name) in _NAMES.items():
        project.visual.entities.items[entity_id] = VisualEntity(
            entity_id=entity_id,
            kind=kind,
            name=name,
            required_variant_ids=[],
        )
        project.visual.entities.order.append(entity_id)
    return project


def test_known_entity_names_keep_reference_markers_and_project_intact():
    project = _project()
    before = project.model_dump_json()
    prompt = (
        "角色身份锁定：char:woman（灰色外套）。\n"
        "[Image 2]为角色锚点（visual-entity:char:woman），"
        "[Image 3]为场景（scene:apartment_door），"
        "[Image 4]为道具（prop:silver_key）。"
    )
    expected = (
        "角色身份锁定：找钥匙的女子（灰色外套）。\n"
        "[Image 2]为角色锚点（找钥匙的女子），"
        "[Image 3]为场景（公寓门口），"
        "[Image 4]为道具（银色钥匙）。"
    )
    assert media_prompt_entity_names(prompt, project) == expected
    assert media_prompt_entity_names(expected, project) == expected
    assert project.model_dump_json() == before


@pytest.mark.parametrize(
    "literal",
    [
        '她说“char:woman”，字幕为"scene:apartment_door"。',
        "台词：char:woman",
        "对白: char:woman",
        "字幕：prop:silver_key",
        "旁白：scene:apartment_door",
        "Dialogue: char:woman",
        "Caption: char:woman",
        "Narration: char:woman",
        "《char:woman》 'prop:silver_key' `char:woman`",
        "「char:woman」 『scene:apartment_door』",
        r'"say \"char:woman\" please"',
        '"char:woman\nscene:apartment_door"',
        '{"entity_id":"char:woman"}',
        "https://example.com/char:woman?q=prop:silver_key#char:woman",
        "oss://bucket/ref?id=char:woman",
        "artifact://char:woman@version:1 asset://char:woman@v1",
        "@char:woman @image_1 char:woman@variant:default",
        "char:unknown char:woman_extra char:woman-extra /char:woman",
        "asset:char:woman visual-entity:char:woman:other",
        "````\n```\nchar:woman\n````",
        "~~~python\nchar:woman\n~~~",
        "```python\nchar:woman",  # An unclosed fence stays opaque.
        "    char:woman\n\tprop:silver_key",
    ],
)
def test_literal_machine_and_unknown_content_is_preserved(literal):
    assert media_prompt_entity_names(literal, _project()) == literal


def test_unlabelled_or_plain_word_entities_are_not_guessed():
    project = _project()
    project.visual.entities.items["char:woman"].name = "prop:silver_key"
    project.visual.entities.items["scene:apartment_door"].name = "  "
    project.visual.entities.items["hero"] = VisualEntity(
        entity_id="hero",
        kind="character",
        name="英雄",
        required_variant_ids=[],
    )
    prompt = "char:woman scene:apartment_door hero variant:default"
    assert media_prompt_entity_names(prompt, project) == prompt


# Assert the whole edit-to-publication lifecycle in one regression case.
# pylint: disable-next=too-many-statements
def test_image_and_video_provider_receive_names_and_exact_reference_order(
    tmp_path,
    monkeypatch,
):
    """
    Use real execution/storage with fake providers, including role append.
    """
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(
        model_config,
        "get_video_model_name",
        lambda: "wan3.0-video-prime",
    )
    monkeypatch.setattr(model_config, "get_video_backend", lambda: "wan")
    services = CreatorFileServices.create(tmp_path)
    project = _project()
    element = make_r2v_element(
        "shot:search",
        storyboard_prompt="char:woman站在scene:apartment_door，拿prop:silver_key。",
        video_prompt="char:woman拿着prop:silver_key走到scene:apartment_door。",
    )
    project.timelines.items["timeline:main"].elements_by_id[
        element.element_id
    ] = element
    # Source order is intentionally different from alphabetical entity order.
    refs = ["ref:key", "ref:woman", "ref:scene"]
    names = ["prop:silver_key", "char:woman", "scene:apartment_door"]
    candidate = project.model_dump(mode="json")
    for version_id, name in zip(refs, names, strict=True):
        url = f"https://example.invalid/{version_id}.png"
        candidate["assets"]["source_versions_by_id"][version_id] = {
            "version_id": version_id,
            "logical_asset_id": f"asset:{version_id}",
            "name": name,
            "checksum": hashlib.sha256(url.encode()).hexdigest(),
            "media_kind": "image",
            "media_type": "image/png",
            "created_at": project.created_at.isoformat(),
            "metadata": {
                "sourceKind": "remote_url",
                "checksumKind": "source_url_sha256",
                "publicSourceUrl": url,
            },
        }
    creation = candidate["timelines"]["items"]["timeline:main"][
        "elements_by_id"
    ][element.element_id]["creation"]
    creation["storyboard_reference_version_ids"] = refs
    creation["video_reference_version_ids"] = refs
    services.projects.create(Project.model_validate(candidate))
    png_path = tmp_path / "fixture.png"
    write_png(
        png_path,
        width=4,
        height=4,
        pixel=lambda _x, _y: (24, 36, 48, 255),
    )

    class ImageProvider:
        model_name = "qwen-image-3.0-pro"
        calls = []

        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "content": png_path.read_bytes(),
                "media_type": "image/png",
            }

    class VideoProvider:
        calls = []

        async def submit(self, **kwargs):
            self.calls.append(kwargs)
            return "fake-provider-task"

        async def poll(self, provider_task_id):
            path = unique_task_work_path("video", ".mp4", prefix="name-test-")
            path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"fake" * 64)
            return {
                "task_id": provider_task_id,
                "status": "SUCCEEDED",
                "result_url": path.resolve().as_uri(),
                "media_type": "video/mp4",
                "durationSeconds": 4,
            }

    image_provider, video_provider = ImageProvider(), VideoProvider()
    image_result = asyncio.run(
        FileImageExecutionService(services, provider=image_provider).execute(
            project_id=project.project_id,
            command="GENERATE_STORYBOARD_IMAGE",
            target_ref=f"element:{element.element_id}",
            arguments={},
            idempotency_key="names-storyboard",
        ),
    )
    accept_pending_reviews(services, project.project_id)

    async def video():
        worker = FileR2VExecutionService(
            services,
            provider=video_provider,
            poll_interval_seconds=0.01,
        )
        result = await worker.dispatch(
            project_id=project.project_id,
            target_ref=f"element:{element.element_id}",
            arguments={},
            idempotency_key="names-video",
        )
        task = await worker.wait_for_task(
            project.project_id,
            result.task_id,
            timeout_seconds=5,
        )
        await worker.shutdown()
        return task

    video_task = asyncio.run(video())
    assert video_task.status.value == "SUCCEEDED"
    assert len(image_provider.calls) == len(video_provider.calls) == 1
    for call in [*image_provider.calls, *video_provider.calls]:
        for entity_id, (_, name) in _NAMES.items():
            assert entity_id not in call["prompt"]
            assert name in call["prompt"]
    image_prompt = image_provider.calls[0]["prompt"]
    video_prompt = video_provider.calls[0]["prompt"]
    assert "图1 = 银色钥匙" in image_prompt
    assert "图2 = 找钥匙的女子" in image_prompt
    assert "图3 = 公寓门口" in image_prompt
    assert "图2 = 银色钥匙" in video_prompt
    assert "图3 = 找钥匙的女子" in video_prompt
    assert "图4 = 公寓门口" in video_prompt
    # The stored request retains exact source IDs, and video prepends only its
    # selected storyboard. Existing authored project text remains unmodified.
    assert video_task.metadata["requestSnapshot"]["referenceVersionIds"] == [
        image_result.artifact_version_id,
        *refs,
    ]
    after = services.projects.read(project.project_id).project
    saved = (
        after.timelines.items["timeline:main"]
        .elements_by_id[element.element_id]
        .creation
    )
    assert saved.storyboard_prompt == creation["storyboard_prompt"]
    assert saved.video_prompt == creation["video_prompt"]
    assert saved.storyboard_reference_version_ids == refs
    assert saved.video_reference_version_ids == refs
