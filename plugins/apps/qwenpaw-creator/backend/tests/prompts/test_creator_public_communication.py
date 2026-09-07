# -*- coding: utf-8 -*-
"""The main prompt must separate public narration from its machine contract."""

from __future__ import annotations

import pytest

from services.file_agent_runtime import prompts

pytestmark = pytest.mark.unit


def _render() -> str:
    spec = prompts.FILE_AGENT_PROMPT_SPECS["creator_agent.system"]
    values = dict.fromkeys(spec.placeholders, "")
    values.update(
        project_id="project-public-communication",
        workspace_schema=(
            'PROJECT_JSON_SCHEMA={"entity_id":"char:test",'
            '"narrative":"scene intent","min_dialogue_ratio":0}'
        ),
        external_skills="EXTERNAL_SKILL_TECHNICAL_INSTRUCTIONS",
    )
    return prompts.render_file_agent_prompt("creator_agent.system", **values)


def test_public_narration_boundary_covers_intermediate_and_final_content():
    prompt = _render()
    boundary = prompt.split("# 面向用户的沟通边界", 1)[1].split(
        "# Workspace 基础 Schema",
        1,
    )[0]
    for contract in (
        "工具调用前后的过程说明",
        "等待中的更新和本轮最终答复",
        "只说明正在做的创作工作",
        "过程说明和总结使用用户当前的交流语言",
        "不要输出思考、推理草稿",
        "普通正文不得列出内部 ID",
        "Schema 字段名、参数赋值、JSON",
        "可展开详情",
        "剧情、表演、构图、时长和修改内容",
        "只有真实任务已开始，才说正在生成",
        "只有系统实际返回待审阅或待授权事项时",
        "不虚构卡片、自动续跑、剩余时间或百分比",
    ):
        assert contract in boundary
    assert prompt.index("# 面向用户的沟通边界") < prompt.index(
        "PROJECT_JSON_SCHEMA=",
    )
    assert "所有普通 assistant 正文是否遵守「面向用户的沟通边界」" in prompt


def test_public_boundary_preserves_the_exact_machine_schema_and_skill_input():
    prompt = _render()
    # Public communication guidance must not rewrite the actual schema or IDs
    # needed to execute a user's task; those still belong in tool arguments.
    assert (
        'PROJECT_JSON_SCHEMA={"entity_id":"char:test",'
        '"narrative":"scene intent","min_dialogue_ratio":0}'
    ) in prompt
    assert "EXTERNAL_SKILL_TECHNICAL_INSTRUCTIONS" in prompt
    assert "工具调用参数、项目写入、素材引用与委派任务仍须使用准确的现有 ID" in prompt
    assert "不能为了隐藏技术名称改名、删字段、翻译 ID" in prompt


def test_next_prompt_render_reloads_the_communication_contract_without_restart(
    tmp_path,
    monkeypatch,
):
    # The running driver builds a system prompt at the start of each model
    # loop. Prove file edits reach the next render without changing a prompt
    # string that an already-running loop has captured.
    name = "creator_agent.system"
    source = prompts.load_file_agent_prompt(name)
    path = tmp_path / prompts.FILE_AGENT_PROMPT_SPECS[name].filename
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(prompts, "_PROMPT_ROOT", tmp_path)
    captured = _render()
    path.write_text(
        source + "\nPUBLIC_CONTRACT_RELOAD_PROBE\n",
        encoding="utf-8",
    )
    assert "PUBLIC_CONTRACT_RELOAD_PROBE" not in captured
    assert "PUBLIC_CONTRACT_RELOAD_PROBE" in _render()


def test_blocked_review_request_is_not_presented_as_submitted_media():
    prompt = _render()
    assert "先前内容尚待审阅，因此本次没有启动新制作" in prompt
    assert "请求已经结束，没有排队" in prompt
    assert "read_project`、写入提示词、绑定参考图和审阅通过本身都不创建媒体任务" in prompt
    for stale_claim in (
        "补齐后自动恢复生成",
        "角色图就绪后调度器自动渲染阵容图",
        "prompt 就绪后调度器会自动并行生成并选定产物",
        "调度器按 Project 字段自动生成",
        "指纹变化后调度器自动重生成",
        "修复后调度器会自动重新生成",
        "等待审阅通过后自动继续",
    ):
        assert stale_claim not in prompt


def test_media_prompt_authoring_separates_names_from_exact_reference_fields():
    prompt = _render()
    rule = prompt.split("**生成提示词的名称与引用边界（必遵）**", 1)[1].split(
        "**storyboard_prompt 制作级编译（必遵）**",
        1,
    )[0]
    assert "人物、场景和道具用 Project 中实际的 name 指称" in rule
    assert "保持编号与实际提交顺序一致" in rule
    assert "准确的内部 ID 仅保留在" in rule
    assert "有序版本引用等结构化字段中" in rule
    assert "用户明确要求照录的台词、标题、代码、URL" in rule


def test_storyboard_guidance_separates_keyframes_and_continuous_shots():
    prompt = _render()
    assert "一个完整的 6 秒连续镜头可以保持 1 Shot" in prompt
    assert "几列几行、每格内部比例" in prompt
    assert "不为了凑网格扩增 Shot" in prompt
    assert "这是按本段动作需要选择的例子，不是所有项目固定九格" in prompt
    assert "首格必须承接上一镜头末态" in prompt
    assert "不把每格变成一次切镜" in prompt
    assert "因此规划 Shot 数量时优先取完全平方数" not in prompt
    assert "Prompt 开头依次写清：交付模式" not in prompt


@pytest.mark.parametrize("mode", ["required", "allow_all"])
def test_main_prompt_states_the_actual_authorization_mode(monkeypatch, mode):
    from models import config

    monkeypatch.setattr(
        config,
        "get_execution_authorization_mode",
        lambda: mode,
    )
    prompt = prompts.render_creator_system_prompt(
        project_id="public-status-test",
        workspace_schema="schema",
        external_skills="",
        live_operation="",
    )
    actual = prompt.split("# 当前制作执行方式", 1)[1]
    if mode == "required":
        assert "全局后台调度器不会自动启动制作" in actual
        assert "该请求已结束、未排队" in actual
        assert "审阅通过后须重新请求" in actual
    else:
        assert "允许后台自动执行就绪的媒体节点" in actual
        assert "只依据真实任务和产物报告进展" in actual
