# -*- coding: utf-8 -*-
# Pytest fixtures and contract probes retain exact types and private seams.
# pylint: disable=protected-access
# pylint: disable=redefined-outer-name
# pylint: disable=unused-argument
# pylint: disable=use-implicit-booleaness-not-comparison
"""Real commits and frozen text proposals; no media or live model calls."""

import asyncio
import hashlib
import json
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI

from api.dependencies import project_file_services
from api.prompt_sync_routes import router
from domain.errors import ConflictError, ValidationError
from models import config as model_config
from services.file_agent_runtime.model_client import (
    AgentModelTurn,
    CallbackAgentChatClient,
)
from services.file_agent_runtime.work_graph import derive_work_graph
from services.media_files.r2v_execution import _resolve_request
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    ElementLocation,
    EntityCollection,
    ArtifactSlot,
    ArtifactVersion,
    IndexedFile,
    Project,
    R2VCreation,
    TimelineElement,
    TimelineSpan,
    VisualEntity,
    VisualVariant,
)
from services.project_files.prompt_sync import (
    assert_r2v_prompt_sync,
    prompt_sync_status,
    sync_stamp,
)
from services.prompt_sync_service import PromptSyncService
from services.runtime_files.execution_store import ProjectExecutionStore

pytestmark = pytest.mark.unit
PID, TID, EID = "prompt-sync-test", "timeline:main", "scene-one"
SB = "输出9:16分镜图，9个分镜格，3×3网格，每一个分镜格内部均为9:16，完整清晰分隔边界，依次展示连续动作。"
VD = "[Image 1]是本镜头分镜参考，读取时间顺序生成一个连续6秒视频，不展示宫格。女子只说“原来在这儿。”。"


@pytest.fixture()
def services(tmp_path, monkeypatch):
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(
        model_config,
        "get_video_model_name",
        lambda: "wan3.0-video-prime",
    )
    monkeypatch.setattr(model_config, "get_video_backend", lambda: "wan")
    monkeypatch.setattr(
        model_config,
        "get_image_model_name",
        lambda: "qwen-image-3.0-pro",
    )
    services = CreatorFileServices.create(tmp_path)
    project = Project.new(project_id=PID, name="找钥匙")
    project.settings.aspect_ratio = "9:16"
    element = TimelineElement(
        element_id=EID,
        label="发现钥匙",
        location=ElementLocation(),
        span=TimelineSpan(start_tick=0, duration_tick=6000),
        creation=R2VCreation(
            narrative="左手持钥匙，右手停包边。对白：原来在这儿。",
            storyboard_prompt=SB,
            video_prompt=VD,
        ),
    )
    project.timelines.items[TID].elements_by_id[EID] = element
    services.projects.create(project)
    return services


def creation(document):
    return document["timelines"]["items"][TID]["elements_by_id"][EID][
        "creation"
    ]


def edit(services, mutator, **kwargs):
    base = services.projects.read(PID)
    candidate = base.project.model_dump(mode="json")
    mutator(creation(candidate))
    return services.commits.commit(
        base=base,
        candidate=candidate,
        origin="frontend_edit",
        **kwargs,
    )


def plan_edit(services):
    return edit(
        services,
        lambda c: c.update(
            narrative="右手停包边，只有左前臂抬钥匙至胸前。对白：原来在这儿。",
        ),
    )


def state(services):
    return PromptSyncService(services).status(PID, TID, EID)


def model_payload(messages):
    return {
        **json.loads(messages[1]["content"]),
        **json.loads(messages[2]["content"]),
    }


def input_value(payload, source):
    return (
        payload["authoritativeInputs"]
        if source in payload["authoritativeInputs"]
        else payload["referenceOnly"]
    )[source]


def model_narrative(payload):
    return input_value(payload, "currentPlan")


def client(callback=None):
    async def complete(messages, tools):
        assert tools == []
        payload = model_payload(messages)
        if callback:
            callback()
        return AgentModelTurn(
            content=json.dumps(
                {
                    "narrative": model_narrative(payload),
                    "storyboardPrompt": SB + "左手抬钥匙。",
                    "videoPrompt": VD + "右手停包边。",
                },
                ensure_ascii=False,
            ),
        )

    return CallbackAgentChatClient(complete)


def test_legacy_and_no_op_do_not_create_sync_or_change_generation(services):
    assert state(services)["status"] == "legacy"
    result = edit(services, lambda c: None)
    assert result.snapshot.generation == 0
    assert (
        result.snapshot.project.timelines.items[TID]
        .elements_by_id[EID]
        .creation.prompt_sync
        is None
    )
    assert_r2v_prompt_sync(result.snapshot.project, TID, EID)


def test_plan_edit_gates_graph_and_fresh_video_resolution(services):
    snapshot = plan_edit(services).snapshot
    assert state(services)["status"] == "needs_update"
    graph = derive_work_graph(snapshot.project)
    for node_id in (f"storyboard:{EID}", f"video:{EID}"):
        node = graph.by_id[node_id]
        assert node.status.value == "gated"
        assert "镜头与提示词待同步" in node.missing[0]
    with pytest.raises(ValidationError, match="片段内容"):
        _resolve_request(
            snapshot=snapshot,
            project_root=services.projects.project_root(PID),
            target_ref=f"element:{EID}",
            arguments={},
        )


