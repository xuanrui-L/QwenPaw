# -*- coding: utf-8 -*-
"""Near-miss challenge pass: filters, verdicts and payload ordering."""

from __future__ import annotations

import json

import pytest

from schemas.render_review import (
    ChallengeFinding,
    RenderReviewReport,
    ReviewDimension,
    ReviewFinding,
    ReviewFrame,
)
from services.render_review.challenge import (
    filter_questions,
    parse_challenge_verdicts,
)
from services.render_review.protocol import findings_feedback_payload

pytestmark = pytest.mark.unit

_PLAN = {
    "project_brief": "老人提着红色灯笼走过庭院",
    "edit_plan": {"concept": "灯笼作为贯穿符号"},
}


def test_filter_questions_drops_straw_men_and_anchorless() -> None:
    accepted = filter_questions(
        [
            # Valid: anchor exists in the plan, near-miss polarity.
            {
                "question": "请确认红色灯笼没有在后半段变成蓝色",
                "anchor": "红色灯笼",
                "severity": "major",
            },
            # Straw-man: the plan never mentioned music.
            {
                "question": "请确认背景音乐没有跑调",
                "anchor": "背景音乐",
                "severity": "minor",
            },
            # Anchor-less: dropped.
            {"question": "请确认画面没有问题", "anchor": ""},
            # Open-ended (no near-miss polarity): dropped.
            {"question": "整体质量如何", "anchor": "灯笼"},
        ],
        plan_context=_PLAN,
    )
    assert len(accepted) == 1
    assert accepted[0]["anchor"] == "红色灯笼"


def test_filter_questions_caps_at_six() -> None:
    many = [
        {
            "question": f"请确认灯笼没有出现第{index}类缺陷特征表现",
            "anchor": "灯笼",
            "severity": "minor",
        }
        for index in range(10)
    ]
    assert len(filter_questions(many, plan_context=_PLAN)) == 6


def test_parse_challenge_verdicts_anti_hallucination() -> None:
    questions = [
        {
            "question_id": "cq1",
            "question": "请确认灯笼没有变色",
            "anchor": "灯笼",
            "severity": "major",
        },
        {
            "question_id": "cq2",
            "question": "请确认结尾没有截断",
            "anchor": "灯笼",
            "severity": "major",
        },
        {
            "question_id": "cq3",
            "question": "请确认没有水印",
            "anchor": "灯笼",
            "severity": "minor",
        },
    ]
    response = json.dumps(
        {
            "verdicts": [
                # Valid CT (timestamp within evidence set).
                {
                    "question_id": "cq1",
                    "verdict": "CT",
                    "evidence_timestamp_ms": 2000,
                    "reason": "灯笼在 2s 变蓝",
                    "suggestion": "统一灯笼颜色",
                },
                # CT citing an unseen frame -> demoted to ET.
                {
                    "question_id": "cq2",
                    "verdict": "CT",
                    "evidence_timestamp_ms": 88000,
                    "reason": "截断",
                    "suggestion": "补收尾",
                },
                # NA without a reason -> demoted to ET.
                {"question_id": "cq3", "verdict": "NA"},
            ],
        },
        ensure_ascii=False,
    )
    findings = parse_challenge_verdicts(
        response,
        questions=questions,
        valid_timestamps=[0, 1000, 2000, 3000],
    )
    verdicts = {item.question_id: item.verdict for item in findings}
    assert verdicts == {"cq1": "CT", "cq2": "ET", "cq3": "ET"}


def _report(**overrides) -> RenderReviewReport:
    base = {
        "video_ref": "artifact-version:v1",
        "round": 1,
        "findings": [],
        "verdict": "revise",
    }
    base.update(overrides)
    return RenderReviewReport.model_validate(base)


def test_feedback_payload_orders_major_first_and_carries_challenges() -> None:
    report = _report(
        findings=[
            ReviewFinding(
                dimension=ReviewDimension.RHYTHM,
                passed=False,
                severity="minor",
                evidence_timestamp_ms=1000,
                suggestion="节奏略平",
            ),
            ReviewFinding(
                dimension=ReviewDimension.ENGINEERING,
                passed=False,
                severity="major",
                evidence_timestamp_ms=2000,
                suggestion="移除中段黑帧",
            ),
        ],
        challenge_findings=[
            ChallengeFinding(
                question_id="cq1",
                question="请确认灯笼没有变色",
                verdict="CT",
                severity="major",
                evidence_timestamp_ms=2000,
                reason="灯笼变蓝",
                suggestion="统一颜色",
            ),
            ChallengeFinding(
                question_id="cq2",
                question="请确认没有水印",
                verdict="ET",
                severity="minor",
            ),
        ],
    )
    payload = findings_feedback_payload(report)
    # Severity-weighted ordering: the major finding is delivered first.
    assert payload["findings"][0]["dimension"] == "engineering"
    assert payload["findings"][1]["dimension"] == "rhythm"
    # Only confirmed (CT) challenges ride along, as reasoning entries.
    challenge_ids = [
        item["question_id"] for item in payload["challenge_findings"]
    ]
    assert challenge_ids == ["cq1"]
    # No numeric score anywhere in the delivered payload.
    assert "score" not in json.dumps(payload["challenge_findings"])


def test_report_schema_stays_backward_compatible() -> None:
    legacy = {
        "video_ref": "artifact-version:v0",
        "round": 1,
        "findings": [],
        "verdict": "pass",
    }
    report = RenderReviewReport.model_validate(legacy)
    assert report.challenge_findings == []
    assert report.objective_facts is None


def test_build_review_user_text_injects_objective_facts() -> None:
    from schemas.render_review import AudioProfile
    from services.render_review.protocol import build_review_user_text

    text = build_review_user_text(
        frames=[ReviewFrame(timestamp_ms=0, image_path="/tmp/f0.jpg")],
        audio_profile=AudioProfile(has_audio=True),
        video_duration_seconds=10.0,
        plan_context={"timeline_ref": "timeline:main"},
        objective_facts={"video_index": {"cut_count": 2}},
    )
    assert "客观事实提示" in text
    assert "cut_count" in text
    # The facts block must land before the eight-row checklist.
    assert text.index("客观事实提示") < text.index("八行检查要点")
