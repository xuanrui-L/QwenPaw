# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Run a billed Creator short-drama acceptance flow with real providers.

The runner owns an isolated Creator data root and never persists an API key.
The default asks for a three-second R2V element. ``--profile minute60`` asks
for five continuous elements with deliberately varied legal durations and
shot densities, validating a one-minute arc without turning HappyHorse's
fifteen-second maximum into a per-segment default. ``--prompt-only`` keeps the
real text-model run but disables every media submission, which isolates main
Agent prompt authorship and deterministic pre-generation review.

Run from the repository root::

    uv run python \
      plugins/apps/qwenpaw-creator/backend/scripts/manual/run_real_short_drama_e2e.py

``DASHSCOPE_API_KEY`` must already be present in the environment. The script
reuses it in-process for text, qwen-image-3.0-pro and happyhorse-1.1 only.
Generated media and a sanitised report are kept below ``tmp/creator-real-e2e``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = BACKEND_DIR.parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


GOAL = """\
请完成一个真实的、可播放的 3 秒 16:9 单场景微短剧《雨夜纸鹤》，必须使用一个 R2V Element，
不要改成 T2V、I2V 或多个 Element。原创主角是“阿沐”：圆头、磨砂白外壳、胸前一颗青蓝色小灯的
旧式小机器人；它在雨夜屋檐下发现一只青蓝色发光纸鹤，先警惕后轻轻伸手，纸鹤突然亮起，阿沐的
情绪从孤独转为希望。全片有意静默，无对白、旁白、TTS、字幕、Logo、水印或额外人物；将
min_dialogue_ratio 设为 0，并在 narrative 明写“有意静默”。

只规划并生成以下生产单元：
1. 一个角色与一个 canonical/default 视觉变体；视觉资产 prompt 必须是专业的电影感角色身份板，
   包含严格身份锁定、非对称留白布局、英雄全身视角、辅助视角、轮廓/表情/细节研究和负向约束。
2. 一个从 0 tick 开始、duration_tick=72 的 R2V Element（Timeline 为 24 ticks/s，恰好 3 秒），
   location 为全画幅。只要 3 个关键动作 Shot，三者共同描述这一段 3 秒连续动作，不平均分配时长。
3. 一张供视频模型消费的纯净生成参考分镜：禁止编号、箭头、色标、注释、镜头文字、UI 与时间戳；
   prompt 要逐格明确构图、人物动作、连续性、灯光与可见状态，保持阿沐身份和纸鹤空间关系稳定。
   3 个面板使用紧凑的 2 列×2 行等尺寸布局并按行优先排列，末行的第 3 格居中；空余面积只作
   无边框外层留白，不画第 4 个空槽。每个面板内部画框严格为 16:9，同一格只能出现一个阿沐。
4. 一个严格 3 秒的 HappyHorse R2V 视频；video_prompt 要写清主体、动作时间顺序、摄影机、光线、
   环境动态、身份连续性、首尾状态与负向约束，不要把分镜表布局或标注生成进成片。

使用 professional-media-prompts skill。先完整写入项目结构和专业提示词，再让无人值守工作图依次完成
角色图、分镜图、R2V 视频和最终合成；必须等真实媒体写回 Project 后才算完成。不要只给方案或口头总结。
"""


ONE_MINUTE_GOAL = """\
请完成真实、可播放、总时长严格 60 秒的 16:9 无对白动画短剧《雨夜灯塔》。使用 HappyHorse
能力范围内的 5 个首尾连续 R2V Element，时长依次为 8、13、10、15、14 秒；Shot 数依次为
3、5、4、6、4。这个分布来自各段动作密度，禁止改成统一 15 秒或统一 5 Shot。

故事是旧式小机器人阿沐在暴雨夜跟随失去光芒的青蓝折纸鹤，穿过积水老城和断桥，到达熄灭的
海边灯塔，献出胸前能量让灯塔在黎明前重亮。全片有意静默。只使用 `char:amu`、`prop:crane`、
`scene:rain-city-lighthouse` 三个 canonical 视觉资产。每个 storyboard 的面板数等于对应 Shot 数，
每格内部与最终视频同为 16:9；使用等尺寸紧凑布局、末行居中和外侧留白，禁止空槽、重复占位、
拉伸、裁切、混合尺寸、编号、箭头、色标、说明文字、字幕、时间戳和 UI。

每个 video_prompt 遵守 HappyHorse `[Image N]` 协议和 Runtime 的真实顺序：storyboard →
cast lineup → character → scene → prop → explicit extra refs。写清每项采用/排除职责、首帧承接、
可执行主动作链、摄影机、光线/天气、声音、身份/空间不变量与明确末帧。使用
professional-media-prompts skill；完成 3 张视觉资产、5 张分镜、5 段视频和最终 60 秒合成。
"""


