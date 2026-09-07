# -*- coding: utf-8 -*-
"""Story publication precedes new short-drama visual production."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from domain.errors import ValidationError
from services.file_agent_runtime.work_graph import (
    WorkNodeStatus,
    derive_work_graph,
)
from services.media_files.image_execution import FileImageExecutionService
from services.project_files.facade import CreatorFileServices
from services.project_files.blueprint_readiness import visual_story_missing
from test_work_graph import _entity, _project, _element, _add_element

pytestmark = pytest.mark.unit


def unpublished_project():
    project = _project()
    project.scenario = "short_drama"
    project.visual.entities.items["char:puppy"] = _entity(
        "char:puppy",
        {"default": None},
    )
    project.visual.entities.order.append("char:puppy")
    project.strategy.creative_brief = "小狗蹦蹦跳跳"
    return project


def test_brief_and_visual_prompts_are_not_published_story():
    project = unpublished_project()
    project.timelines.items["timeline:main"].title = "快乐小狗"
    node = derive_work_graph(project).by_id["visual:char:puppy:default"]
    assert node.status is WorkNodeStatus.GATED
    assert node.authored_text_gap
    assert "剧情梗概或剧本" in node.missing[0]


@pytest.mark.parametrize("field", ["synopsis", "description"])
def test_publishing_story_unlocks_visuals_without_any_user_plan_gate(field):
    project = unpublished_project()
    setattr(
        project.timelines.items["timeline:main"],
        field,
        "小狗从草地起身，轻快跳跃，再停下望向镜头。",
    )
    node = derive_work_graph(project).by_id["visual:char:puppy:default"]
    assert node.status is WorkNodeStatus.READY


def test_legacy_story_and_other_scenarios_keep_their_workflow():
    project = unpublished_project()
    _add_element(project, _element("e1", character_refs=["char:puppy"]))
    assert not visual_story_missing(project, "char:puppy")
    project.timelines.items["timeline:main"].elements_by_id.clear()
    project.scenario = "general"
    assert not visual_story_missing(project, "char:puppy")


def test_snapshot_story_excluded_and_unrelated_empty_episode_ignored():
    project = unpublished_project()
    main = project.timelines.items["timeline:main"]
    historical = main.model_copy(
        deep=True,
        update={"timeline_id": "snapshot:old", "description": "旧故事"},
    )
    project.timelines.items[historical.timeline_id] = historical
    project.timelines.order.append(historical.timeline_id)
    assert visual_story_missing(project, "char:puppy")
    other = main.model_copy(
        deep=True,
        update={"timeline_id": "timeline:other"},
    )
    project.timelines.items[other.timeline_id] = other
    project.timelines.order.append(other.timeline_id)
    main.synopsis = "本集已经发布的故事"
    assert not visual_story_missing(project, "char:puppy")
    # A reference from the unfinished episode must use that episode's story.
    other.elements_by_id["e2"] = _element(
        "e2",
        narrative="",
        character_refs=["char:puppy"],
    )
    assert visual_story_missing(project, "char:puppy")


def test_direct_visual_command_cannot_bypass_story_publication(tmp_path):
    services = CreatorFileServices.create(tmp_path.resolve())
    project = unpublished_project()
    services.projects.create(project)
    provider = AsyncMock()
    service = FileImageExecutionService(services, provider=provider)
    provider.reset_mock()
    with pytest.raises(ValidationError, match="先在剧集蓝图"):
        asyncio.run(
            service.execute(
                project_id=project.project_id,
                command="GENERATE_ASSET",
                target_ref="asset:char:puppy",
                arguments={"variantId": "default"},
                idempotency_key="no-story-no-spend",
            ),
        )
    assert not provider.mock_calls
    assert service.executions.list_tasks(project.project_id) == []


def test_uploaded_character_anchors_do_not_bypass_lineup_story_gate(tmp_path):
    from services.project_files.models import VisualCastLineup
    from test_work_graph import _select_slot

    project = unpublished_project()
    project.visual.entities.items["char:puppy"] = _entity(
        "char:puppy",
        {"default": "uploaded-anchor"},
    )
    project.visual.entities.items["char:friend"] = _entity(
        "char:friend",
        {"default": "uploaded-friend"},
    )
    project.visual.entities.order.append("char:friend")
    project.visual.cast_lineups.items["cast"] = VisualCastLineup(
        lineup_id="cast",
        name="小狗合影",
        character_refs=["char:puppy", "char:friend"],
    )
    project.visual.cast_lineups.order.append("cast")
    for owner, version_id in (
        ("char:puppy", "uploaded-anchor"),
        ("char:friend", "uploaded-friend"),
    ):
        project.visual.entities.items[owner].variants.items[
            "default"
        ].generated_artifact_version_ids = [version_id]
        _select_slot(
            project,
            slot_id=f"asset:{owner}:default",
            kind="visual_asset_image",
            owner_ref=f"asset:{owner}",
            version_id=version_id,
        )
    node = derive_work_graph(project).by_id["lineup:cast"]
    assert node.status is WorkNodeStatus.GATED and node.authored_text_gap
    services = CreatorFileServices.create(tmp_path.resolve())
    services.projects.create(project)
    provider = AsyncMock()
    service = FileImageExecutionService(services, provider=provider)
    provider.reset_mock()
    with pytest.raises(ValidationError, match="先在剧集蓝图"):
        asyncio.run(
            service.execute(
                project_id=project.project_id,
                command="GENERATE_CAST_LINEUP_IMAGE",
                target_ref="lineup:cast",
                arguments={},
                idempotency_key="no-lineup-before-story",
            ),
        )
    assert not provider.mock_calls
    assert service.executions.list_tasks(project.project_id) == []
    project.timelines.items["timeline:main"].synopsis = "小狗们相遇，互相打招呼并合影。"
    assert (
        derive_work_graph(project).by_id["lineup:cast"].status
        is WorkNodeStatus.READY
    )
