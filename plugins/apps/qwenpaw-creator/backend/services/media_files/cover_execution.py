# -*- coding: utf-8 -*-
"""Whole-piece cover generation as a project-level runtime task.

Mirrors the shape of ``interaction_execution`` but is deliberately simpler:
one image, no per-element parsing, and no review gate — the poster is accepted
automatically (``ReviewPolicy.AUTO_FIX``) and written straight onto
``interactive_presentation`` so a later export can read it without touching
the image model.

Idempotency is by input fingerprint: re-dispatching with unchanged inputs
replays instead of re-billing, and an edited brief reopens the task.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from domain.enums import TaskKind, TaskStatus
from domain.errors import ConflictError
from services.project_files.assets import (
    AssetAlreadyExists,
    AssetFileStore,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import IndexedFile, cover_document_file_id
from services.runtime_files.execution_models import TaskRecord
from services.runtime_files.execution_store import ProjectExecutionStore
from services.runtime_files.errors import RecordNotFoundError
from services.runtime_files.models import ChangeOrigin, ReviewPolicy

from .cover_generation import (
    cover_input_fingerprint,
    cover_is_current,
    cover_reference_version_ids,
    render_cover_bytes,
)

logger = logging.getLogger(__name__)

_FINGERPRINT_MARKER = "input_fingerprint="


def _stable_id(prefix: str, project_id: str, idempotency_key: str) -> str:
    digest = uuid5(
        NAMESPACE_URL,
        f"qwenpaw-creator:cover:{prefix}:{project_id}:{idempotency_key}",
    ).hex
    return f"{prefix}-{digest}"


@dataclass(frozen=True, slots=True)
class FileCoverExecutionResult:
    input_fingerprint: str
    project_etag: str
    project_generation: int
    replayed: bool
    task_id: str | None = None

    def command_response(self, command_id: str) -> dict[str, Any]:
        return {
            "commandId": command_id,
            "status": "APPLIED",
            "eventSeq": 0,
            "workingHead": self.project_etag,
        }


async def execute_file_cover_command(
    services: CreatorFileServices,
    *,
    project_id: str,
    target_ref: str,
    arguments: Mapping[str, Any],
    idempotency_key: str,
    expected_object_versions: Sequence[str] = (),
) -> FileCoverExecutionResult:
    """Render and persist the whole-piece cover poster."""

    del target_ref  # The project is the only cover target.
    snapshot = await asyncio.to_thread(services.projects.read, project_id)
    if any(
        not value.startswith(f"project:{snapshot.etag}:")
        for value in expected_object_versions
    ):
        raise ConflictError("Cover command target changed before admission")

    project = snapshot.project
    input_fingerprint = cover_input_fingerprint(project)
    regenerate = bool(arguments.get("regenerate"))
    if cover_is_current(project) and not regenerate:
        return FileCoverExecutionResult(
            input_fingerprint=input_fingerprint,
            project_etag=snapshot.etag,
            project_generation=snapshot.generation,
            replayed=True,
        )

    dispatch_key = idempotency_key
    idempotency_key = _stable_id(
        "cover",
        project_id,
        f"{dispatch_key}:{input_fingerprint}",
    )
    execution = ProjectExecutionStore(services.root)
    task_id = _stable_id("task", project_id, idempotency_key)
    attempt_id = f"{task_id}-attempt-1"

    def admit():
        with services.projects.lifecycle_lock(project_id):
            try:
                existing = execution.get_task(project_id, task_id)
            except RecordNotFoundError:
                existing = None
            if existing is not None:
                if existing.status in {
                    TaskStatus.QUEUED,
                    TaskStatus.RUNNING,
                }:
                    raise ConflictError("Cover generation already running")
                reason = (existing.error or {}).get("message")
                raise ConflictError(
                    (f"此前生成封面失败：{reason}。" if reason else "")
                    + "封面任务已结束，请重新发起。",
                )
            execution.create_task(
                TaskRecord(
                    task_id=task_id,
                    project_id=project_id,
                    kind=TaskKind.COVER_GENERATION,
                    request_fingerprint=input_fingerprint,
                    idempotency_key=dispatch_key,
                    input_generation=snapshot.generation,
                    input_etag=snapshot.etag,
                    input_refs=[f"project:{project_id}"],
                    metadata={"inputFingerprint": input_fingerprint},
                ),
                _lifecycle_lock_held=True,
            )
            execution.append_task_attempt(
                project_id,
                task_id,
                event_id=f"{attempt_id}-start",
                attempt_id=attempt_id,
                status="RUNNING",
                input={"fingerprint": input_fingerprint},
                _lifecycle_lock_held=True,
            )

    await asyncio.to_thread(admit)
    try:
        references = await asyncio.to_thread(
            _resolve_cover_references,
            services,
            project_id=project_id,
            project=project,
        )
        content = await render_cover_bytes(project, references=references)
        result = await asyncio.to_thread(
            _publish_cover_image,
            services,
            project_id=project_id,
            content=content,
            input_fingerprint=input_fingerprint,
            idempotency_key=idempotency_key,
            task_id=task_id,
        )
        await asyncio.to_thread(
            execution.append_task_attempt,
            project_id,
            task_id,
            event_id=f"{attempt_id}-end",
            attempt_id=attempt_id,
            status="SUCCEEDED",
            output_refs=[f"project:{project_id}"],
            output={"projectGeneration": result.project_generation},
        )
        return result
    except BaseException as exc:
        status = (
            TaskStatus.CANCELLED
            if isinstance(exc, asyncio.CancelledError)
            else TaskStatus.FAILED
        )
        current = await asyncio.to_thread(
            execution.get_task,
            project_id,
            task_id,
        )
        if current.status is TaskStatus.RUNNING:
            await asyncio.to_thread(
                execution.append_task_attempt,
                project_id,
                task_id,
                event_id=f"{attempt_id}-end",
                attempt_id=attempt_id,
                status=status.value,
                error={
                    "message": str(exc),
                    "retryable": bool(getattr(exc, "retryable", False)),
                },
            )
        exc.creator_task_id = task_id
        raise


def _resolve_cover_references(
    services: CreatorFileServices,
    *,
    project_id: str,
    project: Any,
) -> list[tuple[str, str]]:
    """Turn the first scene + protagonist images into model reference URLs.

    Best-effort: an unready or unreadable reference asset falls back to an
    empty list so the cover degrades to text-to-image instead of failing the
    task - the poster is a nicety, not a hard dependency.
    """

    refs = cover_reference_version_ids(project)
    if not refs:
        return []
    # Imported lazily: image_execution pulls the whole media stack, which the
    # read-only cover paths must not load.
    # pylint: disable-next=import-outside-toplevel
    from services.media_files.image_execution import (
        _resolve_version_references,
    )

    try:
        urls, _, _ = _resolve_version_references(
            project=project,
            project_root=services.projects.project_root(project_id),
            version_ids=[version_id for _, version_id in refs],
        )
    except Exception:  # noqa: BLE001 - any resolution miss -> text-only
        logger.warning("封面参考图解析失败，回退文生图", exc_info=True)
        return []
    if len(urls) != len(refs):
        return []
    return [(label, url) for (label, _), url in zip(refs, urls)]


def _publish_cover_image(
    services: CreatorFileServices,
    *,
    project_id: str,
    content: bytes,
    input_fingerprint: str,
    idempotency_key: str,
    task_id: str,
) -> FileCoverExecutionResult:
    """Persist the poster bytes as a cover file and commit the pointer."""

    with services.projects.lifecycle_lock(project_id):
        base = services.projects.read(project_id)
        working = base.project.model_copy(deep=True)
        if cover_input_fingerprint(working) != input_fingerprint:
            raise ConflictError(
                "Cover inputs changed during generation; result discarded",
            )
        task = ProjectExecutionStore(services.root).get_task(
            project_id,
            task_id,
        )
        if task.status is not TaskStatus.RUNNING:
            raise ConflictError(
                "Cover task was cancelled; result was discarded",
            )

        checksum = hashlib.sha256(content).hexdigest()
        file_id = cover_document_file_id(checksum)
        is_jpeg = content.startswith(b"\xff\xd8\xff")
        indexed = IndexedFile(
            file_id=file_id,
            kind="other",
            relative_uri=PurePosixPath(
                "assets",
                "cover",
                f"{checksum}.{'jpg' if is_jpeg else 'png'}",
            ).as_posix(),
            sha256=checksum,
            size_bytes=len(content),
            media_type="image/jpeg" if is_jpeg else "image/png",
            schema_name="cover_document",
            schema_version=1,
            created_at=datetime.now(UTC),
        )
        store = AssetFileStore(services.projects.project_root(project_id))
        staged = store.stage_bytes(content, staging_id="cover")
        try:
            store.publish(
                staged,
                indexed.relative_uri,
                expected_sha256=checksum,
                expected_size_bytes=len(content),
            )
        except AssetAlreadyExists:
            store.abandon(staged)

        working.assets.files_by_id[file_id] = indexed
        working.interactive_presentation = (
            working.interactive_presentation.model_copy(
                update={
                    "cover_file_id": file_id,
                    "cover_checksum": checksum,
                    "cover_fingerprint": (
                        f"{_FINGERPRINT_MARKER}{input_fingerprint}"
                    ),
                },
            )
        )

        commit = services.commits.commit(
            base=base,
            candidate=working.model_dump(mode="json"),
            origin=ChangeOrigin.RUNTIME_TASK,
            review_policy=ReviewPolicy.AUTO_FIX,
            caused_by_request_id=idempotency_key,
            round_id=_stable_id("round", project_id, idempotency_key),
            transaction_id=_stable_id(
                "transaction",
                project_id,
                idempotency_key,
            ),
            advance_accepted_baseline=True,
            _lifecycle_lock_held=True,
        )
        services.poller.note_commit(commit.snapshot)
    return FileCoverExecutionResult(
        input_fingerprint=input_fingerprint,
        project_etag=commit.snapshot.etag,
        project_generation=commit.snapshot.generation,
        replayed=False,
        task_id=task_id,
    )


__all__ = [
    "FileCoverExecutionResult",
    "execute_file_cover_command",
]