def test_proposal_is_text_only_then_accepts_both_prompts_atomically(services):
    plan_edit(services)
    service = PromptSyncService(services, client=client())

    async def run():
        proposal = await service.propose(PID, TID, EID)
        assert proposal["beforeStoryboardPrompt"] == SB
        assert proposal["beforeVideoPrompt"] == VD
        assert proposal["beforeNarrative"] == state(services)["narrative"]
        assert proposal["baselineToken"] == state(services)["baselineToken"]
        assert state(services)["storyboardPrompt"] == SB
        assert ProjectExecutionStore(services.root).list_tasks(PID) == []
        result = await service.accept(PID, TID, EID, proposal["proposalId"])
        assert result["ok"] is True
        assert result["generation"] == 2
        assert state(services)["status"] == "current"
        assert (
            state(services)["storyboardPrompt"] == proposal["storyboardPrompt"]
        )
        assert state(services)["videoPrompt"] == proposal["videoPrompt"]
        assert state(services)["narrative"] == proposal["narrative"]
        assert ProjectExecutionStore(services.root).list_tasks(PID) == []
        repeated = await service.accept(PID, TID, EID, proposal["proposalId"])
        assert repeated == {"ok": True, "generation": 2, "replayed": True}
        assert services.projects.read(PID).generation == 2

    asyncio.run(run())


@pytest.mark.parametrize("source", ["storyboardPrompt", "videoPrompt"])
def test_prompt_source_updates_shot_and_other_prompt_in_one_replayable_commit(
    services,
    source,
):
    new_line = "走吧。"
    new_description = "女子只抬一次左手，身体朝向不变，右手停包边。她说：“走吧。”。"
    new_sb = SB + "女子只抬一次左手，身体朝向不变。声音意图为“走吧。”，不把台词画在图内。"
    new_vd = VD.replace("原来在这儿。", new_line) + "身体朝向不变，女子只抬一次左手。"
    field, text = (
        ("storyboard_prompt", new_sb)
        if source == "storyboardPrompt"
        else ("video_prompt", new_vd)
    )
    edit(services, lambda c: c.update({field: text}))
    assert state(services)["changedSources"] == [source]
    assert state(services)["suggestedSource"] == source
    before = state(services)

    async def complete(messages, tools):
        assert tools == []
        payload = model_payload(messages)
        assert payload["source"] == source
        assert payload["changedSources"] == [source]
        assert payload["authoritativeInputs"] == {source: text}
        assert source not in payload["referenceOnly"]
        assert payload["targetFields"] == [
            "narrative",
            (
                "videoPrompt"
                if source == "storyboardPrompt"
                else "storyboardPrompt"
            ),
        ]
        assert "beforeVideoPrompt" not in messages[1]["content"]
        assert "beforeStoryboardPrompt" not in messages[1]["content"]
        assert (
            json.loads(messages[-1]["content"])["authoritativeInputs"][source]
            == text
        )
        assert "不恢复已删除的台词" in messages[0]["content"]
        shots = model_narrative(payload)
        shots = new_description
        return AgentModelTurn(
            content=json.dumps(
                {
                    "narrative": shots,
                    "storyboardPrompt": new_sb,
                    "videoPrompt": new_vd,
                },
                ensure_ascii=False,
            ),
        )

    service = PromptSyncService(
        services,
        client=CallbackAgentChatClient(complete),
    )

    async def run():
        draft = await service.propose(PID, TID, EID, source=source)
        assert draft["source"] == source
        assert draft["beforeNarrative"] == before["narrative"]
        assert state(services) == before
        result = await service.accept(PID, TID, EID, draft["proposalId"])
        current = state(services)
        assert current["status"] == "current"
        assert (
            current["changedSources"] == []
            and current["suggestedSource"] is None
        )
        assert current["narrative"] == new_description
        assert new_line in current["narrative"]
        assert current["storyboardPrompt"] == new_sb
        assert current["videoPrompt"] == new_vd
        assert "原来在这儿。" not in json.dumps(
            [current["narrative"], new_sb, new_vd],
            ensure_ascii=False,
        )
        replay = await service.accept(PID, TID, EID, draft["proposalId"])
        assert replay == {
            "ok": True,
            "generation": result["generation"],
            "replayed": True,
        }
        assert ProjectExecutionStore(services.root).list_tasks(PID) == []

    asyncio.run(run())


def test_reverse_prompt_can_remove_old_speech_without_hidden_fallback(
    services,
):
    silent_video = "[Image 1]提供分镜顺序，生成9:16连续6秒视频。有意静默，不说话，不旁白，不配乐；女子只抬一次左手。"
    edit(services, lambda c: c.update(video_prompt=silent_video))

    async def complete(messages, tools):
        payload = model_payload(messages)
        assert "原来在这儿。" in input_value(payload, "currentPlan")
        shots = model_narrative(payload)
        shots = "女子只抬一次左手，右手停在包边。无对白或画外旁白。"
        return AgentModelTurn(
            content=json.dumps(
                {
                    "narrative": shots,
                    "storyboardPrompt": SB + "有意静默。",
                    "videoPrompt": silent_video,
                },
                ensure_ascii=False,
            ),
        )

    service = PromptSyncService(
        services,
        client=CallbackAgentChatClient(complete),
    )

    async def run():
        draft = await service.propose(PID, TID, EID, source="videoPrompt")
        await service.accept(PID, TID, EID, draft["proposalId"])
        current = state(services)
        assert "原来在这儿" not in current["narrative"]
        assert current["videoPrompt"] == silent_video
        assert current["status"] == "current"

    asyncio.run(run())


