# -*- coding: utf-8 -*-
"""A completed tool request is not proof that media was submitted."""

from __future__ import annotations

import pytest

from services.file_agent_runtime.workgraph_execution import (
    summarize_workgraph_results,
)

pytestmark = pytest.mark.unit


def test_review_blocked_request_explicitly_has_no_new_task_or_queue():
    result = summarize_workgraph_results(
        [
            {
                "nodeId": "storyboard:shot1_search",
                "status": "BLOCKED",
                "reason": "WAITING_REVIEW",
            },
            {
                "nodeId": "storyboard:shot2_discovery",
                "status": "BLOCKED",
                "reason": "WAITING_REVIEW",
            },
        ],
    )
    assert "尚未启动制作" in result
    assert "2 项需要先完成现有审阅" in result
    assert "未创建制作任务，也未加入等待队列" in result
    assert "条件满足后需要重新提出制作请求" in result
    assert "已提交" not in result
    assert "storyboard" not in result


def test_mixed_result_counts_only_actual_tasks_results_and_waits():
    result = summarize_workgraph_results(
        [
            {"status": "SUCCEEDED", "taskId": "task-new"},
            {"status": "SUCCEEDED", "taskId": "task-old", "replayed": True},
            {"status": "BLOCKED", "reason": "WAITING_REVIEW"},
            {"status": "BLOCKED", "reason": "GATED"},
            {"status": "FAILED", "taskId": "task-failed"},
            {"status": "BLOCKED", "reason": "EXECUTION_NOT_COMPLETED"},
        ],
    )
    for expected in [
        "返回 3 个制作任务",
        "已完成制作 1 项",
        "复用已有成果 1 项",
        "1 项等待现有审阅，尚未开始",
        "1 项制作条件尚未满足，尚未开始",
        "1 项未能完成",
        "1 项执行结果尚未确认",
    ]:
        assert expected in result
    assert "未创建制作任务" not in result
    assert "task-" not in result
    assert "EXECUTION_NOT_COMPLETED" not in result


@pytest.mark.parametrize(
    "reason",
    ["EXECUTION_NOT_COMPLETED", "RUNNING", "UNKNOWN"],
)
def test_missing_task_id_never_proves_admission_did_not_happen(reason):
    result = summarize_workgraph_results(
        [{"status": "BLOCKED", "reason": reason}],
    )
    assert "执行结果尚未确认" in result
    assert "未创建" not in result
    assert "尚未开始" not in result
    assert "未加入等待队列" not in result


def test_existing_result_reuse_does_not_invent_a_new_task():
    result = summarize_workgraph_results(
        [{"status": "SUCCEEDED", "replayed": True}],
    )
    assert result == "复用已有成果 1 项。"


def test_task_bearing_blocked_item_cannot_be_reported_as_unstarted():
    result = summarize_workgraph_results(
        [
            {
                "status": "BLOCKED",
                "reason": "WAITING_REVIEW",
                "taskId": "task-real",
            },
        ],
    )
    assert "返回 1 个制作任务" in result
    assert "执行结果尚未确认" in result
    assert "尚未开始" not in result


@pytest.mark.parametrize(
    "result, expected",
    [
        (
            {
                "status": "BLOCKED",
                "items": [{"status": "BLOCKED", "reason": "WAITING_REVIEW"}],
            },
            True,
        ),
        ({"status": "BLOCKED", "items": []}, False),
        (
            {
                "status": "PARTIAL",
                "items": [
                    {"status": "SUCCEEDED", "taskId": "real"},
                    {"status": "BLOCKED", "reason": "WAITING_REVIEW"},
                ],
            },
            False,
        ),
        (
            {
                "status": "BLOCKED",
                "items": [
                    {
                        "status": "BLOCKED",
                        "reason": "WAITING_REVIEW",
                        "taskId": "real",
                    },
                ],
            },
            False,
        ),
        (
            {
                "status": "BLOCKED",
                "items": [
                    {"status": "BLOCKED", "reason": "EXECUTION_NOT_COMPLETED"},
                ],
            },
            False,
        ),
    ],
)
def test_only_wholly_unstarted_review_block_can_end_model_loop(
    result,
    expected,
):
    from services.file_agent_runtime.workgraph_execution import (
        workgraph_waits_only_for_review,
    )

    assert workgraph_waits_only_for_review(result) is expected