ONE_MINUTE_VISUAL_GOAL = """\
请只完成 60 秒动画短剧《雨夜灯塔》的视觉开发阶段，不创建任何 Timeline Element、
Shot、分镜、视频或合成。使用 professional-media-prompts skill，把三个实体及其专业
生产 Prompt 一次性完整写入 Project；每个实体只能有一个 canonical/default Variant，
然后等待三张真实 qwen-image3 视觉资产生成并选中，不能只写一句实体简介代替 Prompt。

全片是 16:9 高端电影动画：旧式小机器人“阿沐”在暴雨夜跟随青蓝折纸鹤穿过积水
老城和断桥，到达海边熄灭的灯塔，献出胸前能量让灯塔在黎明前重亮。全片有意静默，
冷蓝雨夜逐步过渡到青蓝能量与暖金黎明。

只创建以下三个 VisualEntity，每个只有一个 Variant 和一张产物：
1. `char:amu`：圆头略宽于紧凑箱形躯干，磨砂米白外壳、深灰接缝、唯一一颗青蓝圆形
   胸灯、短小三指手、旧式可见关节、约三头高和轻微磨损。生成艺术性 16:9 电影角色
   身份板：非对称留白、一个大型英雄全身、正侧背与姿态辅助研究、2–3 个黑色轮廓、
   表情/眼灯变化、胸灯/手/关节/外壳细节；视图不重叠、不裁脸、不隐藏肢体。所有视图
   严格同一身份和比例；无场景、无无关道具、无长文、无 Logo、水印或身份漂移。
2. `prop:crane`：一掌大小、单张青蓝半透明纸折成的经典折纸鹤，锐利准确折痕，具有暗淡
   与柔和发光两种状态。生成艺术性 16:9 道具身份板：一个英雄视图、互不重复的正侧背/
   俯仰角、翼尖与折痕材质特写、熄灭/发光状态和清晰尺度参照；无人物、无环境叙事、
   无堆叠融合、无额外纸鹤、无文字或水印。
3. `scene:rain-city-lighthouse`：无人物的 16:9 场景连续性图集，清楚分离但不重叠地展示
   同一世界的木屋檐、积水老街、锈铁与风化混凝土断桥、圆形石砌灯塔入口、中央熄灭
   光学透镜和螺旋铁梯的灯塔核心；以方向关系/消失点明确从城内到海边的拓扑，材料、
   冷蓝暴雨、远处微暖光和空间方向一致。不得出现阿沐、纸鹤、路人、文本、Logo 或水印。

三个 Variant.prompt 都必须包含：生成目标、主体/空间身份锁定、艺术化布局与独立研究区、
材质/光线/配色、生产用途和最小排除项。先持久化完整 Prompt，真实图片生成并选中后才完成。
"""


