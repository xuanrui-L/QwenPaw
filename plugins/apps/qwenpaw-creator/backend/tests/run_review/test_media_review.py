# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,unused-argument
"""Async media review: admission, parsing, scheduling and the image loop."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from schemas.run_review import MediaReviewReport
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    ArtifactSlot,
    ArtifactVersion,
    IndexedFile,
    Project,
)
from services.run_review import admission
from services.run_review import media_review as media_module
from services.run_review.media_review import (
    parse_media_report,
    schedule_media_review,
)

pytestmark = pytest.mark.unit

_FFMPEG = shutil.which("ffmpeg")
requires_ffmpeg = pytest.mark.skipif(
    _FFMPEG is None,
    reason="ffmpeg is required for media review loop tests",
)

PROJECT_ID = "project-media-review"
SLOT_ID = "element:e1:storyboard"
VERSION_ID = "artifact-version-img-1"


def _video_findings(*, black_fail: bool) -> str:
    findings = []
    for key in (
        "devices",
        "type_fonts",
        "composition_safety",
        "motion_quality",
        "technical",
        "watch_once",
    ):
        failed = black_fail and key == "technical"
        findings.append(
            {
                "check_key": key,
                "passed": not failed,
                "severity": "major" if failed else "minor",
                "evidence_timestamp_ms": 1200 if failed else None,
                "suggestion": "移除中段黑帧" if failed else "",
            },
        )
    return json.dumps({"findings": findings}, ensure_ascii=False)


def _image_findings(*, craft_fail: bool) -> str:
    findings = []
    for key in ("devices", "type_fonts", "composition_safety", "craft"):
        failed = craft_fail and key == "craft"
        findings.append(
            {
                "check_key": key,
                "passed": not failed,
                "severity": "major" if failed else "minor",
                "evidence_timestamp_ms": None,
                "suggestion": "画面右下角手指畸变，重新生成" if failed else "",
            },
        )
    return json.dumps({"findings": findings}, ensure_ascii=False)


def test_parse_media_report_video_evidence_discipline() -> None:
    report = parse_media_report(
        _video_findings(black_fail=True),
        kind="element_video",
        artifact_ref="artifact-version:v1",
        round_number=1,
        gate_block={"passed": False},
        stats={"y_mean": 0.5},
    )
    assert report.verdict == "revise"
    failed = report.failed_findings()
    assert [item.check_key for item in failed] == ["technical"]
    # A video failure without a timestamp cannot stand.
    payload = json.loads(_video_findings(black_fail=True))
    for item in payload["findings"]:
        if item["check_key"] == "technical":
            item["evidence_timestamp_ms"] = None
    report = parse_media_report(
        json.dumps(payload),
        kind="element_video",
        artifact_ref="artifact-version:v1",
        round_number=1,
        gate_block=None,
        stats=None,
    )
    assert report.verdict == "pass"


def test_parse_media_report_image_requires_suggestion_evidence() -> None:
    report = parse_media_report(
        _image_findings(craft_fail=True),
        kind="image",
        artifact_ref="artifact-version:v1",
        round_number=1,
        gate_block=None,
        stats=None,
    )
    assert report.verdict == "revise"
    payload = json.loads(_image_findings(craft_fail=True))
    for item in payload["findings"]:
        if item["check_key"] == "craft":
            item["suggestion"] = ""
    report = parse_media_report(
        json.dumps(payload),
        kind="image",
        artifact_ref="artifact-version:v1",
        round_number=1,
        gate_block=None,
        stats=None,
    )
    assert report.verdict == "pass"


def test_parse_media_report_requires_all_checks() -> None:
    payload = json.loads(_image_findings(craft_fail=False))
    payload["findings"] = payload["findings"][:2]
    with pytest.raises(ValueError):
        parse_media_report(
            json.dumps(payload),
            kind="image",
            artifact_ref="artifact-version:v1",
            round_number=1,
            gate_block=None,
            stats=None,
        )


def test_media_admission_rounds_and_dedup(tmp_path: Path) -> None:
    root = tmp_path / "run-review"
    assert (
        admission.admit_media_round(
            root,
            slot_id=SLOT_ID,
            version_id="v1",
            owner="owner-a",
        )
        == 1
    )
    # A live claim by the same owner dedups a replayed schedule.
    assert (
        admission.admit_media_round(
            root,
            slot_id=SLOT_ID,
            version_id="v1",
            owner="owner-a",
        )
        is None
    )
    assert admission.finalize_media_round(
        root,
        slot_id=SLOT_ID,
        version_id="v1",
        owner="owner-a",
        counted=True,
    )
    # Reviewed versions never re-admit.
    assert (
        admission.admit_media_round(
            root,
            slot_id=SLOT_ID,
            version_id="v1",
            owner="owner-a",
        )
        is None
    )
    assert (
        admission.admit_media_round(
            root,
            slot_id=SLOT_ID,
            version_id="v2",
            owner="owner-a",
        )
        == 2
    )
    assert admission.finalize_media_round(
        root,
        slot_id=SLOT_ID,
        version_id="v2",
        owner="owner-a",
        counted=True,
    )
    # The slot's advisory budget is spent after MAX_MEDIA_REVIEW_ROUNDS.
    assert (
        admission.admit_media_round(
            root,
            slot_id=SLOT_ID,
            version_id="v3",
            owner="owner-a",
        )
        is None
    )


def test_media_admission_foreign_owner_cannot_finalize(tmp_path: Path) -> None:
    root = tmp_path / "run-review"
    assert admission.admit_media_round(
        root,
        slot_id=SLOT_ID,
        version_id="v1",
        owner="owner-a",
    )
    assert not admission.finalize_media_round(
        root,
        slot_id=SLOT_ID,
        version_id="v1",
        owner="owner-b",
        counted=True,
    )


def _published(relative_uri: str) -> dict:
    return {
        "commandType": "GENERATE_STORYBOARD_IMAGE",
        "targetRef": "element:e1",
        "transactionId": "txn-img-1",
        "indexedFile": {"relative_uri": relative_uri},
        "artifactVersion": {
            "version_id": VERSION_ID,
            "slot_id": SLOT_ID,
            "name": "分镜图 1",
        },
    }


def test_schedule_is_a_no_op_when_off(monkeypatch) -> None:
    monkeypatch.delenv("CREATOR_MEDIA_REVIEW_ENABLED", raising=False)

    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("review loop must not start when off")

    monkeypatch.setattr(media_module, "run_media_review_loop", _boom)

    async def _run() -> None:
        before = set(asyncio.all_tasks())
        schedule_media_review(
            SimpleNamespace(),
            project_id=PROJECT_ID,
            published_result=_published("assets/artifacts/a.png"),
        )
        assert set(asyncio.all_tasks()) == before

    asyncio.run(_run())


def test_schedule_filters_unreviewed_commands(monkeypatch) -> None:
    monkeypatch.setenv("CREATOR_MEDIA_REVIEW_ENABLED", "1")
    started: list[str] = []

    async def fake_loop(services, *, project_id, published, kind):
        del services, project_id, published
        started.append(kind)

    monkeypatch.setattr(media_module, "run_media_review_loop", fake_loop)

    async def _run() -> None:
        published = _published("assets/artifacts/a.png")
        published["commandType"] = "COMPOSE_FINAL_VIDEO"
        schedule_media_review(
            SimpleNamespace(),
            project_id=PROJECT_ID,
            published_result=published,
        )
        schedule_media_review(
            SimpleNamespace(),
            project_id=PROJECT_ID,
            published_result=_published("assets/artifacts/a.png"),
        )
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert started == ["image"]


@pytest.fixture()
def media_services(tmp_path, monkeypatch):
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    return CreatorFileServices.create(tmp_path.resolve())


def _make_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            _FFMPEG,
            "-y",
            "-hide_banner",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:d=1",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _publish_image_project(services: CreatorFileServices) -> str:
    import hashlib

    project = Project.new(project_id=PROJECT_ID, name="Media Review")
    created_at = project.created_at
    relative_uri = "assets/artifacts/file-img-1.png"
    root = services.projects.project_root(PROJECT_ID)
    # Stage the payload first so the checksum matches the index.
    staging = Path(str(root) + "-staging.png")
    _make_png(staging)
    payload = staging.read_bytes()
    sha = hashlib.sha256(payload).hexdigest()
    project.assets.files_by_id["file-img-1"] = IndexedFile(
        file_id="file-img-1",
        kind="artifact_payload",
        relative_uri=relative_uri,
        sha256=sha,
        size_bytes=len(payload),
        media_type="image/png",
        created_at=created_at,
    )
    project.assets.artifact_versions_by_id[VERSION_ID] = ArtifactVersion(
        version_id=VERSION_ID,
        slot_id=SLOT_ID,
        kind="r2v_storyboard_image",
        owner_ref="element:e1",
        name="分镜图 1",
        file_id="file-img-1",
        checksum=sha,
        based_on_generation=0,
        created_at=created_at,
    )
    project.assets.artifact_slots_by_id[SLOT_ID] = ArtifactSlot(
        slot_id=SLOT_ID,
        kind="r2v_storyboard_image",
        owner_ref="element:e1",
        version_ids=[VERSION_ID],
        selected_version_id=VERSION_ID,
    )
    services.projects.create(project)
    destination = root / relative_uri
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging.replace(destination)
    return relative_uri


def _stub_vlm(monkeypatch, responses: list[str]) -> list[dict]:
    calls: list[dict] = []

    async def fake_chat_completion(content, **kwargs):
        calls.append({"content": content, "kwargs": kwargs})
        return responses[min(len(calls), len(responses)) - 1]

    monkeypatch.setattr(media_module, "chat_completion", fake_chat_completion)
    return calls


@requires_ffmpeg
def test_image_review_loop_delivers_feedback_and_dedups(
    media_services,
    monkeypatch,
) -> None:
    services = media_services
    monkeypatch.setenv("CREATOR_MEDIA_REVIEW_ENABLED", "1")
    relative_uri = _publish_image_project(services)
    services.sessions.create_project_runtime(PROJECT_ID)
    calls = _stub_vlm(monkeypatch, [_image_findings(craft_fail=True)])

    report = asyncio.run(
        media_module.run_media_review_loop(
            services,
            project_id=PROJECT_ID,
            published=_published(relative_uri),
            kind="image",
        ),
    )
    assert isinstance(report, MediaReviewReport)
    assert report.verdict == "revise"
    assert len(calls) == 1
    report_path = (
        services.projects.project_root(PROJECT_ID)
        / "runtime"
        / "run-review"
        / "media"
        / admission.safe_ref(VERSION_ID)
        / "round-1.json"
    )
    assert report_path.is_file()
    session = services.sessions.get_project_session_snapshot(PROJECT_ID)
    messages = services.sessions.list_messages(
        PROJECT_ID,
        session.session_id,
        after_seq=0,
        limit=None,
    )
    feedback = [
        item for item in messages if item.source == "run_review_feedback"
    ]
    assert len(feedback) == 1
    assert "运行审阅反馈" in feedback[0].content_parts[0].text

    # The same artifact version is never reviewed twice.
    replay = asyncio.run(
        media_module.run_media_review_loop(
            services,
            project_id=PROJECT_ID,
            published=_published(relative_uri),
            kind="image",
        ),
    )
    assert replay is None
    assert len(calls) == 1


@requires_ffmpeg
def test_image_review_pass_sends_no_feedback(
    media_services,
    monkeypatch,
) -> None:
    services = media_services
    monkeypatch.setenv("CREATOR_MEDIA_REVIEW_ENABLED", "1")
    relative_uri = _publish_image_project(services)
    services.sessions.create_project_runtime(PROJECT_ID)
    _stub_vlm(monkeypatch, [_image_findings(craft_fail=False)])
    report = asyncio.run(
        media_module.run_media_review_loop(
            services,
            project_id=PROJECT_ID,
            published=_published(relative_uri),
            kind="image",
        ),
    )
    assert report is not None
    assert report.verdict == "pass"
    session = services.sessions.get_project_session_snapshot(PROJECT_ID)
    messages = services.sessions.list_messages(
        PROJECT_ID,
        session.session_id,
        after_seq=0,
        limit=None,
    )
    assert [
        item for item in messages if item.source == "run_review_feedback"
    ] == []


@requires_ffmpeg
def test_vlm_failure_releases_claim_for_retry(
    media_services,
    monkeypatch,
) -> None:
    services = media_services
    monkeypatch.setenv("CREATOR_MEDIA_REVIEW_ENABLED", "1")
    relative_uri = _publish_image_project(services)
    services.sessions.create_project_runtime(PROJECT_ID)

    async def fake_chat_completion(content, **kwargs):
        del content, kwargs
        raise RuntimeError("VLM exploded")

    monkeypatch.setattr(media_module, "chat_completion", fake_chat_completion)
    report = asyncio.run(
        media_module.run_media_review_loop(
            services,
            project_id=PROJECT_ID,
            published=_published(relative_uri),
            kind="image",
        ),
    )
    assert report is None
    # The claim was released: a retry admits round 1 again.
    _stub_vlm(monkeypatch, [_image_findings(craft_fail=False)])
    retry = asyncio.run(
        media_module.run_media_review_loop(
            services,
            project_id=PROJECT_ID,
            published=_published(relative_uri),
            kind="image",
        ),
    )
    assert retry is not None
    assert retry.round == 1
