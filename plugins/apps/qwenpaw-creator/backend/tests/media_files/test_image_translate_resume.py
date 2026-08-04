# -*- coding: utf-8 -*-
# pylint: disable=protected-access,unused-argument
"""Restart recovery resumes a billed image translate instead of losing it.

A crashed process leaves a RUNNING image Task with a claimed provider call
and no result. The qwen-mt-image task behind it is already billed and its
id is durable, so recovery must resume polling, download the result and
publish it — never discard it and never resubmit.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
from datetime import UTC, datetime

import pytest
from PIL import Image

from domain.enums import CreatorCommandType, TaskStatus
from services.media_files import image_execution
from services.media_files.image_execution import FileImageExecutionService
from services.media_files.r2v_execution import recover_interrupted_image_tasks
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project

# pylint: disable=no-name-in-module
from utils.paths import media_task_scope, media_url_for, unique_task_work_path

# pylint: enable=no-name-in-module

pytestmark = pytest.mark.unit

_TRANSLATED_PNG = b"\x89PNG\r\n\x1a\n" + b"translated-poster" * 32

PROJECT_ID = "translate-resume-project"
ENTITY_ID = "poster-entity"
SOURCE_VERSION_ID = "asset-version-poster"
PROVIDER_TASK_ID = "provider-translate-1"


def _poster_png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (640, 480), color="white").save(output, format="PNG")
    return output.getvalue()


class _UncalledProvider:
    """Fails the test if a resumed task submits to the provider again."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        raise AssertionError("a resumed task must never resubmit")


def _services(tmp_path, monkeypatch) -> CreatorFileServices:
    """Project with one character entity and one real poster image version."""

    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    services = CreatorFileServices.create(tmp_path.resolve())
    project = Project.new(project_id=PROJECT_ID, name="Translate Resume")
    services.projects.create(
        Project.model_validate(project.model_dump(mode="json")),
    )
    poster = _poster_png()
    checksum = hashlib.sha256(poster).hexdigest()
    relative = "assets/sources/poster-source.png"
    target = services.projects.project_root(PROJECT_ID) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(poster)
    created = datetime.now(UTC).isoformat()

    snapshot = services.projects.read(PROJECT_ID)
    candidate = snapshot.project.model_dump(mode="json")
    candidate["visual"]["entities"]["items"][ENTITY_ID] = {
        "entity_id": ENTITY_ID,
        "kind": "character",
        "name": "海报",
        "description": "含中文标题的海报",
        "continuity": "",
        "required_variant_ids": [],
        "variants": {"items": {}, "order": []},
        "voice": None,
    }
    candidate["visual"]["entities"]["order"] = [ENTITY_ID]
    candidate["assets"]["files_by_id"]["file-poster"] = {
        "file_id": "file-poster",
        "kind": "source_original",
        "relative_uri": relative,
        "sha256": checksum,
        "size_bytes": len(poster),
        "media_type": "image/png",
        "created_at": created,
    }
    candidate["assets"]["source_versions_by_id"][SOURCE_VERSION_ID] = {
        "version_id": SOURCE_VERSION_ID,
        "logical_asset_id": "asset-poster",
        "name": "poster",
        "file_id": "file-poster",
        "checksum": checksum,
        "media_kind": "image",
        "media_type": "image/png",
        "created_at": created,
    }
    from services.runtime_files.models import ChangeOrigin, ReviewPolicy

    services.commits.commit(
        base=snapshot,
        candidate=candidate,
        origin=ChangeOrigin.RUNTIME_TASK,
        review_policy=ReviewPolicy.AUTO_FIX,
    )
    return services


async def _leave_interrupted_translate_task(
    worker: FileImageExecutionService,
    services: CreatorFileServices,
    *,
    idempotency_key: str,
) -> str:
    """Reproduce the durable state a crash leaves mid-translate."""

    from models.provider_tasks import note_provider_task

    base = await asyncio.to_thread(services.projects.read, PROJECT_ID)
    arguments = {
        "prompt": "翻译海报文字",
        "mode": "translate",
        "referenceImageRefs": [SOURCE_VERSION_ID],
        "sourceLang": "zh",
        "targetLang": "en",
    }
    resolved = image_execution._resolve_request(
        snapshot=base,
        project_root=services.projects.project_root(PROJECT_ID),
        command=CreatorCommandType.GENERATE_ASSET,
        target_ref=f"asset:{ENTITY_ID}",
        arguments=arguments,
    )
    ids = worker._ids(PROJECT_ID, idempotency_key)
    run, task = await worker._admit(
        base=base,
        resolved=resolved,
        request_fingerprint=f"sha256:{'a' * 64}",
        command_request_hash=f"sha256:{'b' * 64}",
        idempotency_key=idempotency_key,
        ids=ids,
    )
    task = await worker._start(run=run, task=task, resolved=resolved, ids=ids)
    assert await worker._claim_provider(task)
    # The provider accepted (and billed) the async translation, recorded it,
    # and the process died before the first poll returned.
    with media_task_scope(task.task_id, project_id=PROJECT_ID):
        note_provider_task(
            provider_task_id=PROVIDER_TASK_ID,
            model="qwen-mt-image",
            kind="image_translate",
        )
    return task.task_id