def test_mixed_edits_are_all_authoritative_and_conflict_creates_no_draft(
    services,
):
    plan_edit(services)
    edit(
        services,
        lambda c: c.update(video_prompt=VD + "只抬右手，左手始终不动。"),
    )
    assert state(services)["changedSources"] == ["currentPlan", "videoPrompt"]
    assert state(services)["suggestedSource"] == "mixed"
    before = services.projects.read(PID).project.model_dump_json()

    async def complete(messages, tools):
        payload = model_payload(messages)
        assert payload["source"] == "mixed"
        assert payload["changedSources"] == ["currentPlan", "videoPrompt"]
        assert "只有左前臂" in input_value(payload, "currentPlan")
        assert "只抬右手" in payload["authoritativeInputs"]["videoPrompt"]
        return AgentModelTurn(
            content=json.dumps(
                {
                    "conflict": "镜头要求抬左手，视频提示词要求左手不动，请先统一。",
                },
                ensure_ascii=False,
            ),
        )

    with pytest.raises(ValidationError, match="存在冲突"):
        asyncio.run(
            PromptSyncService(
                services,
                client=CallbackAgentChatClient(complete),
            ).propose(PID, TID, EID, source="videoPrompt"),
        )
    assert services.projects.read(PID).project.model_dump_json() == before
    assert not (
        services.projects.project_root(PID) / "runtime/prompt-proposals"
    ).exists()


@pytest.mark.parametrize("change", ["plan", "storyboard", "video"])
def test_result_from_old_model_snapshot_cannot_overwrite_edits(
    services,
    change,
):
    def concurrent_edit():
        if change == "plan":
            plan_edit(services)
        else:
            edit(
                services,
                lambda c: c.update(
                    {
                        f"{change}_prompt": c[f"{change}_prompt"] + "用户最新手改。",
                    },
                ),
            )

    service = PromptSyncService(services, client=client(concurrent_edit))

    async def run():
        proposal = await service.propose(PID, TID, EID)
        before = services.projects.read(PID).project.model_dump_json()
        with pytest.raises(ConflictError):
            await service.accept(PID, TID, EID, proposal["proposalId"])
        assert services.projects.read(PID).project.model_dump_json() == before

    asyncio.run(run())


def test_compare_is_repeated_under_commit_lock(services, monkeypatch):
    service = PromptSyncService(services, client=client())

    async def run():
        proposal = await service.propose(PID, TID, EID)
        original = services.commit_candidate

        async def racing(**kwargs):
            plan_edit(services)
            # Forward the production call unchanged through this test spy.
            # pylint: disable-next=missing-kwoa
            return await original(**kwargs)

        monkeypatch.setattr(
            type(services),
            "commit_candidate",
            lambda _self, **kwargs: racing(**kwargs),
        )
        with pytest.raises(ConflictError):
            await service.accept(PID, TID, EID, proposal["proposalId"])
        assert state(services)["videoPrompt"] == VD

    asyncio.run(run())


def test_ordinary_writer_cannot_forge_current_sync_stamp(services):
    base = services.projects.read(PID)
    candidate = base.project.model_dump(mode="json")
    creation(candidate)["narrative"] = "不同动作"
    creation(candidate)["prompt_sync"] = sync_stamp(candidate, TID, EID)
    result = services.commits.commit(
        base=base,
        candidate=candidate,
        origin="frontend_edit",
    )
    assert (
        prompt_sync_status(result.snapshot.project, TID, EID)["status"]
        == "needs_update"
    )


def test_both_new_prompts_and_plan_need_review_not_automatic_alignment(
    services,
):
    edit(
        services,
        lambda c: (
            c.update(narrative="新动作"),
            c.update(
                storyboard_prompt=SB + "新动作",
                video_prompt=VD + "新动作",
            ),
        ),
    )
    assert state(services)["status"] == "needs_confirmation"
    # A partial review restoring one original prompt must not stay aligned.
    edit(services, lambda c: c.update(video_prompt=VD))
    assert state(services)["status"] == "needs_update"


def test_get_route_returns_one_consistent_baseline_and_rejects_snapshots(
    services,
):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[project_file_services] = lambda: services

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as http:
            response = await http.get(
                f"/projects/{PID}/timelines/{TID}/elements/{EID}/prompt-sync",
            )
            assert response.status_code == 200
            assert response.json()["videoPrompt"] == VD
            response = await http.get(
                f"/projects/{PID}/timelines/{TID}"
                f"/elements/snapshot:{EID}/prompt-sync",
            )
            assert response.status_code == 422

    asyncio.run(run())


def test_empty_narrative_is_rejected_before_text_model_call(services):
    edit(
        services,
        lambda c: c.update(narrative=""),
    )
    called = []
    with pytest.raises(ValidationError, match="片段内容"):
        asyncio.run(
            PromptSyncService(
                services,
                client=client(lambda: called.append(True)),
            ).propose(PID, TID, EID),
        )
    assert not called


def test_generated_proposal_completes_canonical_reference_coverage(
    services,
    monkeypatch,
):
    from services import prompt_sync_service

    monkeypatch.setattr(
        prompt_sync_service,
        "_references",
        lambda *_: {
            "storyboard": [{"index": 1, "kind": "artifact", "name": "女子"}],
            "video": [
                {"index": 1, "kind": "storyboard", "name": "本镜头分镜"},
            ],
            "storyboardReferenceLimit": 3,
        },
    )
    proposal = asyncio.run(
        PromptSyncService(services, client=client()).propose(PID, TID, EID),
    )
    assert "[Image 1]是女子" in proposal["storyboardPrompt"]
    assert proposal["videoPrompt"].count("[Image 1]") == 1
    assert state(services)["storyboardPrompt"] == SB
    assert ProjectExecutionStore(services.root).list_tasks(PID) == []


