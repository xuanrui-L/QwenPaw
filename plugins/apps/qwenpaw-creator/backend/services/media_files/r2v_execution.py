# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access,raise-missing-from
# pylint: disable=too-many-boolean-expressions,too-many-branches
# pylint: disable=too-many-return-statements,too-many-statements
# pylint: disable=try-except-raise,unused-argument
"""Restart-safe, file-native reference-to-video execution.

``GENERATE_R2V_VIDEO`` admission freezes exact Project versions and returns
immediately.  A process-local supervisor performs provider submission and
polling from a durable state record below ``runtime/tasks/<task>/``.  The
provider never owns Project authority: successful bytes are published as an
immutable Asset and become visible only through one Project commit.

The state machine is deliberately fail-closed around the non-idempotent
provider submission window.  A surviving submit claim without a durable
provider task id is never submitted again after its safety lease expires.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import socket
import stat
import subprocess
import threading
import time
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import Field

import httpx

from domain.enums import (
    CreatorCommandType,
    SpecialistRole,
    SpecialistRunStatus,
    TERMINAL_SPECIALIST_STATUSES,
    TERMINAL_TASK_STATUSES,
    TaskKind,
    TaskStatus,
)
from domain.errors import (
    ConflictError,
    NotFoundError,
    StorageIntegrityError,
    ValidationError,
)
from models.config import is_media_review_enabled
from models.reference_markers import canonical_marker_indices
from models.video_task_plans import (
    continuity_signals,
    VideoTaskKind,
    VideoTaskPlan,
    planned_predecessor,
    uses_storyboard,
    WAN_30_EXTEND_HEADROOM_SECONDS,
    WAN_30_MAX_INPUT_PLUS_OUTPUT_SECONDS,
    WAN_30_MAX_REFERENCE_VIDEO_SECONDS,
)
from schemas.common import StrictModel
from services.prompt_text import (
    missing_narrative_dialogue,
    video_prompt_time_error,
)
from services.project_files.assets import (
    AssetAlreadyExists,
    AssetFileStore,
    StagedAsset,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    ArtifactSlot,
    ArtifactVersion,
    I2VCreation,
    IndexedFile,
    Project,
    R2VCreation,
    S2VCreation,
    T2VCreation,
    Timeline,
    TimelineElement,
)
from services.media_files.publication_retry import (
    commit_with_lock_retry,
    record_materialized_result,
)
from services.media_files.element_adapter import (
    bind_candidate_output,
    find_timeline_element,
    reconcile_candidate_span,
    selected_element_output,
    target_element_id,
)
from services.media_files.review_admission import (
    assert_media_review_admission,
    media_review_policy,
)
from services.media_files.transient_errors import (
    MAX_TRANSIENT_RETRY_SLOTS,
    is_transient_error_message,
    is_transient_task_error,
    transient_retry_slot_key,
)
from services.media_files.prompt_labels import media_prompt_entity_names
from services.media_files.provider_source_url import (
    provider_source_url_usable,
    stored_provider_source_url,
)
from services.media_files.task_scratch import terminal_release_listener
from services.project_files.prompt_sync import assert_r2v_prompt_sync
from services.media_files.visual_reference_resolution import (
    number_references_by_media,
    resolve_r2v_visual_reference_version_ids,
    video_reference_plan,
)
from services.media_files.segment_continuity import (
    ContinuityEntry,
    continuity_reference_ids,
    extract_continuity_artifacts,
    find_predecessor_video_element,
    resolve_predecessor_video_version,
)
from services.observability import report_error
from services.project_files.remote_cache import public_source_url
from services.project_files.store import ProjectSnapshot, ProjectStoreError
from services.run_review.media_review import (
    release_media_review_reservation,
    reserve_media_review,
    schedule_media_review,
)
from services.runtime_files.atomic_store import (
    AtomicJsonRecordStore,
    canonical_json_bytes,
)
from services.runtime_files.errors import LockTimeoutError, RecordNotFoundError
from services.runtime_files.execution_models import (
    SpecialistRunRecord,
    TaskAttemptStatus,
    TaskRecord,
)
from services.runtime_files.execution_store import (
    ExecutionPayloadConflict,
    ExecutionStateConflict,
)
from services.runtime_files.models import ChangeOrigin, ReviewPolicy
from services.runtime_files.path_safety import is_link_stat
from services.runtime_files.reconciliation import (
    reconcile_orphaned_queued_runs,
    reconcile_terminal_task_runs,
)

# pylint: disable=no-name-in-module
from utils.logger import setup_logger
from utils.paths import media_task_scope, task_work_root

# pylint: enable=no-name-in-module

from .secure_video_stream import (
    MaterializedVideo,
    PeerAddressMismatchError,
    materialize_r2v_video,
)

_MAX_VIDEO_BYTES = 256 * 1024 * 1024
_SUBMIT_TIMEOUT_SECONDS = 180.0
_SUBMIT_CLAIM_SECONDS = 600.0
_MATERIALIZE_TIMEOUT_SECONDS = 180.0
_MATERIALIZE_CLAIM_SECONDS = 300.0
_MATERIALIZE_RETRY_DELAYS = (2.0, 5.0, 10.0)
_TERMINAL_RECOVERY_POLL_SECONDS = 1.0
_ACTIVE_PHASES = frozenset(
    {
        "ADMITTED",
        "SUBMIT_CLAIMED",
        "POLLING",
        "PROVIDER_SUCCEEDED",
        "PUBLISHED",
    },
)
_TERMINAL_TASKS = TERMINAL_TASK_STATUSES

logger = setup_logger("services.media_files.r2v_execution")

_GOOGLE_API_KEY_AUTH = "x-goog-api-key"
# Matches models.video_backends.minimax_sglang.BEARER_DOWNLOAD_AUTH.
_BEARER_VIDEO_API_KEY_AUTH = "authorization-bearer"
_CREDENTIAL_QUERY_NAMES = frozenset(
    {"key", "api_key", "token", "access_token"},
)


def _strip_credential_query(value: object) -> object:
    """Remove credential-shaped query parameters from one durable URL."""

    if not isinstance(value, str) or not value:
        return value
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.query:
        return value
    kept = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in _CREDENTIAL_QUERY_NAMES
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(kept),
            parsed.fragment,
        ),
    )


def _durable_provider_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return a credential-free provider result suitable for task state."""

    durable = dict(result)
    if str(durable.get("download_auth") or "") == _GOOGLE_API_KEY_AUTH:
        for field in ("url", "result_url", "video_url"):
            if field in durable:
                durable[field] = _strip_credential_query(durable[field])
    return durable


def _provider_download_headers(result: Mapping[str, Any]) -> dict[str, str]:
    """Resolve non-durable provider download credentials at request time."""

    auth = str(result.get("download_auth") or "")
    if not auth:
        return {}
    if auth not in (_GOOGLE_API_KEY_AUTH, _BEARER_VIDEO_API_KEY_AUTH):
        raise ValidationError(f"未知 provider 下载鉴权类型: {auth}")
    from models import config as model_config

    api_key = model_config.get_video_api_key()
    if not api_key:
        raise ValidationError(
            (
                "Veo 视频下载需要当前模型配置中的 API Key"
                if auth == _GOOGLE_API_KEY_AUTH
                else "受保护的自部署视频下载需要当前模型配置中的 API Key"
            ),
        )
    if auth == _BEARER_VIDEO_API_KEY_AUTH:
        return {"Authorization": f"Bearer {api_key}"}
    return {_GOOGLE_API_KEY_AUTH: api_key}


def _provider_trusted_private_origins() -> frozenset[tuple[str, str, int]]:
    """Origins of operator-configured self-hosted video endpoints.

    Only the exact scheme/host/port the user typed into the active
    ``minimax_sglang`` configuration may resolve to a non-global address
    during result materialization; every other provider URL — including
    redirects off this origin — keeps the public-network requirement.
    Derived from live config at download time, never from durable state.
    """

    from models import config as model_config

    if model_config.get_video_backend() != "minimax_sglang":
        return frozenset()
    parsed = urlsplit(str(model_config.get_video_base_url() or ""))
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold()
    if scheme not in {"http", "https"} or not host:
        return frozenset()
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return frozenset()
    return frozenset({(scheme, host, port)})


class VideoReferenceBudgetError(ValidationError):
    """Resolved Project references exceed the active video model contract."""

    code = "VIDEO_REFERENCE_BUDGET_EXCEEDED"


class VideoModelCapabilityError(ValidationError):
    """A configured video model alias has no verified reference limit."""

    code = "VIDEO_MODEL_CAPABILITY_UNKNOWN"


def _log_safe(value: object) -> str:
    """Neutralise CR/LF so identifier values cannot forge log lines."""
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


class R2VProvider(Protocol):
    """Injectable provider boundary.  Calls happen outside all file locks."""

    async def submit(
        self,
        *,
        prompt: str,
        reference_image_urls: Sequence[str],
        ratio: str,
        duration_seconds: int,
        resolution: str,
        watermark: bool,
        generate_audio: bool,
        mode: str = "r2v",
        first_frame_url: str | None = None,
        last_frame_url: str | None = None,
        video_url: str | None = None,
        reference_voice_urls: Sequence[str] = (),
    ) -> str: ...

    async def poll(self, provider_task_id: str) -> Mapping[str, Any]: ...

    async def submit_s2v(
        self,
        *,
        image_url: str,
        audio_url: str,
        resolution: str,
    ) -> str: ...

    async def poll_s2v(self, provider_task_id: str) -> Mapping[str, Any]: ...


class ExistingR2VProvider:
    """Lazy adapter over the configured video model.

    Keeping the import inside the call means read-only Creator startup does not
    import the model transport, while the packaged runtime can still select it
    when a real R2V command is submitted.
    """

    async def submit(
        self,
        *,
        prompt: str,
        reference_image_urls: Sequence[str],
        ratio: str,
        duration_seconds: int,
        resolution: str,
        watermark: bool,
        generate_audio: bool,
        mode: str = "r2v",
        first_frame_url: str | None = None,
        last_frame_url: str | None = None,
        video_url: str | None = None,
        reference_voice_urls: Sequence[str] = (),
        reference_bgm_url: str | None = None,
    ) -> str:
        from models.video_model import submit_video_task

        return await submit_video_task(
            prompt=prompt,
            reference_image_url_list=list(reference_image_urls),
            preserve_reference_slots=True,
            ratio=ratio,
            duration=duration_seconds,
            resolution=resolution,
            watermark=watermark,
            generate_audio=generate_audio,
            mode=mode,
            first_frame_url=first_frame_url,
            last_frame_url=last_frame_url,
            video_url=video_url,
            reference_voice_urls=(
                list(reference_voice_urls) if reference_voice_urls else None
            ),
            reference_bgm_url=reference_bgm_url,
        )

    async def poll(self, provider_task_id: str) -> Mapping[str, Any]:
        from models.video_model import check_task_status

        return await check_task_status(provider_task_id)

    async def submit_s2v(
        self,
        *,
        image_url: str,
        audio_url: str,
        resolution: str,
    ) -> str:
        from models.s2v_model import submit_s2v_task

        return await submit_s2v_task(
            image_url=image_url,
            audio_url=audio_url,
            resolution=resolution,
        )

    async def poll_s2v(self, provider_task_id: str) -> Mapping[str, Any]:
        from models.s2v_model import check_s2v_task_status

        return await check_s2v_task_status(provider_task_id)


class R2VTaskState(StrictModel):
    """Mutable recovery head stored atomically beside the Task aggregate."""

    schema_version: Literal[1] = 1
    project_id: str
    task_id: str
    run_id: str
    request_fingerprint: str
    phase: Literal[
        "ADMITTED",
        "SUBMIT_CLAIMED",
        "POLLING",
        "PROVIDER_SUCCEEDED",
        "PUBLISHED",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "QUARANTINED",
    ] = "ADMITTED"
    request: dict[str, Any]
    submit_owner: str | None = None
    submit_claim_token: str | None = None
    submit_claimed_at_epoch: float | None = None
    submit_heartbeat_at_epoch: float | None = None
    submit_claim_expires_at_epoch: float | None = None
    provider_task_id: str | None = None
    last_poll_at_epoch: float | None = None
    next_poll_at_epoch: float | None = None
    poll_lease_owner: str | None = None
    poll_lease_token: str | None = None
    poll_lease_expires_at_epoch: float | None = None
    provider_result: dict[str, Any] | None = None
    materialize_owner: str | None = None
    materialize_claim_token: str | None = None
    materialize_claimed_at_epoch: float | None = None
    materialize_heartbeat_at_epoch: float | None = None
    materialize_claim_expires_at_epoch: float | None = None
    materialized_result: dict[str, Any] | None = None
    published_result: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class FileR2VDispatch:
    task_id: str
    run_id: str
    transaction_id: str
    input_etag: str
    replayed: bool

    def command_response(self, command_id: str) -> dict[str, Any]:
        return {
            "commandId": command_id,
            "status": "QUEUED",
            "eventSeq": 0,
            "transactionId": self.transaction_id,
            "workingHead": self.input_etag,
        }


class _R2VClaimLost(RuntimeError):
    """
    A stale supervisor lost durable ownership; it must never fail the Task.
    """


@dataclass(frozen=True, slots=True)
class _ResolvedR2V:
    target_ref: str
    element_id: str
    element_label: str
    prompt: str
    ratio: str
    duration_seconds: int
    resolution: str
    watermark: bool
    generate_audio: bool
    reference_version_ids: tuple[str, ...]
    reference_urls: tuple[str, ...]
    reference_checksums: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    read_set: tuple[dict[str, Any], ...]
    slot_id: str
    slot_kind: str
    owner_ref: str
    mode: str = "r2v"
    # Materialized Wan3.0 task-shape decisions (extension only): the
    # provider duration is ``material + new`` so it differs from the
    # Element's timeline duration, and the returned video carries a
    # material prefix that must be trimmed before publication. Joined
    # into the frozen request only when non-default, so legacy tasks
    # keep their fingerprint identity (same convention as voices).
    continuity_shape: str = ""
    continuity_trim_offset: float = 0.0
    provider_duration_seconds: int | None = None
    # Parallel to reference_urls: the enrolled character-voice sample audio
    # bound to each reference ("" = none). Only filled when the configured
    # video model documents an audio reference input.
    reference_voice_urls: tuple[str, ...] = ()
    # Shared project BGM shipped as one extra ``reference_audio`` entry so
    # every segment syncs its soundtrack to the same clip (music seam
    # continuity). "" = no bgm Element / budget skip → native audio.
    bgm_reference_url: str = ""
    first_frame_version_id: str = ""
    first_frame_url: str = ""
    last_frame_version_id: str = ""
    last_frame_url: str = ""
    authored_last_frame_version_id: str = ""
    video_version_id: str = ""
    video_url: str = ""
    # "video" (default) drives the configured video model; "s2v" drives the
    # wan2.2-s2v digital-human provider through the same durable machinery.
    provider: str = "video"
    s2v_image_version_id: str = ""
    s2v_image_url: str = ""
    s2v_audio_version_id: str = ""
    s2v_audio_url: str = ""
    s2v_resolution: str = ""


def _stable_id(prefix: str, project_id: str, key: str) -> str:
    digest = uuid5(
        NAMESPACE_URL,
        f"qwenpaw-creator:file-r2v:{prefix}:{project_id}:{key}",
    ).hex
    return f"{prefix}-{digest}"


def _ids(project_id: str, key: str) -> dict[str, str]:
    return {
        "run_id": _stable_id("run", project_id, key),
        "round_id": _stable_id("round", project_id, key),
        "task_id": _stable_id("task", project_id, key),
        "attempt_id": _stable_id("attempt", project_id, key),
        "attempt_started_event_id": _stable_id(
            "attempt-started",
            project_id,
            key,
        ),
        "attempt_succeeded_event_id": _stable_id(
            "attempt-succeeded",
            project_id,
            key,
        ),
        "attempt_failed_event_id": _stable_id(
            "attempt-failed",
            project_id,
            key,
        ),
        "attempt_quarantined_event_id": _stable_id(
            "attempt-quarantined",
            project_id,
            key,
        ),
        "file_id": _stable_id("file", project_id, key),
        "artifact_version_id": _stable_id("artifact-version", project_id, key),
        "transaction_id": _stable_id("media-transaction", project_id, key),
        "quarantine_id": _stable_id("quarantine", project_id, key),
    }