def test_recovery_resumes_and_publishes_a_billed_translate(
    tmp_path,
    monkeypatch,
) -> None:
    services = _services(tmp_path, monkeypatch)
    provider = _UncalledProvider()
    worker = FileImageExecutionService(services, provider=provider)
    task_id = asyncio.run(
        _leave_interrupted_translate_task(
            worker,
            services,
            idempotency_key="translate-1",
        ),
    )
    interrupted = worker.executions.get_task(PROJECT_ID, task_id)
    assert interrupted.status is TaskStatus.RUNNING
    assert interrupted.result is None
    # Publish inputs are frozen on the Task, so recovery needs no re-resolve.
    assert isinstance(interrupted.metadata.get("requestSnapshot"), dict)

    polls: list[str] = []

    async def fake_poll(provider_task_id: str) -> dict:
        polls.append(provider_task_id)
        return {
            "status": "SUCCEEDED",
            "image_url": "https://oss.test/translated.png",
        }

    async def fake_download(url: str, model_name: str) -> str:
        assert url == "https://oss.test/translated.png"
        path = unique_task_work_path("images", ".png", prefix="resumed-")
        path.write_bytes(_TRANSLATED_PNG)
        return media_url_for(path)

    monkeypatch.setattr("models.image.poll_image_translate_task", fake_poll)
    monkeypatch.setattr(
        "models.image.base.download_remote_image",
        fake_download,
    )

    recovered = asyncio.run(recover_interrupted_image_tasks(services))

    assert recovered == 1
    # The same paid task was resumed, and nothing was resubmitted.
    assert polls == [PROVIDER_TASK_ID]
    assert provider.calls == 0
    task = worker.executions.get_task(PROJECT_ID, task_id)
    assert task.status is TaskStatus.SUCCEEDED
    published = services.projects.read(PROJECT_ID).project
    versions = [
        version
        for version in published.assets.artifact_versions_by_id.values()
        if version.slot_id == f"asset:{ENTITY_ID}:image"
    ]
    assert versions, "the paid translation must be published as an artifact"
    indexed = published.assets.files_by_id[versions[0].file_id]
    stored = (
        services.projects.project_root(PROJECT_ID) / indexed.relative_uri
    ).read_bytes()
    assert stored == _TRANSLATED_PNG


def test_recovery_keeps_a_still_running_translate_resumable(
    tmp_path,
    monkeypatch,
) -> None:
    """A task still running upstream stays active for the next pass."""

    services = _services(tmp_path, monkeypatch)
    worker = FileImageExecutionService(services, provider=_UncalledProvider())
    task_id = asyncio.run(
        _leave_interrupted_translate_task(
            worker,
            services,
            idempotency_key="translate-2",
        ),
    )

    async def still_running(provider_task_id: str) -> dict:
        return {"status": "RUNNING"}

    monkeypatch.setattr(
        "models.image.poll_image_translate_task",
        still_running,
    )
    original = FileImageExecutionService.resume_provider_task

    async def bounded(self, task, **kwargs):
        # One poll, then defer to the next recovery pass (keeps the test fast).
        return await original(
            self,
            task,
            poll_interval_seconds=0.0,
            poll_budget_seconds=0.0,
        )

    monkeypatch.setattr(
        FileImageExecutionService,
        "resume_provider_task",
        bounded,
    )
    asyncio.run(recover_interrupted_image_tasks(services))

    task = worker.executions.get_task(PROJECT_ID, task_id)
    # Not terminalized: the billed task can still be resumed later.
    assert task.status is TaskStatus.RUNNING


def test_recovery_fails_a_translate_the_provider_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    """A provider-side failure terminalizes with the provider's reason."""

    services = _services(tmp_path, monkeypatch)
    worker = FileImageExecutionService(services, provider=_UncalledProvider())
    task_id = asyncio.run(
        _leave_interrupted_translate_task(
            worker,
            services,
            idempotency_key="translate-3",
        ),
    )

    async def failed(provider_task_id: str) -> dict:
        return {"status": "FAILED", "error": "unsupported language pair"}

    monkeypatch.setattr("models.image.poll_image_translate_task", failed)
    asyncio.run(recover_interrupted_image_tasks(services))

    task = worker.executions.get_task(PROJECT_ID, task_id)
    assert task.status is TaskStatus.FAILED
    assert "unsupported language pair" in str(task.error)