def test_derived_hashes_excluded_from_full_and_partial_review(
    services,
):
    from services.project_files.review import ReviewDecisionItem
    from .conftest import review_boundary, review_commit_kwargs

    base = services.projects.read(PID)
    candidate = base.project.model_dump(mode="json")
    c = creation(candidate)
    c["narrative"] = "新动作只抬左手"
    c.update(storyboard_prompt=SB + "新动作。", video_prompt=VD + "新动作。")
    result = services.commits.commit(
        base=base,
        candidate=candidate,
        **review_commit_kwargs(review_boundary(base)),
    )
    assert result.review is not None
    assert len(result.review.operations) == 3
    assert all(
        "prompt_sync" not in row.json_pointer
        for row in result.review.operations
    )
    assert any(
        "prompt_sync" in row.json_pointer for row in result.changeset.changes
    )
    services.reviews.decide(
        project_id=PID,
        review_id=result.review.review_id,
        decision_token=result.review.decision_token,
        decisions=[
            ReviewDecisionItem(
                operation_id=row.operation_id,
                decision=(
                    "REJECT"
                    if row.json_pointer.endswith("/video_prompt")
                    else "ACCEPT"
                ),
            )
            for row in result.review.operations
        ],
    )
    assert services.reviews.all_pending(PID) == []
    assert state(services)["status"] == "needs_update"


def test_frozen_image_and_video_replays_survive_later_plan_edits(
    services,
    tmp_path,
):
    from services.media_files.image_execution import FileImageExecutionService
    from services.media_files.r2v_execution import FileR2VExecutionService
    from media_files.conftest import write_png, accept_pending_reviews

    png = tmp_path / "fixture.png"
    write_png(png, width=4, height=4, pixel=lambda _x, _y: (40, 50, 60, 255))

    class ImageProvider:
        model_name = "qwen-image-3.0-pro"
        calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            return {"content": png.read_bytes(), "media_type": "image/png"}

    class NoVideoProvider:
        async def submit(self, **kwargs):
            raise AssertionError("No paid video calls in replay test")

    image_provider = ImageProvider()
    images = FileImageExecutionService(services, provider=image_provider)

    async def run():
        first = await images.execute(
            project_id=PID,
            command="GENERATE_STORYBOARD_IMAGE",
            target_ref=f"element:{EID}",
            arguments={},
            idempotency_key="old-image",
        )
        accept_pending_reviews(services, PID)
        project = services.projects.read(PID).project
        assert state(services)["status"] == "legacy"
        assert (
            derive_work_graph(project).by_id[f"storyboard:{EID}"].status.value
            == "done"
        )
        videos = FileR2VExecutionService(services, provider=NoVideoProvider())
        video = await videos.dispatch(
            project_id=PID,
            target_ref=f"element:{EID}",
            arguments={},
            idempotency_key="old-video",
            start=False,
        )
        executions = ProjectExecutionStore(services.root)
        frozen = executions.get_task(PID, video.task_id).metadata[
            "requestSnapshot"
        ]
        plan_edit(services)
        repeated_image = await images.execute(
            project_id=PID,
            command="GENERATE_STORYBOARD_IMAGE",
            target_ref=f"element:{EID}",
            arguments={},
            idempotency_key="old-image",
        )
        assert (
            repeated_image.replayed and repeated_image.task_id == first.task_id
        )
        repeated_video = await videos.dispatch(
            project_id=PID,
            target_ref=f"element:{EID}",
            arguments={},
            idempotency_key="old-video",
            start=False,
        )
        assert (
            repeated_video.replayed and repeated_video.task_id == video.task_id
        )
        assert (
            executions.get_task(PID, video.task_id).metadata["requestSnapshot"]
            == frozen
        )
        assert image_provider.calls == 1
        with pytest.raises(ValidationError, match="片段内容"):
            await images.execute(
                project_id=PID,
                command="GENERATE_STORYBOARD_IMAGE",
                target_ref=f"element:{EID}",
                arguments={},
                idempotency_key="new-image",
            )
        await videos.shutdown()

    asyncio.run(run())


def test_late_draft_does_not_reappear_in_recreated_project(services):
    def recreate():
        project = services.projects.read(PID).project.model_copy(deep=True)
        services.projects.delete(PID)
        project.updated_at = project.created_at + timedelta(seconds=1)
        project.created_at = project.updated_at
        services.projects.create(project)

    with pytest.raises(ConflictError, match="重新建立"):
        asyncio.run(
            PromptSyncService(services, client=client(recreate)).propose(
                PID,
                TID,
                EID,
            ),
        )
    assert services.projects.read(PID).generation == 0
    assert not (
        services.projects.project_root(PID) / "runtime/prompt-proposals"
    ).exists()


def test_model_settings_are_rechecked_inside_commit_lock(
    services,
    monkeypatch,
):
    service = PromptSyncService(services, client=client())

    async def run():
        proposal = await service.propose(PID, TID, EID)
        original = services.commit_candidate

        async def racing(**kwargs):
            monkeypatch.setattr(
                model_config,
                "get_image_model_name",
                lambda: "gpt-image-2",
            )
            # Forward the production call unchanged through this test spy.
            # pylint: disable-next=missing-kwoa
            return await original(**kwargs)

        monkeypatch.setattr(
            type(services),
            "commit_candidate",
            lambda _self, **kwargs: racing(**kwargs),
        )
        with pytest.raises(ConflictError, match="参考图、模型"):
            await service.accept(PID, TID, EID, proposal["proposalId"])
        assert services.projects.read(PID).generation == 0
        assert state(services)["videoPrompt"] == VD

    asyncio.run(run())


