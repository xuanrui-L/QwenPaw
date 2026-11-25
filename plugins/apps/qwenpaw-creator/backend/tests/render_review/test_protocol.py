# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Render review evidence, findings, feedback, and challenge protocol."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from schemas.render_review import (
    AudioProfile,
    ChallengeFinding,
    RenderReviewReport,
    ReviewDimension,
    ReviewFinding,
    ReviewFrame,
)
from services.render_review.challenge import (
    clear_question_cache,
    filter_questions,
    generate_challenge_questions,
    judge_challenges,
    parse_challenge_verdicts,
)
from services.render_review.protocol import (
    MAX_REVIEW_ROUNDS,
    build_review_user_text,
    findings_feedback_payload,
    parse_review_report,
)
from services.render_review.review import derive_plan_context

pytestmark = pytest.mark.unit


def _findings_payload(**overrides) -> dict:
    findings = []
    for dimension in ReviewDimension:
        entry = {
            "dimension": dimension.value,
            "passed": True,
            "severity": "minor",
            "evidence_timestamp_ms": None,
            "suggestion": "",
        }
        entry.update(overrides.get(dimension.value, {}))
        findings.append(entry)
    return {"findings": findings, "verdict": "pass"}


def _parse(payload: dict, *, round_number: int = 1) -> RenderReviewReport:
    return parse_review_report(
        json.dumps(payload, ensure_ascii=False),
        video_ref="artifact-version:v1",
        round_number=round_number,
    )


def test_parse_review_report_pass_roundtrip_accepts_fenced_json() -> None:
    payload = _findings_payload()
    text = "审阅完成：\n```json\n" + json.dumps(payload) + "\n```"
    report = parse_review_report(
        text,
        video_ref="artifact-version:v1",
        round_number=1,
    )
    assert report.verdict == "pass"
    assert len(report.findings) == len(ReviewDimension)
    assert report.failed_findings() == []
    restored = RenderReviewReport.model_validate(report.model_dump())
    assert restored == report


def test_parse_review_report_normalizes_weak_failures_to_pass() -> None:
    payload = _findings_payload(
        pacing={
            "passed": False,
            "severity": "minor",
            "evidence_timestamp_ms": 4000,
        },
    )
    payload["verdict"] = "revise"
    assert _parse(payload).verdict == "pass"
    # A major failure without frame evidence is discarded (fail closed).
    payload = _findings_payload(
        visual_quality={"passed": False, "severity": "major"},
    )
    report = _parse(payload)
    assert report.verdict == "pass"
    assert report.failed_findings() == []


def test_parse_review_report_rejects_missing_dimensions() -> None:
    payload = _findings_payload()
    payload["findings"] = payload["findings"][:-1]
    with pytest.raises(ValueError, match="missing dimensions"):
        _parse(payload)


def test_findings_feedback_payload_contains_only_failures() -> None:
    payload = _findings_payload(
        sound={
            "passed": False,
            "severity": "major",
            "evidence_timestamp_ms": 2000,
            "suggestion": "补齐 2s 起缺失的配音轨",
        },
    )
    feedback = findings_feedback_payload(_parse(payload, round_number=2))
    assert feedback["type"] == "render_review_feedback"
    assert feedback["round"] == 2
    assert feedback["max_rounds"] == MAX_REVIEW_ROUNDS
    assert [item["dimension"] for item in feedback["findings"]] == ["sound"]


def test_build_review_user_text_lists_all_evidence() -> None:
    frames = [ReviewFrame(timestamp_ms=0, image_path="/tmp/f0.jpg")]
    profile = AudioProfile(
        has_audio=True,
        integrated_lufs=-19.2,
    )
    text = build_review_user_text(
        frames=frames,
        audio_profile=profile,
        video_duration_seconds=5.0,
        plan_context={
            "target_duration_seconds": 5,
            "edit_plan": {"concept": "猫的越狱日记"},
        },
    )
    assert "t=0ms" in text
    for dimension in ReviewDimension:
        assert dimension.value in text
    # The edit-plan contract ships verbatim, exactly once.
    assert "【剪辑契约" in text
    assert text.count("猫的越狱日记") == 1


def test_build_review_user_text_scopes_live_tutorial_rubric() -> None:
    profile = AudioProfile(has_audio=False)
    ordinary = build_review_user_text(
        frames=[],
        audio_profile=profile,
        video_duration_seconds=3.0,
        plan_context={},
    )
    tutorial = build_review_user_text(
        frames=[],
        audio_profile=profile,
        video_duration_seconds=3.0,
        plan_context={
            "live_operation_tutorial": True,
            "live_operation_take_count": 2,
        },
    )
    assert "【真实操作教程专项验收】" not in ordinary
    assert "【真实操作教程专项验收】" in tutorial