ONE_MINUTE_ELEMENT_COMMON = """\
只新增指定的一个 R2V Element，不创建其他 Element，不新增或重做 VisualEntity/Variant，
不改动已经存在的 Element。Timeline 沿用 Project 默认 1000 ticks/s，Project 与最终视频均为 16:9。复用
现有 `char:amu`、`prop:crane`、`scene:rain-city-lighthouse` 的 canonical/default 选中产物，
在 creation 顶层写全 character_refs、prop_refs、scene_ref 和 visual_variant_refs。

当前 Element 的时长与 Shot 数已经在上方按动作密度给出；不得改成统一 15 秒或统一 5 Shot，
也不得平均分配 Shot 时长。min_dialogue_ratio=0，narrative 必须明写“有意静默”及原因；
无对白、旁白、TTS、字幕、Logo、水印或额外人物，以雨、机械、风、海浪和音乐表达。

使用 professional-media-prompts skill 编写一张面板数严格等于当前 Shot 数的纯净生成参考分镜 Prompt。
硬规则：每一个独立分镜格的内部画框都严格为 16:9，整张输出也为 16:9；使用接近方形的紧凑
等尺寸布局，按行优先排列并把不足的末行居中，剩余面积只作无边框外层留白，不画空槽；
不拉伸、不压扁、不裁边、不合并、不改成另一宽高比，不使用 masonry、英雄大格或混合尺寸布局。同一格
只能出现一个阿沐。按 Shot 顺序逐格写动作相位与末态、景别/机位/构图、主体/道具/环境空间关系、
主体和摄影机运动、光线与衔接；禁止分镜编号、箭头、色标、说明文字、字幕、时间戳、UI。

video_prompt 使用 HappyHorse 当前官方引用语法并服从 Runtime 实际图片顺序：storyboard 固定
为 `[Image 1]`，随后依次是 cast lineup、character、scene、prop、explicit extra refs；逐一映射
真实参考并写采用项/排除项；不得使用“图1”、`image1`、
`character1`。明确排除身份板白底、多视图/标签、场景图集分区，以及分镜宫格/边框进入成片。
Prompt 必须包含当前实际时长的一句话目标、首帧承接、可执行动作链、摄影机、光线/天气、声音、
身份/空间不变量、明确末帧状态和最小禁止项。等待当前分镜与 happyhorse1.1 R2V 视频真实生成。
"""


ONE_MINUTE_ELEMENT_GOALS = (
    """\
为《雨夜灯塔》新增且只新增 `elem:signal`：start_tick=0、duration_tick=8000、全画幅，恰好 3 个 Shot。
0–8 秒“发现与决定”：屋檐下的阿沐发现暗淡纸鹤；纸鹤微亮，远方熄灭灯塔成为视线目标；
阿沐从孤独、试探转为决定出发。末帧必须是阿沐面向画面左侧出口，纸鹤在其左前方悬停，
胸灯低亮，能直接接下段同方向运动。
"""
    + ONE_MINUTE_ELEMENT_COMMON,
    """\
为《雨夜灯塔》新增且只新增 `elem:alley`：start_tick=8000、duration_tick=13000、全画幅，恰好 5 个 Shot。
8–21 秒“追随与第一次风险”：首帧逐项承接 `elem:signal` 末态与向左运动，阿沐跟随纸鹤
穿过积水老街；浪涌几乎卷走纸鹤，阿沐伸手护住它并继续。末帧两者到达断桥右端，阿沐仍
朝左前方，纸鹤在手边，远方灯塔可见。
"""
    + ONE_MINUTE_ELEMENT_COMMON,
    """\
为《雨夜灯塔》新增且只新增 `elem:approach`：start_tick=21000、duration_tick=10000、全画幅，恰好 4 个 Shot。
21–31 秒“风暴逼近”：首帧承接断桥右端；阿沐与纸鹤沿第一段桥面前进，风势升级，远处灯塔
在闪电中短暂显形，松动的桥板预示危险。末帧阿沐抓住右侧缆索，重心降低，纸鹤贴近胸灯避风。
"""
    + ONE_MINUTE_ELEMENT_COMMON,
    """\
为《雨夜灯塔》新增且只新增 `elem:bridge`：start_tick=31000、duration_tick=15000、全画幅，恰好 6 个 Shot。
31–46 秒“危机与突破”：首帧逐项承接 `elem:approach` 的抓索姿态；阿沐沿湿滑断桥前进，
桥板崩落造成一次清楚失足，它抓住缆索重新爬起并护住纸鹤，到达灯塔入口。末帧阿沐跪在
入口平台，右手撑地，纸鹤稳定悬停，胸灯更暗，铁门在前方。
"""
    + ONE_MINUTE_ELEMENT_COMMON,
    """\
为《雨夜灯塔》新增且只新增 `elem:lighthouse`：start_tick=46000、duration_tick=14000、全画幅，恰好 4 个 Shot。
46–60 秒“牺牲与希望”：首帧逐项承接 `elem:bridge` 入口平台状态；阿沐进入灯塔核心，把
胸灯最后能量传入熄灭装置，灯塔逐级点亮、光束扫过海面，纸鹤恢复明亮。最后突然打开空间：
暖金黎明大远景，灯塔光照亮海面，阿沐与纸鹤是安静、清楚的小剪影。
"""
    + ONE_MINUTE_ELEMENT_COMMON,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("micro3s", "minute60"),
        default="micro3s",
        help="acceptance scenario to run",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="isolated run directory (default: tmp/creator-real-e2e/<UTC stamp>)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=1800.0,
        help="wall-clock deadline for planning, generation and final compose",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="progress polling cadence",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume the single existing project below --output-root",
    )
    parser.add_argument(
        "--prompt-only",
        action="store_true",
        help=(
            "use the real text model but disable image/video/compose calls; "
            "supported by the minute60 profile"
        ),
    )
    return parser.parse_args()