def test_legacy_auto_trim_cannot_create_unusable_canonical_proposal(services):
    from media_files.conftest import write_png

    base = services.projects.read(PID)
    project = base.project.model_copy(deep=True)
    c = project.timelines.items[TID].elements_by_id[EID].creation
    png = services.projects.project_root(PID) / "assets/artifacts/fixture.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    write_png(png, width=4, height=4, pixel=lambda _x, _y: (40, 50, 60, 255))
    checksum = hashlib.sha256(png.read_bytes()).hexdigest()
    project.assets.files_by_id["fixture"] = IndexedFile(
        file_id="fixture",
        kind="artifact_payload",
        relative_uri="assets/artifacts/fixture.png",
        sha256=checksum,
        size_bytes=png.stat().st_size,
        media_type="image/png",
        created_at=project.created_at,
    )
    for index in range(4):
        entity_id, version_id = f"character:{index}", f"artifact:{index}"
        slot_id, owner = (
            f"visual:{index}:default",
            f"visual-entity:{entity_id}",
        )
        project.assets.artifact_slots_by_id[slot_id] = ArtifactSlot(
            slot_id=slot_id,
            kind="visual_asset_image",
            owner_ref=owner,
            version_ids=[version_id],
            selected_version_id=version_id,
        )
        project.assets.artifact_versions_by_id[version_id] = ArtifactVersion(
            version_id=version_id,
            slot_id=slot_id,
            kind="visual_asset_image",
            owner_ref=owner,
            name=f"人物{index}",
            checksum=checksum,
            file_id="fixture",
            based_on_generation=0,
            created_at=project.created_at,
            metadata={"variantId": "default"},
        )
        project.visual.entities.items[entity_id] = VisualEntity(
            entity_id=entity_id,
            kind="character",
            name=f"人物{index}",
            required_variant_ids=["default"],
            variants=EntityCollection(
                order=["default"],
                items={
                    "default": VisualVariant(
                        variant_id="default",
                        generated_artifact_version_ids=[version_id],
                        selected_artifact_version_id=version_id,
                    ),
                },
            ),
        )
        project.visual.entities.order.append(entity_id)
        c.character_refs.append(entity_id)
        c.visual_variant_refs[entity_id] = "default"
    services.commits.commit(
        base=base,
        candidate=project.model_dump(mode="json"),
        origin="frontend_edit",
    )
    from services.prompt_sync_service import _references

    references = _references(services.projects.read(PID).project, EID)
    assert len(references["storyboard"]) == 3
    assert references["storyboardBudgetDroppedVersionIds"] == ["artifact:3"]
    called = []
    before = services.projects.read(PID).project.model_dump_json()
    with pytest.raises(ValidationError, match="明确整理"):
        asyncio.run(
            PromptSyncService(
                services,
                client=client(lambda: called.append(True)),
            ).propose(PID, TID, EID),
        )
    assert not called
    assert services.projects.read(PID).project.model_dump_json() == before


def test_http_propose_accept_and_retry_use_one_text_call_no_media(
    services,
    monkeypatch,
):
    from services import prompt_sync_service

    calls = []
    monkeypatch.setattr(
        prompt_sync_service,
        "AgentScopeAgentChatClient",
        lambda **_kwargs: client(lambda: calls.append("text")),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[project_file_services] = lambda: services
    prefix = f"/projects/{PID}/timelines/{TID}/elements/{EID}"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as http:
            draft = await http.post(
                prefix + "/prompt-proposals",
                json={"source": "videoPrompt"},
            )
            assert draft.status_code == 200, draft.text
            value = draft.json()
            assert value["source"] == "videoPrompt"
            assert "左手" in value["beforeNarrative"]
            assert "原来在这儿。" in value["narrative"]
            assert value["beforeStoryboardPrompt"] == SB
            endpoint = (
                prefix + f"/prompt-proposals/{value['proposalId']}/accept"
            )
            applied = await http.post(endpoint, json={})
            assert applied.status_code == 200, applied.text
            repeated = await http.post(endpoint, json={})
            assert repeated.status_code == 200
            assert (
                repeated.json()["generation"] == applied.json()["generation"]
            )
            assert repeated.json()["replayed"] is True
            assert calls == ["text"]
            assert ProjectExecutionStore(services.root).list_tasks(PID) == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "source",
    ["storyboardPrompt", "videoPrompt", "currentPlan", "mixed"],
)
def test_synchronization_preserves_authoritative_user_content_exactly(
    services,
    source,
):
    if source in ("currentPlan", "mixed"):
        plan_edit(services)
    if source in ("storyboardPrompt", "mixed"):
        edit(
            services,
            lambda c: c.update(
                storyboard_prompt=SB + "保留原文与换行。\n  左手抬起钥匙。",
            ),
        )
    if source == "videoPrompt":
        edit(
            services,
            lambda c: c.update(
                video_prompt=VD + "保留原文与换行。\n  左手抬起钥匙。",
            ),
        )
    before = state(services)

    async def complete(messages, tools):
        payload = model_payload(messages)
        shots = model_narrative(payload)
        shots += "左手抬起钥匙。模型改写。"
        return AgentModelTurn(
            content=json.dumps(
                {
                    "narrative": shots,
                    "storyboardPrompt": SB + "左手抬起钥匙。模型改写。",
                    "videoPrompt": VD + "左手抬起钥匙。模型改写。",
                },
                ensure_ascii=False,
            ),
        )

    service = PromptSyncService(
        services,
        client=CallbackAgentChatClient(complete),
    )

    async def run():
        proposal = await service.propose(PID, TID, EID, source=source)
        for key in before["changedSources"]:
            field = "narrative" if key == "currentPlan" else key
            assert proposal[field] == before[field]
        await service.accept(PID, TID, EID, proposal["proposalId"])
        assert state(services)["status"] == "current"
        assert ProjectExecutionStore(services.root).list_tasks(PID) == []

    asyncio.run(run())


