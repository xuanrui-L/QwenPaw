# -*- coding: utf-8 -*-
"""interaction_draft 执行服务：文本模型起草抉择动效并写回 element.motion。"""
from __future__ import annotations

import asyncio

import pytest

from domain.errors import ValidationError
from services.media_files import interaction_execution
from services.media_files.interaction_execution import (
    execute_file_interaction_command,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    InteractionCreation,
    InteractionOption,
    NarrativeEdge,
    Project,
    Timeline,
    TimelineElement,
    TimelineSpan,
)
from utils.exceptions import ModelError

pytestmark = pytest.mark.unit

PROJECT_ID = "p-interaction-exec"
ELEMENT_ID = "el:choice"

GOOD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"/><style>
@keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.05)}}
.option{animation:pulse 4s ease-in-out infinite}
</style></head>
<body>
<div class="question">是否当众揭发沈修？</div>
<button class="option" data-edge-ref="edge:a">选择A · 揭发真相</button>
<button class="option" data-edge-ref="edge:b">选择B · 保持沉默</button>
</body>
</html>"""

BAD_HTML_MISSING_REF = """<!DOCTYPE html>
<html><body>
<button data-edge-ref="edge:a">选择A · 揭发真相</button>
<button>选择B · 保持沉默</button>
</body></html>"""


def _services(tmp_path) -> CreatorFileServices:
    services = CreatorFileServices.create(tmp_path.resolve())
    project = Project.new(project_id=PROJECT_ID, name="Interaction Exec")
    project.timelines.items["timeline:main"].title = "第3集 · 双重身份"
    for timeline_id, title in (
        ("timeline:ep4a", "第4集A · 真相大白"),
        ("timeline:ep4b", "第4集B · 沉默代价"),
    ):
        project.timelines.items[timeline_id] = Timeline(
            timeline_id=timeline_id,
            title=title,
        )
        project.timelines.order.append(timeline_id)
    project.narrative_edges = [
        NarrativeEdge(
            edge_id="edge:a",
            source_timeline_id="timeline:main",
            target_timeline_id="timeline:ep4a",
            label="选择A · 揭发真相",
        ),
        NarrativeEdge(
            edge_id="edge:b",
            source_timeline_id="timeline:main",
            target_timeline_id="timeline:ep4b",
            label="选择B · 保持沉默",
        ),
    ]
    project.timelines.items["timeline:main"].elements_by_id[
        ELEMENT_ID
    ] = TimelineElement(
        element_id=ELEMENT_ID,
        label="观众抉择",
        span=TimelineSpan(start_tick=88_000, duration_tick=4_000),
        creation=InteractionCreation(
            type="interaction",
            question="是否当众揭发沈修？",
            options=[
                InteractionOption(edge_ref="edge:a"),
                InteractionOption(edge_ref="edge:b"),
            ],
            countdown_seconds=10,
            default_edge_ref="edge:a",
        ),
    )
    services.projects.create(
        Project.model_validate(project.model_dump(mode="json")),
    )
    return services


def _mock_chat(monkeypatch, replies: list[str]):
    calls: list[dict] = []

    async def fake_chat_completion(prompt, *, system_prompt="", **_kwargs):
        calls.append({"prompt": prompt, "system": system_prompt})
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(
        interaction_execution.text_model,
        "chat_completion",
        fake_chat_completion,
    )
    return calls


def _execute(services, key: str = "dag-interaction-1"):
    return asyncio.run(
        execute_file_interaction_command(
            services,
            project_id=PROJECT_ID,
            target_ref=f"element:{ELEMENT_ID}",
            arguments={},
            idempotency_key=key,
        ),
    )


def test_interaction_command_writes_motion_back(tmp_path, monkeypatch):
    services = _services(tmp_path)
    # 模型输出裹了 markdown 代码围栏：必须被剥掉后再校验/写回。
    calls = _mock_chat(monkeypatch, [f"```html\n{GOOD_HTML}\n```"])

    result = _execute(services)

    assert not result.replayed
    assert result.timeline_id == "timeline:main"
    assert result.element_id == ELEMENT_ID
    snapshot = services.projects.read(PROJECT_ID)
    element = snapshot.project.timelines.items["timeline:main"].elements_by_id[
        ELEMENT_ID
    ]
    motion = element.creation.motion
    assert motion is not None
    assert motion.format == "html_css"
    assert motion.fps == 24
    assert motion.loop is True
    assert 'data-edge-ref="edge:a"' in motion.html
    assert 'data-edge-ref="edge:b"' in motion.html
    assert "```" not in motion.html
    # design_notes = prompt 摘要 + 指纹标记。
    assert "是否当众揭发沈修？" in motion.design_notes
    assert f"input_fingerprint={result.input_fingerprint}" in (
        motion.design_notes
    )
    # prompt 携带问题、边 label（join narrative_edges）与倒计时。
    assert "是否当众揭发沈修？" in calls[0]["prompt"]
    assert "选择A · 揭发真相" in calls[0]["prompt"]
    assert "选择B · 保持沉默" in calls[0]["prompt"]
    assert "10 秒" in calls[0]["prompt"]
    assert "data-edge-ref" in calls[0]["system"]


def test_same_inputs_replay_without_second_model_call(tmp_path, monkeypatch):
    services = _services(tmp_path)
    calls = _mock_chat(monkeypatch, [GOOD_HTML])

    first = _execute(services, key="dag-interaction-1")
    replay = _execute(services, key="dag-interaction-2")

    assert not first.replayed
    assert replay.replayed
    assert replay.input_fingerprint == first.input_fingerprint
    assert len(calls) == 1


def test_bad_output_retries_once_then_succeeds(tmp_path, monkeypatch):
    services = _services(tmp_path)
    calls = _mock_chat(monkeypatch, [BAD_HTML_MISSING_REF, GOOD_HTML])

    result = _execute(services)

    assert not result.replayed
    assert len(calls) == 2
    # 重试 prompt 点名了不合格原因。
    assert "不合格" in calls[1]["prompt"]


def test_persistently_bad_output_raises_model_error(tmp_path, monkeypatch):
    services = _services(tmp_path)
    calls = _mock_chat(
        monkeypatch,
        [BAD_HTML_MISSING_REF, BAD_HTML_MISSING_REF],
    )

    with pytest.raises(ModelError, match="data-edge-ref"):
        _execute(services)

    assert len(calls) == 2
    # 失败不写回：element.motion 保持为空。
    snapshot = services.projects.read(PROJECT_ID)
    element = snapshot.project.timelines.items["timeline:main"].elements_by_id[
        ELEMENT_ID
    ]
    assert element.creation.motion is None


def test_bad_inputs_are_rejected_fail_closed(tmp_path, monkeypatch):
    services = _services(tmp_path)
    # Model output smuggling a <script> is a deterministic model error.
    scripted = GOOD_HTML.replace(
        "</body>",
        "<script>alert(1)</script></body>",
    )
    calls = _mock_chat(monkeypatch, [scripted, scripted, GOOD_HTML])
    with pytest.raises(ModelError, match="script"):
        _execute(services)
    # An unknown target element never reaches the model.
    del calls[:]
    with pytest.raises(ValidationError, match="element 不存在"):
        asyncio.run(
            execute_file_interaction_command(
                services,
                project_id=PROJECT_ID,
                target_ref="element:el:ghost",
                arguments={},
                idempotency_key="dag-interaction-x",
            ),
        )
    assert not calls