def _fingerprint(value: Mapping[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def _failed_task_conflict(
    task: TaskRecord | None,
    *,
    exhausted_retries: bool = False,
) -> ConflictError:
    """Name the original failure and the exact way out of the replay wall."""

    status = task.status.value if task is not None else "FAILED"
    reason = ""
    error = getattr(task, "error", None)
    if isinstance(error, Mapping) and error.get("message"):
        reason = f"；原失败原因：{str(error['message'])[:200]}"
    if exhausted_retries:
        advice = "瞬态重试槽位已用尽，说明故障持续存在。请停止重发相同请求，向用户报告故障或稍后再试。"
    else:
        advice = (
            "相同 arguments 的重发将始终返回此错误；若需重试，请先修正失败原因"
            "并调整 arguments（如更换分镜图或修改 prompt 措辞）以生成新任务。"
        )
    return ConflictError(f"R2V Task 已终止: {status}{reason}。{advice}")


def _is_transient_materialize_error(error: BaseException) -> bool:
    if isinstance(error, httpx.TransportError):
        return True
    if isinstance(error.__cause__, socket.gaierror):
        # The secure downloader wraps DNS failures as validation errors.
        # Retry the download, retaining the already-paid provider result.
        return True
    # Large public CDNs can legitimately rotate the selected edge between
    # getaddrinfo and the TLS connection.  The secure downloader still rejects
    # that individual hop (so DNS rebinding remains fail-closed), then a fresh
    # materialization attempt performs a new DNS resolution and connection.
    # Treat only this exact peer-pinning rejection as transient; private,
    # reserved or malformed addresses remain deterministic validation errors.
    if isinstance(error, PeerAddressMismatchError):
        return True
    return is_transient_error_message(str(error))


def _json_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} 必须是 JSON object")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValidationError(f"{label} 必须可以持久化为 JSON") from error
    if not isinstance(
        decoded,
        dict,
    ):  # pragma: no cover - mapping encodes object
        raise ValidationError(f"{label} 必须是 JSON object")
    return decoded


def _target_element_id(
    target_ref: str,
    command: str = "GENERATE_R2V_VIDEO",
) -> str:
    return target_element_id(target_ref, command=command)


def _duration(value: Any) -> int:
    if isinstance(value, bool):
        raise ValidationError("R2V durationSeconds 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValidationError("R2V durationSeconds 必须是数字") from error
    if not 1 <= number <= 60:
        raise ValidationError("R2V durationSeconds 必须在 1 到 60 秒之间")
    # Shots are planned at sub-second precision but providers only accept
    # whole seconds — round instead of rejecting the whole dispatch.
    return max(1, round(number))


def _indexed_path(project_root: Path, indexed: IndexedFile) -> Path:
    return project_root.joinpath(*PurePosixPath(indexed.relative_uri).parts)


def _resolve_reference_versions(
    *,
    project: Project,
    project_root: Path,
    version_ids: Sequence[str],
) -> tuple[list[str], list[str], list[str], list[dict[str, Any]]]:
    urls: list[str] = []
    checksums: list[str] = []
    provenance: list[str] = []
    read_set: list[dict[str, Any]] = []
    files = AssetFileStore(project_root)
    for version_id in dict.fromkeys(version_ids):
        source = project.assets.source_versions_by_id.get(version_id)
        artifact = project.assets.artifact_versions_by_id.get(version_id)
        version = source or artifact
        if version is None:
            raise NotFoundError(f"R2V reference version 不存在: {version_id}")
        # Check for source_url stored by Token Plan image generation.
        # Stored at metadata["provider"]["source_url"] by
        # _materialize_and_publish. A URL that outlived its grant must fall
        # back to the immutable local copy: the provider's download refusal
        # is indistinguishable from a model failure (issue #8592).
        source_url = stored_provider_source_url(artifact)
        if source_url and not provider_source_url_usable(source_url):
            logger.warning(
                "Stored provider source URL for %s expired or is "
                "unverifiable; falling back to the local asset copy",
                version_id,
            )
            source_url = ""
        if source_url:
            indexed = project.assets.files_by_id.get(version.file_id)
            if indexed is None or not indexed.media_type.casefold().startswith(
                ("image/", "video/"),
            ):
                raise ValidationError(
                    f"R2V reference 不是图片或视频: {version_id}",
                )
            ref = f"artifact-version:{version_id}"
            urls.append(source_url)
            checksums.append(version.checksum)
            provenance.append(ref)
            read_set.append(
                {
                    "ref": ref,
                    "versionId": version_id,
                    "fileId": indexed.file_id,
                    "checksum": version.checksum,
                    "sourceUrl": source_url,
                },
            )
            continue
        remote_url = public_source_url(source) if source is not None else None
        if remote_url is not None:
            if not version.media_type.casefold().startswith(
                ("image/", "video/"),
            ):
                raise ValidationError(
                    f"R2V reference 不是图片或视频: {version_id}",
                )
            ref = f"asset-version:{version_id}"
            urls.append(remote_url)
            checksums.append(version.checksum)
            provenance.append(ref)
            read_set.append(
                {
                    "ref": ref,
                    "versionId": version_id,
                    "fileId": None,
                    "checksum": version.checksum,
                    "sourceUrl": remote_url,
                },
            )
            continue
        indexed = project.assets.files_by_id.get(version.file_id)
        if indexed is None:
            raise StorageIntegrityError(
                f"R2V reference 缺少 IndexedFile: {version_id}",
            )
        if indexed.sha256 != version.checksum:
            raise StorageIntegrityError(
                f"R2V reference checksum/index 不一致: {version_id}",
            )
        if not indexed.media_type.casefold().startswith(
            ("image/", "video/"),
        ):
            raise ValidationError(
                f"R2V reference 不是图片或视频: {version_id}",
            )
        inspection = files.inspect(indexed)
        if not inspection.available:
            raise StorageIntegrityError(
                f"R2V reference 文件不可用: {version_id} ({inspection.status.value})",
            )
        ref = (
            f"asset-version:{version_id}"
            if source is not None
            else f"artifact-version:{version_id}"
        )
        urls.append(_indexed_path(project_root, indexed).resolve().as_uri())
        checksums.append(version.checksum)
        provenance.append(ref)
        read_set.append(
            {
                "ref": ref,
                "versionId": version_id,
                "fileId": indexed.file_id,
                "checksum": version.checksum,
            },
        )
    return urls, checksums, provenance, read_set


def _resolve_single_media_version(
    *,
    project: Project,
    project_root: Path,
    version_id: str,
    media_prefix: str,
    label: str,
) -> tuple[str, str, str, dict[str, Any]]:
    """Resolve one exact version id into ``(url, checksum, ref, read_entry)``.

    Same integrity rules as ``_resolve_reference_versions`` but with a
    caller-selected media kind, so i2v first frames stay images and
    video_edit inputs stay videos.
    """

    source = project.assets.source_versions_by_id.get(version_id)
    artifact = project.assets.artifact_versions_by_id.get(version_id)
    version = source or artifact
    if version is None:
        raise NotFoundError(f"{label} version 不存在: {version_id}")
    # Check for source_url stored by Token Plan image generation.
    # A URL that outlived its grant must fall back to the local indexed
    # copy for first frames and other single-media inputs too.
    source_url = stored_provider_source_url(artifact)
    if source_url and not provider_source_url_usable(source_url):
        logger.warning(
            "Stored provider source URL for %s expired or is "
            "unverifiable; falling back to the local asset copy",
            version_id,
        )
        source_url = ""
    if source_url:
        indexed = project.assets.files_by_id.get(version.file_id)
        if indexed is None or not indexed.media_type.casefold().startswith(
            media_prefix,
        ):
            raise ValidationError(
                f"{label} 必须是 {media_prefix}* 媒体: {version_id}",
            )
        ref = f"artifact-version:{version_id}"
        return (
            source_url,
            version.checksum,
            ref,
            {
                "ref": ref,
                "versionId": version_id,
                "fileId": indexed.file_id,
                "checksum": version.checksum,
                "sourceUrl": source_url,
            },
        )
    remote_url = public_source_url(source) if source is not None else None
    if remote_url is not None:
        if not version.media_type.casefold().startswith(media_prefix):
            raise ValidationError(
                f"{label} 必须是 {media_prefix}* 媒体: {version_id}",
            )
        ref = f"asset-version:{version_id}"
        return (
            remote_url,
            version.checksum,
            ref,
            {
                "ref": ref,
                "versionId": version_id,
                "fileId": None,
                "checksum": version.checksum,
                "sourceUrl": remote_url,
            },
        )
    indexed = project.assets.files_by_id.get(version.file_id)
    if indexed is None:
        raise StorageIntegrityError(
            f"{label} 缺少 IndexedFile: {version_id}",
        )
    if indexed.sha256 != version.checksum:
        raise StorageIntegrityError(
            f"{label} checksum/index 不一致: {version_id}",
        )
    if not indexed.media_type.casefold().startswith(media_prefix):
        raise ValidationError(
            f"{label} 必须是 {media_prefix}* 媒体: {version_id}",
        )
    inspection = AssetFileStore(project_root).inspect(indexed)
    if not inspection.available:
        raise StorageIntegrityError(
            f"{label} 文件不可用: {version_id} ({inspection.status.value})",
        )
    ref = (
        f"asset-version:{version_id}"
        if source is not None
        else f"artifact-version:{version_id}"
    )
    return (
        _indexed_path(project_root, indexed).resolve().as_uri(),
        version.checksum,
        ref,
        {
            "ref": ref,
            "versionId": version_id,
            "fileId": indexed.file_id,
            "checksum": version.checksum,
        },
    )


def media_version_duration_seconds(
    project: Project,
    version_id: str,
) -> float | None:
    """Recorded duration of one exact source/artifact version, when known."""

    version = project.assets.source_versions_by_id.get(
        version_id,
    ) or project.assets.artifact_versions_by_id.get(version_id)
    return None if version is None else version.duration_seconds


def _probed_media_duration_seconds(
    project: Project,
    project_root: Path,
    version_id: str,
) -> float | None:
    """Probe the indexed local file of one version for its real duration.

    Older assets (and anything imported before duration probing) carry no
    recorded duration, so the file itself is the fallback source of truth.
    """

    version = project.assets.source_versions_by_id.get(
        version_id,
    ) or project.assets.artifact_versions_by_id.get(version_id)
    if version is None or version.file_id is None:
        return None
    indexed = project.assets.files_by_id.get(version.file_id)
    if indexed is None:
        return None
    path = _indexed_path(project_root, indexed)
    if not path.is_file():
        return None
    from services.runtime_files.media_probe import MediaProbeError, probe_media

    try:
        probe = probe_media(os.fspath(path))
    except (MediaProbeError, OSError, ValueError):
        return None
    duration = getattr(probe, "duration_seconds", None)
    return float(duration) if duration else None


def effective_video_duration_seconds(
    project: Project,
    project_root: Path,
    version_id: str,
) -> float | None:
    """The duration a billed request will really be charged for.

    Single resolver for authorization and execution: the recorded metadata
    first, then a probe of the indexed local file for assets that carry
    none. Returning None means the length is genuinely unknown.
    """

    recorded = media_version_duration_seconds(project, version_id)
    if recorded is not None:
        return recorded
    return _probed_media_duration_seconds(project, project_root, version_id)


def _assert_video_edit_input_duration(
    project: Project,
    project_root: Path,
    version_id: str,
) -> None:
    """Fail before a billed submission when the input length is unusable.

    video_edit takes its duration from the input video (3-60s, with the
    provider keeping only the first 15s), and the tool's durationSeconds is
    not sent for this mode, so the input itself is the only thing to check.
    An unknown duration is never waved through: the local file is probed,
    and a still-unknown length is rejected rather than billed blindly.
    """

    duration = effective_video_duration_seconds(
        project,
        project_root,
        version_id,
    )
    from models.video_capabilities import (
        HAPPYHORSE_VIDEO_EDIT_KEPT_SECONDS,
        HAPPYHORSE_VIDEO_EDIT_MAX_INPUT_SECONDS,
        HAPPYHORSE_VIDEO_EDIT_MIN_INPUT_SECONDS,
    )

    if duration is None:
        raise ValidationError(
            f"无法确定 videoRef 的时长，而 video_edit 输入必须在 "
            f"{HAPPYHORSE_VIDEO_EDIT_MIN_INPUT_SECONDS}–"
            f"{HAPPYHORSE_VIDEO_EDIT_MAX_INPUT_SECONDS} 秒之间：该版本既未记录"
            " duration_seconds，本地文件也探测不到（缺失、不可读或无 "
            "ffprobe/ffmpeg）。请重新引入该视频以补齐元数据后重试",
        )
    if (
        duration < HAPPYHORSE_VIDEO_EDIT_MIN_INPUT_SECONDS
        or duration > HAPPYHORSE_VIDEO_EDIT_MAX_INPUT_SECONDS
    ):
        raise ValidationError(
            f"video_edit 输入视频时长必须在 "
            f"{HAPPYHORSE_VIDEO_EDIT_MIN_INPUT_SECONDS}–"
            f"{HAPPYHORSE_VIDEO_EDIT_MAX_INPUT_SECONDS} 秒之间，"
            f"当前 videoRef 为 {duration:.2f} 秒；超过 "
            f"{HAPPYHORSE_VIDEO_EDIT_KEPT_SECONDS} 秒时上游只取前 "
            f"{HAPPYHORSE_VIDEO_EDIT_KEPT_SECONDS} 秒",
        )


def _validated_request_mode(arguments: Mapping[str, Any]) -> str:
    """Normalize the requested mode against the runtime capability matrix."""

    from models import config as model_config
    from models.video_capabilities import validate_video_mode

    model_name = model_config.get_video_model_name()
    protocol_backend = model_config.get_video_backend()
    try:
        return validate_video_mode(
            protocol_backend,
            model_name,
            str(arguments.get("mode") or "r2v"),
        )
    except ValueError as error:
        raise ValidationError(str(error)) from error


# Internal sentinels standing in for the synthetic continuity anchors
# inside the budget counters. They are never version IDs and are
# stripped from any error details before they reach an Agent.
_CONTINUITY_IMAGE_SENTINEL = "<continuity-image>"
_CONTINUITY_VIDEO_SENTINEL = "<continuity-video>"


def _assert_r2v_reference_budget(
    project: Project,
    version_ids: Sequence[str],
    *,
    project_root: Path | None = None,
    output_duration_seconds: int | None = None,
    continuity_media: Sequence[tuple[str, float | None]] = (),
) -> None:
    """Fail before admission when Project references exceed model limits.

    ``continuity_media`` describes the entries the segment-continuity
    layer is about to append (``("image", None)`` per last frame,
    ``("video", seconds)`` per tail clip). Synthetic ``continuity:*``
    IDs never resolve through the Asset Index, so the Project-side
    counting loop below silently skips them; without this parameter a
    Wan3.0 request that would exceed the reference-video cap on the
    wire would silently pass the pre-admission gate and get rejected
    or truncated by the provider after we already paid for uploads.
    """

    from models import config as model_config
    from models.video_capabilities import (
        effective_video_model_name,
        is_wan3_video_model,
        video_backend_key,
        video_reference_capability,
        video_reference_violation,
    )

    configured_model = model_config.get_video_model_name().strip()
    backend_key = video_backend_key(
        configured_model,
        model_config.get_video_backend(),
    )
    effective_model = effective_video_model_name(
        configured_model,
        "r2v",
        backend_key,
    )
    capability = video_reference_capability(effective_model)

    image_ids: list[str] = []
    video_ids: list[str] = []
    for version_id in dict.fromkeys(version_ids):
        source = project.assets.source_versions_by_id.get(version_id)
        artifact = project.assets.artifact_versions_by_id.get(version_id)
        version = source or artifact
        if version is None:
            # The resolver below owns the stable NotFoundError contract.
            continue
        if source is not None:
            media_type = source.media_type.casefold()
        else:
            indexed = project.assets.files_by_id.get(artifact.file_id)
            if indexed is None:
                # The resolver below owns the integrity error and its details.
                continue
            media_type = indexed.media_type.casefold()
        if media_type.startswith("image/"):
            image_ids.append(version_id)
        elif media_type.startswith("video/"):
            video_ids.append(version_id)

    # Segment-continuity entries are declared by the caller; they
    # participate in both the reference-count and duration caps even
    # though they are not Asset Index versions. The sentinels are
    # internal counters, never real IDs: they are filtered out of the
    # error details below so an Agent reading a budget failure does
    # not try to resolve them.
    continuity_video_durations: list[float] = []
    continuity_image_count = 0
    for kind, duration in continuity_media:
        if kind == "image":
            image_ids.append(_CONTINUITY_IMAGE_SENTINEL)
            continuity_image_count += 1
        elif kind == "video":
            video_ids.append(_CONTINUITY_VIDEO_SENTINEL)
            continuity_video_durations.append(float(duration or 0.0))

    details = {
        "modelName": configured_model or "未配置",
        "effectiveModelName": effective_model or "未配置",
        "resolvedCount": len(image_ids) + len(video_ids),
        "imageCount": len(image_ids),
        "videoCount": len(video_ids),
        "referenceVersionIds": list(dict.fromkeys(version_ids)),
        "imageReferenceVersionIds": [
            item for item in image_ids if item != _CONTINUITY_IMAGE_SENTINEL
        ],
        "videoReferenceVersionIds": [
            item for item in video_ids if item != _CONTINUITY_VIDEO_SENTINEL
        ],
        "continuityImageCount": continuity_image_count,
        "continuityVideoCount": len(continuity_video_durations),
    }
    if capability is None:
        raise VideoModelCapabilityError(
            "VIDEO_MODEL_CAPABILITY_UNKNOWN: 执行层已解析 R2V 的 Project "
            "参考素材，但无法从官方能力表确认视频模型 "
            f"{effective_model or '未配置'} 的输入上限，因此没有创建任务、"
            "上传素材或调用 provider。如果这是兼容网关别名，请先将别名"
            "映射到官方模型能力，不可使用 Wan 或通用猜测上限。",
            details={**details, "knownModelRequired": True},
        )

    violation = video_reference_violation(
        capability,
        image_count=len(image_ids),
        video_count=len(video_ids),
    )
    if violation is not None:
        raise VideoReferenceBudgetError(
            "VIDEO_REFERENCE_BUDGET_EXCEEDED: 执行层解析 storyboard 与 Project "
            f"exact reference version 后，共得到 {details['resolvedCount']} 个"
            f"参考素材，但视频模型 {effective_model} 的官方限制为：{violation}。"
            "执行层没有静默截断，也没有创建任务、上传素材或调用 provider。"
            "请先 read_project，缩减目标 Element 的角色、场景、道具、阵容锚点"
            "或 video_reference_version_ids，再用已变更的 Project 重试。",
            details={
                **details,
                "modelFamily": capability.family,
                "maxReferenceImages": capability.max_reference_images,
                "maxReferenceVideos": capability.max_reference_videos,
                "maxReferenceMedia": capability.max_reference_media,
                "documentationUrl": capability.documentation_url,
            },
        )

    if not is_wan3_video_model(effective_model) or not video_ids:
        return
    max_input_duration = capability.max_reference_video_duration_seconds
    max_combined_duration = capability.max_input_output_duration_seconds
    if max_input_duration is None or max_combined_duration is None:
        raise VideoModelCapabilityError(
            "VIDEO_MODEL_CAPABILITY_UNKNOWN: Wan3.0 参考视频能力缺少官方时长"
            "预算，未创建上游任务。",
            details={**details, "knownDurationBudgetRequired": True},
        )
    durations: list[float] = []
    for version_id in video_ids:
        if version_id == _CONTINUITY_VIDEO_SENTINEL:
            # The caller already supplied the real seconds via
            # ``continuity_media``; add them once per synthetic entry.
            continue
        duration = media_version_duration_seconds(project, version_id)
        if duration is None and project_root is not None:
            duration = effective_video_duration_seconds(
                project,
                project_root,
                version_id,
            )
        if duration is None:
            raise VideoReferenceBudgetError(
                "VIDEO_REFERENCE_DURATION_UNKNOWN: Wan3.0 参考视频"
                f" {version_id} 缺少可验证的时长，未创建上游任务。",
                details={**details, "unknownDurationVersionId": version_id},
            )
        durations.append(duration)
    durations.extend(continuity_video_durations)
    input_duration = sum(durations)
    if input_duration > max_input_duration or (
        output_duration_seconds is not None
        and input_duration + output_duration_seconds > max_combined_duration
    ):
        raise VideoReferenceBudgetError(
            "VIDEO_REFERENCE_DURATION_EXCEEDED: Wan3.0 参考视频总时长"
            f" {input_duration:.2f} 秒，输出时长 {output_duration_seconds or 0} "
            f"秒；官方要求参考视频合计不超过 {max_input_duration} 秒，且"
            "输入视频与输出视频时长之和不超过 "
            f"{max_combined_duration} 秒。",
            details={
                **details,
                "inputVideoDurationSeconds": input_duration,
                "outputDurationSeconds": output_duration_seconds,
                "maxInputVideoDurationSeconds": max_input_duration,
                "maxCombinedDurationSeconds": max_combined_duration,
            },
        )


_REFERENCE_ROLE_MARKER = "[REFERENCE IMAGE ROLES — RUNTIME FACT]"


def _resolve_reference_voices(
    *,
    project: Project,
    project_root: Path,
    creation: Any,
    version_ids: Sequence[str],
) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    """Pair each reference version with its character's enrolled voice.

    Returns ``(voice_urls, read_entries)`` where *voice_urls* parallels
    ``version_ids`` ("" = no voice). Only characters referenced by the
    creation whose enrolled voice keeps a sample audio contribute; the
    sample rides with the character's own reference image so providers
    that document per-subject voices (wan2.7) bind them correctly and
    standalone-audio providers (wan3.0 / Seedance 2.x) receive the same
    deduplicated set.
    """

    from models import config as model_config
    from models.video_capabilities import video_reference_voice_support

    if (
        video_reference_voice_support(
            model_config.get_video_model_name(),
            model_config.get_video_backend(),
        )
        is None
    ):
        return (), []

    voice_by_version: dict[str, str] = {}
    read_entries: list[dict[str, Any]] = []
    resolved_samples: dict[str, str] = {}
    for entity_ref in getattr(creation, "character_refs", ()) or ():
        entity = project.visual.entities.items.get(entity_ref)
        if entity is None or entity.kind != "character":
            continue
        voice = entity.voice
        if voice is None or not voice.sample_source_version_id:
            continue
        contributed = {
            variant.selected_artifact_version_id
            for variant in entity.variants.items.values()
            if variant.selected_artifact_version_id
        }
        if entity.selected_artifact_version_id:
            contributed.add(entity.selected_artifact_version_id)
        matched = [vid for vid in version_ids if vid in contributed]
        if not matched:
            continue
        sample_id = voice.sample_source_version_id
        sample_url = resolved_samples.get(sample_id)
        if sample_url is None:
            sample_url, _checksum, _ref, entry = _resolve_single_media_version(
                project=project,
                project_root=project_root,
                version_id=sample_id,
                media_prefix="audio/",
                label="characterVoiceSample",
            )
            resolved_samples[sample_id] = sample_url
            read_entries.append(entry)
        for vid in matched:
            voice_by_version.setdefault(vid, sample_url)
    if not voice_by_version:
        return (), []
    return (
        tuple(voice_by_version.get(vid, "") for vid in version_ids),
        read_entries,
    )


def _append_reference_role_mapping(
    prompt: str,
    project: Project,
    version_ids: Sequence[str],
    *,
    storyboard_id: str,
    continuity_labels: Mapping[str, str | None] | None = None,
) -> str:
    """State what each numbered reference image actually is.

    The payload carries several references, so a prompt that never names
    them leaves the model inferring each one's job from its pixels.
    Numbering comes from ``number_references_by_media`` — the same
    per-type sequences the provider wire uses (images in 图N order,
    reference videos in their own 视频N order), the continuity suffix
    consults, and the preview exposes as ``imageIndex``/``videoIndex``.
    A previous attempt mixed positional numbering here with per-type
    numbering in the suffix, which put two materials on one ``图N`` or
    pointed at a nonexistent one; the shared helper is what keeps the
    paid prompt self-consistent.

    ``continuity_labels`` carries the human-readable role text for the
    synthetic ``continuity:*`` IDs the segment-continuity module injects
    at submit time; those IDs never live in the Project Asset Index so
    the ordinary lookup cannot describe them, and a ``None`` label marks
    the tail clip — a video the suffix already addresses as 视频N.
    """

    if _REFERENCE_ROLE_MARKER in prompt or not version_ids:
        return prompt

    from models import config as model_config
    from models.reference_markers import canonical_marker
    from models.video_capabilities import video_reference_marker_spec

    model_name = model_config.get_video_model_name()
    protocol_backend = model_config.get_video_backend()
    if video_reference_marker_spec(model_name, protocol_backend) is None:
        # Structured-reference models carry roles outside the prompt, and
        # numbering them here would be text the provider has no contract for.
        return prompt
    video_continuity_ids = {
        version_id
        for version_id, label in (continuity_labels or {}).items()
        if label is None
    }
    lines: list[str] = []
    for version_id, kind, ordinal in number_references_by_media(
        project,
        version_ids,
        force_video=video_continuity_ids,
    ):
        if continuity_labels and version_id in continuity_labels:
            label = continuity_labels[version_id]
            if label is None:
                # Video-typed continuity entries (tail clips) are referenced
                # directly in the segment-continuity suffix as "视频N";
                # 图N has no meaning for them, so nothing to declare here.
                continue
            lines.append(f"{canonical_marker(ordinal)} = {label}")
            continue
        source = project.assets.source_versions_by_id.get(version_id)
        artifact = project.assets.artifact_versions_by_id.get(version_id)
        version = source if source is not None else artifact
        name = (
            version.name
            if version is not None and version.name
            else version_id
        )
        role = f"分镜图（{name}）" if version_id == storyboard_id else name
        if kind == "video":
            # Reference videos live in the provider's 视频N sequence; a
            # 图N label would point at the wrong slot on the wire.
            lines.append(f"视频{ordinal} = {role}")
        else:
            lines.append(f"{canonical_marker(ordinal)} = {role}")
    body = "\n".join(lines)
    return (
        f"{prompt.rstrip()}\n\n"
        f"{_REFERENCE_ROLE_MARKER}\n"
        "以下是本次实际发送的参考素材及其职责：图片按 图N、参考视频按 视频N "
        "各自独立编号，均与各自序列的发送顺序一致。"
        "按各素材声明的职责使用它们，不要依据图内文字或标签猜测用途。\n"
        f"{body}"
    )


def _render_reference_markers(prompt: str) -> str:
    """Rewrite canonical ``[Image N]`` into the configured provider's syntax.

    Authors write one form; the provider sees the one it documents. Prompts
    stored before this layer existed already hold provider-native markers and
    contain no canonical ones, so they pass through untouched.
    """

    from models import config as model_config
    from models.reference_markers import render_reference_markers
    from models.video_capabilities import video_reference_marker_spec

    model_name = model_config.get_video_model_name()
    protocol_backend = model_config.get_video_backend()
    return render_reference_markers(
        prompt,
        video_reference_marker_spec(model_name, protocol_backend),
    )


def _reference_ids_with_continuity(
    project: Project,
    timeline: Timeline,
    element: TimelineElement,
    *,
    mode: str = "r2v",
    legacy_intent_rule: bool = False,
) -> tuple[str, ...]:
    """Return the reference list exactly as the submit path will emit it.

    ``mode`` matters: the continuity layer only injects on the r2v
    reference stack and the i2v first-frame fallback. A ``video_edit``
    submission consumes exactly one ``videoRef`` and never reads the
    predecessor, so its frozen reference list keeps the pre-continuity
    shape — counting synthetic anchors there would fail the claim check
    on every predecessor reroll and re-queue a paid render for inputs
    the wire request never carried.

    R2V appends the predecessor's synthetic ``continuity:*`` IDs after
    the Project reference plan so a predecessor reroll shifts this
    successor's fingerprint: parked FAILED nodes re-qualify for a fresh
    dispatch and in-flight claims fail the submit-time check, while
    completed artifacts keep their selection (no paid cascade).

    I2V without an authored ``first_frame_version_id`` auto-adopts the
    predecessor's last frame at submit time. That fallback is the only
    input the wire request carries beyond what lives on the Element,
    so its synthetic ID has to reach the fingerprint too — otherwise a
    prev segment regenerated between ADMIT and SUBMIT would still pass
    the ``_submission_input_fingerprint`` check and get paid out on the
    stale anchor.
    """

    creation = element.creation
    if mode == "video_edit":
        # Mirrors the pre-continuity fingerprint payload exactly.
        return (
            video_reference_plan(project, element)
            if isinstance(creation, R2VCreation)
            else ()
        )
    if mode == "video_extend":
        # The extension's only continuity input is the predecessor's real
        # published artifact; a reroll changes this version and drifts
        # the fingerprint naturally (no synthetic ids needed).
        prev = planned_predecessor(timeline, element)
        if prev is None and legacy_intent_rule:
            prev = find_predecessor_video_element(timeline, element)
        ver = (
            resolve_predecessor_video_version(project, prev) if prev else None
        )
        return (ver,) if ver else ()
    if isinstance(creation, R2VCreation):
        base = video_reference_plan(project, element)
        if _r2v_strong_continuity(
            project,
            timeline,
            element,
            legacy_intent_rule=legacy_intent_rule,
        ):
            # The promoted last-frame anchor really ships inside the
            # image stack, so it must drift this fingerprint when the
            # predecessor rerolls (claim fails, parked FAILED re-
            # qualifies) exactly like the old injected anchors did.
            # Share the resolver's swap decision via _storyboard_swap_for.
            predecessor = planned_predecessor(timeline, element)
            anchors = continuity_reference_ids(
                project,
                timeline,
                element,
                predecessor=predecessor,
            )
            if not anchors:
                return base
            if creation.generation_plan is None and (
                _storyboard_swap_for(
                    str(getattr(creation, "video_prompt", "") or ""),
                    project,
                    timeline,
                    element,
                )
                is not None
            ):
                selected = selected_element_output(
                    project,
                    element,
                    "storyboard",
                )
                storyboard_id = selected[1] if selected is not None else ""
                base = tuple(item for item in base if item != storyboard_id)
            return (anchors[0], *base)
        return base
    if isinstance(creation, I2VCreation):
        if creation.first_frame_version_id:
            # Explicit input already lives inside ``creation`` in the
            # fingerprint payload; nothing to add.
            return ()
        predecessor = planned_predecessor(timeline, element)
        if predecessor is None and not legacy_intent_rule:
            return ()
        continuity = continuity_reference_ids(
            project,
            timeline,
            element,
            predecessor=predecessor,
        )
        # Only the last-frame ID actually flows into the first-frame
        # slot; the tail clip is not consumed in I2V mode.
        return (continuity[0],) if continuity else ()
    return ()


def _submission_input_fingerprint(
    project: Project,
    element_id: str,
    *,
    include_default_audio: bool = False,
    mode: str = "r2v",
    payload_refs: Sequence[str] | None = None,
) -> str:
    timeline, element = find_timeline_element(project, element_id)
    creation = element.creation.model_dump(mode="json")
    creation.pop("prompt_sync", None)
    for field in ("generation_plan", "predecessor_element_id"):
        if creation.get(field) is None:
            creation.pop(field, None)
    if not include_default_audio and creation.get("generate_audio") is True:
        del creation["generate_audio"]
    references = _reference_ids_with_continuity(
        project,
        timeline,
        element,
        mode=mode,
    )
    if payload_refs is not None:
        payload_synthetic = tuple(
            str(item)
            for item in payload_refs
            if str(item).startswith("continuity:")
        )
        if payload_synthetic:
            # Legacy frozen payloads (pre first-class-extend refactor)
            # carried synthetic anchors; keep them in the comparison so
            # in-flight claims from the previous build still validate.
            references = (
                *references,
                *(p for p in payload_synthetic if p not in references),
            )
        else:
            # The frozen wire request carried no synthetic anchors.
            # Anchors that never shipped must not drift the claim on a
            # predecessor reroll — the same rationale the video_edit
            # exclusion states.
            references = tuple(
                item
                for item in references
                if not str(item).startswith("continuity:")
            )
    return _fingerprint(
        {
            "creation": creation,
            "span": element.span.model_dump(mode="json"),
            "ticksPerSecond": timeline.ticks_per_second,
            "ratio": project.settings.aspect_ratio,
            "resolution": project.settings.resolution,
            "references": references,
        },
    )


def _submission_input_fingerprint_legacy(
    project: Project,
    element_id: str,
    *,
    keep_default_audio: bool = False,
    keep_force_storyboard: bool = False,
    reference_shape: str = "plain",
    mode: str = "r2v",
) -> str:
    """Fingerprint shape frozen by builds before segment continuity.

    Pre-upgrade tasks carry a digest without the synthetic ``continuity:*``
    IDs; the current shape can never collide with it, so the M1 claim
    guarantee holds across the upgrade window without weakening it.
    """

    timeline, element = find_timeline_element(project, element_id)
    creation = element.creation.model_dump(mode="json")
    creation.pop("prompt_sync", None)
    for field in ("generation_plan", "predecessor_element_id"):
        if creation.get(field) is None:
            creation.pop(field, None)
    # Pre-force_storyboard builds serialized without the field at all.
    # The default (False) collapses to that historical shape so old
    # frozen digests still validate; an explicit True is a real input
    # change and must never match the legacy shape.
    if not keep_force_storyboard and not creation.get("force_storyboard"):
        creation.pop("force_storyboard", None)
    # Same rule for the structured intent fields: their default (None)
    # serializes absent in the historical shape, explicit values stay
    # and correctly invalidate old digests.
    if creation.get("transition_intent") is None:
        creation.pop("transition_intent", None)
    if creation.get("storyboard_intent") is None:
        creation.pop("storyboard_intent", None)
    if not keep_default_audio and creation.get("generate_audio") is True:
        del creation["generate_audio"]
    return _fingerprint(
        {
            "creation": creation,
            "span": element.span.model_dump(mode="json"),
            "ticksPerSecond": timeline.ticks_per_second,
            "ratio": project.settings.aspect_ratio,
            "resolution": project.settings.resolution,
            "references": (
                _reference_ids_with_continuity(
                    project,
                    timeline,
                    element,
                    mode=mode,
                    legacy_intent_rule=True,
                )
                if (
                    reference_shape == "continuity"
                    and isinstance(element.creation, R2VCreation)
                )
                else (
                    video_reference_plan(project, element)
                    if isinstance(element.creation, R2VCreation)
                    else ()
                )
            ),
        },
    )


def _matches_submission_inputs(
    project: Project,
    element_id: str,
    frozen: str,
    *,
    mode: str = "r2v",
    payload_refs: Sequence[str] | None = None,
) -> bool:
    # Claim-time truth: the roles ACTUALLY frozen in the dispatched
    # payload. A synthetic last-frame anchor must still resolve to the
    # same predecessor version — a reroll fails the claim no matter
    # how today's budget rules would re-derive. The publish policy
    # for already-paid artifacts stays separate (the frozen-inputs
    # gate keeps its no-paid-cascade exception).
    if payload_refs and any(
        str(item).startswith("continuity:last_frame:") for item in payload_refs
    ):
        _claim_timeline, _claim_element = find_timeline_element(
            project,
            element_id,
        )
        try:
            predecessor = planned_predecessor(_claim_timeline, _claim_element)
        except ValidationError:
            return False
        _current_anchors = set(
            continuity_reference_ids(
                project,
                _claim_timeline,
                _claim_element,
                predecessor=predecessor,
            ),
        )
        if any(
            str(item).startswith("continuity:last_frame:")
            and str(item) not in _current_anchors
            for item in payload_refs
        ):
            return False
    if frozen == _submission_input_fingerprint(
        project,
        element_id,
        mode=mode,
        payload_refs=payload_refs,
    ):
        return True
    # Older interactive writers included the enabled default in the digest.
    # Both historical formats encode an explicit mute as a distinct input.
    if frozen == _submission_input_fingerprint(
        project,
        element_id,
        include_default_audio=True,
        mode=mode,
        payload_refs=payload_refs,
    ):
        return True
    # Pre-continuity builds froze digests without synthetic anchors, in
    # BOTH historical audio serializations (the default strips the
    # enabled flag; older interactive writers kept it verbatim).
    # Four historical shapes: each schema generation serialized a
    # different field set (pre-audio-control, pre-force_storyboard,
    # pre-intent), each with or without the verbatim audio flag. One
    # missing candidate turns an upgrade-window claim into a needless
    # re-dispatch.
    return any(
        frozen
        == _submission_input_fingerprint_legacy(
            project,
            element_id,
            keep_default_audio=audio,
            keep_force_storyboard=force,
            reference_shape=shape,
            mode=mode,
        )
        for audio in (False, True)
        for force in (False, True)
        for shape in ("plain", "continuity")
    )


def _continuity_signals(
    timeline: Timeline,
    element: TimelineElement,
    prev: TimelineElement | None,
) -> tuple[str, str, bool]:
    """Project-data author-intent signals (shared with the work graph).

    Thin timeline-shaped wrapper over ``models.video_task_plans.
    continuity_signals`` so the executor, the fingerprint and the
    dependency graph can never drift apart on keyword tables or
    Transition lookup rules.
    """

    return continuity_signals(timeline.elements_by_id, element, prev)


def _r2v_strong_continuity(
    project: Project,
    timeline: Timeline,
    element: TimelineElement,
    *,
    legacy_intent_rule: bool = False,
) -> bool:
    """Read the authored method, or reconstruct a frozen legacy digest."""
    plan = getattr(element.creation, "generation_plan", None)
    if plan is not None:
        return plan.method == "r2v_last_frame"
    if not legacy_intent_rule:
        return False
    prev = find_predecessor_video_element(timeline, element)
    if prev is None or not resolve_predecessor_video_version(project, prev):
        return False
    transition_kind, intent, one_take = _continuity_signals(
        timeline,
        element,
        prev,
    )
    if transition_kind != "none" or intent == "cut":
        return False
    authored = getattr(element.creation, "transition_intent", None)
    if authored not in ("continue", "one_take"):
        # Undeclared intent keeps the default-continuous gate: same
        # scene, no newly introduced cast/props. An authored continue
        # anchors even across scenes (identical to the scheduler).
        prev_scene = getattr(prev.creation, "scene_ref", None)
        cur_scene = getattr(element.creation, "scene_ref", None)
        if not prev_scene or not cur_scene or prev_scene != cur_scene:
            # Scene unknown (empty refs) is not a confirmed same
            # scene: no default anchoring.
            return False
        prev_refs = set(
            getattr(prev.creation, "character_refs", []) or [],
        )
        prev_refs |= set(getattr(prev.creation, "prop_refs", []) or [])
        cur_refs = set(
            getattr(element.creation, "character_refs", []) or [],
        )
        cur_refs |= set(getattr(element.creation, "prop_refs", []) or [])
        if cur_refs - prev_refs:
            return False
    if one_take:
        from models import config as model_config
        from models.video_capabilities import supports_video_continuation
        from models.video_task_plans import (
            WAN_30_EXTEND_HEADROOM_SECONDS,
            WAN_30_MAX_INPUT_PLUS_OUTPUT_SECONDS,
            WAN_30_MAX_REFERENCE_VIDEO_SECONDS,
        )

        # Only exclude the anchor when the dispatch would REALLY go
        # VIDEO_EXTEND — capability alone is not enough: an
        # over-budget one_take falls back to anchored r2v and the
        # fingerprint must then carry the anchor or a stale claim
        # could ship an outdated opening.
        prev_version = resolve_predecessor_video_version(project, prev)
        material_seconds = (
            media_version_duration_seconds(project, prev_version)
            if prev_version
            else None
        )
        segment_seconds = element.span.duration_tick / max(
            timeline.ticks_per_second,
            1,
        )
        extend_budget = (
            WAN_30_MAX_INPUT_PLUS_OUTPUT_SECONDS
            - WAN_30_EXTEND_HEADROOM_SECONDS
        )
        can_extend = (
            supports_video_continuation(
                model_config.get_video_model_name(),
                model_config.get_video_backend(),
            )
            and material_seconds is not None
            and 0 < material_seconds <= WAN_30_MAX_REFERENCE_VIDEO_SECONDS
            and material_seconds + segment_seconds <= extend_budget
        )
        if can_extend:
            return False
    from services.file_agent_runtime.work_graph import probe_ffmpeg_ready

    return bool(probe_ffmpeg_ready())


def _shift_authored_image_refs(prompt: str, delta: int) -> str:
    """Move every authored ``[Image N]`` marker by ``delta``.

    Fallback shape for strong continuity when the storyboard sentence
    cannot be rewritten (see ``_storyboard_anchor_swap``): the anchor is
    prepended at 图1 and the author's own per-image sentences must
    follow the new ordinals or "[Image 1] 为本镜头分镜图" would address
    the anchor. Markers are the canonical authoring form; rendering to
    provider labels happens later in ``_render_reference_markers``.
    """

    from models.reference_markers import CANONICAL_MARKER_PATTERN

    def bump(match: "re.Match[str]") -> str:
        shifted = int(match.group(1)) + delta
        return match.group(0).replace(match.group(1), str(shifted), 1)

    return CANONICAL_MARKER_PATTERN.sub(bump, prompt or "")


# The skill template fixes the storyboard's authored sentence to lead
# with "[Image 1] 为本镜头分镜图" and end at the first clause break;
# strong continuity replaces exactly that sentence with the anchor's
# role, which lets the storyboard leave the reference stack without
# leaving the author's words pointing at the wrong image.
_STORYBOARD_CLAUSE_RE = re.compile(
    r"\[Image 1\][^；。\n]*?(?:分镜图|上一片段末帧)[^；。\n]*?[；。]",
)
_ANCHOR_CLAUSE = (
    "[Image 1] 为上一片段末帧（本段起始画面），第一帧严格对齐该图；"
)


def _storyboard_anchor_swap(prompt: str) -> str | None:
    """Swap the templated storyboard sentence for the anchor sentence.

    Returns the rewritten prompt when the clause is found (the
    storyboard can then be dropped from the stack — every OTHER
    authored ordinal stays intact because the anchor inherits the
    storyboard's 图1 slot), or ``None`` when the author deviated from
    the template, in which case the caller keeps the storyboard and
    shifts all markers instead. Deterministic on project data, so the
    dispatch resolver and the fingerprint derive the same shape.
    """

    if not prompt or not _STORYBOARD_CLAUSE_RE.search(prompt):
        return None
    return _STORYBOARD_CLAUSE_RE.sub(_ANCHOR_CLAUSE, prompt, count=1)


_INJECTED_ANCHOR_PREFIX = (
    "开场第一帧严格对齐图1（上一片段末帧），全段布景陈设、"
    "招牌位置、道具数量与光线氛围须与图1及场景参考图保持一致，"
    "随后按本段剧本节奏自然展开。\n"
)
# Pure reference-directive sentences only: marker-led clauses that name
# the predecessor's last frame (zh/en), and opening-alignment
# imperatives. Both stop at the FIRST 。or ； so authored actions in
# the same breath survive; narrative sentences are never touched.
_FRAME_WORDS = r"(?:末帧|最后一帧|尾帧|final frame|last frame|ending frame)"
_PREV_WORDS = r"(?:上一|前一段|前一|上一片段|previous|preceding|prior)"
_LAST_FRAME_CLAUSE_RE = re.compile(
    r"\[Image \d+\][^。;\n]*?(?:"
    + _PREV_WORDS
    + r"[^。;\n]*?"
    + _FRAME_WORDS
    + r"|"
    + _FRAME_WORDS
    + r"[^。;\n]*?of the "
    + _PREV_WORDS
    + r")[^。;\n]*?[；。;.]",
)
# The ONE deletable legacy artifact: the verbatim role line the old
# authoring guide generated at the prompt head. Anything else stays
# for the Agent to repair — the system never guesses which part of a
# sentence is narrative.
_LEGACY_ROLE_PREFIX = "[Image 1] 为上一片段末帧（本段起始状态）"
_OPENING_ALIGN_RE = re.compile(
    r"(?:开场第一帧严格对齐图\d+"
    r"|[Tt]he (?:first|opening) frame (?:strictly )?aligns with Image \d+"
    r"|Align the (?:first|opening) frame (?:strictly )?to Image \d+)"
    r"[^。；\n]*[。；.]?",
)


def _remap_legacy_image_refs_after_prefix_removal(prompt: str) -> str:
    """The old guide reserved 图1 for the virtual last frame and
    started real materials at [Image 2]; once that fixed role line is
    removed every authored ordinal shifts down by one. Markers below
    2 (outside the old scheme) stay put; author actions are untouched
    because only marker tokens are rewritten. New-format prompts never
    enter this path."""

    from models.reference_markers import CANONICAL_MARKER_PATTERN

    def bump(match: "re.Match[str]") -> str:
        ordinal = int(match.group(1))
        if ordinal < 2:
            return match.group(0)
        return match.group(0).replace(
            match.group(1),
            str(ordinal - 1),
            1,
        )

    return CANONICAL_MARKER_PATTERN.sub(bump, prompt or "")


def _rebuild_prompt_without_anchor(
    prompt: str,
    version_ids: Sequence[str],
    storyboard_id: str | None,
    *,
    project: Project,
    element_id: str,
) -> str:
    """Non-anchored paths cancel ONLY structured instructions.

    Author narrative is never regex-deleted: the sole removal is the
    anchor prefix this module injects on anchored paths (exact
    constant match). Author-written last-frame directives stay in the
    prose but are explicitly VOIDED by a structured note, and the
    runtime roles block states what each 图N really is; a marker that
    references nothing still fails bounds validation loudly, handing
    the prompt back to the Agent instead of silently shipping a
    mismatch.
    """

    cancelled = False
    if prompt.startswith(_INJECTED_ANCHOR_PREFIX):
        prompt = prompt[len(_INJECTED_ANCHOR_PREFIX) :]
        cancelled = True
    image_count = sum(
        1
        for _id, kind, _ordinal in number_references_by_media(
            project,
            version_ids,
        )
        if kind == "image"
    )
    images_ship = image_count > 0
    has_lastframe = _LAST_FRAME_CLAUSE_RE.search(prompt) is not None
    opening_dangles = (
        _OPENING_ALIGN_RE.search(prompt) is not None and not images_ship
    )
    if prompt.startswith(_LEGACY_ROLE_PREFIX):
        # Remove exactly the fixed guide-generated role line (plus one
        # leading separator), never a regex guess at where the
        # author's action begins.
        prompt = prompt[len(_LEGACY_ROLE_PREFIX) :].lstrip("，,；;、 ")
        from models.reference_markers import canonical_marker_indices

        if 1 in canonical_marker_indices(prompt):
            # The old 图1 (virtual last frame) is still referenced by
            # authored sentences: remapping around them would silently
            # rebind those references onto another image. Mark the
            # lost frame as missing and hand back for Agent repair.
            raise ValidationError(
                f"项目 {project.project_id} 元素 {element_id} 的旧格式"
                "提示词仍含指向上一段末帧的 [Image 1] 引用，而本段不"
                "发送该帧；系统不会静默重绑素材。下一步：交 Agent 依"
                "据职责表把这些引用改写到实际素材编号或删除后重新"
                "派发。",
            )
        prompt = _remap_legacy_image_refs_after_prefix_removal(prompt)
        cancelled = True
        has_lastframe = _LAST_FRAME_CLAUSE_RE.search(prompt) is not None
        report_error(
            component="continuity.prompt",
            code="CONTINUITY_LEGACY_PROMPT_REPAIRED",
            message=(
                "已移除旧指南生成的固定末帧职责行并降级为普通 R2V；"
                "请按新版 skill 更新提示词（末帧职责由系统结构化"
                "渲染，不再手写）。"
            ),
            project_id=str(project.project_id),
            element_id=element_id,
        )
    if has_lastframe and not images_ship:
        # Free-form legacy directives stay in the prose untouched:
        # only the Agent can decide which part of a sentence is
        # narrative. Hand back loudly with a repair hint instead of
        # shipping a dangling marker or deleting author text.
        raise ValidationError(
            f"项目 {project.project_id} 元素 {element_id} 的提示词仍含"
            "手写的上一段末帧职责句，而本段不发送任何参考图，无法安全"
            "组装请求。下一步：按新版 skill 删除正文中的末帧职责句"
            "（末帧职责由系统结构化渲染，不再手写），或为该段补选"
            "分镜/参考图后重新派发；正文其余部分已原样保留，系统不"
            "猜测删除。",
        )
    if cancelled or has_lastframe or opening_dangles:
        note = (
            "\n[衔接说明] 本段不发送上一段末帧引用；"
            + "叙述中关于上一段末帧的指令作废，"
            + "参考职责以职责表为准。"
        )
        if opening_dangles:
            note += "本段未发送任何参考图，开场对齐指令亦作废。"
        prompt += note
    return prompt


def _storyboard_swap_for(
    prompt: str,
    project: Project,
    timeline: Timeline,
    element: TimelineElement,
) -> str | None:
    """Canonical swap decision, shared by the resolver and the
    fingerprint: the storyboard leaves the stack only when the tier is
    not ``combined`` and its authored sentence is rewritable. One
    implementation keeps the wire payload and the claim digest from
    drifting apart."""

    from models.video_task_plans import r2v_continuity_storyboard_need

    if (
        r2v_continuity_storyboard_need(project, timeline, element)
        == "combined"
    ):
        return None
    return _storyboard_anchor_swap(prompt)


def descriptor_phase_for_recovery(
    scratch_loaded: bool,
    descriptor: object,
) -> bool:
    """Whether the trimmed phase marker may be inherited.

    Only when the descriptor's OWN bytes were loaded: a missing
    scratch falls through to a fresh provider download, which is
    always untrimmed no matter what the stale descriptor recorded.
    """

    return bool(
        scratch_loaded
        and isinstance(descriptor, Mapping)
        and descriptor.get("trimmed"),
    )


def extend_trim_decision(
    trim_offset: float,
    descriptor_trimmed: bool,
) -> tuple[bool, bool]:
    """(should_cut, subtract_duration) for one materialize attempt.

    A recovered scratch descriptor may already hold the TRIMMED bytes:
    duration checks cannot tell 8s-from-13 from a native 8s, so the
    phase is recorded explicitly and recovery reuses the cut instead of
    paying it twice.
    """

    if trim_offset <= 0:
        return False, False
    if descriptor_trimmed:
        return False, True
    return True, True


def _voice_audio_seconds(project: Project, creation: Any) -> float:
    """Seconds of reference audio the enrolled character voices consume.

    wan3.0 caps reference audio at five clips / 15s TOTAL; the shared
    bgm excerpt must fit what the voices leave over, not just the slot
    count. Unknown durations count zero — the excerpt verification
    still bounds whatever actually ships.
    """

    total = 0.0
    seen: set[str] = set()
    for ref in getattr(creation, "character_refs", None) or []:
        entity = project.visual.entities.items.get(ref)
        voice = getattr(entity, "voice", None) if entity is not None else None
        sample_id = str(
            getattr(voice, "sample_source_version_id", "") or "",
        )
        if not sample_id or sample_id in seen:
            continue
        seen.add(sample_id)
        version = project.assets.source_versions_by_id.get(sample_id)
        seconds = getattr(version, "duration_seconds", None)
        if isinstance(seconds, (int, float)):
            total += float(seconds)
    return total


def _resolve_bgm_reference(
    project: Project,
    timeline: Timeline,
    project_root: Path,
    *,
    voice_count: int,
    voice_seconds: float = 0.0,
    element_id: str,
) -> str:
    """Locate the shared project BGM to ship as one extra reference_audio.

    Every segment generating its own native BGM makes the soundtrack
    jump at each seam. When the author carries one enabled ``role=bgm``
    audio Element on the timeline (user-uploaded配乐), the same clip
    rides along EVERY segment as ``reference_audio`` so the provider
    syncs each render to it. Skips are conservative and leave the
    segment on native audio (status quo): no bgm Element, unreadable
    file, or a model whose standalone audio-reference slots cannot fit
    ``voices + 1`` within the documented budget.
    """

    from models import config as model_config
    from models.video_capabilities import (
        REFERENCE_VOICE_STANDALONE,
        video_reference_voice_support,
    )
    from services.project_files.models import AudioCreation

    bgm_creation = None
    for item in timeline.elements_by_id.values():
        creation = item.creation
        if (
            item.enabled
            and isinstance(creation, AudioCreation)
            and creation.role == "bgm"
        ):
            bgm_creation = creation
            break
    if bgm_creation is None:
        return ""
    version = project.assets.source_versions_by_id.get(
        bgm_creation.source_asset_version_id,
    )
    indexed = (
        project.assets.files_by_id.get(version.file_id)
        if version is not None
        else None
    )
    if indexed is None:
        return ""
    try:
        path = _indexed_path(project_root, indexed)
    except (NotFoundError, ValidationError, OSError):
        return ""
    if not path.exists():
        return ""
    support = video_reference_voice_support(
        model_config.get_video_model_name(),
        model_config.get_video_backend(),
    )
    shape, budget = support or ("", 0)
    if shape != REFERENCE_VOICE_STANDALONE or voice_count + 1 > budget:
        report_error(
            component="r2v.bgm",
            code="BGM_SKIPPED_BUDGET",
            message=(
                "reference_audio 槽位预算不足以同时装角色音色与配乐，"
                "本段跳过 bgm 参考音频（音乐退回原生生成）"
            ),
            project_id=str(project.project_id),
            element_id=element_id,
            voice_count=voice_count,
            audio_budget=budget,
        )
        return ""
    remaining = 15.0 - max(voice_seconds, 0.0)
    if remaining < 1.0:
        report_error(
            component="r2v.bgm",
            code="BGM_SKIPPED_AUDIO_BUDGET",
            message=(
                "角色音色参考音频已占满 15s 总时长预算，"
                "本段跳过 bgm 参考音频（音乐退回原生生成）"
            ),
            project_id=str(project.project_id),
            element_id=element_id,
            voice_count=voice_count,
            voice_seconds=voice_seconds,
        )
        return ""
    try:
        return _bgm_excerpt(
            project,
            project_root,
            path,
            element_id,
            max_seconds=remaining,
            source_identity=str(
                getattr(version, "checksum", "") or "",
            ),
        )
    except ValidationError as error:
        # An excerpt that cannot be cut or verified must not block the
        # render: skip the bgm reference (native audio continues) and
        # surface the reason, rather than shipping an over-budget file.
        report_error(
            component="r2v.bgm",
            code="BGM_EXCERPT_SKIPPED",
            message=f"背景音乐裁剪未达预算要求，本段跳过 bgm 参考音频: {error}",
            project_id=str(project.project_id),
            element_id=element_id,
        )
        return ""


def _bgm_excerpt(
    project: Project,
    project_root: Path,
    source: Path,
    element_id: str,
    *,
    max_seconds: float = 15.0,
    source_identity: str = "",
) -> str:
    """Return a file URI for the first <=max_seconds of the bgm track.

    Wan3.0 caps reference audio at five clips / 15s TOTAL, so the
    budget is what the enrolled character voices leave over. A track
    that cannot be probed, cut or verified within budget raises: the
    untouched full-length source must never ship silently (the caller
    turns that into a loud skip, never a fallback to the raw file).

    The cache identity covers the source version's checksum, the file
    name and the EXACT budget: truncating 10.75s and 10.2s must not
    collide into one integer-keyed file, and a cache hit is only ever
    served for bytes that passed verification before being stored.
    """

    cache_dir = project_root / "runtime" / "media-cache" / "bgm"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(
        f"{source_identity}|{source.name}|{max_seconds:.2f}".encode(),
    ).hexdigest()[:16]
    target = cache_dir / f"bgm-{key}-head{max_seconds:.2f}{source.suffix}"
    if target.exists():
        return target.resolve().as_uri()
    from services.runtime_files.runtime_dependencies import (
        resolve_ffmpeg,
        resolve_ffprobe,
    )
    from services.media_files.segment_continuity import _probe_duration

    ffmpeg_path = resolve_ffmpeg()
    ffprobe_path = resolve_ffprobe(ffmpeg_path=ffmpeg_path)
    if not ffmpeg_path:
        raise ValidationError("背景音乐裁剪需要 ffmpeg，当前未安装。")
    tmp: Path | None = None
    try:
        if ffprobe_path and _probe_duration(source, ffprobe_path) <= (
            max_seconds + 0.01
        ):
            return source.resolve().as_uri()
        # One independent temp file per cut: segments resolve bgm in
        # parallel threads (asyncio.to_thread), and a pid-only name
        # made two threads write, verify and REPLACE the same path —
        # the second replace then died on a missing file. mkstemp
        # keeps the real audio suffix (ffmpeg infers the muxer from
        # the OUTPUT extension) with a unique name.
        import tempfile

        handle, tmp_name = tempfile.mkstemp(
            prefix=f"{key}.",
            suffix=target.suffix,
            dir=cache_dir,
        )
        os.close(handle)
        tmp = Path(tmp_name)
        # Re-encode (NOT stream copy): packet-boundary truncation of a
        # 16s WAV asked for 15s measured 15.04s — either over budget
        # or tripping the verification below. Encoding to the
        # container's default codec cuts at the exact frame.
        subprocess.run(
            [
                ffmpeg_path,
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-t",
                str(max_seconds),
                str(tmp),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except (
        RuntimeError,
        ValidationError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        # A source-probe failure happens BEFORE tmp exists; cleanup
        # must stay None-safe or the wrapped error becomes an
        # UnboundLocalError that no caller catches.
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise ValidationError(
            f"背景音乐裁剪失败，无法在 {max_seconds:g}s 预算内发送: {error}",
        ) from error
    assert tmp is not None
    # Verify the excerpt really fits the budget BEFORE publishing the
    # cache entry: whatever lands at `target` has always passed.
    if ffprobe_path:
        try:
            cut_seconds = _probe_duration(tmp, ffprobe_path)
        except RuntimeError as error:
            tmp.unlink(missing_ok=True)
            raise ValidationError(
                f"背景音乐裁剪结果无法探测: {error}",
            ) from error
        if cut_seconds > max_seconds + 0.01:
            tmp.unlink(missing_ok=True)
            raise ValidationError(
                f"背景音乐裁剪结果仍超过 {max_seconds:g}s 预算。",
            )
    try:
        os.replace(tmp, target)
    except OSError as error:
        tmp.unlink(missing_ok=True)
        if target.exists():
            # A parallel cut of the same identity won the race: the
            # verified bytes are already published; reuse them.
            return target.resolve().as_uri()
        raise ValidationError(
            f"背景音乐裁剪结果发布失败: {error}",
        ) from error
    return target.resolve().as_uri()


def _build_task_plan(
    project: Project,
    project_root: Path,
    timeline: Timeline,
    element: TimelineElement,
    *,
    base_mode: str,
    duration_seconds: int,
    ratio: str,
) -> VideoTaskPlan:
    """Prepare the model-authored method and validate its concrete inputs."""
    from models import config as model_config
    from models.video_capabilities import supports_video_continuation
    from services.file_agent_runtime.work_graph import probe_ffmpeg_ready
    from services.media_files.segment_continuity import (
        _probe_duration,
        _resolve_prev_video_path,
    )
    from services.runtime_files.runtime_dependencies import (
        resolve_ffmpeg,
        resolve_ffprobe,
    )

    plan = element.creation.generation_plan
    if plan is None or plan.method == "r2v":
        return VideoTaskPlan(
            kind=VideoTaskKind(base_mode),
            ratio=ratio,
            submit_duration_seconds=duration_seconds,
        )

    predecessor = planned_predecessor(timeline, element)
    version = (
        resolve_predecessor_video_version(project, predecessor)
        if predecessor is not None
        else None
    )
    if not version:
        raise ValidationError("生成方案引用的前段视频尚未发布")
    if not probe_ffmpeg_ready():
        raise ValidationError(
            "当前生成方案需要 ffmpeg/ffprobe；修复依赖或重选生成方案"
        )
    if plan.method == "r2v_last_frame":
        return VideoTaskPlan(
            kind=VideoTaskKind.R2V,
            predecessor_version_id=version,
            ratio=ratio,
            submit_duration_seconds=duration_seconds,
            strong_continuity=True,
        )

    if not supports_video_continuation(
        model_config.get_video_model_name(),
        model_config.get_video_backend(),
    ):
        raise ValidationError("当前视频模型不支持视频延长；请重选生成方案")
    source = _resolve_prev_video_path(project, project_root, version)
    ffprobe = resolve_ffprobe(ffmpeg_path=resolve_ffmpeg())
    if not ffprobe or not source.exists():
        raise ValidationError(
            "无法读取延长素材时长；请检查前段视频或重选生成方案"
        )
    material_seconds = _probe_duration(source, ffprobe)
    budget = (
        WAN_30_MAX_INPUT_PLUS_OUTPUT_SECONDS - WAN_30_EXTEND_HEADROOM_SECONDS
    )
    if (
        not 0 < material_seconds <= WAN_30_MAX_REFERENCE_VIDEO_SECONDS
        or math.ceil(material_seconds + duration_seconds) > budget
    ):
        raise ValidationError("延长素材或总时长超过模型限制；请重选生成方案")
    return VideoTaskPlan(
        kind=VideoTaskKind.VIDEO_EXTEND,
        predecessor_version_id=version,
        material_duration_seconds=material_seconds,
        ratio="adaptive",
        submit_duration_seconds=math.ceil(material_seconds + duration_seconds),
        trim_offset_seconds=material_seconds,
    )


def _resolve_extend_request(
    *,
    project: Project,
    project_root: Path,
    element: TimelineElement,
    element_id: str,
    target_ref: str,
    plan: VideoTaskPlan,
    prompt: str,
    resolution: str,
    watermark: bool,
    generate_audio: bool,
    duration_seconds: int,
) -> _ResolvedR2V:
    """Assemble the first-class VIDEO_EXTEND continuation submission.

    ``input.media`` carries ONLY the predecessor's published video as
    ``reference_video`` plus an extend-intent prompt — no reference
    stack, no synthetic ids, no last-frame image — so the provider
    routes to its video-extension task and continues the predecessor's
    tail (picture, camera motion and audio). ``ratio`` follows the
    material (adaptive) and the provider duration is ``material + new``;
    the returned video's material prefix is trimmed before publication
    so the segment artifact never duplicates its predecessor.
    """

    from models.reference_markers import CANONICAL_MARKER_PATTERN

    if CANONICAL_MARKER_PATTERN.search(prompt):
        raise ValidationError(
            "视频延长只发送前段视频，提示词不能引用未发送的图片"
        )
    prev_version = plan.predecessor_version_id
    video_url, checksum, ref, entry = _resolve_single_media_version(
        project=project,
        project_root=project_root,
        version_id=prev_version,
        media_prefix="video/",
        label="extendMaterial",
    )
    extend_prompt = "以上一段视频为延长素材，随后展开本段内容：\n" + prompt
    return _ResolvedR2V(
        target_ref=target_ref,
        element_id=element_id,
        element_label=element.label,
        prompt=extend_prompt,
        ratio=plan.ratio or "adaptive",
        duration_seconds=duration_seconds,
        resolution=resolution,
        watermark=watermark,
        generate_audio=generate_audio,
        reference_version_ids=(prev_version,),
        reference_urls=(video_url,),
        reference_checksums=(checksum,),
        provenance_refs=(ref,),
        read_set=(entry,),
        reference_voice_urls=(),
        slot_id=f"element:{element_id}:main",
        slot_kind="element_video",
        owner_ref=f"element:{element_id}",
        mode=VideoTaskKind.VIDEO_EXTEND.value,
        continuity_shape=VideoTaskKind.VIDEO_EXTEND.value,
        continuity_trim_offset=float(plan.trim_offset_seconds or 0.0),
        provider_duration_seconds=plan.submit_duration_seconds,
    )


def _i2v_auto_first_frame_entry(
    *,
    project: Project,
    project_root: Path,
    timeline: Timeline,
    element: TimelineElement,
) -> ContinuityEntry | None:
    """Adopt the predecessor's last frame as the I2V first frame.

    Returns ``None`` when there is no published predecessor video or
    the extract fails; the caller then raises the explicit-input
    ValidationError. Every skip is observable: design-level skips are
    traced inside ``extract_continuity_artifacts``, extraction failures
    report ``CONTINUITY_EXTRACTION_FAILED``, and a bundle without a
    last frame violates the extraction invariant and reports
    ``CONTINUITY_LAST_FRAME_MISSING``.
    """

    prev = planned_predecessor(timeline, element)
    if prev is None:
        return None
    if not resolve_predecessor_video_version(project, prev):
        return None
    try:
        bundle = extract_continuity_artifacts(
            project=project,
            project_root=project_root,
            timeline=timeline,
            element=element,
            predecessor=prev,
        )
    except (RuntimeError, ValidationError) as error:
        report_error(
            component="i2v.continuity",
            code="CONTINUITY_EXTRACTION_FAILED",
            message=str(error),
            error=error,
            project_id=str(project.project_id),
            element_id=element.element_id,
        )
        return None
    if bundle is None:
        return None
    last_frame = bundle.entry("last_frame")
    if last_frame is None:
        report_error(
            component="i2v.continuity",
            code="CONTINUITY_LAST_FRAME_MISSING",
            message="段间衔接 bundle 缺少 last_frame，所选的前段末帧不可用",
            project_id=str(project.project_id),
            element_id=element.element_id,
            prev_element_id=prev.element_id,
        )
        return None
    return last_frame


def _validate_authored_marker_bounds(
    prompt: str,
    project: Project,
    version_ids: Sequence[str],
) -> None:
    """Reject ``[Image N]`` markers past the plan's IMAGE sequence.

    The bound comes from the actual image sequence, including an explicitly
    selected anchor. Video references use separate ordinals.
    """

    plan_kinds = [
        kind
        for _id, kind, _ordinal in number_references_by_media(
            project,
            version_ids,
        )
    ]
    image_bound = plan_kinds.count("image")
    if any(
        not 1 <= index <= image_bound
        for index in canonical_marker_indices(prompt)
    ):
        raise ValidationError(
            "参考图编号超出本次实际图片序列；请核对 [Image N] 与参考图片列表"
            "（视频参考按 视频N 独立编号、不计入图N），本次未调用视频模型。",
        )


def _resolve_request(
    *,
    snapshot: ProjectSnapshot,
    project_root: Path,
    target_ref: str,
    arguments: Mapping[str, Any],
) -> _ResolvedR2V:
    project = snapshot.project
    from services.project_files.production_stage import (
        assert_media_production_allowed,
    )

    assert_media_production_allowed(project)
    element_id = _target_element_id(target_ref)
    timeline, element = find_timeline_element(project, element_id)
    creation = element.creation
    if isinstance(creation, R2VCreation):
        assert_r2v_prompt_sync(
            project,
            timeline.timeline_id,
            element_id,
            stage="video",
        )
    mode = _validated_request_mode(arguments)
    # Each creation type declares exactly one generation mode; the request
    # mode must match it so a t2v element can never be submitted as r2v.
    creation_by_mode = {
        "r2v": R2VCreation,
        "video_edit": R2VCreation,
        "t2v": T2VCreation,
        "i2v": I2VCreation,
    }
    expected = creation_by_mode.get(mode)
    if expected is None or not isinstance(creation, expected):
        raise ValidationError(
            f"mode={mode} 与目标 Element 的 creation.type={creation.type} 不匹配；"
            "生成模式由 Element 的 creation 类型声明，不可在提交时替换",
        )
    first_frame_ref = str(arguments.get("firstFrameRef") or "").strip()
    if mode == "i2v" and not first_frame_ref:
        # The declared first frame on the element is the default input.
        first_frame_ref = str(
            getattr(creation, "first_frame_version_id", None) or "",
        ).strip()
    last_frame_ref = ""
    if mode == "i2v":
        last_frame_ref = str(
            getattr(creation, "last_frame_version_id", None) or "",
        ).strip()
    i2v_continuity_entry = None
    if mode == "i2v" and not first_frame_ref:
        # The only alternate source is an explicitly selected predecessor.
        i2v_continuity_entry = _i2v_auto_first_frame_entry(
            project=project,
            project_root=project_root,
            timeline=timeline,
            element=element,
        )
        if i2v_continuity_entry is not None:
            first_frame_ref = i2v_continuity_entry.reference_id
    video_ref = str(arguments.get("videoRef") or "").strip()
    if mode == "i2v" and not first_frame_ref:
        raise ValidationError(
            "i2v 模式必须提供 firstFrameRef（exact 图片 version id）或填写 "
            "creation.first_frame_version_id / predecessor_element_id。若已指定前段，"
            "请确认视频已发布且 FFmpeg 可提取末帧。",
        )
    if mode == "video_edit" and not video_ref:
        raise ValidationError(
            "video_edit 模式必须提供 videoRef（exact 视频 version id）",
        )
    if (
        mode == "i2v"
        and arguments.get("firstFrameRef")
        and creation.predecessor_element_id
    ):
        raise ValidationError(
            "请求首帧与项目指定的前段冲突；请先修改项目生成方案"
        )
    if first_frame_ref and mode != "i2v":
        raise ValidationError("firstFrameRef 仅用于 i2v 模式")
    if video_ref and mode != "video_edit":
        raise ValidationError("videoRef 仅用于 video_edit 模式")

    storyboard_id: str | None = None
    if mode == "r2v" and uses_storyboard(creation):
        selected = selected_element_output(project, element, "storyboard")
        if selected is None:
            raise ValidationError("R2V 生成方案尚未选择分镜产物")
        storyboard_id = selected[1]
        storyboard = project.assets.artifact_versions_by_id.get(storyboard_id)
        if storyboard is None:
            raise StorageIntegrityError(
                "selected storyboard ArtifactVersion 不存在"
            )
        if storyboard.owner_ref != f"element:{element_id}":
            raise StorageIntegrityError(
                "selected storyboard 不属于目标 Element"
            )

    prompt = str(arguments.get("prompt") or creation.video_prompt).strip()
    if not prompt:
        raise ValidationError("生成 R2V 视频需要 video prompt")
    if isinstance(creation, R2VCreation) and missing_narrative_dialogue(
        creation.narrative,
        prompt,
    ):
        raise ValidationError(
            "视频提示词遗漏了片段内容中的对白或旁白原文，请补齐后重新生成。",
        )
    if "referenceImageUrls" in arguments or "referenceVersionIds" in arguments:
        raise ValidationError(
            "R2V reference 只能来自 project.json 的 exact version 列表",
        )
    duration_seconds = _duration(
        element.span.duration_tick / timeline.ticks_per_second,
    )
    time_error = video_prompt_time_error(prompt, duration_seconds)
    if time_error:
        raise ValidationError(time_error)
    ratio = str(
        arguments.get("ratio") or project.settings.aspect_ratio,
    ).strip()
    resolution = str(
        arguments.get("resolution") or project.settings.resolution or "720P",
    ).strip()
    if not ratio or not resolution:
        raise ValidationError("R2V ratio/resolution 不能为空")
    # No provider watermark by default; enabled only on explicit request.
    watermark = arguments.get("watermark", False)
    generate_audio = arguments.get("generateAudio", creation.generate_audio)
    if not isinstance(watermark, bool) or not isinstance(generate_audio, bool):
        raise ValidationError("R2V watermark/generateAudio 必须是 boolean")

    task_plan: VideoTaskPlan | None = None
    if mode == "r2v":
        # The planning layer may route a same-scene continuation onto a
        # first-class continuation task instead of the omni reference
        # stack. Routing happens after the base-mode/creation check so
        # the author's declared creation type still gates admission.
        task_plan = _build_task_plan(
            project,
            project_root,
            timeline,
            element,
            base_mode="r2v",
            duration_seconds=duration_seconds,
            ratio=ratio,
        )
        if task_plan.kind is VideoTaskKind.VIDEO_EXTEND:
            return _resolve_extend_request(
                project=project,
                project_root=project_root,
                element=element,
                element_id=element_id,
                target_ref=target_ref,
                plan=task_plan,
                prompt=prompt,
                resolution=resolution,
                watermark=watermark,
                generate_audio=generate_audio,
                duration_seconds=duration_seconds,
            )
    bgm_reference_url = ""
    if mode == "r2v":
        version_ids = video_reference_plan(project, element)
        strong_anchor_entry = None
        if task_plan is not None and task_plan.strong_continuity:
            strong_anchor_entry = _i2v_auto_first_frame_entry(
                project=project,
                project_root=project_root,
                timeline=timeline,
                element=element,
            )
            if strong_anchor_entry is None:
                raise ValidationError(
                    "无法提取生成方案指定的末帧；请修复素材或重选方案"
                )
            version_ids = (strong_anchor_entry.reference_id, *version_ids)
        if creation.generation_plan is None and strong_anchor_entry is None:
            # Off/weak/plain and failed-extraction paths all ship
            # without an anchor: rebuild the prompt against the stack
            # that actually rides (storyboard clause renamed to its
            # real ordinal, last-frame sentences gone).
            prompt = _rebuild_prompt_without_anchor(
                prompt,
                version_ids,
                storyboard_id,
                project=project,
                element_id=element_id,
            )
        _validate_authored_marker_bounds(prompt, project, version_ids)
        _assert_r2v_reference_budget(
            project,
            version_ids,
            project_root=project_root,
            output_duration_seconds=duration_seconds,
            continuity_media=(
                (("image", None),) if strong_anchor_entry is not None else ()
            ),
        )
        real_version_ids = tuple(
            item for item in version_ids if not item.startswith("continuity:")
        )
        urls, checksums, provenance, read_set = _resolve_reference_versions(
            project=project,
            project_root=project_root,
            version_ids=real_version_ids,
        )
        if strong_anchor_entry is not None:
            anchor = strong_anchor_entry
            urls.insert(0, anchor.url)
            checksums.insert(0, anchor.checksum)
            provenance.insert(0, anchor.provenance_ref)
            read_set.insert(
                0,
                {
                    "ref": anchor.provenance_ref,
                    "versionId": anchor.reference_id,
                    "fileId": None,
                    "checksum": anchor.checksum,
                    "sourceUrl": anchor.url,
                },
            )
            if storyboard_id and storyboard_id not in version_ids:
                # The staleness gate demands the storyboard selection
                # in provenance; the swap drops it from the SENT stack
                # only. Omitting this re-triggers the auto_approve
                # re-bill loop (field case s5, 2026-11-24).
                provenance.append(f"artifact-version:{storyboard_id}")
        voice_urls, voice_read_entries = _resolve_reference_voices(
            project=project,
            project_root=project_root,
            creation=creation,
            version_ids=real_version_ids,
        )
        read_set = [*read_set, *voice_read_entries]
        # Continuity is the planning layer's job: a plain r2v stack
        # carries no synthetic anchors, a strong-continuity one leads
        # with the promoted last frame, and VIDEO_EXTEND returned above.
        prompt = _append_reference_role_mapping(
            prompt,
            project,
            version_ids,
            storyboard_id=storyboard_id,
            continuity_labels=(
                {
                    strong_anchor_entry.reference_id: "生成方案指定的前段末帧",
                }
                if strong_anchor_entry is not None
                else None
            ),
        )
        if strong_anchor_entry is not None:
            prompt = f"图1是所选前段末帧，参考要求：{creation.generation_plan.anchor_instruction}\n{prompt}"
        bgm_reference_url = _resolve_bgm_reference(
            project,
            timeline,
            project_root,
            voice_count=len([item for item in voice_urls if item]),
            voice_seconds=_voice_audio_seconds(project, creation),
            element_id=element_id,
        )
    prompt = _render_reference_markers(
        media_prompt_entity_names(prompt, project),
    )
    if mode != "r2v":
        version_ids = ()
        urls, checksums, provenance, read_set = [], [], [], []
        voice_urls = ()

    first_frame_url = ""
    video_url = ""
    if mode == "i2v":
        if i2v_continuity_entry is None and not first_frame_ref:
            # Routed here by the planner (same-scene continuation with no
            # usable extension contract) or an authored i2v with no
            # frame: adopt the predecessor's last frame as first frame.
            i2v_continuity_entry = _i2v_auto_first_frame_entry(
                project=project,
                project_root=project_root,
                timeline=timeline,
                element=element,
            )
            if i2v_continuity_entry is not None:
                first_frame_ref = i2v_continuity_entry.reference_id
        if i2v_continuity_entry is not None:
            # Synthetic continuity IDs never appear in the Asset Index;
            # their file:// URL and checksum already come straight from
            # the last-frame extract.
            first_frame_url = i2v_continuity_entry.url
            checksum = i2v_continuity_entry.checksum
            ref = i2v_continuity_entry.provenance_ref
            entry = i2v_continuity_entry.as_read_entry()
        else:
            (
                first_frame_url,
                checksum,
                ref,
                entry,
            ) = _resolve_single_media_version(
                project=project,
                project_root=project_root,
                version_id=first_frame_ref,
                media_prefix="image/",
                label="firstFrameRef",
            )
        version_ids = (first_frame_ref,)
        checksums = [checksum]
        provenance = [ref]
        read_set = [entry]
        last_frame_url = ""
        if last_frame_ref:
            from models import config as _model_config
            from models.video_capabilities import supports_last_frame

            if supports_last_frame(
                _model_config.get_video_model_name(),
                _model_config.get_video_backend(),
            ):
                (
                    last_frame_url,
                    last_checksum,
                    last_ref,
                    last_entry,
                ) = _resolve_single_media_version(
                    project=project,
                    project_root=project_root,
                    version_id=last_frame_ref,
                    media_prefix="image/",
                    label="lastFrameRef",
                )
                version_ids = (*version_ids, last_frame_ref)
                checksums.append(last_checksum)
                provenance.append(last_ref)
                read_set.append(last_entry)
            else:
                # No native last-frame parameter on this model: fall
                # back to the explicit first-frame contract and say so
                # loudly — never fake a constrained tail frame through
                # a plain reference image.
                report_error(
                    component="i2v.last_frame",
                    code="I2V_LAST_FRAME_UNSUPPORTED",
                    message=(
                        "当前视频模型不支持原生目标尾帧参数，"
                        "本段沿用首帧回退继续生成。"
                    ),
                    project_id=str(project.project_id),
                    element_id=element_id,
                )
    elif mode == "video_edit":
        video_url, checksum, ref, entry = _resolve_single_media_version(
            project=project,
            project_root=project_root,
            version_id=video_ref,
            media_prefix="video/",
            label="videoRef",
        )
        _assert_video_edit_input_duration(project, project_root, video_ref)
        version_ids = (video_ref,)
        checksums = [checksum]
        provenance = [ref]
        read_set = [entry]

    return _ResolvedR2V(
        target_ref=target_ref,
        element_id=element_id,
        element_label=element.label,
        prompt=prompt,
        ratio=ratio,
        duration_seconds=duration_seconds,
        resolution=resolution,
        watermark=watermark,
        generate_audio=generate_audio,
        reference_version_ids=version_ids,
        reference_urls=tuple(urls),
        reference_checksums=tuple(checksums),
        provenance_refs=tuple(provenance),
        read_set=tuple(read_set),
        reference_voice_urls=tuple(voice_urls),
        slot_id=f"element:{element_id}:main",
        slot_kind="element_video",
        owner_ref=f"element:{element_id}",
        mode=mode,
        continuity_shape="",
        continuity_trim_offset=0.0,
        provider_duration_seconds=None,
        bgm_reference_url=bgm_reference_url,
        first_frame_version_id=first_frame_ref if mode == "i2v" else "",
        first_frame_url=first_frame_url,
        last_frame_version_id=(
            last_frame_ref if mode == "i2v" and last_frame_url else ""
        ),
        last_frame_url=(last_frame_url if mode == "i2v" else ""),
        authored_last_frame_version_id=(
            last_frame_ref if mode == "i2v" else ""
        ),
        video_version_id=video_ref if mode == "video_edit" else "",
        video_url=video_url,
    )


def _resolve_s2v_request(
    *,
    snapshot: ProjectSnapshot,
    project_root: Path,
    target_ref: str,
    arguments: Mapping[str, Any],
) -> _ResolvedR2V:
    """Resolve one s2v_generation call onto the R2V durable machinery.

    The digital-human video publishes into the same element main slot as an
    r2v render; inputs are exact versions (character image + TTS audio).
    """

    from models.s2v_model import normalize_s2v_resolution
    from services.project_files.production_stage import (
        assert_media_production_allowed,
    )

    project = snapshot.project
    assert_media_production_allowed(project)
    element_id = _target_element_id(target_ref)
    _, element = find_timeline_element(project, element_id)
    creation = element.creation
    if not isinstance(creation, S2VCreation):
        raise ValidationError(
            "仅 creation.type=s2v 的 Element 可以生成数字人视频",
        )
    # Explicit refs win; the element's declared portrait/audio are the
    # defaults so the tool call carries no redundant arguments.
    image_ref = str(
        arguments.get("characterImageRef")
        or creation.portrait_version_id
        or "",
    ).strip()
    audio_ref = str(
        arguments.get("audioAssetRef") or creation.audio_version_id or "",
    ).strip()
    if not image_ref:
        raise ValidationError(
            "s2v_generation 需要人像图：传 characterImageRef 或在 "
            "creation.portrait_version_id 声明（exact 图片 version id）",
        )
    if not audio_ref:
        raise ValidationError(
            "s2v_generation 需要驱动音频：传 audioAssetRef 或在 "
            "creation.audio_version_id 声明（可直接使用 tts_generation "
            "产出的 version）",
        )
    resolution = normalize_s2v_resolution(
        str(arguments.get("resolution") or "480P"),
    )
    (
        image_url,
        image_checksum,
        image_prov,
        image_entry,
    ) = _resolve_single_media_version(
        project=project,
        project_root=project_root,
        version_id=image_ref,
        media_prefix="image/",
        label="characterImageRef",
    )
    (
        audio_url,
        audio_checksum,
        audio_prov,
        audio_entry,
    ) = _resolve_single_media_version(
        project=project,
        project_root=project_root,
        version_id=audio_ref,
        media_prefix="audio/",
        label="audioAssetRef",
    )
    # Task duration metadata follows the (header-corrected) audio duration
    # recorded on the TTS source version; the provider derives the real one.
    audio_version = project.assets.source_versions_by_id.get(audio_ref)
    audio_duration = (
        audio_version.duration_seconds
        if audio_version is not None and audio_version.duration_seconds
        else 5.0
    )
    duration_seconds = max(1, min(60, round(audio_duration)))
    return _ResolvedR2V(
        target_ref=target_ref,
        element_id=element_id,
        element_label=element.label,
        prompt=f"数字人口型合成：{element.label or element_id}",
        ratio=project.settings.aspect_ratio,
        duration_seconds=duration_seconds,
        resolution=resolution,
        watermark=False,
        generate_audio=True,
        reference_version_ids=(image_ref, audio_ref),
        reference_urls=(),
        reference_checksums=(image_checksum, audio_checksum),
        provenance_refs=(image_prov, audio_prov),
        read_set=(image_entry, audio_entry),
        slot_id=f"element:{element_id}:main",
        slot_kind="element_video",
        owner_ref=f"element:{element_id}",
        provider="s2v",
        s2v_image_version_id=image_ref,
        s2v_image_url=image_url,
        s2v_audio_version_id=audio_ref,
        s2v_audio_url=audio_url,
        s2v_resolution=resolution,
    )


def _stage_materialized_video(
    file_store: AssetFileStore,
    materialized: MaterializedVideo,
    *,
    staging_id: str,
) -> StagedAsset:
    """Stream a verified private scratch file into immutable Asset staging."""

    flags = (
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    else:
        value = materialized.path.lstat()
        if is_link_stat(value):
            raise ValidationError("R2V Asset staging refuses symlink output")
    descriptor = os.open(materialized.path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise StorageIntegrityError(
                "R2V materialized output is not a private file",
            )
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            staged = file_store.stage_stream(source, staging_id=staging_id)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        staged.sha256 != materialized.sha256
        or staged.size_bytes != materialized.size_bytes
    ):
        file_store.abandon(staged)
        raise StorageIntegrityError("R2V scratch changed during Asset staging")
    return staged


class FileR2VExecutionService:
    """Durable R2V admission and background supervisor for one data root."""

    def __init__(
        self,
        services: CreatorFileServices,
        *,
        provider: R2VProvider | None = None,
        poll_interval_seconds: float = 2.0,
        poll_lease_seconds: float = 120.0,
        poll_timeout_seconds: float | None = None,
        submit_timeout_seconds: float = _SUBMIT_TIMEOUT_SECONDS,
        submit_claim_seconds: float = _SUBMIT_CLAIM_SECONDS,
        materialize_timeout_seconds: float = _MATERIALIZE_TIMEOUT_SECONDS,
        materialize_claim_seconds: float = _MATERIALIZE_CLAIM_SECONDS,
        materialize_retry_delays: Sequence[float] = _MATERIALIZE_RETRY_DELAYS,
        max_output_bytes: int = _MAX_VIDEO_BYTES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if poll_lease_seconds <= 0 or submit_timeout_seconds <= 0:
            raise ValueError("provider lease durations must be positive")
        if poll_timeout_seconds is not None and (
            poll_timeout_seconds <= 0
            or poll_timeout_seconds >= poll_lease_seconds
        ):
            raise ValueError(
                "poll timeout must be positive and below its lease",
            )
        if submit_claim_seconds <= submit_timeout_seconds:
            raise ValueError(
                "submit claim must exceed the total submit timeout",
            )
        if materialize_timeout_seconds <= 0:
            raise ValueError("materialize_timeout_seconds must be positive")
        if materialize_claim_seconds <= materialize_timeout_seconds:
            raise ValueError(
                "materialize claim must exceed the total materialize timeout",
            )
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        self.services = services
        self.provider = provider or ExistingR2VProvider()
        from services.file_agent_runtime.manual_regeneration_hold import (
            HoldAwareExecutionStore,
        )

        self.executions = HoldAwareExecutionStore(
            services.root,
            on_task_terminal=terminal_release_listener(services),
        )
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.poll_lease_seconds = float(poll_lease_seconds)
        self.poll_timeout_seconds = float(
            (
                poll_timeout_seconds
                if poll_timeout_seconds is not None
                else min(60.0, poll_lease_seconds * 0.8)
            ),
        )
        self.submit_timeout_seconds = float(submit_timeout_seconds)
        self.submit_claim_seconds = float(submit_claim_seconds)
        self.materialize_timeout_seconds = float(materialize_timeout_seconds)
        self.materialize_claim_seconds = float(materialize_claim_seconds)
        self.materialize_retry_delays = tuple(
            float(delay) for delay in materialize_retry_delays
        )
        self.max_output_bytes = int(max_output_bytes)
        self.clock = clock or time.time
        self.owner_id = f"r2v-supervisor-{uuid4().hex}"
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._job_projects: dict[str, str] = {}
        self._terminal_recovery_jobs: dict[
            tuple[str, str],
            asyncio.Task[None],
        ] = {}

    def _state_store(
        self,
        project_id: str,
        task_id: str,
    ) -> AtomicJsonRecordStore[R2VTaskState]:
        return AtomicJsonRecordStore(
            self.services.projects.project_root(project_id)
            / "runtime"
            / "tasks"
            / task_id
            / "r2v-state.json",
            R2VTaskState,
        )

    def _read_state_sync(self, project_id: str, task_id: str) -> R2VTaskState:
        state = self._state_store(project_id, task_id).read()
        if state.project_id != project_id or state.task_id != task_id:
            raise StorageIntegrityError("R2V Runtime state ownership 损坏")
        return state

    def _update_state_sync(
        self,
        project_id: str,
        task_id: str,
        mutator: Callable[[R2VTaskState], R2VTaskState | Mapping[str, Any]],
        *,
        admission: bool = False,
    ) -> R2VTaskState:
        with self.services.projects.lifecycle_lock(project_id, shared=True):
            snapshot = self.services.projects.read(project_id)
            if admission:
                from services.project_files.production_stage import (
                    assert_media_production_allowed,
                )

                assert_media_production_allowed(snapshot.project)

            def update(current: R2VTaskState) -> Any:
                if (
                    current.project_id != project_id
                    or current.task_id != task_id
                ):
                    raise StorageIntegrityError(
                        "R2V Runtime state ownership 损坏",
                    )
                candidate = mutator(current)
                if isinstance(candidate, R2VTaskState):
                    dumped = candidate.model_dump(mode="python")
                else:
                    dumped = dict(candidate)
                dumped["updated_at"] = datetime.now(UTC)
                return dumped

            if admission:
                from services.file_agent_runtime.manual_regeneration_hold import (
                    admission_guard,
                )

                task = self.executions.get_task(project_id, task_id)
                with admission_guard(
                    self.services.root,
                    project_id,
                    node_id=task.metadata.get("automaticWorkNodeId"),
                    _lifecycle_lock_held=True,
                ):
                    return (
                        self._state_store(project_id, task_id)
                        .update(update)
                        .value
                    )
            return self._state_store(project_id, task_id).update(update).value

    @staticmethod
    def _frozen_request(
        run: SpecialistRunRecord,
        task: TaskRecord | None = None,
    ) -> dict[str, Any]:
        raw = None if task is None else task.metadata.get("requestSnapshot")
        if not isinstance(raw, Mapping):
            raw = run.metadata.get("requestSnapshot")
        if not isinstance(raw, Mapping):
            raise StorageIntegrityError(
                "R2V admission 缺少 frozen request snapshot",
            )
        request = _json_mapping(raw, label="R2V frozen request snapshot")
        fingerprint = _fingerprint(request)
        expected = (
            task.request_fingerprint
            if task is not None
            else run.request_fingerprint
        )
        if not expected or fingerprint != expected:
            raise StorageIntegrityError(
                "R2V frozen request fingerprint 不一致"
            )
        if (
            request.get("inputGeneration") != run.input_generation
            or request.get("inputEtag") != run.input_etag
            or request.get("targetRef") not in run.target_refs
        ):
            raise StorageIntegrityError(
                "R2V frozen request 与 Run input 不一致"
            )
        if task is not None and (
            task.run_id != run.run_id
            or task.input_generation != run.input_generation
            or task.input_etag != run.input_etag
        ):
            raise StorageIntegrityError("R2V Task 与 frozen Run 不一致")
        return request

    def _task_from_frozen_run(
        self,
        run: SpecialistRunRecord,
        stable: Mapping[str, str],
        request: Mapping[str, Any],
    ) -> TaskRecord:
        key = str(run.caused_by_request_id or "")
        if not key or stable["run_id"] != run.run_id:
            raise StorageIntegrityError(
                "R2V Run 缺少可恢复的 stable idempotency key",
            )
        command_hash = str(run.metadata.get("commandRequestHash") or "")
        if not command_hash:
            raise StorageIntegrityError("R2V Run 缺少 command request hash")
        candidate = TaskRecord(
            task_id=stable["task_id"],
            project_id=run.project_id,
            round_id=run.round_id,
            run_id=run.run_id,
            kind=TaskKind.R2V_GENERATION,
            request_fingerprint=str(run.request_fingerprint),
            idempotency_key=key,
            input_generation=run.input_generation,
            input_etag=run.input_etag,
            expected_target_version=run.input_etag,
            input_refs=[
                str(request["targetRef"]),
                *[str(item) for item in request["referenceVersionIds"]],
            ],
            read_set=list(request["readSet"]),
            caused_by_request_id=run.caused_by_request_id,
            caused_by_message_id=run.caused_by_message_id,
            caused_by_message_seq=run.caused_by_message_seq,
            review_policy=run.review_policy,
            metadata={
                "commandType": CreatorCommandType.GENERATE_R2V_VIDEO.value,
                "targetRef": str(request["targetRef"]),
                "commandRequestHash": command_hash,
                "slotId": str(request["slotId"]),
                "artifactVersionId": str(request["artifactVersionId"]),
                "fileId": str(request["fileId"]),
                "requestSnapshot": dict(request),
            },
        )
        return self.executions.create_task(candidate)

    def _create_or_validate_state_sync(
        self,
        run: SpecialistRunRecord,
        task: TaskRecord,
        request: Mapping[str, Any],
    ) -> R2VTaskState:
        self._ensure_task_scratch_sync(task)
        candidate = R2VTaskState(
            project_id=task.project_id,
            task_id=task.task_id,
            run_id=run.run_id,
            request_fingerprint=task.request_fingerprint,
            request=dict(request),
        )
        with self.services.projects.lifecycle_lock(
            task.project_id,
            shared=True,
        ):
            self.services.projects.read(task.project_id)
            store = self._state_store(task.project_id, task.task_id)
            created = store.try_create(candidate)
            existing = created.value if created is not None else store.read()
            if (
                existing.request_fingerprint != task.request_fingerprint
                or existing.request != dict(request)
                or existing.run_id != run.run_id
            ):
                raise StorageIntegrityError("R2V Runtime state 与 Task 不一致")
            return existing

    def _ensure_task_scratch_sync(self, task: TaskRecord) -> Path:
        """
        Create and validate the durable scratch owned by one active R2V Task.
        """

        with self.services.projects.lifecycle_lock(
            task.project_id,
            shared=True,
        ):
            self.services.projects.read(task.project_id)
            with media_task_scope(task.task_id, project_id=task.project_id):
                scratch = task_work_root()
            expected = (
                self.services.projects.project_root(task.project_id)
                / "runtime"
                / "task-work"
                / task.task_id
            )
            if scratch != expected:
                raise StorageIntegrityError("R2V Task scratch 归属路径不一致")
            return scratch

    async def _fail_orphan_run(
        self,
        run: SpecialistRunRecord,
        *,
        code: str,
        message: str,
    ) -> None:
        if run.status in TERMINAL_SPECIALIST_STATUSES:
            return
        try:
            await asyncio.to_thread(
                self.executions.transition_run,
                run.project_id,
                run.run_id,
                expected_status=run.status,
                status=SpecialistRunStatus.FAILED,
                updates={
                    "final_marker": "FAILED",
                    "final_summary_text": f"{code}: {message}"[:2000],
                },
            )
        except ExecutionStateConflict:
            pass

    async def _ensure_state_for_task(
        self,
        run: SpecialistRunRecord,
        task: TaskRecord,
    ) -> bool:
        try:
            state = await asyncio.to_thread(
                self._read_state_sync,
                task.project_id,
                task.task_id,
            )
        except RecordNotFoundError:
            state = None
        if state is not None:
            if (
                state.request_fingerprint != task.request_fingerprint
                or _fingerprint(state.request) != task.request_fingerprint
                or state.run_id != run.run_id
            ):
                await self._fail(
                    task,
                    code="R2V_RECOVERY_STATE_MISMATCH",
                    message="R2V durable state ownership/fingerprint 损坏",
                )
                return False
            try:
                await asyncio.to_thread(self._ensure_task_scratch_sync, task)
            except Exception as error:
                await self._fail(
                    task,
                    code="R2V_RECOVERY_SCRATCH_FAILED",
                    message=str(error),
                )
                return False
            return True
        if task.status is TaskStatus.QUEUED:
            try:
                request = self._frozen_request(run, task)
                await asyncio.to_thread(
                    self._create_or_validate_state_sync,
                    run,
                    task,
                    request,
                )
            except Exception as error:
                await self._fail(
                    task,
                    code="R2V_ADMISSION_STATE_REBUILD_FAILED",
                    message=str(error),
                )
                return False
            return True
        if task.status is TaskStatus.RUNNING:
            await self._fail(
                task,
                code="R2V_RECOVERY_STATE_MISSING",
                message=(
                    "RUNNING R2V Task 缺少 durable state；provider 是否已提交不可知，"
                    "拒绝自动重提"
                ),
            )
            return False
        return False

    async def _recover_run_admission_gap(
        self,
        run: SpecialistRunRecord,
    ) -> TaskRecord | None:
        key = str(run.caused_by_request_id or "")
        stable = _ids(run.project_id, key) if key else {}
        if (
            not key
            or stable.get("run_id") != run.run_id
            or run.metadata.get("commandType")
            != CreatorCommandType.GENERATE_R2V_VIDEO.value
        ):
            await self._fail_orphan_run(
                run,
                code="R2V_ADMISSION_IDENTITY_UNRECOVERABLE",
                message="R2V Run 缺少 stable admission identity",
            )
            return None
        try:
            task = await asyncio.to_thread(
                self.executions.get_task,
                run.project_id,
                stable["task_id"],
            )
        except RecordNotFoundError:
            if run.status in TERMINAL_SPECIALIST_STATUSES:
                return None
            try:
                request = self._frozen_request(run)
                task = await asyncio.to_thread(
                    self._task_from_frozen_run,
                    run,
                    stable,
                    request,
                )
            except Exception as error:
                await self._fail_orphan_run(
                    run,
                    code="R2V_ADMISSION_TASK_REBUILD_FAILED",
                    message=str(error),
                )
                return None
        if (
            task.kind is not TaskKind.R2V_GENERATION
            or task.run_id != run.run_id
        ):
            await self._fail_orphan_run(
                run,
                code="R2V_ADMISSION_TASK_CONFLICT",
                message="stable Task id 已被不相容记录占用",
            )
            return None
        await self._ensure_state_for_task(run, task)
        return await asyncio.to_thread(
            self.executions.get_task,
            run.project_id,
            task.task_id,
        )

    async def dispatch(
        self,
        *,
        project_id: str,
        target_ref: str,
        arguments: Mapping[str, Any],
        idempotency_key: str,
        expected_object_versions: Sequence[str] = (),
        start: bool = True,
        s2v: bool = False,
    ) -> FileR2VDispatch:
        command_hash = _fingerprint(
            {
                "command": CreatorCommandType.GENERATE_R2V_VIDEO.value,
                "targetRef": target_ref,
                "arguments": dict(arguments),
            },
        )
        # Identical retries reuse the same durable slot, so a transient
        # provider failure would otherwise replay the FAILED task forever.
        # Probe a bounded number of derived retry slots for transient
        # failures only; deterministic failures keep the terminal wall.
        stable = _ids(project_id, idempotency_key)
        existing: TaskRecord | None = None
        for attempt in range(MAX_TRANSIENT_RETRY_SLOTS + 1):
            slot_key = transient_retry_slot_key(idempotency_key, attempt)
            stable = _ids(project_id, slot_key)
            try:
                existing = await asyncio.to_thread(
                    self.executions.get_task,
                    project_id,
                    stable["task_id"],
                )
            except RecordNotFoundError:
                existing = None
                idempotency_key = slot_key
                break
            if existing.status is TaskStatus.QUARANTINED:
                continue
            self._assert_replay(existing, target_ref, command_hash)
            if existing.status is TaskStatus.FAILED:
                if is_transient_task_error(existing.error):
                    continue
                raise _failed_task_conflict(existing)
            run = await asyncio.to_thread(
                self.executions.get_run,
                project_id,
                str(existing.run_id),
            )
            if existing.status not in _TERMINAL_TASKS:
                await self._ensure_state_for_task(run, existing)
                existing = await asyncio.to_thread(
                    self.executions.get_task,
                    project_id,
                    existing.task_id,
                )
            if start and existing.status not in _TERMINAL_TASKS:
                self.start_task(project_id, existing.task_id)
            return self._dispatch_result(existing, stable, replayed=True)
        else:
            raise _failed_task_conflict(existing, exhausted_retries=True)

        try:
            orphan_run = await asyncio.to_thread(
                self.executions.get_run,
                project_id,
                stable["run_id"],
            )
        except RecordNotFoundError:
            orphan_run = None
        if orphan_run is not None:
            self._assert_run_replay(orphan_run, target_ref, command_hash)
            recovered = await self._recover_run_admission_gap(orphan_run)
            if recovered is None:
                raise ConflictError(
                    "R2V admission 在 Run/Task 边界中断且无法安全恢复",
                )
            if start and recovered.status not in _TERMINAL_TASKS:
                self.start_task(project_id, recovered.task_id)
            return self._dispatch_result(recovered, stable, replayed=True)

        # Two independent tool calls may legally submit the same command:
        # an interrupted director gets re-delegated after the next review
        # approval while its first submission is still generating. Their
        # stable ids differ (derived from the tool-call id), so the replay
        # checks above cannot see the first submission — attach to any
        # live task carrying the same command hash instead of paying the
        # provider for an identical video twice.
        in_flight = await asyncio.to_thread(
            self._find_in_flight_duplicate,
            project_id,
            command_hash,
            target_ref,
        )
        if in_flight is not None:
            logger.info(
                "r2v dispatch attached to in-flight duplicate: "
                "project=%s task=%s target=%s",
                _log_safe(project_id),
                _log_safe(in_flight.task_id),
                _log_safe(target_ref),
            )
            if start and in_flight.status not in _TERMINAL_TASKS:
                self.start_task(project_id, in_flight.task_id)
            return self._dispatch_result(in_flight, stable, replayed=True)

        base = await asyncio.to_thread(self.services.projects.read, project_id)
        conflicts = [
            value
            for value in expected_object_versions
            if f"project:{base.etag}:" not in value
        ]
        if conflicts:
            raise ConflictError("R2V 命令目标已被其他写者修改")
        resolved = await asyncio.to_thread(
            _resolve_s2v_request if s2v else _resolve_request,
            snapshot=base,
            project_root=self.services.projects.project_root(project_id),
            target_ref=target_ref,
            arguments=dict(arguments),
        )
        reviews = await asyncio.to_thread(
            self.services.reviews.all_pending,
            project_id,
        )
        assert_media_review_admission(
            reviews=reviews,
            command_type=CreatorCommandType.GENERATE_R2V_VIDEO.value,
            target_ref=resolved.target_ref,
            reference_version_ids=resolved.reference_version_ids,
        )
        request = self._request_payload(base, resolved, stable)
        request_fingerprint = _fingerprint(request)
        logger.info(
            "r2v dispatch: project=%s task=%s target=%s",
            project_id,
            stable["task_id"],
            target_ref,
        )
        run, task = await self._admit(
            base=base,
            resolved=resolved,
            request=request,
            request_fingerprint=request_fingerprint,
            command_hash=command_hash,
            idempotency_key=idempotency_key,
            stable=stable,
        )
        del run
        if start and task.status not in _TERMINAL_TASKS:
            self.start_task(project_id, task.task_id)
        return self._dispatch_result(task, stable, replayed=False)

    @staticmethod
    def _request_payload(
        base: ProjectSnapshot,
        resolved: _ResolvedR2V,
        stable: Mapping[str, str],
    ) -> dict[str, Any]:
        payload = {
            "targetRef": resolved.target_ref,
            "elementId": resolved.element_id,
            "elementLabel": resolved.element_label,
            "prompt": resolved.prompt,
            "ratio": resolved.ratio,
            "durationSeconds": resolved.duration_seconds,
            "resolution": resolved.resolution,
            "watermark": resolved.watermark,
            "generateAudio": resolved.generate_audio,
            "referenceVersionIds": list(resolved.reference_version_ids),
            "referenceUrls": list(resolved.reference_urls),
            "referenceChecksums": list(resolved.reference_checksums),
            "provenanceRefs": list(resolved.provenance_refs),
            "readSet": list(resolved.read_set),
            "slotId": resolved.slot_id,
            "slotKind": resolved.slot_kind,
            "ownerRef": resolved.owner_ref,
            "inputGeneration": base.generation,
            "inputEtag": base.etag,
            "fileId": stable["file_id"],
            "artifactVersionId": stable["artifact_version_id"],
            "transactionId": stable["transaction_id"],
            "projectInputFingerprint": _submission_input_fingerprint(
                base.project,
                resolved.element_id,
                mode=resolved.mode,
                payload_refs=list(resolved.reference_version_ids),
            ),
        }
        _, element = find_timeline_element(base.project, resolved.element_id)
        project_audio = getattr(element.creation, "generate_audio", True)
        authored_plan = getattr(element.creation, "generation_plan", None)
        if authored_plan is not None:
            payload["generationPlan"] = authored_plan.model_dump(mode="json")
        predecessor = getattr(element.creation, "predecessor_element_id", None)
        if predecessor is not None:
            payload["predecessorElementId"] = predecessor
        if project_audio != resolved.generate_audio:
            # An explicit command override can differ from the project intent.
            payload["projectGenerateAudio"] = project_audio
        if any(resolved.reference_voice_urls):
            # Joins the frozen request only when a voice actually rides
            # along, so legacy tasks keep their fingerprint identity.
            payload["referenceVoiceUrls"] = list(
                resolved.reference_voice_urls,
            )
        if resolved.bgm_reference_url:
            # Same fingerprint-identity rule: the shared bgm reference
            # audio joins only when it actually ships.
            payload["bgmReferenceUrl"] = resolved.bgm_reference_url
        if resolved.continuity_shape:
            # Extension semantics join only for real EXTEND requests,
            # same fingerprint-identity rule as the voice field.
            payload["continuityShape"] = resolved.continuity_shape
            if resolved.continuity_trim_offset:
                payload["continuityTrimOffset"] = (
                    resolved.continuity_trim_offset
                )
        if resolved.provider_duration_seconds:
            payload["providerDurationSeconds"] = (
                resolved.provider_duration_seconds
            )
        if resolved.mode != "r2v":
            # Only non-default modes join the frozen request so legacy r2v
            # tasks keep their fingerprint identity across the upgrade.
            payload.update(
                {
                    "mode": resolved.mode,
                    "firstFrameVersionId": resolved.first_frame_version_id,
                    "firstFrameUrl": resolved.first_frame_url,
                    "lastFrameVersionId": resolved.last_frame_version_id,
                    "lastFrameUrl": resolved.last_frame_url,
                    "lastFrameAuthoredVersionId": (
                        resolved.authored_last_frame_version_id
                    ),
                    "videoVersionId": resolved.video_version_id,
                    "videoUrl": resolved.video_url,
                },
            )
        if resolved.provider == "s2v":
            payload.update(
                {
                    "provider": "s2v",
                    "s2vImageVersionId": resolved.s2v_image_version_id,
                    "s2vImageUrl": resolved.s2v_image_url,
                    "s2vAudioVersionId": resolved.s2v_audio_version_id,
                    "s2vAudioUrl": resolved.s2v_audio_url,
                    "s2vResolution": resolved.s2v_resolution,
                },
            )
        return payload

    async def _admit(
        self,
        *,
        base: ProjectSnapshot,
        resolved: _ResolvedR2V,
        request: Mapping[str, Any],
        request_fingerprint: str,
        command_hash: str,
        idempotency_key: str,
        stable: Mapping[str, str],
    ) -> tuple[SpecialistRunRecord, TaskRecord]:
        run_candidate = SpecialistRunRecord(
            run_id=stable["run_id"],
            project_id=base.project.project_id,
            round_id=stable["round_id"],
            role=SpecialistRole.R2V_GENERATION_DIRECTOR,
            target_refs=[resolved.target_ref],
            input_generation=base.generation,
            input_etag=base.etag,
            request_fingerprint=request_fingerprint,
            read_set=list(resolved.read_set),
            caused_by_request_id=idempotency_key,
            review_policy=ReviewPolicy.AUTO_FIX,
            metadata={
                "commandType": CreatorCommandType.GENERATE_R2V_VIDEO.value,
                "targetRef": resolved.target_ref,
                "commandRequestHash": command_hash,
                "requestSnapshot": dict(request),
            },
        )
        try:
            run = await asyncio.to_thread(
                self.executions.get_run,
                base.project.project_id,
                stable["run_id"],
            )
        except RecordNotFoundError:
            run = await asyncio.to_thread(
                self.executions.create_run,
                run_candidate,
            )
        if (
            run.request_fingerprint != request_fingerprint
            or run.input_etag != base.etag
            or run.target_refs != [resolved.target_ref]
        ):
            raise ConflictError("Idempotency-Key 已用于不同的 R2V Run")

        task_candidate = TaskRecord(
            task_id=stable["task_id"],
            project_id=base.project.project_id,
            round_id=stable["round_id"],
            run_id=stable["run_id"],
            kind=TaskKind.R2V_GENERATION,
            request_fingerprint=request_fingerprint,
            idempotency_key=idempotency_key,
            input_generation=base.generation,
            input_etag=base.etag,
            expected_target_version=base.etag,
            input_refs=[resolved.target_ref, *resolved.reference_version_ids],
            read_set=list(resolved.read_set),
            caused_by_request_id=idempotency_key,
            review_policy=ReviewPolicy.AUTO_FIX,
            metadata={
                "commandType": CreatorCommandType.GENERATE_R2V_VIDEO.value,
                "targetRef": resolved.target_ref,
                "commandRequestHash": command_hash,
                "slotId": resolved.slot_id,
                "artifactVersionId": stable["artifact_version_id"],
                "fileId": stable["file_id"],
                "requestSnapshot": dict(request),
            },
        )
        try:
            task = await asyncio.to_thread(
                self.executions.get_task,
                base.project.project_id,
                stable["task_id"],
            )
        except RecordNotFoundError:
            try:
                task = await asyncio.to_thread(
                    self.executions.create_task,
                    task_candidate,
                )
            except (ExecutionPayloadConflict, ExecutionStateConflict) as error:
                raise ConflictError(str(error)) from error
        if (
            task.request_fingerprint != request_fingerprint
            or task.input_etag != base.etag
            or task.input_refs != task_candidate.input_refs
        ):
            raise ConflictError("Idempotency-Key 已用于不同的 R2V Task")

        await asyncio.to_thread(
            self._create_or_validate_state_sync,
            run,
            task,
            dict(request),
        )
        return run, task

    @staticmethod
    def _assert_replay(
        task: TaskRecord,
        target_ref: str,
        command_hash: str,
    ) -> None:
        if (
            task.kind is not TaskKind.R2V_GENERATION
            or task.metadata.get("commandType")
            != CreatorCommandType.GENERATE_R2V_VIDEO.value
            or task.metadata.get("targetRef") != target_ref
            or task.metadata.get("commandRequestHash") != command_hash
        ):
            raise ConflictError("Idempotency-Key 已用于不同的 R2V 命令")

    @staticmethod
    def _assert_run_replay(
        run: SpecialistRunRecord,
        target_ref: str,
        command_hash: str,
    ) -> None:
        if (
            run.role is not SpecialistRole.R2V_GENERATION_DIRECTOR
            or run.metadata.get("commandType")
            != CreatorCommandType.GENERATE_R2V_VIDEO.value
            or run.metadata.get("targetRef") != target_ref
            or run.metadata.get("commandRequestHash") != command_hash
        ):
            raise ConflictError("Idempotency-Key 已用于不同的 R2V Run")

    def _find_in_flight_duplicate(
        self,
        project_id: str,
        command_hash: str,
        target_ref: str,
    ) -> TaskRecord | None:
        """Return a live R2V Task already generating for this target.

        The command hash covers command type, target and arguments but not
        the caller's idempotency key, so it identifies "the same video"
        across independent submissions — the caller attaches to it.

        A live task for the same target with a *different* hash is a
        conflict, not a sibling: an Element owns exactly one video slot,
        so two concurrent renders make the selected output depend on
        completion order and double the provider bill (field run
        2026-08-11: the scheduler dispatched the committed video_prompt
        while a specialist re-submitted its own inline rewrite 39s
        later — both rendered). The dispatcher fails such submissions
        closed; the caller retries once the in-flight render settles.
        """

        for task in self.executions.list_tasks(project_id):
            if (
                task.kind is not TaskKind.R2V_GENERATION
                or task.status in _TERMINAL_TASKS
            ):
                continue
            if str(task.metadata.get("targetRef") or "") != target_ref:
                continue
            if (
                str(task.metadata.get("commandRequestHash") or "")
                == command_hash
            ):
                return task
            raise ConflictError(
                f"目标 {target_ref} 已有一个不同内容的视频任务在生成中"
                f"（task={task.task_id}）；同一 Element 同时只允许一个在飞渲染，"
                "请等待其完成后再提交（若需替换请先取消在飞任务）",
            )
        return None

    @staticmethod
    def _dispatch_result(
        task: TaskRecord,
        stable: Mapping[str, str],
        *,
        replayed: bool,
    ) -> FileR2VDispatch:
        return FileR2VDispatch(
            task_id=task.task_id,
            run_id=str(task.run_id or ""),
            transaction_id=stable["transaction_id"],
            input_etag=str(task.input_etag or ""),
            replayed=replayed,
        )

    def start_task(
        self,
        project_id: str,
        task_id: str,
    ) -> asyncio.Task[None] | None:
        current = self._jobs.get(task_id)
        if current is not None and not current.done():
            return current
        # Read durable state inside the supervisor, off the event loop. A
        # concurrent Project commit must not strand an admitted Task here.
        from services.file_agent_runtime.manual_regeneration_hold import (
            background_context,
        )

        worker = asyncio.create_task(
            self._drive(project_id, task_id),
            name=f"file-r2v:{task_id}",
            context=background_context(),
        )
        self._jobs[task_id] = worker
        self._job_projects[task_id] = project_id

        def discard(done: asyncio.Task[None]) -> None:
            if self._jobs.get(task_id) is done:
                self._jobs.pop(task_id, None)
                self._job_projects.pop(task_id, None)
            if not done.cancelled():
                try:
                    done.exception()
                except BaseException:
                    pass

        worker.add_done_callback(discard)
        return worker

    def cancel_project(self, project_id: str) -> None:
        """Signal all provider/recovery workers owned by one Project."""

        task_ids = [
            task_id
            for task_id, owner in self._job_projects.items()
            if owner == project_id
        ]
        for task_id in task_ids:
            worker = self._jobs.pop(task_id, None)
            self._job_projects.pop(task_id, None)
            if worker is not None:
                worker.cancel()
        for key, worker in list(self._terminal_recovery_jobs.items()):
            if key[0] != project_id:
                continue
            self._terminal_recovery_jobs.pop(key, None)
            worker.cancel()

    def _schedule_terminal_recovery(
        self,
        project_id: str,
        task_id: str,
    ) -> asyncio.Task[None]:
        """
        Keep a deferred terminal Task live until its critical lease settles.
        """

        key = (project_id, task_id)
        current = self._terminal_recovery_jobs.get(key)
        if current is not None and not current.done():
            return current
        worker = asyncio.create_task(
            self._recover_deferred_terminal_task(project_id, task_id),
            name=f"file-r2v-terminal-recovery:{task_id}",
        )
        self._terminal_recovery_jobs[key] = worker

        def discard(done: asyncio.Task[None]) -> None:
            if self._terminal_recovery_jobs.get(key) is done:
                self._terminal_recovery_jobs.pop(key, None)
            if not done.cancelled():
                try:
                    done.exception()
                except BaseException:
                    pass

        worker.add_done_callback(discard)
        return worker

    def notify_terminal_task(
        self,
        task: TaskRecord,
    ) -> asyncio.Task[None] | None:
        """Wake terminal convergence after an API-side Task cancellation."""

        if (
            task.kind is not TaskKind.R2V_GENERATION
            or task.status not in _TERMINAL_TASKS
        ):
            return None
        return self._schedule_terminal_recovery(task.project_id, task.task_id)

    @staticmethod
    def _terminal_barrier_expiry(state: R2VTaskState) -> float | None:
        if state.phase == "SUBMIT_CLAIMED" and state.provider_task_id is None:
            return state.submit_claim_expires_at_epoch
        if state.phase == "PROVIDER_SUCCEEDED":
            return state.materialize_claim_expires_at_epoch
        return None

    async def _recover_deferred_terminal_task(
        self,
        project_id: str,
        task_id: str,
    ) -> None:
        while True:
            try:
                task = await asyncio.to_thread(
                    self.executions.get_task,
                    project_id,
                    task_id,
                )
                if task.status not in _TERMINAL_TASKS:
                    return
                if await self._align_state_to_terminal_task(task):
                    await asyncio.to_thread(
                        reconcile_terminal_task_runs,
                        self.services.root,
                        [project_id],
                    )
                    return
                state = await asyncio.to_thread(
                    self._read_state_sync,
                    project_id,
                    task_id,
                )
                expiry = self._terminal_barrier_expiry(state)
                remaining = (
                    max(0.0, expiry - float(self.clock()))
                    if expiry is not None
                    else self.poll_interval_seconds
                )
                delay = min(
                    _TERMINAL_RECOVERY_POLL_SECONDS,
                    max(0.01, remaining),
                )
            except asyncio.CancelledError:
                raise
            except RecordNotFoundError:
                return
            except ProjectStoreError as error:
                # An unparseable project.json never becomes parseable, so
                # retrying is a hot loop that only floods the log. The corrupt
                # Project is already surfaced by the recovery scan.
                logger.warning(
                    "abandoning deferred R2V terminal recovery for %s/%s: %s",
                    _log_safe(project_id),
                    _log_safe(task_id),
                    error,
                )
                return
            except _R2VClaimLost:
                delay = min(
                    _TERMINAL_RECOVERY_POLL_SECONDS,
                    max(0.01, self.poll_interval_seconds),
                )
            except Exception:
                logger.exception(
                    "deferred R2V terminal recovery failed for %s/%s",
                    _log_safe(project_id),
                    _log_safe(task_id),
                )
                delay = min(
                    _TERMINAL_RECOVERY_POLL_SECONDS,
                    max(0.05, self.poll_interval_seconds),
                )
            await asyncio.sleep(delay)

    async def wait_for_task(
        self,
        project_id: str,
        task_id: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> TaskRecord:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            task = await asyncio.to_thread(
                self.executions.get_task,
                project_id,
                task_id,
            )
            if task.status in _TERMINAL_TASKS:
                worker = self._jobs.get(task_id)
                if worker is not None and not worker.done():
                    remaining = max(
                        0.001,
                        deadline - asyncio.get_running_loop().time(),
                    )
                    await asyncio.wait_for(
                        asyncio.shield(worker),
                        timeout=remaining,
                    )
                    task = await asyncio.to_thread(
                        self.executions.get_task,
                        project_id,
                        task_id,
                    )
                return task
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"R2V Task did not finish: {task_id}")
            await asyncio.sleep(min(self.poll_interval_seconds, 0.05))

    async def _align_state_to_terminal_task(self, task: TaskRecord) -> bool:
        target = {
            TaskStatus.SUCCEEDED: "SUCCEEDED",
            TaskStatus.FAILED: "FAILED",
            TaskStatus.CANCELLED: "CANCELLED",
            TaskStatus.QUARANTINED: "QUARANTINED",
        }.get(task.status)
        if target is None:
            return True
        action = "aligned"

        def align(current: R2VTaskState) -> Mapping[str, Any]:
            nonlocal action
            if (
                current.phase == "SUBMIT_CLAIMED"
                and current.provider_task_id is None
                and current.submit_owner
                and current.submit_claim_token
                and (current.submit_claim_expires_at_epoch or 0.0)
                > self.clock()
            ):
                # Provider submission is non-idempotent.  A terminal Task must
                # not erase the only durable ownership anchor before the live
                # owner has bound the provider id.
                action = "defer"
                return current.model_dump(mode="python")
            if task.status is TaskStatus.CANCELLED:
                if current.phase == "QUARANTINED":
                    return current.model_dump(mode="python")
                if current.phase == "PUBLISHED":
                    action = "complete"
                    return current.model_dump(mode="python")
                if current.phase == "PROVIDER_SUCCEEDED":
                    live_claim = bool(
                        current.materialize_owner
                        and current.materialize_claim_token
                        and (current.materialize_claim_expires_at_epoch or 0.0)
                        > self.clock(),
                    )
                    if live_claim:
                        action = "defer"
                        return current.model_dump(mode="python")
                    if current.materialized_result is not None:
                        action = "adopt_cancelled"
                        return current.model_dump(mode="python")
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "phase": target,
                    "submit_owner": None,
                    "submit_claim_token": None,
                    "submit_claim_expires_at_epoch": None,
                    "poll_lease_owner": None,
                    "poll_lease_token": None,
                    "poll_lease_expires_at_epoch": None,
                    "materialize_owner": None,
                    "materialize_claim_token": None,
                    "materialize_claimed_at_epoch": None,
                    "materialize_heartbeat_at_epoch": None,
                    "materialize_claim_expires_at_epoch": None,
                },
            )
            if isinstance(task.result, Mapping):
                dumped["published_result"] = dict(task.result)
            if isinstance(task.error, Mapping):
                dumped["last_error"] = dict(task.error)
            return dumped

        try:
            state = await asyncio.to_thread(
                self._update_state_sync,
                task.project_id,
                task.task_id,
                align,
            )
        except RecordNotFoundError:
            return True
        if task.status is TaskStatus.QUARANTINED:
            await self._ensure_terminal_quarantine_record(task, state)
        if action == "complete":
            await self._complete_success(task, state)
            return True
        if action == "adopt_cancelled":
            return await self._adopt_cancelled_materialized_output(task)
        return action != "defer"

    async def _ensure_terminal_quarantine_record(
        self,
        task: TaskRecord,
        state: R2VTaskState,
    ) -> None:
        """
        Repair the crash gap between a quarantined Attempt and its record.
        """

        result = task.result
        if not isinstance(result, Mapping):
            result = state.published_result
        if not isinstance(result, Mapping):
            raise StorageIntegrityError(
                "QUARANTINED R2V Task is missing its quarantined result",
            )
        task_error = task.error if isinstance(task.error, Mapping) else {}
        state_error = (
            state.last_error if isinstance(state.last_error, Mapping) else {}
        )
        reason = str(
            task_error.get("quarantineReason")
            or task_error.get("code")
            or state_error.get("code")
            or "R2V_TERMINAL_QUARANTINED",
        )
        stable = _ids(
            task.project_id,
            str(task.idempotency_key or task.task_id),
        )
        await asyncio.to_thread(
            self.executions.quarantine_task_result,
            task.project_id,
            task.task_id,
            reason=reason,
            result=dict(result),
            quarantine_id=stable["quarantine_id"],
            transition_task=False,
        )

    def _align_terminal_state_locked(self, task: TaskRecord) -> None:
        """Align state to the Task authority while lifecycle_lock is held."""

        target = {
            TaskStatus.SUCCEEDED: "SUCCEEDED",
            TaskStatus.FAILED: "FAILED",
            TaskStatus.CANCELLED: "CANCELLED",
            TaskStatus.QUARANTINED: "QUARANTINED",
        }.get(task.status)
        if target is None:
            return

        def align(current: R2VTaskState) -> Mapping[str, Any]:
            if (
                current.phase == "SUBMIT_CLAIMED"
                and current.provider_task_id is None
                and current.submit_owner
                and current.submit_claim_token
                and (current.submit_claim_expires_at_epoch or 0.0)
                > self.clock()
            ):
                return current.model_dump(mode="python")
            if (
                task.status is TaskStatus.CANCELLED
                and current.phase
                in {"PROVIDER_SUCCEEDED", "PUBLISHED", "QUARANTINED"}
                and (
                    (
                        current.materialize_owner
                        and current.materialize_claim_token
                        and (current.materialize_claim_expires_at_epoch or 0.0)
                        > self.clock()
                    )
                    or current.materialized_result is not None
                    or current.phase == "PUBLISHED"
                    or current.phase == "QUARANTINED"
                )
            ):
                return current.model_dump(mode="python")
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "phase": target,
                    "submit_owner": None,
                    "submit_claim_token": None,
                    "submit_claim_expires_at_epoch": None,
                    "poll_lease_owner": None,
                    "poll_lease_token": None,
                    "poll_lease_expires_at_epoch": None,
                    "materialize_owner": None,
                    "materialize_claim_token": None,
                    "materialize_claimed_at_epoch": None,
                    "materialize_heartbeat_at_epoch": None,
                    "materialize_claim_expires_at_epoch": None,
                    "updated_at": datetime.now(UTC),
                },
            )
            if isinstance(task.result, Mapping):
                dumped["published_result"] = dict(task.result)
            if isinstance(task.error, Mapping):
                dumped["last_error"] = dict(task.error)
            return dumped

        try:
            self._state_store(task.project_id, task.task_id).update(align)
        except RecordNotFoundError:
            return

    def _fail_active_task_locked(
        self,
        task: TaskRecord,
        *,
        error: Mapping[str, Any],
        stable: Mapping[str, str],
    ) -> TaskRecord:
        """Atomically terminalize active Task/state under one Project lock."""

        with self.services.projects.lifecycle_lock(task.project_id):
            latest = self.executions.get_task(
                task.project_id,
                task.task_id,
                _lifecycle_lock_held=True,
            )
            if latest.status is TaskStatus.RUNNING:
                try:
                    state = self._read_state_sync(
                        task.project_id,
                        task.task_id,
                    )
                except RecordNotFoundError:
                    state = None
                try:
                    self.executions.append_attempt(
                        task.project_id,
                        task.task_id,
                        event_id=stable["attempt_failed_event_id"],
                        attempt_id=stable["attempt_id"],
                        status=TaskAttemptStatus.FAILED,
                        provider_task_id=(
                            state.provider_task_id if state else None
                        ),
                        error=dict(error),
                        _lifecycle_lock_held=True,
                    )
                except ExecutionStateConflict:
                    pass
            elif latest.status is TaskStatus.QUEUED:
                try:
                    self.executions.transition_task(
                        task.project_id,
                        task.task_id,
                        expected_status=TaskStatus.QUEUED,
                        status=TaskStatus.FAILED,
                        updates={"error": dict(error)},
                        _lifecycle_lock_held=True,
                    )
                except ExecutionStateConflict:
                    pass
            latest = self.executions.get_task(
                task.project_id,
                task.task_id,
                _lifecycle_lock_held=True,
            )
            if latest.status in _TERMINAL_TASKS:
                self._align_terminal_state_locked(latest)
            return latest

    async def _terminalize_task_from_state(
        self,
        task: TaskRecord,
        state: R2VTaskState,
    ) -> None:
        stable = _ids(
            task.project_id,
            str(task.idempotency_key or task.task_id),
        )
        if state.phase == "SUCCEEDED":
            result = state.published_result
            if not isinstance(result, Mapping):
                await self._fail(
                    task,
                    code="R2V_SUCCEEDED_RESULT_MISSING",
                    message="R2V SUCCEEDED state is missing its published result",
                )
                return
            if task.status is TaskStatus.QUEUED:
                await asyncio.to_thread(
                    self.executions.append_attempt,
                    task.project_id,
                    task.task_id,
                    event_id=stable["attempt_started_event_id"],
                    attempt_id=stable["attempt_id"],
                    status=TaskAttemptStatus.RUNNING,
                    input=state.request,
                )
                task = await asyncio.to_thread(
                    self.executions.get_task,
                    task.project_id,
                    task.task_id,
                )
            await self._converge(task, stable, result)
            return
        if state.phase == "CANCELLED":
            if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                task = await asyncio.to_thread(
                    self.executions.transition_task,
                    task.project_id,
                    task.task_id,
                    expected_status=task.status,
                    status=TaskStatus.CANCELLED,
                    updates={"error": state.last_error},
                )
            await self._mark_cancelled(task)
            return
        if state.phase == "QUARANTINED":
            result = state.published_result
            if not isinstance(result, Mapping):
                await self._fail(
                    task,
                    code="R2V_QUARANTINE_RESULT_MISSING",
                    message="R2V QUARANTINED state is missing its result",
                )
                return
            if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                task = await asyncio.to_thread(
                    self.executions.transition_task,
                    task.project_id,
                    task.task_id,
                    expected_status=task.status,
                    status=TaskStatus.QUARANTINED,
                    updates={
                        "result": dict(result),
                        "error": state.last_error,
                    },
                )
            await self._quarantine(
                task,
                stable,
                result=result,
                reason=str(
                    (state.last_error or {}).get("code") or "QUARANTINED",
                ),
                run_status=SpecialistRunStatus.STALE,
            )

    async def _drive(self, project_id: str, task_id: str) -> None:
        while True:
            try:
                await self._drive_until_idle(project_id, task_id)
                return
            except LockTimeoutError:
                # Re-enter from durable state, retaining the provider ID and
                # claims. Local contention is not a failed generation and
                # must never open a newly billed retry slot. Shutdown can
                # still cancel the backoff and leave recovery to startup.
                logger.warning(
                    "r2v local state busy; resuming same task: "
                    "project=%s task=%s",
                    _log_safe(project_id),
                    _log_safe(task_id),
                )
                await asyncio.sleep(max(0.25, self.poll_interval_seconds))

    async def _drive_until_idle(self, project_id: str, task_id: str) -> None:
        try:
            while True:
                try:
                    task = await asyncio.to_thread(
                        self.executions.get_task,
                        project_id,
                        task_id,
                    )
                except RecordNotFoundError:
                    return
                if task.status in _TERMINAL_TASKS:
                    aligned = await self._align_state_to_terminal_task(task)
                    if not aligned:
                        self._schedule_terminal_recovery(project_id, task_id)
                    await asyncio.to_thread(
                        reconcile_terminal_task_runs,
                        self.services.root,
                        [project_id],
                    )
                    return
                try:
                    state = await asyncio.to_thread(
                        self._read_state_sync,
                        project_id,
                        task_id,
                    )
                except RecordNotFoundError:
                    await self._fail(
                        task,
                        code="R2V_RECOVERY_STATE_MISSING",
                        message="R2V Task 缺少 durable state，已 fail-closed",
                    )
                    return
                if (
                    state.request_fingerprint != task.request_fingerprint
                    or _fingerprint(state.request) != task.request_fingerprint
                ):
                    await self._fail(
                        task,
                        code="R2V_RECOVERY_STATE_MISMATCH",
                        message="R2V durable state fingerprint 损坏",
                    )
                    return
                if state.phase == "FAILED":
                    detail = state.last_error or {}
                    await self._fail(
                        task,
                        code=str(detail.get("code") or "R2V_STATE_FAILED"),
                        message=str(
                            detail.get("message") or "R2V state failed",
                        ),
                        retryable=bool(detail.get("retryable")),
                    )
                    return
                if state.phase == "CANCELLED":
                    await self._terminalize_task_from_state(task, state)
                    return
                if state.phase == "QUARANTINED":
                    await self._terminalize_task_from_state(task, state)
                    return
                if state.phase == "SUCCEEDED":
                    await self._terminalize_task_from_state(task, state)
                    return
                if state.phase == "ADMITTED":
                    claimed = await self._submit(task, state)
                    if claimed:
                        continue
                elif (
                    state.phase == "SUBMIT_CLAIMED"
                    and not state.provider_task_id
                ):
                    deadline = state.submit_claim_expires_at_epoch or 0.0
                    if self.clock() >= deadline:
                        accepted = (
                            await self._expire_submit_claim_if_still_stale(
                                task,
                            )
                        )
                        if accepted:
                            await self._fail(
                                task,
                                code="R2V_PROVIDER_SUBMISSION_UNRECOVERABLE",
                                message=(
                                    "provider submission claim survived without a "
                                    "durable provider task id; refusing duplicate submit"
                                ),
                            )
                            return
                elif state.phase in {"PROVIDER_SUCCEEDED", "PUBLISHED"}:
                    await self._complete_success(task, state)
                    continue
                elif state.provider_task_id:
                    await self._poll_once(task, state)
                    continue
                elif state.phase not in _ACTIVE_PHASES:
                    return
                await asyncio.sleep(self.poll_interval_seconds)
        except LockTimeoutError:
            raise
        except _R2VClaimLost:
            # Another live supervisor owns the durable claim.  A stale worker
            # must exit without changing Task, Run, state, or published files.
            return
        except asyncio.CancelledError:
            # Process shutdown intentionally preserves durable active state.
            raise
        except Exception as error:
            # The durable Task record keeps the failure, but without this
            # line the server log showed a RUNNING -> FAILED transition with
            # no ERROR anywhere — diagnosing field failures required digging
            # through runtime task.json files.
            logger.error(
                "r2v supervisor failed: project=%s task=%s error=%s",
                _log_safe(project_id),
                _log_safe(task_id),
                _log_safe(error),
                exc_info=True,
            )
            try:
                task = await asyncio.to_thread(
                    self.executions.get_task,
                    project_id,
                    task_id,
                )
                await self._fail(
                    task,
                    code="R2V_SUPERVISOR_FAILED",
                    message=str(error),
                    retryable=_is_transient_materialize_error(error),
                    error=error,
                )
            except BaseException:
                logger.exception(
                    "r2v supervisor failure could not be persisted: "
                    "project=%s task=%s",
                    _log_safe(project_id),
                    _log_safe(task_id),
                )

    async def _submit(self, task: TaskRecord, state: R2VTaskState) -> bool:
        now = float(self.clock())
        token = uuid4().hex

        def claim(current: R2VTaskState) -> Mapping[str, Any]:
            if current.phase != "ADMITTED":
                return current.model_dump(mode="python")
            latest = self.executions.get_task(
                task.project_id,
                task.task_id,
                _lifecycle_lock_held=True,
            )
            if latest.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                return current.model_dump(mode="python")
            snapshot = self.services.projects.read(task.project_id)
            frozen = current.request.get("projectInputFingerprint")
            element_id = str(current.request["elementId"])
            if (
                frozen is not None
                and not _matches_submission_inputs(
                    snapshot.project,
                    element_id,
                    frozen,
                    mode=str(current.request.get("mode") or "r2v"),
                    payload_refs=[
                        str(item)
                        for item in current.request.get("referenceVersionIds")
                        or ()
                    ],
                )
            ) or (
                frozen is None
                and current.request.get("inputEtag")
                and snapshot.etag != current.request["inputEtag"]
            ):
                raise ValidationError(
                    "视频输入已改变，请按最新分镜重新确认生成。"
                )
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "phase": "SUBMIT_CLAIMED",
                    "submit_owner": self.owner_id,
                    "submit_claim_token": token,
                    "submit_claimed_at_epoch": now,
                    "submit_heartbeat_at_epoch": now,
                    "submit_claim_expires_at_epoch": now
                    + self.submit_claim_seconds,
                },
            )
            return dumped

        from services.file_agent_runtime.manual_regeneration_hold import (
            ManualHoldConflict,
        )

        try:
            claimed = await asyncio.to_thread(
                self._update_state_sync,
                task.project_id,
                task.task_id,
                claim,
                admission=True,
            )
        except ManualHoldConflict as error:
            # This frozen request has not spent anything. End it so a manual
            # retry (or an explicit resume) resolves the new storyboard rather
            # than submitting the old reference after the hold is cleared.
            await self._fail(
                task,
                code="MANUAL_REGENERATION_HOLD",
                message=str(error),
                retryable=True,
                error=error,
            )
            return False
        except ValidationError as error:
            await self._fail(
                task,
                code="R2V_INPUTS_CHANGED",
                message=str(error),
                retryable=True,
                error=error,
            )
            return False
        if (
            claimed.phase != "SUBMIT_CLAIMED"
            or claimed.submit_owner != self.owner_id
        ):
            return False
        heartbeat = asyncio.create_task(
            self._heartbeat_submit_claim(task, claimed),
            name=f"r2v-submit-heartbeat:{task.task_id}",
        )
        request = claimed.request
        try:
            task = await self._retry_submit_local_io(
                task,
                self._prepare_submit_claim,
                task,
                request,
            )
        except BaseException:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            raise
        try:
            await self._retry_submit_local_io(
                task,
                self._require_live_submit_claim,
                task,
                claimed,
                require_active_task=True,
            )

            async def invoke_provider() -> str:
                with media_task_scope(
                    task.task_id,
                    project_id=task.project_id,
                ):
                    if str(request.get("provider") or "") == "s2v":
                        return await self.provider.submit_s2v(
                            image_url=str(request["s2vImageUrl"]),
                            audio_url=str(request["s2vAudioUrl"]),
                            resolution=str(
                                request.get("s2vResolution") or "480P",
                            ),
                        )
                    request_mode = str(request.get("mode") or "r2v")
                    extra_arguments: dict[str, Any] = {}
                    if request_mode != "r2v":
                        # Passed only for explicit modes so injected test
                        # providers with the legacy signature keep working.
                        extra_arguments = {
                            "mode": request_mode,
                            "first_frame_url": (
                                str(request["firstFrameUrl"])
                                if request.get("firstFrameUrl")
                                else None
                            ),
                            "last_frame_url": (
                                str(request["lastFrameUrl"])
                                if request.get("lastFrameUrl")
                                else None
                            ),
                            "video_url": (
                                str(request["videoUrl"])
                                if request.get("videoUrl")
                                else None
                            ),
                        }
                    if request.get("referenceVoiceUrls"):
                        # Same legacy-signature courtesy as above.
                        extra_arguments["reference_voice_urls"] = tuple(
                            str(item) for item in request["referenceVoiceUrls"]
                        )
                    if request.get("bgmReferenceUrl"):
                        extra_arguments["reference_bgm_url"] = str(
                            request["bgmReferenceUrl"],
                        )
                    return await self.provider.submit(
                        prompt=str(request["prompt"]),
                        reference_image_urls=tuple(request["referenceUrls"]),
                        ratio=str(request["ratio"]),
                        duration_seconds=int(
                            request.get("providerDurationSeconds")
                            or request["durationSeconds"],
                        ),
                        resolution=str(request["resolution"]),
                        watermark=bool(request["watermark"]),
                        generate_audio=bool(request["generateAudio"]),
                        **extra_arguments,
                    )

            provider_task_id = await asyncio.wait_for(
                invoke_provider(),
                timeout=self.submit_timeout_seconds,
            )
            provider_task_id = str(provider_task_id).strip()
            if not provider_task_id:
                raise ValidationError("R2V provider 返回空 task id")

            def bind(current: R2VTaskState) -> Mapping[str, Any]:
                if not self._owns_live_submit_claim(current, claimed):
                    raise _R2VClaimLost("R2V provider id bind claim lost")
                dumped = current.model_dump(mode="python")
                dumped.update(
                    {
                        "phase": "POLLING",
                        "provider_task_id": provider_task_id,
                        "next_poll_at_epoch": float(self.clock()),
                        "submit_claim_token": None,
                        "submit_claim_expires_at_epoch": None,
                    },
                )
                return dumped

            # Binding the provider id is part of the non-idempotent submission
            # critical section.  Keep the durable owner heartbeat alive until
            # this CAS has completed so another supervisor cannot observe an
            # expired claim between provider acceptance and id persistence.
            await self._retry_submit_local_io(
                task,
                asyncio.to_thread,
                self._update_state_sync,
                task.project_id,
                task.task_id,
                bind,
            )
        except _R2VClaimLost:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            detail = str(error) or (
                f"R2V reference preparation or provider submission exceeded "
                f"{self.submit_timeout_seconds:g} seconds"
                if isinstance(error, TimeoutError)
                else type(error).__name__
            )
            await self._mark_submit_failed_if_owned(task, claimed, detail)
            await self._fail(
                task,
                code="R2V_PROVIDER_SUBMISSION_FAILED",
                message=detail,
                error=error,
            )
            return True
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        latest = await asyncio.to_thread(
            self.executions.get_task,
            task.project_id,
            task.task_id,
        )
        if latest.status is TaskStatus.RUNNING:
            await asyncio.to_thread(
                self.executions.transition_task,
                task.project_id,
                task.task_id,
                expected_status=TaskStatus.RUNNING,
                status=TaskStatus.RUNNING,
                updates={
                    "progress": 0.05,
                    "metadata": {
                        **latest.metadata,
                        "providerTaskId": provider_task_id,
                    },
                },
            )
        run = await asyncio.to_thread(
            self.executions.get_run,
            task.project_id,
            str(task.run_id),
        )
        if run.status is SpecialistRunStatus.RUNNING_MODEL:
            await asyncio.to_thread(
                self.executions.transition_run,
                task.project_id,
                run.run_id,
                expected_status=run.status,
                status=SpecialistRunStatus.WAITING_RUNTIME,
            )
        return True

    async def _retry_submit_local_io(
        self,
        task: TaskRecord,
        operation: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Retain the live claim and provider receipt through local contention.

        Only local, replayable operations belong here. Re-entering `_submit`
        would discard an accepted provider ID or strand a pre-submit claim;
        retrying the provider itself could bill the same generation twice.
        Cancellation and non-lock failures retain their existing semantics.
        """
        while True:
            try:
                return await operation(*args, **kwargs)
            except LockTimeoutError:
                logger.warning(
                    "r2v submit state busy; retrying local write: "
                    "project=%s task=%s",
                    _log_safe(task.project_id),
                    _log_safe(task.task_id),
                )
                await asyncio.sleep(
                    min(1.0, max(0.25, self.poll_interval_seconds)),
                )

    async def _prepare_submit_claim(
        self,
        task: TaskRecord,
        request: Mapping[str, Any],
    ) -> TaskRecord:
        run = await asyncio.to_thread(
            self.executions.get_run,
            task.project_id,
            str(task.run_id),
        )
        if run.status in {
            SpecialistRunStatus.QUEUED,
            SpecialistRunStatus.QUEUED_CAPACITY,
        }:
            await asyncio.to_thread(
                self.executions.transition_run,
                task.project_id,
                run.run_id,
                expected_status=run.status,
                status=SpecialistRunStatus.RUNNING_MODEL,
            )
        if task.status is TaskStatus.QUEUED:
            stable = _ids(
                task.project_id,
                str(task.idempotency_key or task.task_id),
            )
            await asyncio.to_thread(
                self.executions.append_attempt,
                task.project_id,
                task.task_id,
                event_id=stable["attempt_started_event_id"],
                attempt_id=stable["attempt_id"],
                status=TaskAttemptStatus.RUNNING,
                input=request,
            )
            task = await asyncio.to_thread(
                self.executions.get_task,
                task.project_id,
                task.task_id,
            )
        return task

    def _owns_live_submit_claim(
        self,
        current: R2VTaskState,
        claim: R2VTaskState,
    ) -> bool:
        return bool(
            current.phase == "SUBMIT_CLAIMED"
            and current.provider_task_id is None
            and current.submit_owner == self.owner_id
            and current.submit_claim_token
            and current.submit_claim_token == claim.submit_claim_token
            and (current.submit_claim_expires_at_epoch or 0.0) > self.clock(),
        )

    async def _require_live_submit_claim(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
        *,
        require_active_task: bool,
    ) -> None:
        current = await asyncio.to_thread(
            self._read_state_sync,
            task.project_id,
            task.task_id,
        )
        if not self._owns_live_submit_claim(current, claim):
            raise _R2VClaimLost("R2V submit claim ownership lost")
        if require_active_task:
            latest = await asyncio.to_thread(
                self.executions.get_task,
                task.project_id,
                task.task_id,
            )
            if latest.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                raise _R2VClaimLost("R2V submit Task is no longer active")

    async def _heartbeat_submit_claim(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
    ) -> None:
        interval = min(30.0, max(0.01, self.submit_claim_seconds / 4.0))
        while True:
            await asyncio.sleep(interval)
            owned = False

            def heartbeat(current: R2VTaskState) -> Mapping[str, Any]:
                nonlocal owned
                if not self._owns_live_submit_claim(current, claim):
                    return current.model_dump(mode="python")
                owned = True
                now = float(self.clock())
                dumped = current.model_dump(mode="python")
                dumped.update(
                    {
                        "submit_heartbeat_at_epoch": now,
                        "submit_claim_expires_at_epoch": (
                            now + self.submit_claim_seconds
                        ),
                    },
                )
                return dumped

            await self._retry_submit_local_io(
                task,
                asyncio.to_thread,
                self._update_state_sync,
                task.project_id,
                task.task_id,
                heartbeat,
            )
            if not owned:
                return

    async def _mark_submit_failed_if_owned(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
        message: str,
    ) -> bool:
        accepted = False

        def fail(current: R2VTaskState) -> Mapping[str, Any]:
            nonlocal accepted
            if not self._owns_live_submit_claim(current, claim):
                return current.model_dump(mode="python")
            accepted = True
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "phase": "FAILED",
                    "submit_owner": None,
                    "submit_claim_token": None,
                    "submit_claim_expires_at_epoch": None,
                    "last_error": {
                        "code": "R2V_PROVIDER_SUBMISSION_FAILED",
                        "message": message[:2000],
                    },
                },
            )
            return dumped

        await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            fail,
        )
        return accepted

    async def _expire_submit_claim_if_still_stale(
        self,
        task: TaskRecord,
    ) -> bool:
        accepted = False
        now = float(self.clock())

        def expire(current: R2VTaskState) -> Mapping[str, Any]:
            nonlocal accepted
            if (
                current.phase != "SUBMIT_CLAIMED"
                or current.provider_task_id is not None
                or (current.submit_claim_expires_at_epoch or 0.0) > now
            ):
                return current.model_dump(mode="python")
            accepted = True
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "phase": "FAILED",
                    "submit_owner": None,
                    "submit_claim_token": None,
                    "submit_claim_expires_at_epoch": None,
                },
            )
            return dumped

        await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            expire,
        )
        return accepted

    async def _claim_poll(
        self,
        task: TaskRecord,
    ) -> R2VTaskState | None:
        now = float(self.clock())
        claimed = False
        token = uuid4().hex

        def claim(current: R2VTaskState) -> Mapping[str, Any]:
            nonlocal claimed
            if current.phase != "POLLING" or not current.provider_task_id:
                return current.model_dump(mode="python")
            if (
                current.next_poll_at_epoch is not None
                and now < current.next_poll_at_epoch
            ):
                return current.model_dump(mode="python")
            if (
                current.poll_lease_owner not in {None, self.owner_id}
                and (current.poll_lease_expires_at_epoch or 0.0) > now
            ):
                return current.model_dump(mode="python")
            claimed = True
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "poll_lease_owner": self.owner_id,
                    "poll_lease_token": token,
                    "poll_lease_expires_at_epoch": now
                    + self.poll_lease_seconds,
                },
            )
            return dumped

        state = await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            claim,
        )
        return state if claimed else None

    def _owns_live_poll_claim(
        self,
        current: R2VTaskState,
        claim: R2VTaskState,
    ) -> bool:
        return bool(
            current.phase == "POLLING"
            and current.provider_task_id == claim.provider_task_id
            and current.poll_lease_owner == self.owner_id
            and current.poll_lease_token
            and current.poll_lease_token == claim.poll_lease_token
            and (current.poll_lease_expires_at_epoch or 0.0) > self.clock(),
        )

    async def _heartbeat_poll_claim(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
    ) -> None:
        interval = min(30.0, max(0.01, self.poll_lease_seconds / 4.0))
        while True:
            await asyncio.sleep(interval)
            owned = False

            def heartbeat(current: R2VTaskState) -> Mapping[str, Any]:
                nonlocal owned
                if not self._owns_live_poll_claim(current, claim):
                    return current.model_dump(mode="python")
                owned = True
                dumped = current.model_dump(mode="python")
                dumped["poll_lease_expires_at_epoch"] = (
                    float(self.clock()) + self.poll_lease_seconds
                )
                return dumped

            await asyncio.to_thread(
                self._update_state_sync,
                task.project_id,
                task.task_id,
                heartbeat,
            )
            if not owned:
                return

    async def _poll_once(self, task: TaskRecord, state: R2VTaskState) -> None:
        del state
        claim = await self._claim_poll(task)
        if claim is None:
            await asyncio.sleep(self.poll_interval_seconds)
            return
        assert claim.provider_task_id is not None
        heartbeat = asyncio.create_task(
            self._heartbeat_poll_claim(task, claim),
            name=f"r2v-poll-heartbeat:{task.task_id}",
        )
        try:

            async def invoke_provider() -> Mapping[str, Any]:
                with media_task_scope(
                    task.task_id,
                    project_id=task.project_id,
                ):
                    if str(claim.request.get("provider") or "") == "s2v":
                        return await self.provider.poll_s2v(
                            claim.provider_task_id,
                        )
                    return await self.provider.poll(claim.provider_task_id)

            raw = await asyncio.wait_for(
                invoke_provider(),
                timeout=self.poll_timeout_seconds,
            )
            result = _durable_provider_result(
                _json_mapping(raw, label="R2V provider poll result"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if getattr(error, "retryable", True) is False:
                if await self._mark_poll_failed_if_owned(
                    task,
                    claim,
                    str(error),
                ):
                    await self._fail(
                        task,
                        code="R2V_PROVIDER_POLL_FAILED",
                        message=str(error),
                    )
                return
            await self._release_poll_with_error(task, claim, str(error))
            return
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

        status = str(result.get("status") or "UNKNOWN").upper()
        if status in {"QUEUED", "PENDING", "RUNNING", "PROCESSING", "UNKNOWN"}:
            await self._record_running_poll(task, claim, result)
            return
        if status != "SUCCEEDED":
            message = str(result.get("error") or f"provider status={status}")
            if await self._mark_poll_failed_if_owned(task, claim, message):
                await self._fail(
                    task,
                    code="R2V_PROVIDER_FAILED",
                    message=message,
                )
            return

        accepted = False

        def success(current: R2VTaskState) -> Mapping[str, Any]:
            nonlocal accepted
            if not self._owns_live_poll_claim(current, claim):
                return current.model_dump(mode="python")
            accepted = True
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "phase": "PROVIDER_SUCCEEDED",
                    "provider_result": result,
                    "last_poll_at_epoch": float(self.clock()),
                    "next_poll_at_epoch": None,
                    "poll_lease_owner": None,
                    "poll_lease_token": None,
                    "poll_lease_expires_at_epoch": None,
                    "last_error": None,
                },
            )
            return dumped

        state = await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            success,
        )
        if accepted:
            await self._complete_success(task, state)

    async def _record_running_poll(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
        result: Mapping[str, Any],
    ) -> None:
        now = float(self.clock())
        accepted = False

        def update(current: R2VTaskState) -> Mapping[str, Any]:
            nonlocal accepted
            if not self._owns_live_poll_claim(current, claim):
                return current.model_dump(mode="python")
            accepted = True
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "last_poll_at_epoch": now,
                    "next_poll_at_epoch": now + self.poll_interval_seconds,
                    "poll_lease_owner": None,
                    "poll_lease_token": None,
                    "poll_lease_expires_at_epoch": None,
                    "last_error": None,
                },
            )
            return dumped

        await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            update,
        )
        if not accepted:
            return
        latest = await asyncio.to_thread(
            self.executions.get_task,
            task.project_id,
            task.task_id,
        )
        if latest.status is TaskStatus.RUNNING:
            progress = result.get("progress")
            try:
                normalized = max(0.05, min(0.89, float(progress)))
            except (TypeError, ValueError):
                normalized = latest.progress or 0.05
            # Skip the no-op CAS write: providers rarely report progress, so
            # this used to rewrite the Task record (under the exclusive
            # project lock, with a task-transition log line) on every ~2s
            # poll with an unchanged payload — field logs showed ~1.8k such
            # writes per day and they were the dominant lock hot spot.
            if latest.progress == normalized:
                return
            await asyncio.to_thread(
                self.executions.transition_task,
                task.project_id,
                task.task_id,
                expected_status=TaskStatus.RUNNING,
                status=TaskStatus.RUNNING,
                updates={"progress": normalized},
            )

    async def _release_poll_with_error(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
        message: str,
    ) -> None:
        now = float(self.clock())

        def update(current: R2VTaskState) -> Mapping[str, Any]:
            if not self._owns_live_poll_claim(current, claim):
                return current.model_dump(mode="python")
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "last_poll_at_epoch": now,
                    "next_poll_at_epoch": now + self.poll_interval_seconds,
                    "poll_lease_owner": None,
                    "poll_lease_token": None,
                    "poll_lease_expires_at_epoch": None,
                    "last_error": {
                        "code": "R2V_PROVIDER_POLL_RETRY",
                        "message": message[:2000],
                    },
                },
            )
            return dumped

        await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            update,
        )

    async def _mark_poll_failed_if_owned(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
        message: str,
    ) -> bool:
        accepted = False

        def update(current: R2VTaskState) -> Mapping[str, Any]:
            nonlocal accepted
            if not self._owns_live_poll_claim(current, claim):
                return current.model_dump(mode="python")
            accepted = True
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "phase": "FAILED",
                    "poll_lease_owner": None,
                    "poll_lease_token": None,
                    "poll_lease_expires_at_epoch": None,
                    "last_error": {
                        "code": "R2V_PROVIDER_FAILED",
                        "message": message[:2000],
                    },
                },
            )
            return dumped

        await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            update,
        )
        return accepted

    async def _claim_materialize(
        self,
        task: TaskRecord,
    ) -> R2VTaskState | None:
        now = float(self.clock())
        claimed = False
        token = uuid4().hex

        def claim(current: R2VTaskState) -> Mapping[str, Any]:
            nonlocal claimed
            if current.phase != "PROVIDER_SUCCEEDED":
                return current.model_dump(mode="python")
            if current.provider_result is None:
                raise StorageIntegrityError(
                    "R2V PROVIDER_SUCCEEDED state is missing provider result",
                )
            owner = current.materialize_owner
            expires = current.materialize_claim_expires_at_epoch or 0.0
            if owner not in {None, self.owner_id} and now < expires:
                return current.model_dump(mode="python")
            claimed = True
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "materialize_owner": self.owner_id,
                    "materialize_claim_token": token,
                    "materialize_claimed_at_epoch": (
                        current.materialize_claimed_at_epoch
                        if owner == self.owner_id
                        else now
                    ),
                    "materialize_heartbeat_at_epoch": now,
                    "materialize_claim_expires_at_epoch": (
                        now + self.materialize_claim_seconds
                    ),
                },
            )
            return dumped

        state = await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            claim,
        )
        if not claimed or state.materialize_owner != self.owner_id:
            return None
        return state

    def _owns_live_materialize_claim(
        self,
        current: R2VTaskState,
        claim: R2VTaskState,
    ) -> bool:
        return bool(
            current.phase == "PROVIDER_SUCCEEDED"
            and current.materialize_owner == self.owner_id
            and current.materialize_claim_token
            and current.materialize_claim_token
            == claim.materialize_claim_token
            and (current.materialize_claim_expires_at_epoch or 0.0)
            > self.clock(),
        )

    async def _require_live_materialize_claim(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
    ) -> None:
        current = await asyncio.to_thread(
            self._read_state_sync,
            task.project_id,
            task.task_id,
        )
        if not self._owns_live_materialize_claim(current, claim):
            raise _R2VClaimLost("R2V materialize claim ownership lost")

    async def _heartbeat_materialize_claim(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
    ) -> None:
        interval = min(30.0, max(0.01, self.materialize_claim_seconds / 4.0))
        while True:
            await asyncio.sleep(interval)
            now = float(self.clock())
            owned = False

            def heartbeat(current: R2VTaskState) -> Mapping[str, Any]:
                nonlocal owned
                if not self._owns_live_materialize_claim(current, claim):
                    return current.model_dump(mode="python")
                owned = True
                dumped = current.model_dump(mode="python")
                dumped.update(
                    {
                        "materialize_heartbeat_at_epoch": now,
                        "materialize_claim_expires_at_epoch": (
                            now + self.materialize_claim_seconds
                        ),
                    },
                )
                return dumped

            await asyncio.to_thread(
                self._update_state_sync,
                task.project_id,
                task.task_id,
                heartbeat,
            )
            if not owned:
                return

    async def _release_materialize_claim(self, task: TaskRecord) -> None:
        def release(current: R2VTaskState) -> Mapping[str, Any]:
            if current.materialize_owner != self.owner_id:
                return current.model_dump(mode="python")
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "materialize_owner": None,
                    "materialize_claim_token": None,
                    "materialize_claimed_at_epoch": None,
                    "materialize_heartbeat_at_epoch": None,
                    "materialize_claim_expires_at_epoch": None,
                },
            )
            return dumped

        await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            release,
        )

    @staticmethod
    def _provider_duration(
        state: R2VTaskState,
        request: Mapping[str, Any],
    ) -> float:
        if state.provider_result is None:
            raise StorageIntegrityError(
                "R2V PROVIDER_SUCCEEDED state is missing provider result",
            )
        raw_duration = state.provider_result.get("durationSeconds")
        try:
            duration = (
                float(raw_duration)
                if raw_duration is not None
                else float(
                    request.get("providerDurationSeconds")
                    or request["durationSeconds"],
                )
            )
        except (TypeError, ValueError) as error:
            raise ValidationError(
                "R2V provider durationSeconds 非法"
            ) from error
        if duration <= 0:
            raise ValidationError("R2V provider durationSeconds 必须为正数")
        return duration

    def _build_materialized_publication(
        self,
        task: TaskRecord,
        state: R2VTaskState,
        *,
        stable: Mapping[str, str],
        materialized: MaterializedVideo,
        actual_duration: float,
    ) -> tuple[IndexedFile, dict[str, Any]]:
        request = state.request
        if state.provider_result is None:
            raise StorageIntegrityError(
                "R2V materialize claim has no provider result",
            )
        timestamp = datetime.now(UTC)
        relative_uri = PurePosixPath(
            "assets",
            "artifacts",
            f"{stable['file_id']}{materialized.path.suffix}",
        ).as_posix()
        indexed = IndexedFile(
            file_id=stable["file_id"],
            kind="artifact_payload",
            relative_uri=relative_uri,
            sha256=materialized.sha256,
            size_bytes=materialized.size_bytes,
            media_type=materialized.media_type,
            created_at=timestamp,
        )
        artifact = ArtifactVersion(
            version_id=stable["artifact_version_id"],
            slot_id=str(request["slotId"]),
            kind=str(request["slotKind"]),
            owner_ref=str(request["ownerRef"]),
            name=(
                f"{str(request.get('elementLabel') or request['elementId'])} R2V 视频"
            ),
            file_id=stable["file_id"],
            checksum=materialized.sha256,
            based_on_generation=task.input_generation or 0,
            provenance_refs=list(request["provenanceRefs"]),
            duration_seconds=actual_duration,
            input_fingerprint=task.request_fingerprint,
            created_at=timestamp,
            metadata={
                "taskId": task.task_id,
                "runId": task.run_id,
                "commandType": CreatorCommandType.GENERATE_R2V_VIDEO.value,
                "targetRef": str(request["targetRef"]),
                "providerTaskId": state.provider_task_id,
                "generateAudio": bool(request.get("generateAudio", True)),
                "provider": {
                    key: value
                    for key, value in state.provider_result.items()
                    if key
                    not in {
                        "url",
                        "result_url",
                        "video_url",
                        "path",
                        "output_path",
                    }
                },
            },
        )
        published = {
            "taskId": task.task_id,
            "runId": task.run_id,
            "transactionId": stable["transaction_id"],
            "commandType": CreatorCommandType.GENERATE_R2V_VIDEO.value,
            "selfReviewEnabled": is_media_review_enabled(),
            "targetRef": request["targetRef"],
            "providerTaskId": state.provider_task_id,
            "indexedFile": indexed.model_dump(mode="json"),
            "artifactVersion": artifact.model_dump(mode="json"),
            "outputRef": f"artifact-version:{artifact.version_id}",
        }
        return indexed, published

    async def _materialize_video_with_retry(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
    ) -> MaterializedVideo:
        """Download the provider result with bounded transient retries.

        The provider already finished the expensive generation; a flaky
        download must not terminalize the Task and waste that result.
        """

        delays = self.materialize_retry_delays
        for attempt in range(len(delays) + 1):
            try:
                materialized = await materialize_r2v_video(
                    claim.provider_result,
                    project_root=self.services.projects.project_root(
                        task.project_id,
                    ),
                    project_id=task.project_id,
                    task_id=task.task_id,
                    max_bytes=self.max_output_bytes,
                    total_timeout_seconds=self.materialize_timeout_seconds,
                    request_headers=_provider_download_headers(
                        claim.provider_result,
                    ),
                    trusted_private_origins=(
                        _provider_trusted_private_origins()
                    ),
                )
                if claim.request.get("generateAudio", True) is False:
                    from .silent_video import silence_materialized_video

                    materialized = await asyncio.to_thread(
                        silence_materialized_video,
                        materialized,
                    )
                return materialized
            except Exception as error:
                if attempt >= len(delays) or not (
                    _is_transient_materialize_error(error)
                ):
                    raise
                logger.warning(
                    "r2v materialize transient failure, retrying: "
                    "project=%s task=%s attempt=%d error=%s",
                    task.project_id,
                    task.task_id,
                    attempt + 1,
                    _log_safe(str(error)),
                )
                await asyncio.sleep(delays[attempt])
                await self._require_live_materialize_claim(task, claim)
        raise StorageIntegrityError("unreachable materialize retry state")

    async def _persist_materialized_result(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
        descriptor: Mapping[str, Any],
    ) -> None:
        accepted = False

        def update(current: R2VTaskState) -> Mapping[str, Any]:
            nonlocal accepted
            if not self._owns_live_materialize_claim(current, claim):
                return current.model_dump(mode="python")
            accepted = True
            dumped = current.model_dump(mode="python")
            dumped["materialized_result"] = dict(descriptor)
            return dumped

        await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            update,
        )
        if not accepted:
            raise _R2VClaimLost("R2V materialize descriptor claim lost")

    async def _mark_published_owned(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
        published: Mapping[str, Any],
    ) -> dict[str, Any]:
        accepted = False

        def update(current: R2VTaskState) -> Mapping[str, Any]:
            nonlocal accepted
            if not self._owns_live_materialize_claim(current, claim):
                return current.model_dump(mode="python")
            accepted = True
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "phase": "PUBLISHED",
                    "published_result": dict(published),
                    "poll_lease_owner": None,
                    "poll_lease_token": None,
                    "poll_lease_expires_at_epoch": None,
                    "materialize_owner": None,
                    "materialize_claim_token": None,
                    "materialize_claimed_at_epoch": None,
                    "materialize_heartbeat_at_epoch": None,
                    "materialize_claim_expires_at_epoch": None,
                },
            )
            return dumped

        state = await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            update,
        )
        if not accepted or state.published_result is None:
            raise _R2VClaimLost("R2V published-state claim lost")
        return dict(state.published_result)

    async def _mark_cancelled_output_missing_owned(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
        *,
        code: str,
        message: str,
    ) -> None:
        accepted = False

        def update(current: R2VTaskState) -> Mapping[str, Any]:
            nonlocal accepted
            if not self._owns_live_materialize_claim(current, claim):
                return current.model_dump(mode="python")
            accepted = True
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "phase": "CANCELLED",
                    "last_error": {
                        "code": code,
                        "message": message[:2000],
                    },
                    "materialize_owner": None,
                    "materialize_claim_token": None,
                    "materialize_claimed_at_epoch": None,
                    "materialize_heartbeat_at_epoch": None,
                    "materialize_claim_expires_at_epoch": None,
                },
            )
            return dumped

        await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            update,
        )
        if not accepted:
            raise _R2VClaimLost("R2V cancelled-output claim lost")

    @staticmethod
    def _validated_materialized_publication(
        state: R2VTaskState,
        task: TaskRecord,
        stable: Mapping[str, str],
    ) -> tuple[IndexedFile, dict[str, Any]] | None:
        descriptor = state.materialized_result
        if not isinstance(descriptor, Mapping):
            return None
        raw_published = descriptor.get("publishedResult")
        if not isinstance(raw_published, Mapping):
            raise StorageIntegrityError(
                "R2V materialized descriptor is corrupt",
            )
        published = _json_mapping(
            raw_published,
            label="R2V materialized publication",
        )
        if (
            published.get("taskId") != task.task_id
            or published.get("runId") != task.run_id
            or published.get("transactionId") != stable["transaction_id"]
        ):
            raise StorageIntegrityError(
                "R2V materialized publication ownership mismatch",
            )
        indexed = IndexedFile.model_validate(published.get("indexedFile"))
        if indexed.file_id != stable["file_id"]:
            raise StorageIntegrityError(
                "R2V materialized IndexedFile id mismatch",
            )
        try:
            descriptor_size = int(descriptor.get("sizeBytes"))
        except (TypeError, ValueError) as error:
            raise StorageIntegrityError(
                "R2V materialized descriptor size is corrupt",
            ) from error
        if (
            str(descriptor.get("sha256") or "").casefold()
            != indexed.sha256.casefold()
            or descriptor_size != indexed.size_bytes
            or str(descriptor.get("mediaType") or "").casefold()
            != indexed.media_type.casefold()
            or not str(descriptor.get("path") or "").strip()
        ):
            raise StorageIntegrityError(
                "R2V materialized descriptor does not match IndexedFile",
            )
        return indexed, published

    async def _materialize_descriptor_scratch(
        self,
        task: TaskRecord,
        state: R2VTaskState,
    ) -> MaterializedVideo | None:
        """Securely copy and revalidate a durable Task-scratch descriptor."""

        descriptor = state.materialized_result
        if not isinstance(descriptor, Mapping):
            return None
        try:
            recovered = await materialize_r2v_video(
                {
                    "path": str(descriptor["path"]),
                    "checksum": str(descriptor["sha256"]),
                    "media_type": str(descriptor["mediaType"]),
                },
                project_root=self.services.projects.project_root(
                    task.project_id,
                ),
                project_id=task.project_id,
                task_id=task.task_id,
                max_bytes=self.max_output_bytes,
                total_timeout_seconds=self.materialize_timeout_seconds,
            )
        except ValidationError as error:
            # A dead owner may have already removed its private scratch file.
            # Only genuine absence falls through to the provider URL; unsafe
            # paths, links, MIME mismatches and other integrity failures do
            # not.
            if isinstance(error.__cause__, FileNotFoundError):
                return None
            raise
        try:
            expected_size = int(descriptor["sizeBytes"])
            expected_sha = str(descriptor["sha256"]).casefold()
            expected_media_type = str(descriptor["mediaType"]).casefold()
            expected_container = str(descriptor["container"]).casefold()
        except (KeyError, TypeError, ValueError) as error:
            recovered.path.unlink(missing_ok=True)
            raise StorageIntegrityError(
                "R2V materialized descriptor integrity fields are corrupt",
            ) from error
        if (
            recovered.size_bytes != expected_size
            or recovered.sha256.casefold() != expected_sha
            or recovered.media_type.casefold() != expected_media_type
            or recovered.container.casefold() != expected_container
        ):
            recovered.path.unlink(missing_ok=True)
            raise StorageIntegrityError(
                "R2V recovered scratch does not match materialized descriptor",
            )
        return recovered

    async def _adopt_cancelled_materialized_output(
        self,
        task: TaskRecord,
    ) -> bool:
        """Adopt an already-published late result without downloading again."""

        claim = await self._claim_materialize(task)
        if claim is None:
            return False
        heartbeat = asyncio.create_task(
            self._heartbeat_materialize_claim(task, claim),
            name=f"r2v-cancelled-adopt-heartbeat:{task.task_id}",
        )
        published: dict[str, Any] | None = None
        try:
            try:
                recovered = self._validated_materialized_publication(
                    claim,
                    task,
                    _ids(
                        task.project_id,
                        str(task.idempotency_key or task.task_id),
                    ),
                )
                if recovered is None:
                    raise StorageIntegrityError(
                        "R2V cancelled Task has no materialized descriptor",
                    )
                indexed, candidate = recovered
                inspection = await asyncio.to_thread(
                    AssetFileStore(
                        self.services.projects.project_root(task.project_id),
                    ).inspect,
                    indexed,
                )
                await self._require_live_materialize_claim(task, claim)
                if inspection.available:
                    published = await self._mark_published_owned(
                        task,
                        claim,
                        candidate,
                    )
                else:
                    await self._mark_cancelled_output_missing_owned(
                        task,
                        claim,
                        code="R2V_CANCELLED_LATE_OUTPUT_MISSING",
                        message=(
                            "Cancelled R2V late output was not durably published; "
                            "recovery will not redownload it"
                        ),
                    )
            except _R2VClaimLost:
                raise
            except Exception as error:
                await self._mark_cancelled_output_missing_owned(
                    task,
                    claim,
                    code="R2V_CANCELLED_LATE_OUTPUT_INVALID",
                    message=str(error),
                )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        if published is None:
            return True
        latest = await asyncio.to_thread(
            self.executions.get_task,
            task.project_id,
            task.task_id,
        )
        await self._quarantine(
            latest,
            _ids(task.project_id, str(task.idempotency_key or task.task_id)),
            result=published,
            reason="TASK_CANCELLED_BEFORE_IMPORT",
            run_status=SpecialistRunStatus.CANCELLED,
        )
        return True

    async def _materialize_and_publish_owned(
        self,
        task: TaskRecord,
        claim: R2VTaskState,
        *,
        stable: Mapping[str, str],
    ) -> dict[str, Any]:
        request = claim.request
        actual_duration = self._provider_duration(claim, request)
        if claim.provider_result is None:  # validated above
            raise StorageIntegrityError(
                "R2V materialize claim has no provider result",
            )
        file_store = AssetFileStore(
            self.services.projects.project_root(task.project_id),
        )
        materialized: MaterializedVideo | None = None
        descriptor_trimmed = False
        staged: StagedAsset | None = None
        heartbeat = asyncio.create_task(
            self._heartbeat_materialize_claim(task, claim),
            name=f"r2v-materialize-heartbeat:{task.task_id}",
        )
        try:
            await self._require_live_materialize_claim(task, claim)
            recovered = self._validated_materialized_publication(
                claim,
                task,
                stable,
            )
            if recovered is not None:
                recovered_indexed, recovered_published = recovered
                inspection = await asyncio.to_thread(
                    file_store.inspect,
                    recovered_indexed,
                )
                await self._require_live_materialize_claim(task, claim)
                if inspection.available:
                    return await self._mark_published_owned(
                        task,
                        claim,
                        recovered_published,
                    )

                scratch = await self._materialize_descriptor_scratch(
                    task,
                    claim,
                )
                if scratch is not None:
                    materialized = scratch
                    descriptor_trimmed = descriptor_phase_for_recovery(
                        True,
                        claim.materialized_result,
                    )

            if materialized is None:
                materialized = await self._materialize_video_with_retry(
                    task,
                    claim,
                )
            # Wan3.0 extension responses embed the material; cut it
            # before Asset staging so the published artifact is exactly
            # the new segment. A failed trim degrades to keeping the
            # paid full response (longer, untrimmed) instead of
            # terminalizing the task. Recovery from an already-trimmed
            # descriptor re-enters here and is rejected by the offset
            # check, so the cut never runs twice on one artifact.
            trim_offset = float(request.get("continuityTrimOffset") or 0.0)
            # A recovered scratch descriptor may already hold the
            # TRIMMED bytes: the duration check cannot tell 8s-from-13
            # from a native 8s, so the phase is recorded explicitly and
            # recovery reuses the cut instead of paying it twice.
            should_cut, subtract = extend_trim_decision(
                trim_offset,
                descriptor_trimmed,
            )
            trim_applied = descriptor_trimmed
            if subtract:
                actual_duration = max(
                    actual_duration - trim_offset,
                    0.0,
                )
            if should_cut:
                from .extension_trim import trim_extension_prefix

                try:
                    trimmed = await asyncio.to_thread(
                        trim_extension_prefix,
                        materialized,
                        trim_offset,
                    )
                except ValidationError as error:
                    report_error(
                        component="continuity.trim",
                        code="EXTEND_TRIM_FAILED",
                        message=str(error),
                        error=error,
                        project_id=task.project_id,
                        element_id=str(request.get("elementId") or ""),
                    )
                    # A failed trim keeps the paid full response: the
                    # pre-subtracted duration no longer describes it.
                    actual_duration = max(
                        actual_duration + trim_offset,
                        0.0,
                    )
                else:
                    materialized = trimmed
                    trim_applied = True
            await self._require_live_materialize_claim(task, claim)
            indexed, published = self._build_materialized_publication(
                task,
                claim,
                stable=stable,
                materialized=materialized,
                actual_duration=actual_duration,
            )
            await self._persist_materialized_result(
                task,
                claim,
                {
                    "path": str(materialized.path),
                    "sha256": materialized.sha256,
                    "sizeBytes": materialized.size_bytes,
                    "mediaType": materialized.media_type,
                    "container": materialized.container,
                    "trimmed": trim_applied,
                    "publishedResult": published,
                },
            )
            await self._require_live_materialize_claim(task, claim)
            staged = await asyncio.to_thread(
                _stage_materialized_video,
                file_store,
                materialized,
                staging_id=task.task_id[:80],
            )
            await self._require_live_materialize_claim(task, claim)
            try:
                await asyncio.to_thread(
                    file_store.publish,
                    staged,
                    indexed.relative_uri,
                    expected_sha256=materialized.sha256,
                    expected_size_bytes=materialized.size_bytes,
                )
                staged = None
            except AssetAlreadyExists:
                await asyncio.to_thread(file_store.abandon, staged)
                staged = None
                inspection = await asyncio.to_thread(
                    file_store.inspect,
                    indexed,
                )
                if not inspection.available:
                    raise StorageIntegrityError(
                        "稳定 R2V 输出路径已存在但内容不同",
                    )
            await self._require_live_materialize_claim(task, claim)
            return await self._mark_published_owned(task, claim, published)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if staged is not None:
                try:
                    await asyncio.to_thread(file_store.abandon, staged)
                except Exception:
                    pass
            if materialized is not None:
                try:
                    await asyncio.to_thread(
                        materialized.path.unlink,
                        missing_ok=True,
                    )
                except OSError:
                    pass

    async def _complete_success(
        self,
        task: TaskRecord,
        state: R2VTaskState,
    ) -> None:
        stable = _ids(
            task.project_id,
            str(task.idempotency_key or task.task_id),
        )
        published = state.published_result
        if published is None:
            claimed = await self._claim_materialize(task)
            if claimed is None:
                return
            published = await self._materialize_and_publish_owned(
                task,
                claimed,
                stable=stable,
            )
        latest = await record_materialized_result(
            self.executions,
            project_id=task.project_id,
            task_id=task.task_id,
            result=published,
            progress=0.95,
        )
        if latest.status is TaskStatus.CANCELLED:
            await self._quarantine(
                latest,
                stable,
                result=published,
                reason="TASK_CANCELLED_BEFORE_IMPORT",
                run_status=SpecialistRunStatus.CANCELLED,
            )
            return
        await self._converge(latest, stable, published)

    @staticmethod
    def _frozen_inputs_still_current(
        project: Project,
        task: TaskRecord,
    ) -> bool:
        """True when the task's render inputs are unchanged in ``project``.

        Whole-project etag drift treats every commit as fatal, but the most
        common mid-render commit is a review approval of an earlier output
        — it does not touch this Element at all, and quarantining discards
        a finished provider render. Publishing stays allowed when the
        target Element still exists and still selects exactly the
        reference versions the video was rendered from (storyboard
        selection first, then the creation's video references). Prompt or
        settings drift is tolerated because the published video is itself
        gated behind a Review; anything else keeps the fail-closed
        quarantine.
        """

        raw = task.metadata.get("requestSnapshot")
        if not isinstance(raw, Mapping):
            return False
        frozen_refs = raw.get("referenceVersionIds")
        element_id = str(raw.get("elementId") or "")
        if not element_id or not isinstance(frozen_refs, list):
            return False
        try:
            timeline, element = find_timeline_element(project, element_id)
        except Exception:
            return False
        creation = element.creation
        creation_type = getattr(creation, "type", "r2v")
        if isinstance(creation, (R2VCreation, T2VCreation, I2VCreation)):
            admitted_audio = raw.get(
                "projectGenerateAudio",
                raw.get("generateAudio", True),
            )
            if creation.generate_audio != admitted_audio:
                # Older requests stored a command override without separately
                # storing the project's audio intent. An unchanged project
                # input fingerprint proves the override is still valid.
                fingerprint = raw.get("projectInputFingerprint")
                if "projectGenerateAudio" in raw or not fingerprint:
                    return False
                if not _matches_submission_inputs(
                    project,
                    element_id,
                    fingerprint,
                    mode=str(raw.get("mode") or "r2v"),
                    payload_refs=[str(item) for item in frozen_refs],
                ):
                    return False
        if creation_type == "r2v":
            if not isinstance(creation, R2VCreation):
                return False
            if "generationPlan" in raw and (
                creation.generation_plan is None
                or creation.generation_plan.model_dump(mode="json")
                != raw["generationPlan"]
            ):
                return False
            selected = selected_element_output(project, element, "storyboard")
            storyboard_id = selected[1] if selected is not None else None
            if not storyboard_id:
                # Same tier rule as the submit path: a none-tier
                # segment legitimately ships on the last-frame anchor
                # alone. Demanding a storyboard here would quarantine
                # an otherwise-valid finished render whenever an
                # unrelated commit moved the Project etag.
                from models.video_task_plans import (
                    r2v_continuity_storyboard_need,
                )

                legacy_anchor_without_storyboard = (
                    creation.generation_plan is None
                    and creation.storyboard_intent == "none"
                    and any(
                        str(ref).startswith("continuity:last_frame:")
                        for ref in frozen_refs
                    )
                )
                if not legacy_anchor_without_storyboard and (
                    r2v_continuity_storyboard_need(project, timeline, element)
                    != "none"
                ):
                    return False
            current_refs = list(
                _reference_ids_with_continuity(
                    project,
                    timeline,
                    element,
                    mode=str(raw.get("mode") or "r2v"),
                ),
            )
            if current_refs == [str(item) for item in frozen_refs]:
                return True
            if "generationPlan" in raw and raw.get("mode") == "video_extend":
                return False
            # Already-paid requests retain their original exact inputs,
            # including the historical explicit-storyboard duplication.
            # This comparison never recompiles or resubmits that request;
            # a changed current storyboard still invalidates its first ID.
            legacy_refs = list(
                dict.fromkeys(
                    [
                        *([storyboard_id] if storyboard_id else []),
                        *resolve_r2v_visual_reference_version_ids(
                            project,
                            creation,
                            creation.video_reference_version_ids,
                        ),
                    ],
                ),
            )
            if legacy_refs == [str(item) for item in frozen_refs]:
                return True
            # Pre-continuity tasks were frozen without the synthetic
            # last_frame / tail_clip IDs. Re-check by stripping them
            # from the current list so already-paid renders still
            # publish instead of being quarantined for a fingerprint
            # drift the submit path never had.
            stripped_current = [
                item
                for item in current_refs
                if not str(item).startswith("continuity:")
            ]
            stripped_frozen = [
                str(item)
                for item in frozen_refs
                if not str(item).startswith("continuity:")
            ]
            # A predecessor reroll mints new ``continuity:*:prevV2`` IDs,
            # but the Project references this render was actually paid
            # for (storyboard + visual anchors) are unchanged. Demanding
            # the frozen prevV1 anchors still resolve would quarantine a
            # finished, already-billed clip on every prev reroll — the
            # exact paid cascade the feature promises not to cause. The
            # Project-side inputs matching is the real publish gate.
            if stripped_current == stripped_frozen and frozen_refs:
                return True
            return False
        elif creation_type == "t2v":
            if not isinstance(creation, T2VCreation):
                return False
            return not frozen_refs
        elif creation_type == "i2v":
            if not isinstance(creation, I2VCreation):
                return False
            resolved_first_frame = raw.get("firstFrameVersionId") or ""
            if str(resolved_first_frame).startswith("continuity:"):
                # The paid render anchored on the predecessor's last
                # frame. A prev reroll mints a new synthetic version but
                # this clip is already generated — publishing it is the
                # "no paid cascade" contract, mirroring the r2v branch.
                # Quarantine only when the element stopped auto-framing
                # (an explicit first frame was authored since: a genuine
                # input change) or the predecessor frame is gone entirely.
                if getattr(creation, "first_frame_version_id", ""):
                    return False
                if (
                    "predecessorElementId" in raw
                    and creation.predecessor_element_id
                    != raw["predecessorElementId"]
                ):
                    return False
                frozen_last = str(raw.get("lastFrameVersionId") or "")
                if frozen_last and frozen_last != str(
                    getattr(creation, "last_frame_version_id", "") or "",
                ):
                    # The shipped tail frame was re-authored since
                    # payment: a genuine input change, quarantine.
                    return False
                try:
                    predecessor = planned_predecessor(timeline, element)
                except ValidationError:
                    return False
                return bool(
                    continuity_reference_ids(
                        project,
                        timeline,
                        element,
                        predecessor=predecessor,
                    )
                )
            authored_first_frame = creation.first_frame_version_id
            if (
                authored_first_frame
                and authored_first_frame != resolved_first_frame
            ):
                return False
            current_refs = (
                [resolved_first_frame] if resolved_first_frame else []
            )
            # Only the frames the payload ACTUALLY shipped are part
            # of the comparison: an authored tail frame the model
            # could not receive was never sent and must not make a
            # good finished clip look stale.
            if str(raw.get("lastFrameUrl") or ""):
                current_last = str(
                    getattr(creation, "last_frame_version_id", "") or "",
                )
                if current_last:
                    current_refs.append(current_last)
            return current_refs == [str(item) for item in frozen_refs]
        elif creation_type == "s2v":
            if not isinstance(creation, S2VCreation):
                return False
            current_refs = []
            s2v_image = raw.get("s2vImageVersionId") or ""
            s2v_audio = raw.get("s2vAudioVersionId") or ""
            if s2v_image:
                current_refs.append(s2v_image)
            if s2v_audio:
                current_refs.append(s2v_audio)
            return current_refs == [str(item) for item in frozen_refs]
        return False

    async def _converge(
        self,
        task: TaskRecord,
        stable: Mapping[str, str],
        result: Mapping[str, Any],
    ) -> None:
        review_reservation = reserve_media_review(
            self.services,
            project_id=task.project_id,
            published_result=result,
        )

        def commit_if_live() -> tuple[str, TaskRecord, ProjectSnapshot | None]:
            with self.services.projects.lifecycle_lock(task.project_id):
                latest = self.executions.get_task(
                    task.project_id,
                    task.task_id,
                    _lifecycle_lock_held=True,
                )
                if latest.status is TaskStatus.CANCELLED:
                    return "CANCELLED", latest, None
                if latest.status in {
                    TaskStatus.FAILED,
                    TaskStatus.QUARANTINED,
                }:
                    return latest.status.value, latest, None
                current = self.services.projects.read(task.project_id)
                if self._result_is_converged(current.project, result):
                    snapshot = current
                else:
                    if (
                        current.etag != latest.input_etag
                        or current.generation != latest.input_generation
                    ) and not self._frozen_inputs_still_current(
                        current.project,
                        latest,
                    ):
                        return "STALE", latest, current
                    candidate = current.project.model_dump(mode="json")
                    self._apply_result(candidate, result)
                    # Generated video is reviewed before acceptance unless
                    # the operator opted into unattended auto-approval; an
                    # AUTO_FIX round must not carry a ReviewBoundary.
                    review_policy = media_review_policy()
                    review_boundary = (
                        self.services.commits.runtime_review_boundary(
                            task.project_id,
                            run_id=str(task.run_id),
                            request_id=latest.caused_by_request_id,
                        )
                        if review_policy is ReviewPolicy.REQUIRE_REVIEW
                        else None
                    )
                    commit = self.services.commits.commit(
                        base=current,
                        candidate=candidate,
                        origin=ChangeOrigin.RUNTIME_TASK,
                        review_policy=review_policy,
                        review_boundary=review_boundary,
                        caused_by_request_id=latest.caused_by_request_id,
                        round_id=stable["round_id"],
                        transaction_id=stable["transaction_id"],
                        advance_accepted_baseline=True,
                        _lifecycle_lock_held=True,
                    )
                    snapshot = commit.snapshot
                success = {
                    **dict(result),
                    "projectEtag": snapshot.etag,
                    "projectGeneration": snapshot.generation,
                }
                if latest.status is TaskStatus.RUNNING:
                    self.executions.append_attempt(
                        task.project_id,
                        task.task_id,
                        event_id=stable["attempt_succeeded_event_id"],
                        attempt_id=stable["attempt_id"],
                        status=TaskAttemptStatus.SUCCEEDED,
                        provider_task_id=(
                            str(result.get("providerTaskId") or "") or None
                        ),
                        output=success,
                        output_refs=[str(success["outputRef"])],
                        _lifecycle_lock_held=True,
                    )
                    latest = self.executions.get_task(
                        task.project_id,
                        task.task_id,
                        _lifecycle_lock_held=True,
                    )
                return "SUCCEEDED", latest, snapshot

        try:
            outcome, latest, snapshot = await commit_with_lock_retry(
                commit_if_live,
                project_id=task.project_id,
                task_id=task.task_id,
            )
        except BaseException:
            release_media_review_reservation(review_reservation)
            raise
        if outcome == "CANCELLED":
            release_media_review_reservation(review_reservation)
            await self._quarantine(
                latest,
                stable,
                result=result,
                reason="TASK_CANCELLED_BEFORE_IMPORT",
                run_status=SpecialistRunStatus.CANCELLED,
            )
            return
        if outcome == "STALE":
            release_media_review_reservation(review_reservation)
            await self._quarantine(
                latest,
                stable,
                result=result,
                reason="PROJECT_INPUT_SNAPSHOT_STALE",
                run_status=SpecialistRunStatus.STALE,
            )
            return
        if outcome != "SUCCEEDED" or snapshot is None:
            release_media_review_reservation(review_reservation)
            return
        try:
            await asyncio.to_thread(self.services.poller.note_commit, snapshot)
            success = (
                latest.result
                if isinstance(latest.result, dict)
                else {
                    **dict(result),
                    "projectEtag": snapshot.etag,
                    "projectGeneration": snapshot.generation,
                }
            )
            # Run-review hook: every successful convergence (fresh render,
            # idempotent replay, crash recovery) flows through this single
            # point. Scheduling is advisory and idempotent: the switch, the
            # command filter and the already-reviewed dedup live on the review
            # side.
            schedule_media_review(
                self.services,
                project_id=task.project_id,
                published_result=success,
                reservation_token=review_reservation,
            )
        except BaseException:
            release_media_review_reservation(review_reservation)
            raise
        await self._finish_run(
            task.project_id,
            str(task.run_id),
            SpecialistRunStatus.SUCCEEDED,
            summary="R2V 视频已生成并写入 Project",
        )

        def succeeded(current: R2VTaskState) -> Mapping[str, Any]:
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "phase": "SUCCEEDED",
                    "published_result": success,
                    "last_error": None,
                },
            )
            return dumped

        await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            succeeded,
        )

    @staticmethod
    def _result_is_converged(
        project: Project,
        result: Mapping[str, Any],
    ) -> bool:
        try:
            indexed = IndexedFile.model_validate(result["indexedFile"])
            artifact = ArtifactVersion.model_validate(
                result["artifactVersion"],
            )
            element_id = _target_element_id(str(result["targetRef"]))
        except (KeyError, TypeError, ValueError):
            return False
        slot = project.assets.artifact_slots_by_id.get(artifact.slot_id)
        try:
            _, element = find_timeline_element(project, element_id)
        except NotFoundError:
            return False
        selected = selected_element_output(project, element, "main")
        return (
            project.assets.files_by_id.get(indexed.file_id) == indexed
            and project.assets.artifact_versions_by_id.get(artifact.version_id)
            == artifact
            and slot is not None
            and slot.selected_version_id == artifact.version_id
            and selected is not None
            and selected[1] == artifact.version_id
        )

    @staticmethod
    def _apply_result(
        candidate: dict[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        indexed = IndexedFile.model_validate(result["indexedFile"])
        artifact = ArtifactVersion.model_validate(result["artifactVersion"])
        element_id = _target_element_id(str(result["targetRef"]))
        assets = candidate["assets"]
        files = assets["files_by_id"]
        versions = assets["artifact_versions_by_id"]
        slots = assets["artifact_slots_by_id"]
        indexed_json = indexed.model_dump(mode="json")
        artifact_json = artifact.model_dump(mode="json")
        if indexed.file_id in files and files[indexed.file_id] != indexed_json:
            raise ConflictError("R2V IndexedFile 稳定 ID 内容冲突")
        if (
            artifact.version_id in versions
            and versions[artifact.version_id] != artifact_json
        ):
            raise ConflictError("R2V ArtifactVersion 稳定 ID 内容冲突")
        files[indexed.file_id] = indexed_json
        versions[artifact.version_id] = artifact_json
        raw_slot = slots.get(artifact.slot_id)
        if raw_slot is None:
            slots[artifact.slot_id] = ArtifactSlot(
                slot_id=artifact.slot_id,
                kind=artifact.kind,
                owner_ref=artifact.owner_ref,
                version_ids=[artifact.version_id],
                selected_version_id=artifact.version_id,
            ).model_dump(mode="json")
        else:
            if (
                raw_slot["kind"] != artifact.kind
                or raw_slot["owner_ref"] != artifact.owner_ref
            ):
                raise ConflictError("R2V ArtifactSlot 归属冲突")
            if artifact.version_id not in raw_slot["version_ids"]:
                raw_slot["version_ids"].append(artifact.version_id)
            raw_slot["selected_version_id"] = artifact.version_id
        bind_candidate_output(
            candidate,
            element_id=element_id,
            output_name="main",
            slot_id=artifact.slot_id,
            select_for_render=True,
        )
        # The provider decides the real footage length (s2v follows its
        # driving audio); align the timeline with what was actually billed
        # and delivered instead of freezing the last frame to pad the plan.
        reconcile_candidate_span(
            candidate,
            element_id=element_id,
            actual_duration_seconds=artifact.duration_seconds,
        )

    async def _quarantine(
        self,
        task: TaskRecord,
        stable: Mapping[str, str],
        *,
        result: Mapping[str, Any],
        reason: str,
        run_status: SpecialistRunStatus,
    ) -> None:
        latest = await asyncio.to_thread(
            self.executions.get_task,
            task.project_id,
            task.task_id,
        )
        if latest.status is TaskStatus.RUNNING:
            try:
                await asyncio.to_thread(
                    self.executions.append_attempt,
                    task.project_id,
                    task.task_id,
                    event_id=stable["attempt_quarantined_event_id"],
                    attempt_id=stable["attempt_id"],
                    status=TaskAttemptStatus.QUARANTINED,
                    provider_task_id=str(result.get("providerTaskId") or "")
                    or None,
                    output=dict(result),
                    output_refs=[str(result.get("outputRef") or "")],
                    error={"code": reason, "message": reason},
                )
            except ExecutionStateConflict:
                pass
        await asyncio.to_thread(
            self.executions.quarantine_task_result,
            task.project_id,
            task.task_id,
            reason=reason,
            result=dict(result),
            quarantine_id=stable["quarantine_id"],
            transition_task=False,
        )
        await self._finish_run(
            task.project_id,
            str(task.run_id),
            run_status,
        )

        def quarantined(current: R2VTaskState) -> Mapping[str, Any]:
            dumped = current.model_dump(mode="python")
            dumped.update(
                {
                    "phase": "QUARANTINED",
                    "published_result": dict(result),
                    "last_error": {"code": reason, "message": reason},
                    "poll_lease_owner": None,
                    "poll_lease_token": None,
                    "poll_lease_expires_at_epoch": None,
                    "materialize_owner": None,
                    "materialize_claim_token": None,
                    "materialize_claimed_at_epoch": None,
                    "materialize_heartbeat_at_epoch": None,
                    "materialize_claim_expires_at_epoch": None,
                },
            )
            return dumped

        await asyncio.to_thread(
            self._update_state_sync,
            task.project_id,
            task.task_id,
            quarantined,
        )

    async def _fail(
        self,
        task: TaskRecord,
        *,
        code: str,
        message: str,
        retryable: bool = False,
        error: BaseException | None = None,
    ) -> None:
        report = report_error(
            component="r2v-execution",
            code=code,
            message=message,
            error=error,
            retryable=retryable,
            details={
                "projectId": task.project_id,
                "taskId": task.task_id,
                "runId": str(task.run_id or ""),
            },
            projectId=task.project_id,
            taskId=task.task_id,
            runId=str(task.run_id or ""),
        )
        failure = {
            key: value for key, value in report.items() if value is not None
        }
        stable = _ids(
            task.project_id,
            str(task.idempotency_key or task.task_id),
        )
        latest = await asyncio.to_thread(
            self._fail_active_task_locked,
            task,
            error=failure,
            stable=stable,
        )
        if latest.status in _TERMINAL_TASKS:
            aligned = await self._align_state_to_terminal_task(latest)
            if not aligned:
                self._schedule_terminal_recovery(
                    latest.project_id,
                    latest.task_id,
                )
        await asyncio.to_thread(
            reconcile_terminal_task_runs,
            self.services.root,
            [task.project_id],
        )

    async def _mark_cancelled(self, task: TaskRecord) -> None:
        try:
            deferred = False

            def cancelled(current: R2VTaskState) -> Mapping[str, Any]:
                nonlocal deferred
                if (
                    current.phase == "SUBMIT_CLAIMED"
                    and current.provider_task_id is None
                    and (current.submit_claim_expires_at_epoch or 0.0)
                    > self.clock()
                ):
                    # The submit owner must durably bind the external id before
                    # cancellation can terminalize this state.  Otherwise a
                    # second supervisor could erase the only recovery anchor.
                    deferred = True
                    return current.model_dump(mode="python")
                dumped = current.model_dump(mode="python")
                dumped.update(
                    {
                        "phase": "CANCELLED",
                        "submit_owner": None,
                        "submit_claim_token": None,
                        "submit_claim_expires_at_epoch": None,
                        "poll_lease_owner": None,
                        "poll_lease_token": None,
                        "poll_lease_expires_at_epoch": None,
                        "materialize_owner": None,
                        "materialize_claim_token": None,
                        "materialize_claimed_at_epoch": None,
                        "materialize_heartbeat_at_epoch": None,
                        "materialize_claim_expires_at_epoch": None,
                    },
                )
                return dumped

            await asyncio.to_thread(
                self._update_state_sync,
                task.project_id,
                task.task_id,
                cancelled,
            )
            if deferred:
                if task.status in _TERMINAL_TASKS:
                    self._schedule_terminal_recovery(
                        task.project_id,
                        task.task_id,
                    )
                return
        except RecordNotFoundError:
            pass
        await self._finish_run(
            task.project_id,
            str(task.run_id),
            SpecialistRunStatus.CANCELLED,
        )

    async def _finish_run(
        self,
        project_id: str,
        run_id: str,
        status: SpecialistRunStatus,
        *,
        summary: str | None = None,
    ) -> None:
        if not run_id:
            return
        try:
            run = await asyncio.to_thread(
                self.executions.get_run,
                project_id,
                run_id,
            )
        except RecordNotFoundError:
            return
        if run.status in {
            SpecialistRunStatus.SUCCEEDED,
            SpecialistRunStatus.BLOCKED,
            SpecialistRunStatus.FAILED,
            SpecialistRunStatus.STALE,
            SpecialistRunStatus.CANCELLED,
        }:
            return
        try:
            await asyncio.to_thread(
                self.executions.transition_run,
                project_id,
                run_id,
                expected_status=run.status,
                status=status,
                updates={"final_summary_text": summary} if summary else None,
            )
        except ExecutionStateConflict:
            pass

    async def recover_all(self) -> int:
        recovered = 0
        for project_id in self.services.projects.discover_project_ids():
            for run in self.executions.list_specialist_runs(project_id):
                if (
                    run.role is SpecialistRole.R2V_GENERATION_DIRECTOR
                    and run.metadata.get("commandType")
                    == CreatorCommandType.GENERATE_R2V_VIDEO.value
                ):
                    try:
                        await self._recover_run_admission_gap(run)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # One corrupt Project must not prevent unrelated
                        # Projects from recovering during service startup.
                        logger.exception(
                            "R2V admission-gap recovery isolated failure "
                            "for %s/%s",
                            project_id,
                            run.run_id,
                        )
            for task in self.executions.list_tasks(project_id):
                if task.kind is not TaskKind.R2V_GENERATION:
                    continue
                try:
                    if task.status in _TERMINAL_TASKS:
                        aligned = await self._align_state_to_terminal_task(
                            task,
                        )
                        if not aligned:
                            self._schedule_terminal_recovery(
                                project_id,
                                task.task_id,
                            )
                        continue
                    if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                        run = self.executions.get_run(
                            project_id,
                            str(task.run_id),
                        )
                        if await self._ensure_state_for_task(run, task):
                            self.start_task(project_id, task.task_id)
                            recovered += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One corrupt Task must not prevent unrelated Projects and
                    # Tasks from recovering during service startup.
                    logger.exception(
                        "R2V startup recovery isolated failure for %s/%s",
                        project_id,
                        task.task_id,
                    )
                    if task.status in _TERMINAL_TASKS:
                        self._schedule_terminal_recovery(
                            project_id,
                            task.task_id,
                        )
        await asyncio.to_thread(
            reconcile_terminal_task_runs,
            self.services.root,
            self.services.projects.discover_project_ids(),
        )
        orphaned = await asyncio.to_thread(
            reconcile_orphaned_queued_runs,
            self.services.root,
            self.services.projects.discover_project_ids(),
        )
        if orphaned.cancelled_count:
            logger.warning(
                "orphaned queued Runs cancelled at startup: %s",
                ", ".join(orphaned.cancelled_run_ids),
            )
        return recovered

    async def shutdown(self) -> None:
        workers = [
            *self._jobs.values(),
            *self._terminal_recovery_jobs.values(),
        ]
        self._jobs.clear()
        self._job_projects.clear()
        self._terminal_recovery_jobs.clear()
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)


def _has_accepted_provider_task(task_id: str, project_id: str) -> bool:
    """Whether a *resumable* billed provider task exists for this Task.

    Only ledger kinds the image resume supervisor supports count: handing
    it an accepted-but-unresumable job (async image generation today) would
    end supervision as "unsupported" and strand the Task in RUNNING, so
    those fall through to the fail-closed terminalization below — the
    billed ids stay named in the error for manual retrieval.
    """

    try:
        from .image_execution import resumable_provider_entries

        return bool(resumable_provider_entries(task_id, project_id))
    except Exception:  # noqa: BLE001 - absence of a ledger is not an error
        return False


def _accepted_provider_tasks_note(task_id: str, project_id: str) -> str:
    """Name any billed provider task an interrupted Task already paid for.

    Asynchronous provider submissions (e.g. qwen-mt-image translation) are
    billed on acceptance and their result stays retrievable for 24h, so a
    terminalized Task must say which ids to fetch instead of losing them.
    """

    from models.provider_tasks import read_provider_tasks

    try:
        entries = read_provider_tasks(task_id, project_id)
    except Exception:  # noqa: BLE001 - bookkeeping must not break recovery
        return ""
    if not entries:
        return ""
    described = ", ".join(
        f"{entry.get('model', '?')}:{entry.get('providerTaskId', '?')}"
        for entry in entries
    )
    return (
        "; already accepted (billed) provider tasks whose results stay "
        f"retrievable for 24h: {described}"
    )


async def recover_interrupted_image_tasks(
    services: CreatorFileServices,
) -> int:
    """Never resubmit an interrupted one-shot image provider call.

    A published Task result is converged from durable files.  Any active image
    Task without such a result is terminalized because the provider may have
    accepted the request before the process died.
    """

    from .image_execution import file_image_execution_service

    worker = file_image_execution_service(services)
    executions = worker.executions
    recovered = 0
    for project_id in services.projects.discover_project_ids():
        for task in executions.list_tasks(project_id):
            if (
                task.kind is not TaskKind.IMAGE_GENERATION
                or task.status
                not in {
                    TaskStatus.QUEUED,
                    TaskStatus.RUNNING,
                }
            ):
                continue
            stable = (
                worker._ids(  # noqa: SLF001 - same package recovery boundary
                    project_id,
                    str(task.idempotency_key or task.task_id),
                )
            )
            if stable["task_id"] != task.task_id:
                # Scheduler-dispatched tasks (idempotency keys like
                # ``dag-storyboard:...``) carry execution-store ids, not
                # the file-image command namespace recomputed above.
                # Field run 2026-08-12 (project 27dc): the recomputed id
                # missed the record, _fail_if_running returned on
                # RecordNotFound and the interrupted RUNNING task
                # survived restart as a zombie pinning its work-graph
                # lane RUNNING forever. Recover the record we actually
                # iterated — including its live attempt, which the
                # fail-close must finish (a foreign-namespace attempt id
                # would open a second attempt and leave the first, and
                # therefore the Task, RUNNING).
                stable = {
                    **stable,
                    "task_id": task.task_id,
                    "run_id": str(task.run_id or stable["run_id"]),
                }
                attempts = executions.list_task_attempts(
                    project_id,
                    task.task_id,
                )
                if attempts:
                    stable["attempt_id"] = attempts[-1].attempt_id
            if isinstance(task.result, dict):
                try:
                    await worker._converge(  # noqa: SLF001
                        task=task,
                        ids=stable,
                        replayed=True,
                    )
                except ConflictError:
                    pass
                recovered += 1
                continue
            if task.status is TaskStatus.RUNNING:
                # A resumable server-side provider job (qwen-mt-image
                # translation) is billed on acceptance and its id is durable,
                # so hand it to the background poller rather than discarding
                # a paid result or blocking startup on it. Unresumable
                # provider jobs and one-shot synchronous calls stay
                # fail-closed below.
                if _has_accepted_provider_task(task.task_id, project_id):
                    worker.schedule_resume(task)
                    recovered += 1
                    continue
                await worker._fail_if_running(  # noqa: SLF001
                    project_id,
                    stable,
                    "IMAGE_PROCESS_RESTARTED",
                    message=(
                        "image provider call was interrupted; refusing automatic "
                        "non-idempotent resubmission"
                        + _accepted_provider_tasks_note(
                            task.task_id,
                            project_id,
                        )
                    ),
                )
            else:
                error = {
                    "code": "IMAGE_PROCESS_RESTARTED",
                    "message": (
                        "image Task was interrupted; refusing automatic provider "
                        "submission"
                    ),
                    "retryable": True,
                }
                executions.transition_task(
                    project_id,
                    task.task_id,
                    expected_status=TaskStatus.QUEUED,
                    status=TaskStatus.FAILED,
                    updates={"error": error},
                )
                await worker._finish_run(  # noqa: SLF001
                    project_id,
                    str(task.run_id),
                    SpecialistRunStatus.FAILED,
                )
            recovered += 1
    return recovered


_registry_lock = threading.RLock()
_registry: dict[Path, FileR2VExecutionService] = {}


def file_r2v_execution_service(
    services: CreatorFileServices,
) -> FileR2VExecutionService:
    root = services.root.resolve()
    with _registry_lock:
        service = _registry.get(root)
        if service is None:
            service = FileR2VExecutionService(services)
            _registry[root] = service
        return service


async def start_file_media_execution_services(
    services: CreatorFileServices,
    *,
    provider: R2VProvider | None = None,
    poll_interval_seconds: float = 2.0,
    poll_lease_seconds: float = 120.0,
    submit_timeout_seconds: float = _SUBMIT_TIMEOUT_SECONDS,
    submit_claim_seconds: float = _SUBMIT_CLAIM_SECONDS,
    materialize_timeout_seconds: float = _MATERIALIZE_TIMEOUT_SECONDS,
    materialize_claim_seconds: float = _MATERIALIZE_CLAIM_SECONDS,
    max_output_bytes: int = _MAX_VIDEO_BYTES,
) -> FileR2VExecutionService:
    """Start recovery/supervision for R2V and fail-close one-shot images."""

    root = services.root.resolve()
    with _registry_lock:
        current = _registry.get(root)
        if current is None:
            current = FileR2VExecutionService(
                services,
                provider=provider,
                poll_interval_seconds=poll_interval_seconds,
                poll_lease_seconds=poll_lease_seconds,
                submit_timeout_seconds=submit_timeout_seconds,
                submit_claim_seconds=submit_claim_seconds,
                materialize_timeout_seconds=materialize_timeout_seconds,
                materialize_claim_seconds=materialize_claim_seconds,
                max_output_bytes=max_output_bytes,
            )
            _registry[root] = current
        elif provider is not None and current.provider is not provider:
            raise RuntimeError(
                "R2V execution service already uses another provider",
            )
    from .interaction_execution import recover_interrupted_interaction_tasks

    await asyncio.to_thread(recover_interrupted_interaction_tasks, services)
    await recover_interrupted_image_tasks(services)
    from .local_execution import recover_file_local_media_project
    from services.project_files.store import ProjectIntegrityError

    for project_id in services.projects.discover_project_ids():
        try:
            await recover_file_local_media_project(services, project_id)
        except ProjectIntegrityError:
            # One corrupt/foreign project.json must not veto the whole
            # runtime: skipping keeps every healthy Project operational
            # while the corrupt one is already surfaced by recovery logs.
            logger.exception(
                "skipping local media recovery for corrupt Project %s",
                project_id,
            )
    project_ids = services.projects.discover_project_ids()
    await asyncio.to_thread(
        reconcile_terminal_task_runs,
        services.root,
        project_ids,
    )
    await current.recover_all()
    return current


async def shutdown_file_media_execution_services() -> None:
    with _registry_lock:
        services = list(_registry.values())
        _registry.clear()
    if services:
        await asyncio.gather(
            *(service.shutdown() for service in services),
            return_exceptions=True,
        )
    from .image_execution import shutdown_file_image_execution_services

    await shutdown_file_image_execution_services()


def clear_file_media_execution_registry_for_tests() -> None:
    with _registry_lock:
        if any(  # noqa: SLF001
            service._jobs or service._terminal_recovery_jobs
            for service in _registry.values()
        ):
            raise RuntimeError(
                "shutdown file media services before clearing registry",
            )
        _registry.clear()


async def execute_file_r2v_command(
    services: CreatorFileServices,
    *,
    project_id: str,
    target_ref: str,
    arguments: Mapping[str, Any],
    idempotency_key: str,
    expected_object_versions: Sequence[str] = (),
) -> FileR2VDispatch:
    return await file_r2v_execution_service(services).dispatch(
        project_id=project_id,
        target_ref=target_ref,
        arguments=arguments,
        idempotency_key=idempotency_key,
        expected_object_versions=expected_object_versions,
    )


async def execute_file_s2v_command(
    services: CreatorFileServices,
    *,
    project_id: str,
    target_ref: str,
    arguments: Mapping[str, Any],
    idempotency_key: str,
    expected_object_versions: Sequence[str] = (),
) -> FileR2VDispatch:
    """Digital-human (wan2.2-s2v) dispatch through the R2V durable poller."""

    return await file_r2v_execution_service(services).dispatch(
        project_id=project_id,
        target_ref=target_ref,
        arguments=arguments,
        idempotency_key=idempotency_key,
        expected_object_versions=expected_object_versions,
        s2v=True,
    )


async def preflight_s2v_face_detect(
    services: CreatorFileServices,
    *,
    project_id: str,
    target_ref: str = "",
    arguments: Mapping[str, Any],
) -> None:
    """Free wan2.2-s2v-detect gate that runs before execution authorization.

    A failed portrait check raises a readable ``ValidationError`` so the
    agent can pick another image without any authorization being created.
    Falls back to creation.portrait_version_id if characterImageRef is
    not provided in arguments.
    """

    from models.s2v_model import detect_face

    image_ref = str(arguments.get("characterImageRef") or "").strip()
    snapshot = None
    if not image_ref and target_ref:
        # Fall back to the element's declared portrait
        snapshot = await asyncio.to_thread(services.projects.read, project_id)
        element_id = _target_element_id(
            target_ref,
            command="GENERATE_S2V_VIDEO",
        )
        _, element = find_timeline_element(snapshot.project, element_id)
        creation = element.creation
        if isinstance(creation, S2VCreation):
            image_ref = str(creation.portrait_version_id or "").strip()
        else:
            raise ValidationError(
                "仅 creation.type=s2v 的 Element 可以生成数字人视频",
            )
    if not image_ref:
        raise ValidationError(
            "s2v_generation 需要 characterImageRef 或 "
            "creation.portrait_version_id（exact 人像图 version id）",
        )
    if snapshot is None:
        snapshot = await asyncio.to_thread(services.projects.read, project_id)
    image_url, _, _, _ = _resolve_single_media_version(
        project=snapshot.project,
        project_root=services.projects.project_root(project_id),
        version_id=image_ref,
        media_prefix="image/",
        label="characterImageRef",
    )
    result = await detect_face(image_url)
    if not result.passed:
        raise ValidationError(
            f"数字人人像检测未通过（免费 detect，未创建执行授权）："
            f"{result.reason or '未知原因'}。常见原因：多人/侧脸/模糊/遮挡/"
            "风格不支持；请换一张单人、正脸、清晰的角色图后重试",
        )


__all__ = [
    "ExistingR2VProvider",
    "FileR2VDispatch",
    "FileR2VExecutionService",
    "R2VProvider",
    "R2VTaskState",
    "clear_file_media_execution_registry_for_tests",
    "execute_file_r2v_command",
    "execute_file_s2v_command",
    "file_r2v_execution_service",
    "media_version_duration_seconds",
    "preflight_s2v_face_detect",
    "recover_interrupted_image_tasks",
    "shutdown_file_media_execution_services",
    "start_file_media_execution_services",
]