def _configure_environment(
    run_root: Path,
    *,
    prompt_only: bool,
) -> Path:
    shared_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not shared_key:
        raise RuntimeError(
            "DASHSCOPE_API_KEY is required for the real E2E run",
        )

    data_root = (run_root / "data").resolve()
    config_path = data_root / "config" / "model_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "execution_authorization": {
                    "mode": "required" if prompt_only else "allow_all",
                },
                "creation_checkpoints": {
                    "mode": "skip",
                    "execution_mode": "delegated",
                },
                "media_review": {
                    "mode": "required" if prompt_only else "auto_approve",
                },
                "self_review": {
                    "sync_enabled": False,
                    "media_enabled": False,
                    "render_enabled": False,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # Keys live only in this process. Dedicated values still win when a caller
    # intentionally supplied them; otherwise the one DashScope key is reused.
    os.environ["CREATOR_DATA_ROOT"] = str(data_root)
    os.environ["CREATOR_MODEL_CONFIG_PATH"] = str(config_path)
    os.environ.setdefault("TEXT_API_KEY", shared_key)
    os.environ.setdefault("TEXT_MODEL_NAME", "qwen3.7-plus")
    os.environ.setdefault("TEXT_PROTOCOL", "OpenAI 兼容")
    os.environ["IMAGE_MODEL"] = "DASHSCOPE"
    os.environ.setdefault("DASHSCOPE_IMAGE_API_KEY", shared_key)
    os.environ["DASHSCOPE_IMAGE_MODEL_NAME"] = "qwen-image-3.0-pro"
    # Multi-reference, long-form professional storyboard prompts regularly
    # need more than the generic 240-second image timeout.  The paid
    # acceptance must wait for the one provider job instead of creating an
    # avoidable duplicate through the scheduler's transient retry path.
    os.environ.setdefault("DASHSCOPE_IMAGE_TIMEOUT", "900")
    os.environ.setdefault("VIDEO_API_KEY", shared_key)
    os.environ["VIDEO_MODEL_NAME"] = "happyhorse-1.1"
    os.environ.setdefault(
        "VIDEO_BASE_URL",
        "https://dashscope.aliyuncs.com/api/v1",
    )
    # The acceptance is about generation correctness, not optional review
    # tiers. Explicit false values prevent a parent shell from turning them on.
    os.environ["CREATOR_SYNC_REVIEW_ENABLED"] = "0"
    os.environ["CREATOR_MEDIA_REVIEW_ENABLED"] = "0"
    os.environ["CREATOR_SELF_REVIEW_ENABLED"] = "0"
    return data_root


def _artifact_paths(project: Any, project_root: Path) -> dict[str, list[str]]:
    selected_ids = {
        slot.selected_version_id
        for slot in project.assets.artifact_slots_by_id.values()
        if slot.selected_version_id
    }
    paths: dict[str, list[str]] = {}
    for version_id in selected_ids:
        version = project.assets.artifact_versions_by_id.get(version_id)
        if version is None:
            continue
        indexed = project.assets.files_by_id.get(version.file_id)
        if indexed is None:
            continue
        path = project_root.joinpath(*Path(indexed.relative_uri).parts)
        paths.setdefault(version.kind, []).append(str(path.resolve()))
    for items in paths.values():
        items.sort()
    return dict(sorted(paths.items()))


def _project_prompt_facts(project: Any) -> dict[str, Any]:
    visual_prompts: list[dict[str, Any]] = []
    for entity_id in project.visual.entities.order:
        entity = project.visual.entities.items[entity_id]
        for variant_id in entity.variants.order:
            variant = entity.variants.items[variant_id]
            visual_prompts.append(
                {
                    "entity_id": entity.entity_id,
                    "entity": entity.name,
                    "kind": entity.kind,
                    "description": entity.description,
                    "continuity": entity.continuity,
                    "variant": variant_id,
                    "requirements": variant.requirements,
                    "reference_asset_version_ids": (
                        variant.reference_asset_version_ids
                    ),
                    "reference_artifact_version_ids": (
                        variant.reference_artifact_version_ids
                    ),
                    "selected_artifact_version_id": (
                        variant.selected_artifact_version_id
                    ),
                    "prompt": variant.prompt,
                },
            )

    elements: list[dict[str, Any]] = []
    for timeline_id in project.timelines.order:
        timeline = project.timelines.items[timeline_id]
        for element in timeline.elements_by_id.values():
            creation = element.creation
            if getattr(creation, "type", "") != "r2v":
                continue
            elements.append(
                {
                    "timeline_id": timeline_id,
                    "element_id": element.element_id,
                    "label": element.label,
                    "start_tick": element.span.start_tick,
                    "duration_tick": element.span.duration_tick,
                    "duration_seconds": (
                        element.span.duration_tick / timeline.ticks_per_second
                    ),
                    "continuity": creation.continuity,
                    "character_refs": creation.character_refs,
                    "scene_ref": creation.scene_ref,
                    "prop_refs": creation.prop_refs,
                    "visual_variant_refs": creation.visual_variant_refs,
                    "shot_count": len(creation.shots.order),
                    "shots": [
                        {
                            "shot_id": shot.shot_id,
                            "description": shot.description,
                            "camera": shot.camera.value,
                            "framing": shot.framing.value,
                            "camera_description": shot.camera_description,
                            "dialogue": shot.dialogue,
                            "duration_seconds": shot.duration_seconds,
                            "character_refs": shot.character_refs,
                            "scene_ref": shot.scene_ref,
                            "prop_refs": shot.prop_refs,
                        }
                        for shot_id in creation.shots.order
                        for shot in [creation.shots.items[shot_id]]
                    ],
                    "min_dialogue_ratio": creation.min_dialogue_ratio,
                    "narrative": creation.narrative,
                    "storyboard_prompt": creation.storyboard_prompt,
                    "storyboard_reference_version_ids": (
                        creation.storyboard_reference_version_ids
                    ),
                    "video_prompt": creation.video_prompt,
                    "video_reference_version_ids": (
                        creation.video_reference_version_ids
                    ),
                },
            )
    return {"visual_prompts": visual_prompts, "r2v_elements": elements}


def _task_report(executions: Any, project_id: str) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for task in reversed(executions.list_tasks(project_id)):
        attempts = executions.list_task_attempts(project_id, task.task_id)
        provider_ids = sorted(
            {
                attempt.provider_task_id
                for attempt in attempts
                if attempt.provider_task_id
            },
        )
        report.append(
            {
                "task_id": task.task_id,
                "kind": task.kind.value,
                "status": task.status.value,
                "target_ref": (task.metadata or {}).get("targetRef"),
                "provider_task_ids": provider_ids,
                "error": task.error,
            },
        )
    return report


# The real-provider runner intentionally keeps the resumable orchestration in
# one routine so its deadline and state transitions have a single owner.
# pylint: disable-next=too-many-branches,too-many-statements
async def _run(
    run_root: Path,
    timeout: float,
    poll_seconds: float,
    profile: str,
    resume: bool,
    prompt_only: bool,
) -> Path:
    if prompt_only and profile != "minute60":
        raise ValueError("--prompt-only currently requires --profile minute60")
    data_root = _configure_environment(run_root, prompt_only=prompt_only)
    is_minute = profile == "minute60"
    goal = ONE_MINUTE_VISUAL_GOAL if is_minute else GOAL
    if prompt_only:
        goal = goal.replace(
            "先持久化完整 Prompt，真实图片生成并选中后才完成。",
            "先持久化完整 Prompt；本次只验收 Prompt，不等待或请求任何媒体生成。",
        )
    target_duration = 60 if is_minute else 3

    # Import only after the isolated model environment has been bound: several
    # provider modules retain environment defaults at import time.
    from services.file_agent_runtime.driver import FileCreatorAgentRuntime
    from services.file_agent_runtime.work_graph import derive_work_graph
    from services.media_files import (
        shutdown_file_media_execution_services,
        start_file_media_execution_services,
    )
    from services.project_files.facade import CreatorFileServices
    from services.project_files.models import Project, ProjectSettings

    services = CreatorFileServices.create(data_root)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if resume:
        project_ids = services.projects.discover_project_ids()
        if len(project_ids) != 1:
            raise RuntimeError(
                "--resume requires exactly one existing project below "
                f"{data_root}; found {len(project_ids)}",
            )
        project_id = project_ids[0]
        session = services.sessions.get_project_session(project_id)
        session_id = session.session_id
        conversations = services.sessions.list_conversations(
            project_id,
            session_id,
        )
        conversation = next(
            (item for item in conversations if item.is_default),
            conversations[0] if conversations else None,
        )
        if conversation is None:
            raise RuntimeError("resumed Creator project has no conversation")
        conversation_id = conversation.conversation_id
        snapshot = services.projects.read(project_id)
    else:
        project_id = f"real-short-drama-{stamp.lower()}"
        session_id = f"session-{stamp.lower()}"
        conversation_id = f"conversation-{stamp.lower()}"
        goal_id = f"goal-{stamp.lower()}"

        def initialize(staged_root: Path) -> None:
            services.sessions.initialize_staged_project(
                staged_root,
                project_id,
                session_id=session_id,
                conversation_id=conversation_id,
                initial_goal=goal,
                goal_id=goal_id,
                initial_message_id=f"message-{stamp.lower()}",
                initial_client_message_id=f"client-{stamp.lower()}",
            )

        project = Project.new(
            project_id=project_id,
            name=(
                "真实主 Agent Prompt 验收：雨夜灯塔（60 秒·变节奏）"
                if prompt_only
                else ("真实 E2E：雨夜灯塔（60 秒）" if is_minute else "真实 E2E：雨夜纸鹤（3 秒）")
            ),
            description=(
                "真实 qwen3.7-plus 主 Agent、无 R2V Specialist、无媒体派发；"
                "用于审阅非机械时长/Shot 分布与生产级 Prompt 合同"
                if prompt_only
                else "qwen-image3 + happyhorse1.1 的 Creator 一分钟真实端到端验收"
                if is_minute
                else "qwen-image3 + happyhorse1.1 的 Creator 真实端到端验收"
            ),
            scenario="short_drama",
            settings=ProjectSettings(
                aspect_ratio="16:9",
                resolution="720P",
                language="zh-CN",
                target_duration_seconds=target_duration,
                content_type="micro_short_drama",
            ),
        )
        snapshot = services.projects.create(
            project,
            initialize_staged_project=initialize,
        )
        services.poller.note_commit(snapshot)

    runtime = FileCreatorAgentRuntime(
        services,
        poll_interval_seconds=0.25,
        max_model_turns=36 if is_minute else 24,
        specialist_max_model_turns=20 if is_minute else 16,
        model_turn_timeout_seconds=300,
    )
    if not prompt_only:
        await start_file_media_execution_services(
            services,
            poll_interval_seconds=1.0,
        )
    await runtime.start()
    runtime.notify(project_id)

    started = time.monotonic()
    last_summary: str | None = None
    success = False
    minute_element_specs = (
        ("elem:signal", 0, 8000, 3),
        ("elem:alley", 8000, 13000, 5),
        ("elem:approach", 21000, 10000, 4),
        ("elem:bridge", 31000, 15000, 6),
        ("elem:lighthouse", 46000, 14000, 4),
    )
    existing_element_ids = {
        element.element_id
        for timeline in snapshot.project.timelines.items.values()
        for element in timeline.elements_by_id.values()
    }
    minute_element_phases_queued = 0
    if is_minute:
        for element_id, _start, _duration, _shots in minute_element_specs:
            if element_id not in existing_element_ids:
                break
            minute_element_phases_queued += 1
    expected_counts = (
        {"visual": 3, "storyboard": 5, "video": 5, "compose": 1}
        if is_minute
        else {"visual": 2, "storyboard": 1, "video": 1, "compose": 1}
    )
    try:
        while time.monotonic() - started < timeout:
            await asyncio.sleep(poll_seconds)
            snapshot = await asyncio.to_thread(
                services.projects.read,
                project_id,
            )
            tasks = await asyncio.to_thread(
                runtime.executions.list_tasks,
                project_id,
            )
            graph = derive_work_graph(snapshot.project, tasks)
            summary = json.dumps(
                {
                    "graph": graph.counts(),
                    "minute_element_phases_queued": (
                        minute_element_phases_queued
                    ),
                },
                sort_keys=True,
            )
            if summary != last_summary:
                elapsed = round(time.monotonic() - started, 1)
                print(
                    json.dumps(
                        {
                            "event": "progress",
                            "elapsed_seconds": elapsed,
                            "generation": snapshot.project.generation,
                            "graph": graph.counts(),
                            "minute_element_phases_queued": (
                                minute_element_phases_queued
                            ),
                            "nodes": {
                                node.node_id: node.status.value
                                for node in graph.nodes
                            },
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                last_summary = summary

            actual_counts = {
                kind: sum(1 for node in graph.nodes if node.kind == kind)
                for kind in expected_counts
            }

            if is_minute and minute_element_phases_queued == 0:
                visual_nodes = [
                    node for node in graph.nodes if node.kind == "visual"
                ]
                visual_prompts_ready = all(
                    entity.variants.order
                    and all(
                        entity.variants.items[variant_id].prompt.strip()
                        for variant_id in entity.variants.order
                    )
                    for entity in snapshot.project.visual.entities.items.values()
                )
                visual_phase_ready = (
                    len(snapshot.project.visual.entities.items) == 3
                    and visual_prompts_ready
                    if prompt_only
                    else len(visual_nodes) == 3
                    and all(
                        node.status.value == "done" for node in visual_nodes
                    )
                )
                if visual_phase_ready:
                    services.sessions.append_message(
                        project_id,
                        session_id,
                        conversation_id,
                        role="user",
                        content_parts=[
                            {
                                "type": "text",
                                "text": (
                                    ONE_MINUTE_ELEMENT_GOALS[0].replace(
                                        "等待当前分镜与 happyhorse1.1 R2V 视频真实生成。",
                                        "本次只验收完整 Prompt，不等待或请求任何媒体生成。",
                                    )
                                    if prompt_only
                                    else ONE_MINUTE_ELEMENT_GOALS[0]
                                ),
                            },
                        ],
                        message_id=f"message-{stamp.lower()}-element-1",
                        client_message_id=(
                            f"client-{stamp.lower()}-element-1"
                        ),
                        source="real_e2e_phase",
                    )
                    minute_element_phases_queued = 1
                    runtime.notify(project_id)
                    print(
                        json.dumps(
                            {
                                "event": "phase_queued",
                                "phase": "elem:signal",
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

            if is_minute and 0 < minute_element_phases_queued < len(
                ONE_MINUTE_ELEMENT_GOALS,
            ):
                (
                    previous_id,
                    previous_start,
                    previous_duration,
                    previous_shots,
                ) = minute_element_specs[minute_element_phases_queued - 1]
                previous = next(
                    (
                        element
                        for timeline in snapshot.project.timelines.items.values()
                        for element in timeline.elements_by_id.values()
                        if element.element_id == previous_id
                    ),
                    None,
                )
                previous_ready = (
                    previous is not None
                    and previous.span.start_tick == previous_start
                    and previous.span.duration_tick == previous_duration
                    and getattr(previous.creation, "type", "") == "r2v"
                    and len(previous.creation.shots.order) == previous_shots
                    and bool(previous.creation.storyboard_prompt.strip())
                    and bool(previous.creation.video_prompt.strip())
                )
                if previous_ready:
                    next_index = minute_element_phases_queued
                    next_id = minute_element_specs[next_index][0]
                    services.sessions.append_message(
                        project_id,
                        session_id,
                        conversation_id,
                        role="user",
                        content_parts=[
                            {
                                "type": "text",
                                "text": (
                                    ONE_MINUTE_ELEMENT_GOALS[
                                        next_index
                                    ].replace(
                                        "等待当前分镜与 happyhorse1.1 R2V 视频真实生成。",
                                        "本次只验收完整 Prompt，不等待或请求任何媒体生成。",
                                    )
                                    if prompt_only
                                    else ONE_MINUTE_ELEMENT_GOALS[next_index]
                                ),
                            },
                        ],
                        message_id=(
                            f"message-{stamp.lower()}-element-{next_index + 1}"
                        ),
                        client_message_id=(
                            f"client-{stamp.lower()}-element-{next_index + 1}"
                        ),
                        source="real_e2e_phase",
                    )
                    minute_element_phases_queued += 1
                    runtime.notify(project_id)
                    print(
                        json.dumps(
                            {"event": "phase_queued", "phase": next_id},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

            element_by_id = {
                element.element_id: element
                for timeline in snapshot.project.timelines.items.values()
                for element in timeline.elements_by_id.values()
            }
            prompt_only_ready = prompt_only and all(
                (
                    (element := element_by_id.get(element_id)) is not None
                    and element.span.start_tick == start_tick
                    and element.span.duration_tick == duration_tick
                    and getattr(element.creation, "type", "") == "r2v"
                    and len(element.creation.shots.order) == shot_count
                    and bool(element.creation.storyboard_prompt.strip())
                    and bool(element.creation.video_prompt.strip())
                )
                for element_id, start_tick, duration_tick, shot_count in (
                    minute_element_specs
                )
            )
            success = (
                prompt_only_ready
                if prompt_only
                else (
                    actual_counts == expected_counts
                    and (
                        not is_minute
                        or minute_element_phases_queued
                        == len(ONE_MINUTE_ELEMENT_GOALS)
                    )
                    and all(
                        node.status.value == "done" for node in graph.nodes
                    )
                )
            )
            if success:
                # A final prompt commit can make the acceptance predicate true
                # while the main Agent is still emitting its closing summary.
                # Let that run reach its durable terminal state before stop()
                # revokes the epoch and produces a misleading StaleAgentRun.
                await runtime.wait_until_idle(project_id)
                break
    finally:
        await runtime.stop()
        if not prompt_only:
            await shutdown_file_media_execution_services()

    snapshot = services.projects.read(project_id)
    project_root = services.projects.project_root(project_id)
    tasks = _task_report(runtime.executions, project_id)
    graph = derive_work_graph(
        snapshot.project,
        runtime.executions.list_tasks(project_id),
    )
    facts = _project_prompt_facts(snapshot.project)
    paths = _artifact_paths(snapshot.project, project_root)
    from services.run_review.prompt_contract import (
        check_changed_r2v_prompt_contracts,
    )

    prompt_contract = check_changed_r2v_prompt_contracts(
        snapshot.project.model_dump(mode="json"),
        ["/timelines"],
    )
    success = success and bool(prompt_contract.get("passed"))
    specialist_runs = runtime.executions.list_specialist_runs(project_id)
    report = {
        "ok": success,
        "profile": profile,
        "prompt_only": prompt_only,
        "resumed": resume,
        "project_id": project_id,
        "models": {
            "image": "qwen-image-3.0-pro",
            "video_configured": "happyhorse-1.1",
            "video_effective_r2v": "happyhorse-1.1-r2v",
            "text": os.environ.get("TEXT_MODEL_NAME", "qwen3.7-plus"),
        },
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "minute_element_phases_queued": minute_element_phases_queued,
        "graph": {
            node.node_id: {
                "kind": node.kind,
                "status": node.status.value,
                "error": node.error,
                "missing": list(node.missing),
            }
            for node in graph.nodes
        },
        "expected_graph_counts": expected_counts,
        "prompt_facts": facts,
        "prompt_contract": prompt_contract,
        "specialist_runs": [
            {
                "run_id": run.run_id,
                "role": run.role.value,
                "status": run.status.value,
            }
            for run in specialist_runs
        ],
        "artifacts": paths,
        "tasks": tasks,
        "project_json": str((project_root / "project.json").resolve()),
    }
    report_path = run_root / "acceptance-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "event": "complete" if success else "incomplete",
                "report": str(report_path.resolve()),
                "artifacts": paths,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if not success:
        raise RuntimeError(
            f"real Creator E2E did not converge before the deadline; see {report_path}",
        )
    return report_path


def main() -> int:
    args = _arguments()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_root = (
        (
            args.output_root
            if args.output_root is not None
            else REPOSITORY_ROOT / "tmp" / "creator-real-e2e" / stamp
        )
        .expanduser()
        .resolve()
    )
    if args.resume:
        if not run_root.is_dir():
            raise RuntimeError("--resume output root does not exist")
    else:
        run_root.mkdir(parents=True, exist_ok=False)
    report = asyncio.run(
        _run(
            run_root,
            timeout=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
            profile=args.profile,
            resume=args.resume,
            prompt_only=args.prompt_only,
        ),
    )
    print(f"acceptance report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
