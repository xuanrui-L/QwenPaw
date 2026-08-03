# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Manual eval-set regression for the six-dimension review prompt.

Runs the *real* VLM against the curated fixture cases under
``tests/fixtures/render_review/cases`` and grades the report against the
human-annotated ``expected.json``. Prompt iteration bar: zero missed defects
across the set and at most one false alarm per case.

Manual invocation (never runs in CI):

    RENDER_REVIEW_EVAL=1 CREATOR_DATA_ROOT=... QWENPAW_KEYRING_ACCOUNT=... \
        pytest tests/render_review/test_eval_set.py -m manual -s
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project
from services.render_review.review import review_render

pytestmark = [
    pytest.mark.manual,
    pytest.mark.manual_real,
    pytest.mark.skipif(
        os.environ.get("RENDER_REVIEW_EVAL") != "1",
        reason=(
            "manual real-VLM eval; set RENDER_REVIEW_EVAL=1 with valid "
            "creator_vlm_model credentials to run"
        ),
    ),
]

CASES_DIR = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "render_review"
    / "cases"
)
PROJECT_ID = "project-render-review-eval"
MAX_FALSE_ALARMS_PER_CASE = 1


def _load_cases() -> list[tuple[str, Path, dict]]:
    cases = []
    for case_dir in sorted(CASES_DIR.iterdir()):
        expected_path = case_dir / "expected.json"
        video_path = case_dir / "video.mp4"
        if not expected_path.is_file() or not video_path.is_file():
            continue
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        cases.append((case_dir.name, video_path, expected))
    return cases


def _grade(report, expected: dict) -> dict:
    expected_failures = set(expected.get("expected_failures") or [])
    acceptable_extra = set(expected.get("acceptable_extra_failures") or [])
    flagged = {
        item.dimension.value for item in report.findings if not item.passed
    }
    missed = sorted(expected_failures - flagged)
    false_alarms = sorted(flagged - expected_failures - acceptable_extra)
    return {
        "case": expected.get("case"),
        "verdict": report.verdict,
        "flagged": sorted(flagged),
        "missed": missed,
        "false_alarms": false_alarms,
        "findings": [
            {
                "dimension": item.dimension.value,
                "severity": item.severity,
                "evidence_timestamp_ms": item.evidence_timestamp_ms,
                "suggestion": item.suggestion,
            }
            for item in report.findings
            if not item.passed
        ],
    }


def test_eval_set_zero_miss_low_false_alarm(tmp_path: Path) -> None:
    cases = _load_cases()
    assert len(cases) >= 7, "eval set is incomplete"
    os.environ.setdefault("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    services = CreatorFileServices.create(tmp_path.resolve())
    services.projects.create(
        Project.new(project_id=PROJECT_ID, name="Render Review Eval"),
    )

    results = []
    for case_name, video_path, expected in cases:
        report = asyncio.run(
            review_render(
                services,
                project_id=PROJECT_ID,
                video_path=video_path,
                video_id=case_name,
                round_number=1,
                plan_context=expected.get("plan_context") or {},
            ),
        )
        graded = _grade(report, expected)
        results.append(graded)
        print(json.dumps(graded, ensure_ascii=False))

    total_missed = [
        (item["case"], item["missed"]) for item in results if item["missed"]
    ]
    over_alarmed = [
        (item["case"], item["false_alarms"])
        for item in results
        if len(item["false_alarms"]) > MAX_FALSE_ALARMS_PER_CASE
    ]
    summary = {
        "cases": len(results),
        "missed": total_missed,
        "over_alarmed": over_alarmed,
        "false_alarm_total": sum(
            len(item["false_alarms"]) for item in results
        ),
    }
    print("EVAL SUMMARY:", json.dumps(summary, ensure_ascii=False))
    assert not total_missed, f"missed defects: {total_missed}"
    assert not over_alarmed, f"too many false alarms: {over_alarmed}"
