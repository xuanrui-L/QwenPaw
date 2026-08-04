# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=redefined-outer-name,unused-argument,protected-access
"""Review loop tests: stubbed VLM pass/revise states, round cap, switch off."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from models.config import is_self_review_enabled
from schemas.render_review import (
    AudioProfile,
    RenderReviewReport,
    ReviewDimension,
    ReviewFinding,
    ReviewFrame,
)
from services.media_files import local_execution
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    ArtifactSlot,
    ArtifactVersion,
    IndexedFile,
    Project,
)
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


def _revise_report(video_id: str, round_number: int) -> RenderReviewReport:
    findings = [
        ReviewFinding(
            dimension=dimension,
            passed=dimension is not ReviewDimension.ENGINEERING,
            severity=(
                "major"
                if dimension is ReviewDimension.ENGINEERING
                else "minor"
            ),
            evidence_timestamp_ms=(
                1000 if dimension is ReviewDimension.ENGINEERING else None
            ),
            suggestion=(
                "移除黑帧" if dimension is ReviewDimension.ENGINEERING else ""
            ),
        )
        for dimension in ReviewDimension
    ]
    return RenderReviewReport(
        video_ref=f"artifact-version:{video_id}",
        round=round_number,
        findings=findings,
        verdict="revise",
    )


def test_superseding_claim_drops_stale_feedback(services) -> None:
    """An in-flight round for an older video must not mutate the timeline."""
    reports_root = review_module._reports_root(services, PROJECT_ID)
    admitted_a = review_module._admit_round(
        reports_root,
        target_ref=TARGET_REF,
        video_id="video-old",
    )
    assert admitted_a == (1, admitted_a[1])
    # A newer composition supersedes the in-flight claim atomically.
    admitted_b = review_module._admit_round(
        reports_root,
        target_ref=TARGET_REF,
        video_id="video-new",
    )
    assert admitted_b is not None
    outcome_a, feedback_a = review_module._finalize_round(
        services,
        reports_root,
        project_id=PROJECT_ID,
        target_ref=TARGET_REF,
        chain_id=admitted_a[1],
        round_number=admitted_a[0],
        video_id="video-old",
        slot_id=None,
        report=_revise_report("video-old", admitted_a[0]),
    )
    assert outcome_a == "superseded"
    assert feedback_a is False
    assert _feedback_messages(services) == []
    outcome_b, feedback_b = review_module._finalize_round(
        services,
        reports_root,
        project_id=PROJECT_ID,
        target_ref=TARGET_REF,
        chain_id=admitted_b[1],
        round_number=admitted_b[0],
        video_id="video-new",
        slot_id=None,
        report=_revise_report("video-new", admitted_b[0]),
    )
    assert outcome_b == "completed"
    assert feedback_b is True
    chain = json.loads(
        (review_module._chain_path(reports_root, TARGET_REF)).read_text(
            encoding="utf-8",
        ),
    )
    # The superseded round consumed no chain budget.
    assert chain["rounds_completed"] == 1
    assert chain["last_video_id"] == "video-new"
    assert "video-old" in chain["reviewed_video_ids"]


def test_unselected_artifact_never_receives_feedback(
    tmp_path,
    monkeypatch,
) -> None:
    """Feedback is dropped when the video is no longer the selected render."""
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    services = CreatorFileServices.create(tmp_path.resolve())
    project = Project.new(project_id=PROJECT_ID, name="Render Review")
    created_at = project.created_at
    for version_id in ("video-a", "video-b"):
        file_id = f"file-{version_id}"
        project.assets.files_by_id[file_id] = IndexedFile(
            file_id=file_id,
            kind="artifact_payload",
            relative_uri=f"assets/artifacts/{file_id}.mp4",
            sha256="0" * 64,
            size_bytes=4,
            media_type="video/mp4",
            created_at=created_at,
        )
        project.assets.artifact_versions_by_id[version_id] = ArtifactVersion(
            version_id=version_id,
            slot_id="slot:render",
            kind="timeline_render",
            owner_ref=TARGET_REF,
            name="final",
            file_id=file_id,
            checksum="0" * 64,
            based_on_generation=0,
            created_at=created_at,
        )
    project.assets.artifact_slots_by_id["slot:render"] = ArtifactSlot(
        slot_id="slot:render",
        kind="timeline_render",
        owner_ref=TARGET_REF,
        version_ids=["video-a", "video-b"],
        selected_version_id="video-b",
    )
    services.projects.create(project)
    services.sessions.create_project_runtime(PROJECT_ID)
    reports_root = review_module._reports_root(services, PROJECT_ID)
    admitted = review_module._admit_round(
        reports_root,
        target_ref=TARGET_REF,
        video_id="video-a",
    )
    assert admitted is not None
    outcome, feedback_sent = review_module._finalize_round(
        services,
        reports_root,
        project_id=PROJECT_ID,
        target_ref=TARGET_REF,
        chain_id=admitted[1],
        round_number=admitted[0],
        video_id="video-a",
        slot_id="slot:render",
        report=_revise_report("video-a", admitted[0]),
    )
    assert outcome == "stale"
    assert feedback_sent is False
    assert _feedback_messages(services) == []


def test_failed_round_releases_claim_for_retry(
    services,
    stubbed_evidence,
    monkeypatch,
) -> None:
    _stub_vlm(monkeypatch, ["不是 JSON", "还不是 JSON"])
    assert _run_round(services, "video-retry-1") is None
    reports_root = review_module._reports_root(services, PROJECT_ID)
    chain = json.loads(
        review_module._chain_path(reports_root, TARGET_REF).read_text(
            encoding="utf-8",
        ),
    )
    assert not chain.get("claim")
    # The same video can be rescheduled after the failure.
    _stub_vlm(monkeypatch, [_vlm_response(verdict_major_failure=False)])
    report = _run_round(services, "video-retry-1")
    assert report is not None and report.verdict == "pass"


def test_schedule_gate_and_dedup(services, monkeypatch) -> None:
    """The single scheduling point filters switch, command and shape."""
    calls: list[str] = []

    async def fake_loop(*args, **kwargs):
        calls.append(kwargs["video_id"])
        return None

    monkeypatch.setattr(review_module, "run_review_loop", fake_loop)
    result = {
        "commandType": "COMPOSE_FINAL_VIDEO",
        "targetRef": TARGET_REF,
        "indexedFile": {"relative_uri": "assets/artifacts/f.mp4"},
        "artifactVersion": {
            "version_id": "video-gate-1",
            "slot_id": "slot:render",
        },
    }

    async def drive() -> None:
        monkeypatch.delenv("CREATOR_SELF_REVIEW_ENABLED", raising=False)
        review_module.schedule_render_review(
            services,
            project_id=PROJECT_ID,
            published_result=result,
        )
        monkeypatch.setenv("CREATOR_SELF_REVIEW_ENABLED", "1")
        review_module.schedule_render_review(
            services,
            project_id=PROJECT_ID,
            published_result={**result, "commandType": "EXECUTE_EDIT"},
        )
        review_module.schedule_render_review(
            services,
            project_id=PROJECT_ID,
            published_result=result,
        )
        await asyncio.sleep(0)

    asyncio.run(drive())
    assert calls == ["video-gate-1"]


def test_result_from_task_routes_every_success_through_review(
    services,
    monkeypatch,
) -> None:
    """Replay/recovery convergences share the same scheduling point."""
    scheduled: list[str] = []

    def fake_schedule(_services, *, project_id, published_result):
        del project_id
        scheduled.append(published_result["artifactVersion"]["version_id"])

    monkeypatch.setattr(
        local_execution,
        "schedule_render_review",
        fake_schedule,
    )
    service = object.__new__(local_execution.FileLocalMediaExecutionService)
    service.services = services
    task = SimpleNamespace(
        task_id="task-1",
        run_id="run-1",
        project_id=PROJECT_ID,
        result={
            "commandType": "COMPOSE_FINAL_VIDEO",
            "targetRef": TARGET_REF,
            "transactionId": "txn-1",
            "projectEtag": "etag-1",
            "projectGeneration": 3,
            "indexedFile": {"relative_uri": "assets/artifacts/f.mp4"},
            "artifactVersion": {
                "version_id": "artifact-version-replay",
                "slot_id": "slot:render",
                "kind": "timeline_render",
                "owner_ref": TARGET_REF,
                "name": "final",
                "file_id": "file-1",
                "checksum": "0" * 64,
                "based_on_generation": 1,
                "created_at": "2026-08-03T00:00:00Z",
            },
        },
    )
    outcome = service._result_from_task(task, replayed=True)
    assert outcome.artifact_version_id == "artifact-version-replay"
    assert outcome.replayed is True
    assert scheduled == ["artifact-version-replay"]