def test_plan_context_detects_live_tutorial_and_any_text_caption() -> None:
    live_version = SimpleNamespace(
        metadata={"sourceKind": "live_operation_take"},
    )
    edit = SimpleNamespace(
        enabled=True,
        creation=SimpleNamespace(type="edit"),
        render_source=SimpleNamespace(version_id="asset-version-live"),
    )
    caption = SimpleNamespace(
        enabled=True,
        creation=SimpleNamespace(type="overlay", text="点击搜索框"),
        render_source=None,
    )
    timeline = SimpleNamespace(
        edit_plan=None,
        elements_by_id={"edit-1": edit, "caption-1": caption},
    )
    project = SimpleNamespace(
        settings=None,
        description="GitHub 使用教程",
        timelines=SimpleNamespace(items={"timeline:main": timeline}),
        assets=SimpleNamespace(
            source_versions_by_id={"asset-version-live": live_version},
        ),
    )

    context = derive_plan_context(project, "timeline:main")

    assert context["expects_subtitles"] is True
    assert context["live_operation_tutorial"] is True
    assert context["live_operation_take_count"] == 1


def test_dimensions_match_the_vendored_appeal_rubric() -> None:
    from vendor.media_toolkit.review_rubrics import APPEAL_RUBRIC_ROWS

    rubric_keys = [row.key for row in APPEAL_RUBRIC_ROWS]
    dimension_keys = [item.value for item in ReviewDimension]
    assert dimension_keys == rubric_keys + ["engineering"]


def test_concept_score_threshold_gates_the_verdict() -> None:
    # The concept row is score-driven: a failing concept needs no frame
    # timestamp (other timestamp-less rows get normalized back to pass).
    report = _parse(_findings_payload(concept={"passed": True, "score": 5}))
    assert report.verdict == "revise"
    concept = next(
        f for f in report.findings if f.dimension is ReviewDimension.CONCEPT
    )
    assert not concept.passed
    assert concept.severity == "major"
    passing = _parse(_findings_payload(concept={"passed": True, "score": 8}))
    assert passing.verdict == "pass"


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


def test_filter_questions_requires_anchor_inside_question() -> None:
    accepted = filter_questions(
        [
            {
                "question": "请确认画面后半段没有突然变成蓝色",
                "anchor": "红色灯笼",
                "severity": "major",
            },
        ],
        plan_context=_PLAN,
    )
    assert not accepted


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


@pytest.mark.parametrize("missing_field", ["reason", "suggestion"])
def test_confirmed_challenge_requires_actionable_evidence(
    missing_field: str,
) -> None:
    verdict = {
        "question_id": "cq1",
        "verdict": "CT",
        "evidence_timestamp_ms": 2000,
        "reason": "灯笼在 2s 变蓝",
        "suggestion": "统一灯笼颜色",
    }
    verdict[missing_field] = ""
    findings = parse_challenge_verdicts(
        json.dumps({"verdicts": [verdict]}, ensure_ascii=False),
        questions=[
            {
                "question_id": "cq1",
                "question": "请确认灯笼没有变色",
                "anchor": "灯笼",
                "severity": "major",
            },
        ],
        valid_timestamps=[0, 1000, 2000],
    )
    assert findings[0].verdict == "ET"
    assert findings[0].suggestion == ""


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


def test_question_generation_is_cached_by_plan_fingerprint(
    monkeypatch,
) -> None:
    clear_question_cache()
    calls = 0

    async def fake_completion(_prompt, **_kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(
            {
                "questions": [
                    {
                        "question": "请确认红色灯笼没有在后半段变蓝",
                        "anchor": "红色灯笼",
                        "severity": "major",
                    },
                ],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        "models.text_model.chat_completion",
        fake_completion,
    )

    async def scenario() -> None:
        first = await generate_challenge_questions(_PLAN)
        second = await generate_challenge_questions(dict(_PLAN))
        changed = await generate_challenge_questions(
            {**_PLAN, "project_brief": "老人提着红色灯笼走过雪地"},
        )
        assert first == second
        assert changed

    asyncio.run(scenario())
    assert calls == 2
    clear_question_cache()


def test_challenge_hypotheses_share_one_batched_vlm_call(
    monkeypatch,
    tmp_path,
) -> None:
    calls = 0
    observed_prompt = ""

    async def fake_completion(content, **_kwargs):
        nonlocal calls, observed_prompt
        calls += 1
        observed_prompt = content[0]["text"]
        return json.dumps(
            {
                "verdicts": [
                    {
                        "question_id": f"cq{index}",
                        "verdict": "ET",
                        "evidence_timestamp_ms": 0,
                        "reason": "未发现该缺陷",
                        "suggestion": "",
                    }
                    for index in range(1, 4)
                ],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        "models.vlm_model.chat_completion",
        fake_completion,
    )
    monkeypatch.setattr(
        "models.vlm_model.multimodal_media_part",
        lambda uri, media_type: {
            "type": "image_url",
            "image_url": {"url": uri},
            "media_type": media_type,
        },
    )
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    questions = [
        {
            "question_id": f"cq{index}",
            "question": f"请确认红色灯笼没有出现缺陷 {index}",
            "anchor": "红色灯笼",
            "severity": "major",
        }
        for index in range(1, 4)
    ]
    findings = asyncio.run(
        judge_challenges(
            questions,
            frames=[ReviewFrame(timestamp_ms=0, image_path=str(frame))],
        ),
    )
    assert [item.question_id for item in findings] == ["cq1", "cq2", "cq3"]
    assert calls == 1
    assert all(f"cq{index}" in observed_prompt for index in range(1, 4))


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
