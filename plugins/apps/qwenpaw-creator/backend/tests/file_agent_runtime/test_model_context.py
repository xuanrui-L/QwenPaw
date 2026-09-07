# -*- coding: utf-8 -*-
from copy import deepcopy
import json

import pytest

from services.file_agent_runtime.driver import _compact_wire_project_snapshots
from services.file_agent_runtime.model_context import (
    MODEL_INPUT_BYTES,
    ModelContextBudgetError,
    compact_conversation_history,
    prepare_model_messages,
)
from services.project_files.model_view import (
    PROJECT_VIEW_BYTES,
    json_bytes,
    json_text,
    project_snapshot_view,
)

pytestmark = pytest.mark.unit


def _large_snapshot():
    timeline = {
        "name": "主时间线",
        "elements_by_id": {
            f"shot:{i}": {
                "element_id": f"shot:{i}",
                "creation": {
                    "storyboard_prompt": "画面动作衔接和声音" * 1800,
                    "video_prompt": "镜头推进并保持角色连续" * 1400,
                },
            }
            for i in range(5)
        },
    }
    items = {"timeline:main": timeline}
    items.update({f"snapshot:{i}": deepcopy(timeline) for i in range(5)})
    return {
        "generation": 52,
        "etag": "etag-52",
        "project": {
            "project_id": "project-context",
            "generation": 52,
            "name": "大项目",
            "timelines": {"items": items, "order": list(items)},
        },
    }


def test_latest_snapshot_bounds_history_without_mutation():
    payload = _large_snapshot()
    original = deepcopy(payload)
    view = project_snapshot_view(payload)
    assert json_bytes(payload) > 1_000_000
    assert json_bytes(view) <= PROJECT_VIEW_BYTES
    assert view["projectView"]["historyTimelineCount"] == 5
    assert view["project"]["timelines"]["order"] == ["timeline:main"]
    assert view["projectView"]["omittedPointers"]
    assert payload == original
    assert project_snapshot_view(view) == view


def test_wire_compaction_bounds_latest_multipart_echo():
    payload = _large_snapshot()
    messages = [
        {
            "role": "tool",
            "name": "read_project",
            "tool_call_id": "old",
            "content": json_text(payload),
        },
        {
            "role": "tool",
            "name": "jq_project",
            "tool_call_id": "new",
            "content": [
                {
                    "type": "text",
                    "text": json_text(payload),
                    "marker": "retain",
                },
            ],
        },
    ]
    _compact_wire_project_snapshots(messages)
    assert json.loads(messages[0]["content"])["supersededProjectSnapshot"]
    assert json_bytes(messages[1]) < PROJECT_VIEW_BYTES + 500
    assert messages[1]["content"][0]["marker"] == "retain"
    first = deepcopy(messages)
    _compact_wire_project_snapshots(messages)
    assert messages == first


def _exchange(call_id, size):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "jq_project",
                        "arguments": json_text(
                            {"program": ".", "jsonArgs": {"text": "中" * size}},
                        ),
                    },
                },
            ],
        },
        {
            "role": "tool",
            "name": "jq_project",
            "tool_call_id": call_id,
            "content": '{"ok":true}',
        },
    ]


def test_budget_retains_user_goals_and_whole_tool_exchanges():
    messages = [
        {"role": "system", "content": "schema and authority"},
        {"role": "user", "content": "只生成一集，保留对白，预算不变"},
    ]
    for index in range(16):
        messages.extend(_exchange(str(index), 30_000))
    messages.append({"role": "user", "content": "只调整第四个镜头"})
    before = deepcopy(messages)
    prepared = prepare_model_messages(messages, [])
    assert json_bytes({"messages": prepared, "tools": []}) <= MODEL_INPUT_BYTES
    assert [row for row in prepared if row["role"] in {"system", "user"}] == [
        row for row in before if row["role"] in {"system", "user"}
    ]
    calls = {
        call["id"] for row in prepared for call in row.get("tool_calls", [])
    }
    replies = {
        row["tool_call_id"] for row in prepared if row["role"] == "tool"
    }
    assert calls == replies
    assert messages == before