def test_conflicting_user_panel_counts_fail_before_model_or_media_call(
    services,
):
    edit(
        services,
        lambda c: c.update(
            storyboard_prompt="输出2列×2行，共4个等尺寸分镜格，展示9个时间关键帧。",
        ),
    )

    async def unexpected(messages, tools):
        raise AssertionError(
            "No provider call for an invalid authoritative layout",
        )

    service = PromptSyncService(
        services,
        client=CallbackAgentChatClient(unexpected),
    )
    with pytest.raises(ValidationError, match="请统一网格"):
        asyncio.run(service.propose(PID, TID, EID, source="storyboardPrompt"))
    assert state(services)["status"] == "needs_confirmation"
    assert ProjectExecutionStore(services.root).list_tasks(PID) == []


def test_missing_generated_reference_mentions_use_exact_input_order():
    from services.prompt_sync_service import _complete_reference_mentions

    prompt = "[Image 1]提供连续动作，女子右手离开包口。"
    refs = [
        {"index": 1, "kind": "storyboard", "name": "当前分镜图"},
        {"index": 2, "kind": "artifact", "name": "女子（default）视觉图"},
        {"index": 3, "kind": "artifact", "name": "公寓门口"},
    ]
    result = _complete_reference_mentions(prompt, refs)
    assert result.startswith(prompt)
    assert "[Image 2]是女子视觉图" in result
    assert "[Image 3]是公寓门口" in result
    assert "[Image 4]" not in result
    assert _complete_reference_mentions(result, refs) == result


def test_accept_rejects_old_proposal_that_rewrites_edited_source(services):
    plan_edit(services)
    service = PromptSyncService(services, client=client())

    async def run():
        proposal = await service.propose(PID, TID, EID)
        file = (
            services.projects.project_root(PID)
            / "runtime/prompt-proposals"
            / (proposal["proposalId"] + ".json")
        )
        data = json.loads(file.read_text())
        data["narrative"] += "旧草稿擅自改写。"
        file.write_text(json.dumps(data, ensure_ascii=False))
        generation = services.projects.read(PID).generation
        with pytest.raises(ConflictError, match="修改了本次编辑"):
            await service.accept(PID, TID, EID, proposal["proposalId"])
        assert services.projects.read(PID).generation == generation

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode",
    ["automatic", "approval", "editing", "late_edit", "invalid"],
)
# Assert the whole edit-to-publication lifecycle in one regression case.
# pylint: disable-next=too-many-statements
def test_scheduler_prepares_changed_content_before_media(
    services,
    monkeypatch,
    mode,
):
    from services.file_agent_runtime import work_scheduler
    from services import prompt_sync_service

    plan_edit(services)
    assert state(services)["status"] == "needs_update"
    before = services.projects.read(PID)
    prepared, dispatched = [], []
    monkeypatch.setattr(
        work_scheduler,
        "get_execution_authorization_mode",
        lambda: "require_approval" if mode == "approval" else "allow_all",
    )
    monkeypatch.setattr(
        work_scheduler.frontend_edit_hold,
        "hold_remaining",
        lambda *_: 30 if mode == "editing" else 0,
    )
    monkeypatch.setattr(work_scheduler, "get_media_parallelism", lambda: 4)

    def during_model():
        prepared.append(True)
        if mode == "late_edit":
            edit(
                services,
                lambda c: c.update(
                    narrative="用户的新修改必须保留。对白：原来在这儿。",
                ),
            )
        if mode == "invalid":
            raise ValueError("修复输入内容后才能继续")

    monkeypatch.setattr(
        prompt_sync_service,
        "AgentScopeAgentChatClient",
        lambda **_: client(during_model),
    )

    async def dispatch(*args, **kwargs):
        dispatched.append(kwargs)
        return {"taskId": "controlled-image-task"}

    async def run():
        scheduler = work_scheduler.WorkGraphScheduler(
            services,
            image_dispatch=dispatch,
        )
        monkeypatch.setattr(scheduler, "wake", lambda _: None)
        graph = derive_work_graph(before.project)
        assert graph.by_id[f"storyboard:{EID}"].prompt_sync_required
        assert graph.model_required_nodes(automatic_regeneration=True) == ()
        if mode in {"automatic", "late_edit", "invalid"}:
            from services.file_agent_runtime.workgraph_execution import (
                ready_request_context,
            )

            _, _, admitted, blocked = await ready_request_context(
                services,
                scheduler.executions,
                PID,
            )
            assert blocked[f"storyboard:{EID}"] == "GATED", blocked
            assert all(
                admitted.by_id[dep].status.value == "done"
                for dep in admitted.by_id[f"storyboard:{EID}"].deps
            )
        await scheduler.tick(PID)
        if mode in {"automatic", "invalid"}:
            await scheduler.tick(PID)
        for _ in range(8):
            await asyncio.sleep(0)
        if mode == "automatic":
            assert (
                len(prepared) == 1
            ), scheduler.deterministic_failure_nodes_for_project(PID)
            assert state(services)["status"] == "current"
            assert (
                state(services)["narrative"]
                == creation(before.project.model_dump(mode="json"))[
                    "narrative"
                ]
            )
            assert len(dispatched) == 1
            assert dispatched[0]["command"] == "GENERATE_STORYBOARD_IMAGE"
        else:
            assert dispatched == []
            assert state(services)["status"] == "needs_update"
            if mode in {"approval", "editing"}:
                assert prepared == []
                if mode == "editing":
                    assert PID in scheduler._sync_gate_rechecks
                assert (
                    services.projects.read(PID).generation == before.generation
                )
            else:
                assert (
                    len(prepared) == 1
                ), scheduler.deterministic_failure_nodes_for_project(PID)
                if mode == "late_edit":
                    assert "用户的新修改必须保留" in state(services)["narrative"]
                else:
                    assert scheduler.deterministic_failure_nodes_for_project(
                        PID,
                    )
        await scheduler.shutdown()

    asyncio.run(run())


