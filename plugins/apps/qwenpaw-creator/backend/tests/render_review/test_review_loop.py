# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=redefined-outer-name,unused-argument
"""Review loop tests: stubbed VLM pass/revise states, round cap, switch off."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from models.config import is_self_review_enabled
from schemas.render_review import (
    AudioProfile,
    ReviewDimension,
    ReviewFrame,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project
from services.render_review import review as review_module
from services.render_review.protocol import MAX_REVIEW_ROUNDS
from services.runtime_files.media_probe import MediaProbe

pytestmark = pytest.mark.unit

PROJECT_ID = "project-render-review"
TARGET_REF = "timeline:main"


def _vlm_response(*, verdict_major_failure: bool) -> str:
    findings = []
    for dimension in ReviewDimension:
        failed = (
            verdict_major_failure and dimension is ReviewDimension.ENGINEERING
        )
        findings.append(
            {
                "dimension": dimension.value,
                "passed": not failed,
                "severity": "major" if failed else "minor",
                "evidence_timestamp_ms": 1000 if failed else None,
                "suggestion": "移除 1s 处黑帧" if failed else "",
            },
        )
    return json.dumps(
        {
            "findings": findings,
            "verdict": "revise" if verdict_major_failure else "pass",
        },
        ensure_ascii=False,
    )


@pytest.fixture()
def services(tmp_path: Path, monkeypatch) -> CreatorFileServices:
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    services = CreatorFileServices.create(tmp_path.resolve())
    project = Project.new(project_id=PROJECT_ID, name="Render Review")
    services.projects.create(project)
    services.sessions.create_project_runtime(PROJECT_ID)
    return services


@pytest.fixture()
def stubbed_evidence(tmp_path: Path, monkeypatch):
    frame_path = tmp_path / "stub-frame.jpg"
    frame_path.write_bytes(b"\xff\xd8\xff\xd9")

    def fake_extract(video_path, *, max_frames=24, output_dir=None):
        del video_path, max_frames, output_dir
        return [
            ReviewFrame(timestamp_ms=0, image_path=str(frame_path)),
            ReviewFrame(timestamp_ms=2000, image_path=str(frame_path)),
        ]

    def fake_audio(video_path):
        del video_path
        return AudioProfile(has_audio=True, integrated_lufs=-19.0)

    def fake_probe(target):
        del target
        return MediaProbe(duration_seconds=2.0, has_audio=True)

    monkeypatch.setattr(review_module, "extract_review_frames", fake_extract)
    monkeypatch.setattr(review_module, "probe_audio_profile", fake_audio)
    monkeypatch.setattr(review_module, "probe_media", fake_probe)


def _stub_vlm(monkeypatch, responses: list[str]) -> list[dict]:
    calls: list[dict] = []

    async def fake_chat_completion(content, **kwargs):
        calls.append({"content": content, "kwargs": kwargs})
        index = min(len(calls), len(responses)) - 1
        return responses[index]

    monkeypatch.setattr(review_module, "chat_completion", fake_chat_completion)
    return calls


def _run_round(services: CreatorFileServices, video_id: str):
    video_path = (
        services.projects.project_root(PROJECT_ID)
        / "assets"
        / "artifacts"
        / f"{video_id}.mp4"
    )
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"stub")
    return asyncio.run(
        review_module.run_review_loop(
            services,
            project_id=PROJECT_ID,
            video_path=video_path,
            video_id=video_id,
            target_ref=TARGET_REF,
        ),
    )


def _feedback_messages(services: CreatorFileServices) -> list:
    session = services.sessions.get_project_session_snapshot(PROJECT_ID)
    messages = services.sessions.list_messages(
        PROJECT_ID,
        session.session_id,
        after_seq=0,
        limit=None,
    )
    return [
        item for item in messages if item.source == "render_review_feedback"
    ]


def test_pass_verdict_closes_chain_without_feedback(
    services,
    stubbed_evidence,
    monkeypatch,
) -> None:
    _stub_vlm(monkeypatch, [_vlm_response(verdict_major_failure=False)])
    report = _run_round(services, "video-pass-1")
    assert report is not None
    assert report.verdict == "pass"
    assert report.round == 1

    review_dir = (
        services.projects.project_root(PROJECT_ID)
        / "runtime"
        / "render-review"
    )
    report_path = review_dir / "video-pass-1" / "round-1.json"
    assert report_path.is_file()
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["verdict"] == "pass"
    assert persisted["round"] == 1

    chain = json.loads(
        (review_dir / "chain-timeline-main.json").read_text(encoding="utf-8"),
    )
    assert chain["status"] == "closed"
    assert _feedback_messages(services) == []


def test_revise_verdict_sends_feedback_and_caps_rounds(
    services,
    stubbed_evidence,
    monkeypatch,
) -> None:
    _stub_vlm(monkeypatch, [_vlm_response(verdict_major_failure=True)])

    for round_number in range(1, MAX_REVIEW_ROUNDS + 1):
        report = _run_round(services, f"video-revise-{round_number}")
        assert report is not None
        assert report.verdict == "revise"
        assert report.round == round_number

    feedback = _feedback_messages(services)
    # Feedback goes out for every revise round except the final one.
    assert len(feedback) == MAX_REVIEW_ROUNDS - 1
    first_text = feedback[0].content_parts[0].text or ""
    assert "render_review_feedback" in first_text
    assert "ai_editing_director" in first_text
    assert TARGET_REF in first_text
    assert feedback[0].metadata["renderReview"]["round"] == 1

    review_dir = (
        services.projects.project_root(PROJECT_ID)
        / "runtime"
        / "render-review"
    )
    chain = json.loads(
        (review_dir / "chain-timeline-main.json").read_text(encoding="utf-8"),
    )
    assert chain["status"] == "closed"
    assert chain["rounds_completed"] == MAX_REVIEW_ROUNDS

    # The chain is spent: a fourth compose starts a fresh chain at round 1.
    report = _run_round(services, "video-revise-4")
    assert report is not None
    assert report.round == 1


def test_duplicate_video_review_is_skipped(
    services,
    stubbed_evidence,
    monkeypatch,
) -> None:
    _stub_vlm(monkeypatch, [_vlm_response(verdict_major_failure=True)])
    first = _run_round(services, "video-dup-1")
    assert first is not None
    duplicate = _run_round(services, "video-dup-1")
    assert duplicate is None
    assert len(_feedback_messages(services)) == 1


def test_unparsable_vlm_response_never_raises(
    services,
    stubbed_evidence,
    monkeypatch,
) -> None:
    calls = _stub_vlm(monkeypatch, ["这不是 JSON", "还是不是 JSON"])
    report = _run_round(services, "video-broken-1")
    assert report is None
    assert len(calls) == 2
    assert _feedback_messages(services) == []


def test_schedule_render_review_tolerates_bad_result(services) -> None:
    # Missing fields must be ignored without raising.
    review_module.schedule_render_review(
        services,
        project_id=PROJECT_ID,
        published_result={"commandType": "COMPOSE_FINAL_VIDEO"},
    )


def test_self_review_switch_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("CREATOR_SELF_REVIEW_ENABLED", raising=False)
    assert is_self_review_enabled() is False
    monkeypatch.setenv("CREATOR_SELF_REVIEW_ENABLED", "1")
    assert is_self_review_enabled() is True
    monkeypatch.setenv("CREATOR_SELF_REVIEW_ENABLED", "off")
    assert is_self_review_enabled() is False
