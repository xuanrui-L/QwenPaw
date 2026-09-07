# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Generation intent has one narrative; retired authoring rows are inert."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from domain.enums import TaskKind, TaskStatus
from services.file_agent_runtime.work_graph import derive_work_graph
from services.project_files.models import (
    ElementLocation,
    Project,
    R2VCreation,
    TimelineElement,
    TimelineSpan,
)
from services.project_files.prompt_sync import prompt_sync_status, sync_stamp

pytestmark = pytest.mark.unit
TID, EID = "timeline:main", "clip"


def _project():
    project = Project.new(project_id="narrative-test", name="Narrative")
    project.timelines.items[TID].elements_by_id[EID] = TimelineElement(
        element_id=EID,
        label="寻找钥匙",
        span=TimelineSpan(start_tick=0, duration_tick=6000),
        location=ElementLocation(),
        creation=R2VCreation(
            narrative="女子推门、停下、回头，轻声说：钥匙在这里。",
            storyboard_prompt="9格3×3分镜，每格16:9，依次呈现动作与反应。",
            video_prompt="[Image 1]分镜顺序。女子推门、停下、回头，轻声说：钥匙在这里。",
        ),
    )
    return project


@pytest.mark.parametrize(
    "legacy",
    [
        None,
        [],
        "invalid",
        {
            "order": ["missing"],
            "items": {
                "bad": {
                    "dialogue": "旧台词",
                    "camera": "未知",
                    "character_refs": ["不存在"],
                    "duration_seconds": -100,
                },
            },
        },
    ],
)
def test_old_shots_are_ignored_even_when_invalid(legacy):
    project = _project()
    document = project.model_dump(mode="json")
    creation = document["timelines"]["items"][TID]["elements_by_id"][EID][
        "creation"
    ]
    creation.update(shots=legacy, min_dialogue_ratio="invalid")
    loaded = Project.model_validate(document)
    assert loaded.model_dump(mode="json") == project.model_dump(mode="json")
    assert derive_work_graph(loaded) == derive_work_graph(project)


def test_new_schema_has_no_authoring_shot_type_or_fields():
    schema = Project.model_json_schema()
    assert "Shot" not in schema["$defs"]
    assert "ShotCamera" not in schema["$defs"]
    fields = schema["$defs"]["R2VCreation"]["properties"]
    assert "shots" not in fields and "min_dialogue_ratio" not in fields
    with pytest.raises(ValidationError, match="extra_forbidden"):
        R2VCreation.model_validate({"narrative": "内容", "invented": True})


def test_legacy_sync_hash_does_not_mark_existing_content_outdated():
    document = _project().model_dump(mode="json")
    creation = document["timelines"]["items"][TID]["elements_by_id"][EID][
        "creation"
    ]
    old = sync_stamp(document, TID, EID)
    del old["contract_version"]
    old["plan_fingerprint"] = "0" * 64
    creation.update(shots={"garbage": True}, prompt_sync=old)
    assert prompt_sync_status(document, TID, EID)["status"] == "legacy"
    loaded = Project.model_validate(document)
    assert prompt_sync_status(loaded, TID, EID)["status"] == "legacy"
    assert (
        loaded.timelines.items[TID].elements_by_id[EID].creation.narrative
        == creation["narrative"]
    )


def test_new_sync_ignores_legacy_rows_but_detects_narrative_edit():
    document = _project().model_dump(mode="json")
    creation = document["timelines"]["items"][TID]["elements_by_id"][EID][
        "creation"
    ]
    creation["prompt_sync"] = sync_stamp(document, TID, EID)
    before = deepcopy(creation)
    creation["shots"] = {"unrelated": "retired rows"}
    assert prompt_sync_status(document, TID, EID)["status"] == "current"
    creation["narrative"] += "左手把钥匙放进包里。"
    assert prompt_sync_status(document, TID, EID)["changedSources"] == [
        "currentPlan",
    ]
    assert creation["storyboard_prompt"] == before["storyboard_prompt"]


def test_legacy_failed_storyboard_does_not_retry_just_for_the_new_hash():
    from types import SimpleNamespace
    from services.file_agent_runtime.work_graph import WorkNodeStatus

    project = _project()
    task = SimpleNamespace(
        task_id="legacy-failed",
        kind=TaskKind.IMAGE_GENERATION,
        status=TaskStatus.FAILED,
        input_refs=[f"element:{EID}"],
        metadata={"targetRef": f"element:{EID}"},
        idempotency_key=f"dag-storyboard:{EID}-0123456789abcdef",
        error={"message": "provider failed"},
        updated_at="2026-09-01T00:00:00Z",
    )
    graph = derive_work_graph(project, [task], media_models=("image", "video"))
    assert graph.by_id[f"storyboard:{EID}"].status is WorkNodeStatus.FAILED
    assert not graph.ready_media_nodes()
