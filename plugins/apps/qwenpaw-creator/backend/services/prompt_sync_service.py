# -*- coding: utf-8 -*-
"""
Explicit text-only prompt proposals with a frozen baseline and atomic accept.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError as ModelValidationError

from domain.errors import ConflictError, NotFoundError, ValidationError
from models import config as model_config
from models.reference_markers import canonical_marker_indices
from services.file_agent_runtime.model_client import AgentScopeAgentChatClient
from services.media_files.prompt_labels import media_prompt_entity_names
from services.media_files.visual_reference_resolution import (
    preview_r2v_reference_order,
)
from services.project_files.edit_impact import apply_frontend_edit_impacts
from services.project_files.models import (
    EntityCollection,
    Project,
    Shot,
    ShotCamera,
    ShotFraming,
    StrictModel,
)
from services.prompt_text import dialogue_match_key, dialogue_spoken_lines
from services.storyboard_layout import declared_storyboard_panel_count
from services.project_files.prompt_sync import (
    digest,
    live_element,
    plan_input,
    prompt_sync_status,
)
from services.project_files.store import ProjectNotFound
from services.run_review.prompt_contract import (
    check_changed_r2v_prompt_contracts,
)
from services.runtime_files.atomic_store import AtomicJsonRecordStore
from services.runtime_files.errors import RecordNotFoundError

PromptSyncSource = Literal[
    "currentPlan",
    "storyboardPrompt",
    "videoPrompt",
    "mixed",
]


class PromptProposal(StrictModel):
    proposal_id: str
    project_id: str
    timeline_id: str
    element_id: str
    baseline_token: str
    reference_fingerprint: str
    model_fingerprint: str
    source: PromptSyncSource = "currentPlan"
    before_shots: EntityCollection[Shot] | None = None
    shots: EntityCollection[Shot] | None = None
    before_storyboard_prompt: str
    before_video_prompt: str
    storyboard_prompt: str
    video_prompt: str


def _pointer(timeline_id: str, element_id: str) -> str:
    def escape(value: str) -> str:
        return value.replace("~", "~0").replace("/", "~1")

    return (
        f"/timelines/items/{escape(timeline_id)}"
        f"/elements_by_id/{escape(element_id)}/creation"
    )


def _model_fingerprint() -> str:
    return digest(
        (
            model_config.get_image_model_name(),
            model_config.get_video_model_name(),
            model_config.get_video_backend(),
        ),
    )


def _references(project: Project, element_id: str) -> dict:
    storyboard = preview_r2v_reference_order(
        project,
        element_id,
        stage="storyboard",
        image_model_name=model_config.get_image_model_name(),
    )
    video = preview_r2v_reference_order(project, element_id, stage="video")
    video_rows = video["references"]
    if not any(row.get("kind") == "storyboard" for row in video_rows):
        # Reserve the actual target's future storyboard role, never invent a
        # version ID or silently use a character image as the first storyboard.
        video_rows = [
            {
                "index": 1,
                "kind": "storyboard",
                "name": "本镜头的分镜图（尚未生成）",
            },
            *[{**row, "index": row["index"] + 1} for row in video_rows],
        ]
    return {
        "storyboard": storyboard["references"],
        "video": video_rows,
        "storyboardBudgetDroppedVersionIds": storyboard[
            "budgetDroppedVersionIds"
        ],
        "storyboardReferenceLimit": storyboard["referenceLimit"],
    }


def _context_token(
    document,
    timeline_id,
    element_id,
    references,
    model_fingerprint,
):
    """
    Bind a reviewed text snapshot to its real input order and model settings.
    """
    return digest(
        {
            "source": prompt_sync_status(document, timeline_id, element_id)[
                "baselineToken"
            ],
            "references": digest(references),
            "models": model_fingerprint,
        },
    )


def _validate_proposal_references(references: dict) -> None:
    if references.get("storyboardBudgetDroppedVersionIds"):
        # Legacy automatic trimming stops once canonical markers are authored.
        # Do not draft against a trimmed order that cannot survive acceptance.
        raise ValidationError(
            "参考图超出当前模型容量，请先明确整理分镜参考图列表",
        )
    limit = references.get("storyboardReferenceLimit")
    if references["storyboard"] and (
        limit is None or len(references["storyboard"]) > limit
    ):
        raise ValidationError("参考图超出当前模型容量，请先整理分镜参考图列表")
    if any(
        row.get("available") is False
        and not (
            stage == "video"
            and row.get("kind") == "storyboard"
            and not row.get("versionId")
        )
        for stage in ("storyboard", "video")
        for row in references[stage]
    ):
        raise ValidationError("当前参考图尚不可用，请先完成或重新选择参考图片")


def _complete_reference_mentions(prompt: str, references: list[dict]) -> str:
    """
    Fill only omitted bindings using the frozen, actual provider input order.
    """
    cited = set(canonical_marker_indices(prompt))
    additions = []
    for row in references:
        index = row["index"]
        if index in cited:
            continue
        name = re.sub(r"[（(]default[）)]", "", row["name"])
        usage = (
            "提供本镜头动作顺序，连续呈现动作，不展示宫格"
            if row.get("kind") == "storyboard"
            else "保持对应视觉内容的一致性"
        )
        additions.append(f"[Image {index}]是{name}，{usage}。")
    return prompt + (
        "\n\n参考图片补充：\n" + "\n".join(additions) if additions else ""
    )


def _validate_prompts(
    document: dict,
    timeline_id: str,
    element_id: str,
    references: dict,
    *,
    proposal: bool = False,
) -> None:
    _, element = live_element(document, timeline_id, element_id)
    creation = element["creation"]
    path = _pointer(timeline_id, element_id)
    report = check_changed_r2v_prompt_contracts(
        document,
        [path + "/storyboard_prompt", path + "/video_prompt"],
    )
    if not report["passed"]:
        raise ValidationError(
            "提示词尚未满足镜头、画幅或对白要求",
            details={"findings": report["findings"]},
        )
    for stage in ("storyboard", "video"):
        text = creation[f"{stage}_prompt"]
        # Spoken literals are creative content, not reference declarations.
        for shot in creation.get("shots", {}).get("items", {}).values():
            literal = (shot.get("dialogue") or "").strip()
            if literal:
                text = text.replace(literal, "")
        indexes = canonical_marker_indices(text)
        if any(
            index < 1 or index > len(references[stage]) for index in indexes
        ):
            raise ValidationError("提示词引用了当前镜头没有的参考图")
        if proposal and set(indexes) != set(
            range(1, len(references[stage]) + 1),
        ):
            raise ValidationError(
                ("分镜图" if stage == "storyboard" else "视频")
                + "提示词缺少部分已绑定参考图片，请补充引用后重新生成",
            )
        if proposal and re.search(
            r"@image_\d|<<<image_\d|(?:图|图片)\s*\d",
            text,
        ):
            raise ValidationError(
                "参考图片请统一写成 [Image N]，模型格式由执行器转换",
            )


def _validate_plan(document: dict, timeline_id: str, element_id: str) -> None:
    timeline, element = live_element(document, timeline_id, element_id)
    shots = element["creation"].get("shots", {})
    ordered = [
        shots.get("items", {}).get(key) for key in shots.get("order", [])
    ]
    if not ordered or any(
        not row
        or not str(row.get("description") or "").strip()
        or float(row.get("duration_seconds") or 0) <= 0
        for row in ordered
    ):
        raise ValidationError("请先填写完整的镜头内容和有效时长")
    duration = element["span"]["duration_tick"] / timeline["ticks_per_second"]
    if (
        sum(float(row["duration_seconds"]) for row in ordered)
        > duration + 1e-6
    ):
        raise ValidationError("镜头安排的总时长不能超过当前片段时长")


def _draft_shots(
    value,
    document,
    timeline_id,
    element_id,
) -> EntityCollection[Shot]:
    """
    Validate complete model-authored shot facts without reviving old audio.
    """
    if not isinstance(value, dict) or not isinstance(value.get("items"), dict):
        raise ValidationError("模型未返回完整的镜头内容")
    required = {
        "shot_id",
        "description",
        "camera",
        "framing",
        "duration_seconds",
        "dialogue",
    }
    if any(
        not isinstance(row, dict) or not required.issubset(row)
        for row in value["items"].values()
    ):
        raise ValidationError(
            "每个镜头必须明确提供完整描述和本轮声音内容，可明确为空",
        )
    try:
        shots = EntityCollection[Shot].model_validate(value)
    except ModelValidationError as error:
        raise ValidationError("模型返回的镜头内容或时长格式不完整") from error
    creation = live_element(document, timeline_id, element_id)[1]["creation"]
    allowed_characters = set(creation.get("character_refs", []))
    allowed_props = set(creation.get("prop_refs", []))
    for key, shot in shots.items.items():
        if key != shot.shot_id:
            raise ValidationError("镜头内容的顺序与身份不一致")
        if shot.camera is None or shot.framing is None:
            raise ValidationError("镜头内容需要明确运镜和景别")
        if any(
            dialogue_match_key(line)
            not in dialogue_match_key(shot.description)
            for line in dialogue_spoken_lines(shot.dialogue)
        ):
            raise ValidationError(
                "镜头声音原文必须完整包含在可见描述中，不能保留隐藏的旧台词",
            )
        if (
            not set(shot.character_refs).issubset(allowed_characters)
            or not set(shot.prop_refs).issubset(allowed_props)
            or (shot.scene_ref and shot.scene_ref != creation.get("scene_ref"))
        ):
            raise ValidationError(
                "镜头内容引用了本片段尚未绑定的人物、场景或道具",
            )
    return shots


def _reject_unchanged_reverse_targets(
    source,
    changed_sources,
    before,
    after,
) -> None:
    """Catch an obvious failed reverse update, not semantic equivalence."""
    if (
        source not in ("storyboardPrompt", "videoPrompt")
        or source not in changed_sources
    ):
        return
    other = (
        "video_prompt" if source == "storyboardPrompt" else "storyboard_prompt"
    )
    if (
        before["shots"] == after["shots"]
        or before[other].strip() == after[other].strip()
    ):
        raise ValidationError(
            "本次源提示词已修改，但模型没有同时更新镜头内容和另一份提示词。请重试同步；本次未提交图片或视频生成。",
        )


class PromptSyncService:
    def __init__(self, services, *, client=None):
        self.services = services
        self.client = client

    def _read(self, project_id: str, timeline_id: str, element_id: str):
        try:
            snapshot = self.services.projects.read(project_id)
        except ProjectNotFound as error:
            raise NotFoundError("项目不存在") from error
        document = snapshot.project.model_dump(mode="json")
        live_element(document, timeline_id, element_id)
        return snapshot, document

    def status(
        self,
        project_id: str,
        timeline_id: str,
        element_id: str,
    ) -> dict:
        snapshot, document = self._read(project_id, timeline_id, element_id)
        _, element = live_element(document, timeline_id, element_id)
        creation = element["creation"]
        status = prompt_sync_status(document, timeline_id, element_id)
        layout_issue = (
            "分镜图提示词的格数不明确或互相冲突，请统一网格、分镜格数和关键帧数量后重新生成"
            if (
                status["status"] == "current"
                or "storyboardPrompt" in status["changedSources"]
            )
            and declared_storyboard_panel_count(creation["storyboard_prompt"])
            is None
            else None
        )
        return {
            **status,
            "validationMessage": layout_issue,
            "baselineToken": _context_token(
                document,
                timeline_id,
                element_id,
                _references(snapshot.project, element_id),
                _model_fingerprint(),
            ),
            "generation": snapshot.generation,
            "storyboardPrompt": creation["storyboard_prompt"],
            "videoPrompt": creation["video_prompt"],
            "shots": creation["shots"],
        }

    def _record(self, project_id: str, proposal_id: str):
        if not re.fullmatch(r"prompt-proposal-[a-f0-9]{32}", proposal_id):
            raise NotFoundError("提示词草稿不存在")
        return AtomicJsonRecordStore(
            self.services.projects.project_root(project_id)
            / "runtime"
            / "prompt-proposals"
            / f"{proposal_id}.json",
            PromptProposal,
        )

    # Keep frozen inputs and all model output checks in one proposal flow.
    # pylint: disable-next=too-many-statements
    async def propose(
        self,
        project_id: str,
        timeline_id: str,
        element_id: str,
        guidance: str = "",
        *,
        source: PromptSyncSource = "currentPlan",
    ) -> dict:
        snapshot, document = await asyncio.to_thread(
            self._read,
            project_id,
            timeline_id,
            element_id,
        )
        if source not in (
            "currentPlan",
            "storyboardPrompt",
            "videoPrompt",
            "mixed",
        ):
            raise ValidationError("请选择本次同步的内容来源")
        sync = prompt_sync_status(document, timeline_id, element_id)
        if len(sync["changedSources"]) > 1:
            source = "mixed"
        if source == "currentPlan" or (
            source == "mixed" and "currentPlan" in sync["changedSources"]
        ):
            _validate_plan(document, timeline_id, element_id)
        # Derive everything from the same snapshot, not a second racing GET.
        _, element = live_element(document, timeline_id, element_id)
        creation = element["creation"]
        references = _references(snapshot.project, element_id)
        model_fingerprint = _model_fingerprint()
        _validate_proposal_references(references)
        baseline_token = _context_token(
            document,
            timeline_id,
            element_id,
            references,
            model_fingerprint,
        )
        from models.video_capabilities import video_model_prompt_guidance

        system = (
            "你是镜头内容和生成提示词编辑。只返回JSON对象，恰好包含shots、storyboardPrompt、vid"
            "eoPrompt三项。"
            "shots沿用当前计划的{order,items}结构，保留仍存在的镜头身份；每项完整提供shot_id、de"
            "scription、camera、framing、camera_description、duration_se"
            "conds、dialogue、character_refs、scene_ref、prop_refs。"
            "storyboardPrompt和videoPrompt为非空自然语言字符串。镜头总时长不可超过片段时长，不能"
            "新增未绑定的人物、场景、道具或引用。"
            "根据source选择本轮权威：currentPlan以当前镜头内容为准更新两份提示词；storyboardPr"
            "ompt以用户刚编辑的分镜提示词为准反向更新镜头内容和视频提示词；videoPrompt以用户刚编辑的视频提示"
            "词为准反向更新镜头内容和分镜提示词。"
            "最后一条用户消息中的authoritativeInputs是本次用户刚编辑的权威原文，不是历史提示词；前一条消"
            "息的referenceOnly仅是待同步的旧目标，不能反过来限制或覆盖权威原文。"
            "逐项把权威原文的动作、姿态、物件关系、顺序、次数、声音和禁止事项明确反写到targetFields的每一项，特"
            "别不要遗漏末尾补充的新要求；只保留源文、照抄旧镜头或旧提示词不算完成同步。"
            "单一来源必须原样保留该来源正文及其动作、运镜、景别、时长、声音意图，不得让其它旧内容覆盖该来源已经删除或改写的要求。"
            "source=mixed时，changedSources中的所有当前改动同时是约束，不能擅自选一份覆盖其他；如"
            '果这些编辑互相矛盾且无法同时满足，改为只返回{"conflict":"简短说明需要用户统一的冲突，使用内容名称'
            '而非内部字段"}，不要起草或猜测取舍。'
            "每个Shot的description是完整可见描述，须自然写清动作、运镜、景别、节奏，以及实际需要的对白、画外"
            "旁白、环境声音或静默。"
            "dialogue仅为从本轮完整description派生的兼容数据，必须每个镜头显式返回，包括空字符串；其中每"
            "句原生人声必须逐字出现在description和视频提示词中。"
            "视频原生画外旁白也应投影到dialogue，并标明画外旁白、人物不开口；已有独立旁白音轨中的TTS脚本不复制为"
            "视频原生人声，不新增、删除或修改音轨。"
            "旧dialogue仅供理解历史，不高于完整描述；不得恢复权威来源已删除的旧台词或声音。若用户明确不需要原生人声"
            "，dialogue为空，description写明有意静默；不得为填满对白字段而新增说话。"
            "尤其新增或修改的动作次数、身体朝向、动作先后顺序、起始状态和结束状态，必须在三份正文中分别明确表达，不能只用笼统或近义的总述代替。"
            "保留旧内容中仍与本轮权威来源一致的用户细节和禁止事项；不要遗漏、不增补矛盾动作，也不要机械复制内部字段名。"
            "两个JSON字符串内部都应使用换行自然分段，分别讲清参考职责、输出规格、逐关键帧或时间段动作、首末衔接；每个关"
            "键帧或动作阶段独立一段，避免把全部细节挤成一大段。"
            "只能使用实际名称，不能在正文暴露内部ID。图片引用统一[Image N]并严格对应各阶段实际参考顺序，不发明参考图。"
            "分镜直接说明画布、每格比例、关键帧数量、平方网格和阅读顺序，不写抽象交付模式标签。"
            "一个连续镜头可以用多个关键帧展示起始、中间动作、反应和明确末态；不能把一个Shot等同一格，也不能把九帧变成九次切镜。"
            "没有用户明确单帧要求时，按动作需要规划4或9个有信息价值的关键帧，不增加新剧情。"
            "每格内部画幅与整图相同；多格有细而完整的分隔边界，图内不画标题、序号、时码或对白。"
            "视频仅从分镜读取动作顺序并连续动画化，不能显示整张宫格、拼贴、边框、字幕或幻灯片。"
            "视频遵守本轮权威来源的人声原文及有意静默，不擅自新增配乐；严格保持角色、左右手、道具及片段开始结束状态。"
            + video_model_prompt_guidance(
                model_config.get_video_model_name(),
                model_config.get_video_backend(),
            )
        )
        plan = plan_input(document, timeline_id, element_id)
        inputs = {
            "currentPlan": plan,
            "storyboardPrompt": creation["storyboard_prompt"],
            "videoPrompt": creation["video_prompt"],
        }
        authoritative_sources = (
            sync["changedSources"] or list(inputs)
            if source == "mixed"
            else [source]
        )
        authoritative_inputs = {
            key: inputs[key] for key in authoritative_sources
        }
        if (
            "storyboardPrompt" in authoritative_sources
            and declared_storyboard_panel_count(creation["storyboard_prompt"])
            is None
        ):
            raise ValidationError(
                "分镜图提示词的格数不明确或互相冲突，请统一网格、分镜格数和关键帧数量后重新生成",
            )
        target_fields = [
            "shots" if key == "currentPlan" else key
            for key in inputs
            if key not in authoritative_sources
        ]
        context = {
            "source": source,
            "changedSources": sync["changedSources"],
            "referenceOnly": {
                key: value
                for key, value in inputs.items()
                if key not in authoritative_sources
            },
            "fixedScope": {
                key: value for key, value in plan.items() if key != "creation"
            },
            "shotValues": {
                "camera": [item.value for item in ShotCamera],
                "framing": [item.value for item in ShotFraming],
            },
            "independentNarration": [
                {
                    "script": row.get("creation", {}).get("script", ""),
                    "span": row.get("span", {}),
                }
                for row in document["timelines"]["items"][timeline_id][
                    "elements_by_id"
                ].values()
                if row.get("creation", {}).get("type") == "audio"
                and row.get("creation", {}).get("role") == "narration"
            ],
            "entityNames": {
                key: entity.name
                for key, entity in (
                    snapshot.project.visual.entities.items.items()
                )
            },
            "referenceOrder": references,
            "userGuidance": guidance,
        }
        authority = {
            "authoritativeInputs": authoritative_inputs,
            "targetFields": target_fields,
            "instruction": (
                "以下是本次必须落实的最新原文。保留其中每个具体要求，并明确同步到所有目标；"
                "旧目标只提供兼容的背景和风格，不能抵消这里新增或改写的细节。"
                "多项权威内容互相冲突时请返回conflict，不要自行选边。"
            ),
        }
        client = self.client or AgentScopeAgentChatClient(
            max_tokens=7000,
            temperature=0.2,
        )
        turn = await client.complete(
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False),
                },
                {
                    "role": "user",
                    "content": json.dumps(authority, ensure_ascii=False),
                },
            ],
            tools=[],
        )
        text = (turn.content or "").strip()
        if text.startswith("```json") and text.endswith("```"):
            text = text[7:-3].strip()
        try:
            result = json.loads(text)
        except (ValueError, TypeError) as error:
            raise ValidationError(
                "模型未返回可用的提示词草稿，请重新起草",
            ) from error
        if isinstance(result, dict) and set(result) == {"conflict"}:
            raise ValidationError(
                "当前多处编辑存在冲突，请先统一镜头内容和提示词后再同步",
            )
        if (
            not isinstance(result, dict)
            or set(result) != {"shots", "storyboardPrompt", "videoPrompt"}
            or not all(
                isinstance(value, str)
                and value.strip()
                and len(value) <= 24000
                for value in (
                    result.get("storyboardPrompt"),
                    result.get("videoPrompt"),
                )
            )
        ):
            raise ValidationError("模型返回的提示词格式不完整")
        storyboard_prompt = media_prompt_entity_names(
            result["storyboardPrompt"].strip(),
            snapshot.project,
        )
        video_prompt = media_prompt_entity_names(
            result["videoPrompt"].strip(),
            snapshot.project,
        )
        candidate = copy.deepcopy(document)
        shots = _draft_shots(
            result["shots"],
            document,
            timeline_id,
            element_id,
        )
        for shot in shots.items.values():
            shot.description = media_prompt_entity_names(
                shot.description,
                snapshot.project,
            )
        target = live_element(candidate, timeline_id, element_id)[1][
            "creation"
        ]
        target.update(
            shots=shots.model_dump(mode="json"),
            storyboard_prompt=storyboard_prompt,
            video_prompt=video_prompt,
        )
        # The user's edited sources are authoritative, including wording.
        # A model may rewrite the other representations, never these inputs.
        fields = {
            "currentPlan": "shots",
            "storyboardPrompt": "storyboard_prompt",
            "videoPrompt": "video_prompt",
        }
        for key in authoritative_sources:
            target[fields[key]] = copy.deepcopy(creation[fields[key]])
        for stage, key in (
            ("storyboard", "storyboardPrompt"),
            ("video", "videoPrompt"),
        ):
            if key not in authoritative_sources:
                target[fields[key]] = _complete_reference_mentions(
                    target[fields[key]],
                    references[stage],
                )
        shots = EntityCollection[Shot].model_validate(target["shots"])
        storyboard_prompt = target["storyboard_prompt"]
        video_prompt = target["video_prompt"]
        _validate_plan(candidate, timeline_id, element_id)
        _validate_prompts(
            candidate,
            timeline_id,
            element_id,
            references,
            proposal=True,
        )
        _reject_unchanged_reverse_targets(
            source,
            sync["changedSources"],
            creation,
            target,
        )
        proposal = PromptProposal(
            proposal_id=f"prompt-proposal-{uuid4().hex}",
            project_id=project_id,
            timeline_id=timeline_id,
            element_id=element_id,
            baseline_token=baseline_token,
            reference_fingerprint=digest(references),
            model_fingerprint=model_fingerprint,
            source=source,
            before_shots=EntityCollection[Shot].model_validate(
                creation["shots"],
            ),
            shots=shots,
            before_storyboard_prompt=creation["storyboard_prompt"],
            before_video_prompt=creation["video_prompt"],
            storyboard_prompt=storyboard_prompt,
            video_prompt=video_prompt,
        )

        # Do not recreate a deleted project even at the read/write boundary.
        def persist():
            with self.services.projects.lifecycle_lock(project_id):
                latest, _ = self._read(project_id, timeline_id, element_id)
                if latest.project.created_at != snapshot.project.created_at:
                    raise ConflictError("项目已重新建立，请重新起草提示词")
                self._record(project_id, proposal.proposal_id).create(proposal)

        await asyncio.to_thread(persist)
        return {
            "proposalId": proposal.proposal_id,
            "baselineToken": proposal.baseline_token,
            "source": source,
            "beforeShots": proposal.before_shots.model_dump(mode="json"),
            "shots": shots.model_dump(mode="json"),
            "beforeStoryboardPrompt": proposal.before_storyboard_prompt,
            "beforeVideoPrompt": proposal.before_video_prompt,
            "storyboardPrompt": storyboard_prompt,
            "videoPrompt": video_prompt,
        }

    async def accept(
        self,
        project_id: str,
        timeline_id: str,
        element_id: str,
        proposal_id: str,
    ) -> dict:
        snapshot, document = await asyncio.to_thread(
            self._read,
            project_id,
            timeline_id,
            element_id,
        )
        try:
            proposal = await asyncio.to_thread(
                self._record(project_id, proposal_id).read,
            )
        except RecordNotFoundError as error:
            raise NotFoundError("提示词草稿不存在") from error
        if (
            proposal.project_id,
            proposal.timeline_id,
            proposal.element_id,
        ) != (
            project_id,
            timeline_id,
            element_id,
        ):
            raise ConflictError("草稿不属于当前镜头")
        if proposal.shots is None or proposal.before_shots is None:
            raise ConflictError(
                "该草稿尚未包含镜头内容，请重新起草后审阅三份内容",
            )
        proposal_shots = proposal.shots.model_dump(mode="json")
        status = prompt_sync_status(document, timeline_id, element_id)
        references = _references(snapshot.project, element_id)
        model_fingerprint = _model_fingerprint()
        if (
            _context_token(
                document,
                timeline_id,
                element_id,
                references,
                model_fingerprint,
            )
            != proposal.baseline_token
        ):
            current = live_element(document, timeline_id, element_id)[1][
                "creation"
            ]
            if (
                status["status"] == "current"
                and current["storyboard_prompt"] == proposal.storyboard_prompt
                and current["video_prompt"] == proposal.video_prompt
                and current["shots"] == proposal_shots
            ):
                restored = copy.deepcopy(document)
                live_element(restored, timeline_id, element_id)[1][
                    "creation"
                ].update(
                    shots=proposal.before_shots.model_dump(mode="json"),
                    storyboard_prompt=proposal.before_storyboard_prompt,
                    video_prompt=proposal.before_video_prompt,
                )
                if (
                    _context_token(
                        restored,
                        timeline_id,
                        element_id,
                        references,
                        model_fingerprint,
                    )
                    == proposal.baseline_token
                ):
                    return {
                        "ok": True,
                        "generation": snapshot.generation,
                        "replayed": True,
                    }
            raise ConflictError("镜头计划或提示词已更新，请重新起草或审阅")
        if (
            digest(references) != proposal.reference_fingerprint
            or model_fingerprint != proposal.model_fingerprint
        ):
            raise ConflictError("参考图或模型设置已更新，请重新起草提示词")
        creation = live_element(document, timeline_id, element_id)[1][
            "creation"
        ]
        before_creation = copy.deepcopy(creation)
        creation.update(
            shots=proposal_shots,
            storyboard_prompt=proposal.storyboard_prompt,
            video_prompt=proposal.video_prompt,
        )
        fields = {
            "currentPlan": "shots",
            "storyboardPrompt": "storyboard_prompt",
            "videoPrompt": "video_prompt",
        }
        sources = (
            (status["changedSources"] or list(fields))
            if proposal.source == "mixed"
            else [proposal.source]
        )
        if any(
            creation[fields[key]] != before_creation[fields[key]]
            for key in sources
        ):
            raise ConflictError("同步结果修改了本次编辑的内容，请重新同步")
        _validate_plan(document, timeline_id, element_id)
        _draft_shots(proposal_shots, document, timeline_id, element_id)
        _validate_prompts(
            document,
            timeline_id,
            element_id,
            references,
            proposal=True,
        )
        _reject_unchanged_reverse_targets(
            proposal.source,
            status["changedSources"],
            before_creation,
            creation,
        )
        return await self._commit(
            snapshot,
            document,
            timeline_id,
            element_id,
            proposal.baseline_token,
            edit=True,
        )

    async def confirm(
        self,
        project_id: str,
        timeline_id: str,
        element_id: str,
        token: str,
    ) -> dict:
        snapshot, document = await asyncio.to_thread(
            self._read,
            project_id,
            timeline_id,
            element_id,
        )
        status = prompt_sync_status(document, timeline_id, element_id)
        references = _references(snapshot.project, element_id)
        if (
            _context_token(
                document,
                timeline_id,
                element_id,
                references,
                _model_fingerprint(),
            )
            != token
        ):
            raise ConflictError("提示词已更新，请重新打开最新内容审阅")
        if status["status"] == "current":
            return {
                "ok": True,
                "generation": snapshot.generation,
                "replayed": True,
            }
        if status["status"] not in ("legacy", "needs_confirmation"):
            raise ValidationError("镜头计划变更后需要先更新两份提示词")
        _validate_plan(document, timeline_id, element_id)
        _validate_prompts(document, timeline_id, element_id, references)
        return await self._commit(
            snapshot,
            document,
            timeline_id,
            element_id,
            token,
            edit=False,
        )

    async def _commit(
        self,
        snapshot,
        document,
        timeline_id,
        element_id,
        token,
        *,
        edit: bool,
    ):
        def validate_context(latest: dict) -> None:
            project = Project.model_validate(latest)
            if (
                _context_token(
                    latest,
                    timeline_id,
                    element_id,
                    _references(project, element_id),
                    _model_fingerprint(),
                )
                != token
            ):
                raise ConflictError(
                    "参考图、模型或镜头内容已更新，请重新打开最新内容审阅",
                )

        source_token = prompt_sync_status(
            snapshot.project,
            timeline_id,
            element_id,
        )["baselineToken"]
        if edit:
            path = _pointer(timeline_id, element_id)
            document, _ = apply_frontend_edit_impacts(
                document,
                [
                    path + "/shots",
                    path + "/storyboard_prompt",
                    path + "/video_prompt",
                ],
                base=snapshot.project.model_dump(mode="json"),
            )
        result = await self.services.commit_candidate(
            base=snapshot,
            candidate=document,
            origin="frontend_edit",
            review_policy="auto_fix",
            prompt_sync_confirmation=(timeline_id, element_id, source_token),
            prompt_sync_expected_etag=snapshot.etag,
            prompt_sync_context_validator=validate_context,
        )
        return {"ok": True, "generation": result.snapshot.generation}