def test_committed_revision_prepares_and_replaces_stale_image(
    services,
    tmp_path,
    monkeypatch,
):
    from services.file_agent_runtime import work_scheduler
    from services import prompt_sync_service
    from services.media_files.image_execution import FileImageExecutionService
    from services.project_files.edit_impact import apply_frontend_edit_impacts
    from media_files.conftest import write_png, accept_pending_reviews

    png = tmp_path / "regeneration.png"
    write_png(png, width=4, height=4, pixel=lambda _x, _y: (40, 50, 60, 255))

    class ImageProvider:
        model_name = "qwen-image-3.0-pro"
        calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            return {"content": png.read_bytes(), "media_type": "image/png"}

    provider = ImageProvider()
    images = FileImageExecutionService(services, provider=provider)
    monkeypatch.setattr(
        prompt_sync_service,
        "AgentScopeAgentChatClient",
        lambda **_: client(),
    )
    monkeypatch.setattr(
        work_scheduler,
        "get_execution_authorization_mode",
        lambda: "allow_all",
    )
    monkeypatch.setattr(
        work_scheduler.frontend_edit_hold,
        "hold_remaining",
        lambda *_: 0,
    )

    async def dispatch(_services, **kwargs):
        # Forward the production call unchanged through this test spy.
        # pylint: disable-next=missing-kwoa
        return await images.execute(**kwargs)

    async def run():
        await images.execute(
            project_id=PID,
            command="GENERATE_STORYBOARD_IMAGE",
            target_ref=f"element:{EID}",
            arguments={},
            idempotency_key="before-revision",
        )
        accept_pending_reviews(services, PID)
        base = services.projects.read(PID)
        old_id = base.project.assets.artifact_slots_by_id[
            f"element:{EID}:storyboard"
        ].selected_version_id
        candidate = base.project.model_dump(mode="json")
        creation(candidate)["narrative"] = "右手停包边，只有左前臂抬钥匙至胸前。对白：原来在这儿。"
        candidate, impact = apply_frontend_edit_impacts(
            candidate,
            [
                f"/timelines/items/{TID}/elements_by_id/{EID}"
                "/creation/narrative",
            ],
            base=base.project.model_dump(mode="json"),
        )
        services.commits.commit(
            base=base,
            candidate=candidate,
            origin="frontend_edit",
        )
        assert impact.regeneration_required
        assert state(services)["status"] == "needs_update"
        assert (
            services.projects.read(PID)
            .project.assets.artifact_versions_by_id[old_id]
            .stale
        )
        scheduler = work_scheduler.WorkGraphScheduler(
            services,
            image_dispatch=dispatch,
        )
        monkeypatch.setattr(scheduler, "wake", lambda _: None)
        await scheduler.tick(PID)
        assert state(services)["status"] == "current"
        graph = derive_work_graph(services.projects.read(PID).project)
        assert [node.node_id for node in graph.regeneration_nodes()] == [
            f"storyboard:{EID}",
        ]
        await scheduler.tick(PID)
        await asyncio.gather(*scheduler._dispatch_tasks.get(PID, set()))
        accept_pending_reviews(services, PID)
        fresh = services.projects.read(PID).project
        new_id = fresh.assets.artifact_slots_by_id[
            f"element:{EID}:storyboard"
        ].selected_version_id
        assert provider.calls == 2
        assert new_id != old_id
        assert fresh.assets.artifact_versions_by_id[old_id].stale
        assert not fresh.assets.artifact_versions_by_id[new_id].stale
        await scheduler.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("source", ["storyboardPrompt", "videoPrompt"])
def test_technical_wording_edit_can_preserve_already_aligned_targets(
    services,
    source,
):
    field = (
        "storyboard_prompt" if source == "storyboardPrompt" else "video_prompt"
    )
    edit(services, lambda c: c.update({field: c[field] + "高质量细节。"}))
    before = state(services)

    async def complete(messages, tools):
        payload = model_payload(messages)
        assert "narrative" not in payload["fixedScope"]["creation"]
        assert "保留已正确的叙述和另一份提示词" in messages[0]["content"]
        return AgentModelTurn(
            content=json.dumps(
                {
                    "narrative": input_value(payload, "currentPlan"),
                    "storyboardPrompt": input_value(
                        payload,
                        "storyboardPrompt",
                    ),
                    "videoPrompt": input_value(payload, "videoPrompt"),
                },
            ),
        )

    async def run():
        service = PromptSyncService(
            services,
            client=CallbackAgentChatClient(complete),
        )
        proposal = await service.propose(PID, TID, EID, source=source)
        await service.accept(PID, TID, EID, proposal["proposalId"])
        after = state(services)
        assert after["status"] == "current"
        assert after["narrative"] == before["narrative"]
        assert after["storyboardPrompt"] == before["storyboardPrompt"]
        assert after["videoPrompt"] == before["videoPrompt"]

    asyncio.run(run())