def test_oversized_required_input_fails_locally():
    with pytest.raises(ModelContextBudgetError):
        prepare_model_messages(
            [{"role": "user", "content": "必需内容" * 10000}],
            [],
            max_bytes=1024,
        )


def test_resumed_history_drops_raw_diagnostics_and_keeps_user_constraints():
    history = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "预算和对白必须保留"}],
        },
    ]
    for index in range(95):
        history.append(
            {
                "role": "tool",
                "messageSeq": index,
                "content": [{"type": "text", "text": "旧回执" * 2000}],
                "metadata": {
                    "toolName": "jq_project",
                    "rawArguments": "x" * 50000,
                },
            },
        )
    result = compact_conversation_history(history)
    assert json_bytes(result) <= 128 * 1024
    assert "预算和对白必须保留" in json_text(result)
    assert "rawArguments" not in json_text(result)
    assert len(history) == 96


def test_removed_multipart_exchange_carries_latest_project_view():
    messages = [{"role": "system", "content": "Keep user constraints"}]
    exchange = _exchange("huge", 50_000)
    exchange[1]["content"] = [
        {"type": "text", "text": json_text(_large_snapshot())},
    ]
    messages.extend(exchange)
    messages.append({"role": "user", "content": "修复第四个镜头"})
    prepared = prepare_model_messages(messages, [], max_bytes=80 * 1024)
    assert not any(row.get("tool_calls") for row in prepared)
    assert any(
        "CURRENT_PROJECT_MODEL_VIEW=" in row.get("content", "")
        for row in prepared
    )
    assert "project-context" in json_text(prepared)
    assert "修复第四个镜头" in json_text(prepared)
    assert json_bytes({"messages": prepared, "tools": []}) <= 80 * 1024


def test_oversized_history_index_does_not_escape_project_budget():
    payload = _large_snapshot()
    for key, timeline in payload["project"]["timelines"]["items"].items():
        if key.startswith("snapshot:"):
            timeline["name"] = "很长的历史名称" * 10000
    view = project_snapshot_view(payload)
    assert json_bytes(view) <= PROJECT_VIEW_BYTES
    assert view["projectView"]["historyTimelineCount"] == 5
    assert "" in view["projectView"]["omittedPointers"]


def test_live_project_is_preserved_when_only_frozen_history_is_large():
    live = {
        "name": "live",
        "elements_by_id": {"e1": {"prompt": "镜头" * 5000}},
    }
    project = {f"metadata_{i}": "descriptive field" for i in range(12)}
    project.update(
        {
            "project_id": "p1",
            "generation": 52,
            "timelines": {
                "order": ["timeline:main", "snapshot:1"],
                "items": {
                    "timeline:main": live,
                    "snapshot:1": deepcopy(live),
                },
            },
            "assets": {
                "artifact_slots_by_id": {
                    "slot1": {"selected_version_id": "v1"},
                },
            },
        },
    )
    payload = {"etag": "base-52", "generation": 52, "project": project}
    view = project_snapshot_view(payload)
    assert view["project"]["timelines"]["items"]["timeline:main"] == live
    assert view["project"]["assets"] == project["assets"]
    assert json_bytes(view) <= PROJECT_VIEW_BYTES
    assert view["projectView"]["omittedPointers"] == []


def test_large_project_keeps_root_working_fields_discoverable():
    payload = _large_snapshot()
    live = payload["project"].pop("timelines")
    payload["project"].update({f"metadata_{i}": "x" for i in range(12)})
    payload["project"]["timelines"] = live
    view = project_snapshot_view(payload)
    assert "timelines" in view["project"]
    assert "timeline:main" in view["project"]["timelines"]["items"]
    assert json_bytes(view) <= PROJECT_VIEW_BYTES