def test_read_and_noop_commit_leave_legacy_project_and_generation_untouched(
    services,
):
    path = services.projects.project_root(PID) / "project.json"
    document = json.loads(path.read_text())
    creation(document).update(
        shots={"items": {"bad": {"dialogue": "废弃台词", "duration_seconds": -3}}},
        min_dialogue_ratio=900,
    )
    raw = json.dumps(document, ensure_ascii=False)
    path.write_text(raw)
    loaded = services.projects.read(PID)
    assert "shots" not in creation(loaded.project.model_dump(mode="json"))
    assert loaded.generation == 0
    result = edit(services, lambda c: None)
    assert result.snapshot.generation == 0
    assert path.read_text() == raw


@pytest.mark.parametrize("source", ["currentPlan", "storyboardPrompt"])
def test_wrong_source_cannot_replace_a_user_video_edit(services, source):
    edit(services, lambda c: c.update(video_prompt=VD + "用户新加的停顿。"))
    before = services.projects.read(PID)

    def unexpected():
        raise AssertionError("Wrong source must fail before a model call")

    service = PromptSyncService(services, client=client(unexpected))
    with pytest.raises(ValidationError, match="同步来源"):
        asyncio.run(service.propose(PID, TID, EID, source=source))
    assert services.projects.read(PID).etag == before.etag
    assert ProjectExecutionStore(services.root).list_tasks(PID) == []


def test_accept_also_rejects_a_proposal_omitting_changed_source(services):
    edit(services, lambda c: c.update(video_prompt=VD + "用户新加的停顿。"))
    service = PromptSyncService(services, client=client())

    async def run():
        proposal = await service.propose(PID, TID, EID, source="videoPrompt")
        record = service._record(PID, proposal["proposalId"])
        record.write(
            record.read().model_copy(update={"source": "storyboardPrompt"}),
        )
        etag = services.projects.read(PID).etag
        with pytest.raises(ConflictError, match="遗漏了本次修改"):
            await service.accept(PID, TID, EID, proposal["proposalId"])
        assert services.projects.read(PID).etag == etag

    asyncio.run(run())


def test_sync_opens_edit_grace_before_commit_listeners_can_wake(
    services,
    monkeypatch,
):
    from services.project_files import frontend_edit_hold

    plan_edit(services)
    frontend_edit_hold.clear(PID)
    commit = services.commit_candidate
    observed = []

    async def guarded(_self, **kwargs):
        observed.append(frontend_edit_hold.hold_remaining(PID, EID))
        return await commit(**kwargs)

    monkeypatch.setattr(CreatorFileServices, "commit_candidate", guarded)
    service = PromptSyncService(services, client=client())

    async def run():
        draft = await service.propose(PID, TID, EID)
        await service.accept(PID, TID, EID, draft["proposalId"])

    try:
        asyncio.run(run())
        assert len(observed) == 1 and observed[0] > 0
    finally:
        frontend_edit_hold.clear(PID)


def test_valid_single_poster_remains_generatable_after_sync(services):
    poster = "输出一张9:16竖向单幅海报，女子手持钥匙，无文字。"
    edit(services, lambda c: c.update(storyboard_prompt=poster))
    assert not state(services).get("validationMessage")
    service = PromptSyncService(services, client=client())

    async def run():
        draft = await service.propose(PID, TID, EID, source="storyboardPrompt")
        await service.accept(PID, TID, EID, draft["proposalId"])

    asyncio.run(run())
    current = state(services)
    assert current["status"] == "current"
    assert current["storyboardPrompt"] == poster
    assert not current.get("validationMessage")


def test_commit_hashes_only_touched_units_but_global_format_reaches_all(
    services,
    monkeypatch,
):
    from services.project_files import prompt_sync

    base = services.projects.read(PID)
    project = base.project.model_copy(deep=True)
    elements = project.timelines.items[TID].elements_by_id
    for index in range(40):
        eid = f"unit-{index}"
        elements[eid] = elements[EID].model_copy(
            deep=True,
            update={"element_id": eid},
        )
    project.generation += 1
    services.projects.replace(PID, project, base.etag)
    original = prompt_sync.sync_stamp
    seen = []

    def count(document, timeline_id, element_id):
        seen.append(element_id)
        return original(document, timeline_id, element_id)

    monkeypatch.setattr(prompt_sync, "sync_stamp", count)
    plan_edit(services)
    assert set(seen) == {EID}
    seen.clear()
    base = services.projects.read(PID)
    candidate = base.project.model_dump(mode="json")
    candidate["strategy"]["creative_brief"] = "Unrelated project note"
    services.commits.commit(
        base=base,
        candidate=candidate,
        origin="frontend_edit",
    )
    assert seen == []
    base = services.projects.read(PID)
    candidate = base.project.model_dump(mode="json")
    candidate["settings"]["aspect_ratio"] = "16:9"
    services.commits.commit(
        base=base,
        candidate=candidate,
        origin="frontend_edit",
    )
    assert set(seen) == set(elements) - {EID}
    assert state(services)["status"] == "needs_update"
