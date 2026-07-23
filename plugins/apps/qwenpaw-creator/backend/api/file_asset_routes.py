# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=raise-missing-from,too-many-branches,too-many-statements
# pylint: disable=unused-argument
"""File-native immutable Asset ingest, index, import and content routes."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import ipaddress
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import socket
from typing import Any, Callable, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import Field
from starlette.datastructures import UploadFile

from domain.errors import (
    ConflictError,
    NotFoundError,
    StorageIntegrityError,
    ValidationError,
)
from domain.enums import TaskKind, TaskStatus
from schemas.assets import TextOrUrlAssetRequest
from schemas.common import StrictModel
from services.project_files.assets import (
    AssetAlreadyExists,
    AssetFileError,
    AssetFileStore,
    StagedAsset,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    IndexedFile,
    ProjectSource,
    SourceAssetVersion,
)
from services.project_files.remote_cache import (
    publish_remote_cache,
    remote_source_media_type,
    remote_source_name,
    resolve_remote_cache,
)
from services.runtime_files.atomic_store import AtomicJsonRecordStore
from services.runtime_files.errors import (
    IdempotencyConflictError,
    IdempotencyStateConflictError,
    RecordNotFoundError,
)
from services.runtime_files.execution_models import TaskRecord
from services.runtime_files.execution_store import (
    ExecutionPayloadConflict,
    ExecutionStateConflict,
    ExecutionStoreError,
    ProjectExecutionStore,
)
from services.runtime_files.idempotency_store import IdempotencyRecordStore
from services.runtime_files.models import (
    ChangeOrigin,
    IdempotencyStatus,
    ReviewPolicy,
)
from services.runtime_files.session_store import (
    ProjectRuntimeSessionStore,
    RuntimeSessionNotFound,
)

from .content_disposition import inline_content_disposition
from .dependencies import (
    CreatorErrorRoute,
    project_file_services,
    resolve_idempotency_key,
)


router = APIRouter(
    prefix="/projects/{project_id}",
    tags=["asset-files"],
    route_class=CreatorErrorRoute,
)

_DEFAULT_MAX_REMOTE_ASSET_BYTES = 2 * 1024 * 1024 * 1024
_DEFAULT_REMOTE_ASSET_TIMEOUT_SECONDS = 60 * 60
_MAX_LOCAL_VIDEO_UPLOAD_BYTES = 100 * 1024 * 1024
_MAX_REMOTE_REDIRECTS = 5
_REMOTE_INGEST_TASKS: dict[tuple[str, str], asyncio.Task[None]] = {}


class AssetImportItemRecord(StrictModel):
    name: str
    asset_version_id: str = Field(alias="assetVersionId")
    checksum: str


class AssetImportRecord(StrictModel):
    import_id: str = Field(alias="importId")
    task_id: str = Field(alias="taskId")
    project_id: str = Field(alias="projectId")
    status: Literal["SUCCEEDED", "FAILED"]
    progress: float = Field(ge=0, le=1)
    items: list[AssetImportItemRecord] = Field(default_factory=list)
    failures: list[dict[str, str]] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    created_at: datetime = Field(alias="createdAt")


class _AssetInput(StrictModel):
    name: str
    content: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class _StagedAssetInput:
    name: str
    staged: StagedAsset
    media_type: str


@dataclass(frozen=True, slots=True)
class _RemoteAssetDownload:
    name: str
    staged: StagedAsset
    media_type: str


class _BoundedRawReader:
    """Expose an HTTP byte iterator as a bounded file-like stream."""

    def __init__(
        self,
        chunks: Any,
        *,
        max_bytes: int,
        total_bytes: int | None = None,
        on_progress: Callable[[int, int | None], None] | None = None,
    ) -> None:
        self._chunks = iter(chunks)
        self._max_bytes = max_bytes
        self._size_bytes = 0
        self._total_bytes = total_bytes
        self._on_progress = on_progress

    def read(self, _size: int = -1) -> bytes:
        # ``response.iter_raw()`` yields transport-sized chunks (commonly
        # 16-64 KiB). Coalesce them up to the caller's requested size so the
        # progress callback, the staging write, and the sha256 update fire once
        # per caller chunk (1 MiB from AssetFileStore) rather than once per
        # transport chunk. For a 1 GiB asset that is ~1e3 callbacks instead of
        # ~1e5 — each callback currently triggers a locked ``task.json`` read,
        # so this alone removes ~64x of that overhead.
        target = _size if _size and _size > 0 else self._max_bytes
        parts: list[bytes] = []
        gathered = 0
        while gathered < target:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                break
            if not chunk:
                continue
            self._size_bytes += len(chunk)
            if self._size_bytes > self._max_bytes:
                raise ValidationError(
                    f"远程 Asset 超过 {_format_byte_limit(self._max_bytes)} 限制",
                )
            parts.append(chunk)
            gathered += len(chunk)
        if not parts:
            return b""
        if self._on_progress is not None:
            self._on_progress(self._size_bytes, self._total_bytes)
        return b"".join(parts) if len(parts) != 1 else parts[0]


def _stable_id(
    prefix: str,
    project_id: str,
    key: str,
    suffix: str = "",
) -> str:
    value = uuid5(
        NAMESPACE_URL,
        f"qwenpaw-creator:file:{prefix}:{project_id}:{key}:{suffix}",
    ).hex
    return f"{prefix}-{value}"


def _format_byte_limit(size_bytes: int) -> str:
    gibibyte = 1024 * 1024 * 1024
    mebibyte = 1024 * 1024
    if size_bytes % gibibyte == 0:
        return f"{size_bytes // gibibyte} GiB"
    if size_bytes % mebibyte == 0:
        return f"{size_bytes // mebibyte} MiB"
    return f"{size_bytes} bytes"


def _remote_asset_max_bytes() -> int:
    variable = "CREATOR_REMOTE_ASSET_MAX_BYTES"
    raw = os.environ.get(variable)
    if raw is None:
        variable = "CREATOR_DOWNLOAD_MAX_BYTES"
        raw = os.environ.get(variable)
    if raw is None:
        return _DEFAULT_MAX_REMOTE_ASSET_BYTES
    try:
        value = int(raw)
    except ValueError as error:
        raise ValidationError(f"{variable} 必须是正整数") from error
    if value <= 0:
        raise ValidationError(f"{variable} 必须是正整数")
    return value


def _validate_local_video_upload(
    *,
    name: str,
    media_type: str,
    size_bytes: int | None,
) -> None:
    guessed_type = mimetypes.guess_type(name)[0] or ""
    is_video = media_type.casefold().startswith(
        "video/",
    ) or guessed_type.casefold().startswith("video/")
    if (
        is_video
        and size_bytes is not None
        and size_bytes > _MAX_LOCAL_VIDEO_UPLOAD_BYTES
    ):
        raise ValidationError("本地视频超过 100 MiB 上传限制")


def _asset_checksum(item: _AssetInput | _StagedAssetInput) -> str:
    if isinstance(item, _StagedAssetInput):
        return item.staged.sha256
    return sha256(item.content).hexdigest()


def _asset_size_bytes(item: _AssetInput | _StagedAssetInput) -> int:
    if isinstance(item, _StagedAssetInput):
        return item.staged.size_bytes
    return len(item.content)


def _media_kind(media_type: str) -> str:
    normalized = media_type.casefold()
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("text/"):
        return "text"
    if (
        normalized in {"application/pdf", "application/msword"}
        or "document" in normalized
    ):
        return "document"
    return "other"


def _extension(name: str, media_type: str) -> str:
    suffix = Path(name).suffix[:20]
    if suffix and suffix.replace(".", "").isalnum():
        return suffix.casefold()
    guessed = mimetypes.guess_extension(media_type.split(";", 1)[0].strip())
    return guessed or ".bin"


def _records_root(services: CreatorFileServices, project_id: str) -> Path:
    return (
        services.projects.project_root(project_id)
        / "runtime"
        / "idempotency"
        / "asset-http"
    )


def _ensure_existing_file(
    file_store: AssetFileStore,
    indexed: IndexedFile,
) -> None:
    inspection = file_store.inspect(indexed)
    if not inspection.available:
        raise StorageIntegrityError(
            f"已存在的 Asset 路径与请求内容不一致: {indexed.relative_uri}",
        )


def _ingest_many_sync(
    services: CreatorFileServices,
    *,
    project_id: str,
    key: str,
    inputs: list[_AssetInput | _StagedAssetInput],
    attach_source: bool,
    scope: str,
) -> tuple[dict[str, Any], bool]:
    # The lifecycle lock is acquired before the per-Project idempotency path
    # can be created. A concurrent DELETE therefore cannot be followed by a
    # late upload recreating a phantom Project directory.
    with services.projects.lifecycle_lock(project_id):
        services.projects.read(project_id)
        return _ingest_many_locked(
            services,
            project_id=project_id,
            key=key,
            inputs=inputs,
            attach_source=attach_source,
            scope=scope,
        )


def _ingest_many_locked(
    services: CreatorFileServices,
    *,
    project_id: str,
    key: str,
    inputs: list[_AssetInput | _StagedAssetInput],
    attach_source: bool,
    scope: str,
) -> tuple[dict[str, Any], bool]:
    if not inputs:
        raise ValidationError("至少需要一个 Asset")
    records = IdempotencyRecordStore(_records_root(services, project_id))
    request_payload = {
        "projectId": project_id,
        "attachSource": attach_source,
        "items": [
            {
                "name": item.name,
                "mediaType": item.media_type,
                "sha256": _asset_checksum(item),
                "size": _asset_size_bytes(item),
            }
            for item in inputs
        ],
    }
    request_hash = records.request_hash(request_payload)
    operation_id = records.operation_id(
        namespace="asset",
        owner_id=project_id,
        scope=scope,
        idempotency_key=key,
        request_hash=request_hash,
    )
    with records.operation_lock(
        owner_id=project_id,
        scope=scope,
        idempotency_key=key,
    ):
        reservation = records.reserve(
            owner_id=project_id,
            scope=scope,
            idempotency_key=key,
            request_hash=request_hash,
            record_id=operation_id,
        )
        if reservation.record.status is IdempotencyStatus.COMPLETED:
            if not isinstance(reservation.record.response, dict):
                raise StorageIntegrityError("Asset idempotency response 损坏")
            return reservation.record.response, True
        if reservation.record.status is IdempotencyStatus.FAILED:
            raise StorageIntegrityError(
                "上一次 Asset 写入失败，请使用新的 Idempotency-Key 重试",
            )

        # The public helper owns the Project lifecycle lock for this complete
        # idempotency/file/index publication boundary.
        with nullcontext():
            base = services.projects.read(project_id)
            candidate = base.project.model_dump(mode="json")
            files = candidate["assets"]["files_by_id"]
            versions = candidate["assets"]["source_versions_by_id"]
            source_items = candidate["sources"]["sources"]["items"]
            source_order = candidate["sources"]["sources"]["order"]
            file_store = AssetFileStore(
                services.projects.project_root(project_id),
            )
            output_items: list[dict[str, str]] = []
            created_at = datetime.now(UTC)
            for index, item in enumerate(inputs):
                suffix = str(index)
                logical_asset_id = _stable_id("asset", project_id, key, suffix)
                version_id = _stable_id(
                    "asset-version",
                    project_id,
                    key,
                    suffix,
                )
                file_id = _stable_id("file", project_id, key, suffix)
                source_id = _stable_id("source", project_id, key, suffix)
                checksum = _asset_checksum(item)
                size_bytes = _asset_size_bytes(item)
                relative_uri = PurePosixPath(
                    "assets",
                    "sources",
                    f"{file_id}{_extension(item.name, item.media_type)}",
                ).as_posix()
                indexed = IndexedFile(
                    file_id=file_id,
                    kind="source_original",
                    relative_uri=relative_uri,
                    sha256=checksum,
                    size_bytes=size_bytes,
                    media_type=item.media_type,
                    created_at=created_at,
                )
                version = SourceAssetVersion(
                    version_id=version_id,
                    logical_asset_id=logical_asset_id,
                    name=item.name,
                    file_id=file_id,
                    checksum=checksum,
                    media_kind=_media_kind(item.media_type),
                    media_type=item.media_type,
                    created_at=created_at,
                    metadata={"clientRequestId": key},
                )
                existing_file = files.get(file_id)
                existing_version = versions.get(version_id)
                if existing_file is not None or existing_version is not None:
                    if existing_file != indexed.model_dump(
                        mode="json",
                    ) or existing_version != version.model_dump(mode="json"):
                        raise ConflictError("Asset 稳定 ID 已存在但内容不同")
                    _ensure_existing_file(file_store, indexed)
                else:
                    staged = (
                        item.staged
                        if isinstance(item, _StagedAssetInput)
                        else file_store.stage_bytes(
                            item.content,
                            staging_id=operation_id[:80],
                        )
                    )
                    try:
                        file_store.publish(
                            staged,
                            relative_uri,
                            expected_sha256=checksum,
                            expected_size_bytes=size_bytes,
                        )
                    except AssetAlreadyExists:
                        file_store.abandon(staged)
                        _ensure_existing_file(file_store, indexed)
                    files[file_id] = indexed.model_dump(mode="json")
                    versions[version_id] = version.model_dump(mode="json")
                if attach_source and source_id not in source_items:
                    source = ProjectSource(
                        source_id=source_id,
                        display_name=item.name,
                        logical_asset_id=logical_asset_id,
                        selected_asset_version_id=version_id,
                    )
                    source_items[source_id] = source.model_dump(mode="json")
                    source_order.append(source_id)
                output_items.append(
                    {
                        "name": item.name,
                        "assetId": logical_asset_id,
                        "assetVersionId": version_id,
                        "checksum": checksum,
                    },
                )

            # A recovered retry may find every index entry already published.
            # Avoid manufacturing a no-op generation in that case.
            if candidate != base.project.model_dump(mode="json"):
                result = services.commits.commit(
                    base=base,
                    candidate=candidate,
                    origin=ChangeOrigin.FRONTEND_EDIT,
                    review_policy=ReviewPolicy.AUTO_FIX,
                    caused_by_request_id=key,
                    round_id=f"round-{operation_id}",
                    transaction_id=operation_id,
                    advance_accepted_baseline=True,
                    _lifecycle_lock_held=True,
                )
                services.poller.note_commit(result.snapshot)

        response = {
            "taskId": _stable_id("task", project_id, key, scope),
            "status": "SUCCEEDED",
            "progress": 1.0,
            "items": output_items,
        }
        records.complete(
            owner_id=project_id,
            scope=scope,
            idempotency_key=key,
            request_hash=request_hash,
            response=response,
            response_status=status.HTTP_202_ACCEPTED,
        )
        return response, not reservation.created


def _require_public_ip(address: str) -> None:
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as error:
        raise ValidationError("Asset URL 解析到了非法 IP") from error
    if not parsed.is_global:
        raise ValidationError("Asset URL 不允许访问本机、私有或保留网络")


def _validate_public_remote_url(
    value: str,
    *,
    resolver: Any = socket.getaddrinfo,
) -> str:
    parsed = urlsplit(str(value or "").strip())
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
    ):
        raise ValidationError("Asset URL 必须是公网 http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError("Asset URL 不允许携带用户名或密码")
    try:
        port = parsed.port or (
            443 if parsed.scheme.casefold() == "https" else 80
        )
    except ValueError as error:
        raise ValidationError("Asset URL 端口非法") from error
    host = parsed.hostname
    try:
        _require_public_ip(host)
    except ValidationError:
        try:
            ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            try:
                records = resolver(host, port, type=socket.SOCK_STREAM)
            except socket.gaierror as error:
                raise ValidationError("Asset URL 主机无法解析") from error
            addresses = {str(record[4][0]) for record in records if record[4]}
            if not addresses:
                raise ValidationError("Asset URL 主机无法解析")
            for address in addresses:
                _require_public_ip(address)
        else:
            raise
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""),
    )


def _validate_response_peer(response: httpx.Response) -> None:
    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        raise ValidationError("Asset URL 连接缺少可验证的 peer address")
    peer = stream.get_extra_info("server_addr")
    if not isinstance(peer, tuple) or not peer:
        raise ValidationError("Asset URL 连接缺少可验证的 peer address")
    _require_public_ip(str(peer[0]))


def _download_remote_to_staging(
    url: str,
    *,
    file_store: AssetFileStore,
    staging_id: str,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> _RemoteAssetDownload:
    """Stream one public remote Asset directly into Project staging."""

    current = _validate_public_remote_url(url)
    original = urlsplit(current)
    max_bytes = _remote_asset_max_bytes()
    staged: StagedAsset | None = None
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(
                _DEFAULT_REMOTE_ASSET_TIMEOUT_SECONDS,
                connect=30,
            ),
            trust_env=False,
            headers={"Accept": "*/*", "Accept-Encoding": "identity"},
        ) as client:
            for redirect_index in range(_MAX_REMOTE_REDIRECTS + 1):
                with client.stream("GET", current) as response:
                    _validate_response_peer(response)
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect_index >= _MAX_REMOTE_REDIRECTS:
                            raise ValidationError("Asset URL 重定向次数超过限制")
                        location = response.headers.get("location")
                        if not location:
                            raise ValidationError("Asset URL 重定向缺少 Location")
                        current = _validate_public_remote_url(
                            urljoin(current, location),
                        )
                        continue
                    response.raise_for_status()
                    if response.headers.get(
                        "content-encoding",
                        "identity",
                    ).casefold() not in {
                        "",
                        "identity",
                    }:
                        raise ValidationError("Asset URL 未返回 identity 原始字节")
                    declared = response.headers.get("content-length")
                    declared_size: int | None = None
                    if declared:
                        try:
                            declared_size = int(declared)
                        except ValueError as error:
                            raise ValidationError(
                                "Asset URL Content-Length 非法",
                            ) from error
                        if declared_size < 0:
                            raise ValidationError(
                                "Asset URL Content-Length 非法",
                            )
                        if declared_size > max_bytes:
                            raise ValidationError(
                                f"远程 Asset 超过 {_format_byte_limit(max_bytes)} 限制",
                            )
                    staged = file_store.stage_stream(
                        _BoundedRawReader(
                            response.iter_raw(),
                            max_bytes=max_bytes,
                            total_bytes=declared_size,
                            on_progress=on_progress,
                        ),
                        staging_id=staging_id,
                    )
                    if staged.size_bytes == 0:
                        file_store.abandon(staged)
                        raise ValidationError("远程 Asset 为空")
                    media_type = response.headers.get(
                        "content-type",
                        "",
                    ).split(";", 1)[0]
                    break
            else:  # pragma: no cover - the bounded loop always breaks or raises
                raise ValidationError("Asset URL 未产生响应")
        if (
            staged is None
        ):  # pragma: no cover - successful response always stages
            raise ValidationError("Asset URL 未产生内容")
        name = (
            Path(urlsplit(current).path or original.path).name
            or "remote-asset.bin"
        )
        return _RemoteAssetDownload(
            name=name,
            staged=staged,
            media_type=media_type or "application/octet-stream",
        )
    except (httpx.HTTPError, ValueError) as error:
        if staged is not None:
            file_store.abandon(staged)
        raise ValidationError(f"无法读取远程 Asset: {error}") from error
    except BaseException:
        if staged is not None:
            file_store.abandon(staged)
        raise


def _ingest_remote_sync(
    services: CreatorFileServices,
    *,
    project_id: str,
    key: str,
    url: str,
    requested_name: str,
    attach_source: bool,
    scope: str,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Download outside the Project lock into a non-authoritative Runtime cache.

    The URL-backed AssetVersion already exists before this starts. Cache
    completion never creates another version or advances ``project.json``.
    """

    snapshot = services.projects.read(project_id)
    version_id = _stable_id("asset-version", project_id, key, "0")
    logical_asset_id = _stable_id("asset", project_id, key, "0")
    version = snapshot.project.assets.source_versions_by_id.get(version_id)
    if version is None or version.logical_asset_id != logical_asset_id:
        raise StorageIntegrityError("远程 AssetVersion 注册信息不存在")
    staging_store = AssetFileStore(services.projects.project_root(project_id))
    download: _RemoteAssetDownload | None = None
    try:
        download = _download_remote_to_staging(
            url,
            file_store=staging_store,
            staging_id=_stable_id("remote-stage", project_id, key),
            on_progress=on_progress,
        )
        name = requested_name
        if name.strip() == url.strip() or not Path(name).suffix:
            name = download.name
        cache = publish_remote_cache(
            services.projects.project_root(project_id),
            version,
            download.staged,
            media_type=download.media_type,
        )
        return (
            {
                "taskId": _stable_id("task", project_id, key, scope),
                "status": "SUCCEEDED",
                "progress": 1.0,
                "items": [
                    {
                        "name": name,
                        "assetId": logical_asset_id,
                        "assetVersionId": version_id,
                        "checksum": version.checksum,
                        "cacheChecksum": cache.sha256,
                        "cacheSizeBytes": cache.size_bytes,
                        "cacheRelativeUri": cache.relative_uri,
                        "mediaType": cache.media_type,
                    },
                ],
            },
            False,
        )
    finally:
        if download is not None:
            staging_store.abandon(download.staged)


def _execution_task_view(task: TaskRecord) -> dict[str, Any]:
    return {
        "id": task.task_id,
        "projectId": task.project_id,
        "transactionId": task.round_id,
        "specialistRunId": task.run_id,
        "kind": task.kind.value,
        "targetRef": str(task.metadata.get("targetRef") or ""),
        "status": task.status.value,
        "progress": task.progress,
        "resultRefs": task.output_refs,
        "result": task.result,
        "error": task.error,
        "createdAt": task.created_at.isoformat(),
        "updatedAt": task.updated_at.isoformat(),
    }


def _publish_remote_task_event(
    services: CreatorFileServices,
    task: TaskRecord,
    *,
    event_type: str,
) -> None:
    sessions = ProjectRuntimeSessionStore(services.root)
    try:
        session = sessions.get_project_session(task.project_id)
    except RuntimeSessionNotFound:
        # Store-level tests and recovery tools may create a Project without a
        # Creator Session. The durable Task remains the authority in that case.
        return
    sessions.append_event(
        task.project_id,
        session.session_id,
        event_type=event_type,
        payload={
            "taskId": task.task_id,
            "progress": task.progress,
            "status": task.status.value,
            "task": _execution_task_view(task),
        },
    )


def _remote_task_fingerprint(
    *,
    project_id: str,
    key: str,
    url: str,
    requested_name: str,
    attach_source: bool,
) -> str:
    payload = json.dumps(
        {
            "projectId": project_id,
            "key": key,
            "url": url,
            "requestedName": requested_name,
            "attachSource": attach_source,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _register_remote_asset_sync(
    services: CreatorFileServices,
    *,
    project_id: str,
    key: str,
    url: str,
    requested_name: str,
    attach_source: bool,
    scope: str,
) -> dict[str, str]:
    """Publish the URL identity before the optional byte cache starts."""

    logical_asset_id = _stable_id("asset", project_id, key, "0")
    version_id = _stable_id("asset-version", project_id, key, "0")
    source_id = _stable_id("source", project_id, key, "0")
    task_id = _stable_id("task", project_id, key, scope)
    name = remote_source_name(url, requested_name)
    media_type = remote_source_media_type(url, name)
    checksum = sha256(url.encode("utf-8")).hexdigest()
    created_at = datetime.now(UTC)
    version = SourceAssetVersion(
        version_id=version_id,
        logical_asset_id=logical_asset_id,
        name=name,
        file_id=None,
        checksum=checksum,
        media_kind=_media_kind(media_type),
        media_type=media_type,
        provenance_refs=[url],
        created_at=created_at,
        metadata={
            "clientRequestId": key,
            "sourceKind": "remote_url",
            "publicSourceUrl": url,
            "checksumKind": "source_url_sha256",
            "cacheTaskId": task_id,
        },
    )
    with services.projects.lifecycle_lock(project_id):
        base = services.projects.read(project_id)
        candidate = base.project.model_dump(mode="json")
        versions = candidate["assets"]["source_versions_by_id"]
        existing = versions.get(version_id)
        serialized = version.model_dump(mode="json")
        if existing is not None:
            existing_version = SourceAssetVersion.model_validate(existing)
            if (
                existing_version.logical_asset_id != logical_asset_id
                or existing_version.name != name
                or existing_version.checksum != checksum
                or existing_version.media_type != media_type
                or existing_version.metadata.get("publicSourceUrl") != url
            ):
                raise ConflictError("远程 Asset 稳定 ID 已存在但 URL 身份不同")
        else:
            versions[version_id] = serialized
        if attach_source:
            source_items = candidate["sources"]["sources"]["items"]
            source_order = candidate["sources"]["sources"]["order"]
            source = ProjectSource(
                source_id=source_id,
                display_name=name,
                logical_asset_id=logical_asset_id,
                selected_asset_version_id=version_id,
            ).model_dump(mode="json")
            existing_source = source_items.get(source_id)
            if existing_source is not None and existing_source != source:
                raise ConflictError("远程 ProjectSource 稳定 ID 已存在但内容不同")
            if existing_source is None:
                source_items[source_id] = source
                source_order.append(source_id)
        if candidate != base.project.model_dump(mode="json"):
            result = services.commits.commit(
                base=base,
                candidate=candidate,
                origin=ChangeOrigin.FRONTEND_EDIT,
                review_policy=ReviewPolicy.AUTO_FIX,
                caused_by_request_id=key,
                round_id=f"round-{task_id}",
                transaction_id=task_id,
                advance_accepted_baseline=True,
                _lifecycle_lock_held=True,
            )
            services.poller.note_commit(result.snapshot)
    return {
        "name": name,
        "assetId": logical_asset_id,
        "assetVersionId": version_id,
        "checksum": checksum,
        "mediaType": media_type,
    }


def _ensure_remote_task(
    services: CreatorFileServices,
    *,
    project_id: str,
    key: str,
    url: str,
    requested_name: str,
    attach_source: bool,
    scope: str,
) -> TaskRecord:
    executions = ProjectExecutionStore(services.root)
    snapshot = services.projects.read(project_id)
    task_id = _stable_id("task", project_id, key, scope)
    fingerprint = _remote_task_fingerprint(
        project_id=project_id,
        key=key,
        url=url,
        requested_name=requested_name,
        attach_source=attach_source,
    )
    candidate = TaskRecord(
        task_id=task_id,
        project_id=project_id,
        kind=TaskKind.ASSET_INGEST,
        request_fingerprint=fingerprint,
        idempotency_key=key,
        input_generation=snapshot.generation,
        input_etag=snapshot.etag,
        expected_target_version=snapshot.etag,
        input_refs=[
            url,
            f"asset-version:{_stable_id('asset-version', project_id, key, '0')}",
        ],
        progress=0.0,
        caused_by_request_id=key,
        review_policy=ReviewPolicy.AUTO_FIX,
        metadata={
            "targetRef": f"asset:{_stable_id('asset', project_id, key, '0')}",
            "sourceUrl": url,
            "requestedName": requested_name,
            "attachSource": attach_source,
            "assetId": _stable_id("asset", project_id, key, "0"),
            "assetVersionId": _stable_id(
                "asset-version",
                project_id,
                key,
                "0",
            ),
        },
    )
    try:
        existing = executions.get_task(project_id, task_id)
    except RecordNotFoundError:
        try:
            return executions.create_task(candidate)
        except (ExecutionPayloadConflict, ExecutionStateConflict) as error:
            raise ConflictError(str(error)) from error
    if existing.request_fingerprint != fingerprint:
        raise ConflictError("Idempotency-Key 已用于不同的远程 Asset 请求")
    return existing


def _run_remote_task_sync(
    services: CreatorFileServices,
    *,
    project_id: str,
    key: str,
    url: str,
    requested_name: str,
    attach_source: bool,
    scope: str,
) -> None:
    executions = ProjectExecutionStore(services.root)
    task_id = _stable_id("task", project_id, key, scope)
    task = executions.get_task(project_id, task_id)
    if task.status in {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }:
        return
    task = executions.transition_task(
        project_id,
        task_id,
        expected_status={TaskStatus.QUEUED, TaskStatus.RUNNING},
        status=TaskStatus.RUNNING,
        updates={"progress": task.progress or 0.0},
    )
    _publish_remote_task_event(services, task, event_type="task.started")
    last_progress = float(task.progress or 0.0)

    def report_progress(downloaded: int, total: int | None) -> None:
        nonlocal last_progress
        current = executions.get_task(project_id, task_id)
        if current.status is TaskStatus.CANCELLED:
            raise RuntimeError("远程素材入库已取消")
        if current.status is not TaskStatus.RUNNING:
            return
        progress = (
            min(0.9, max(0.01, downloaded / total * 0.9))
            if total and total > 0
            else min(0.85, 0.01 + downloaded / (128 * 1024 * 1024))
        )
        if progress < 0.9 and progress - last_progress < 0.01:
            return
        last_progress = progress
        updated = executions.transition_task(
            project_id,
            task_id,
            expected_status=TaskStatus.RUNNING,
            status=TaskStatus.RUNNING,
            updates={"progress": progress},
        )
        _publish_remote_task_event(
            services,
            updated,
            event_type="task.progress_updated",
        )

    try:
        result, replayed = _ingest_remote_sync(
            services,
            project_id=project_id,
            key=key,
            url=url,
            requested_name=requested_name,
            attach_source=attach_source,
            scope=scope,
            on_progress=report_progress,
        )
        items = list(result.get("items") or [])
        refs = [
            f"asset-version:{item['assetVersionId']}"
            for item in items
            if item.get("assetVersionId")
        ]
        task = executions.transition_task(
            project_id,
            task_id,
            expected_status=TaskStatus.RUNNING,
            status=TaskStatus.SUCCEEDED,
            updates={
                "progress": 1.0,
                "output_refs": refs,
                "result": {"items": items, "idempotentReplay": replayed},
            },
        )
        _publish_remote_task_event(services, task, event_type="task.completed")
    except BaseException as error:
        try:
            current = executions.get_task(project_id, task_id)
        except (ExecutionStoreError, RecordNotFoundError):
            # Project deletion is the terminal cancellation boundary for every
            # Project-local Runtime task. Nothing remains to publish.
            return
        if current.status is TaskStatus.CANCELLED:
            _publish_remote_task_event(
                services,
                current,
                event_type="task.cancelled",
            )
            return
        task = executions.transition_task(
            project_id,
            task_id,
            expected_status={TaskStatus.QUEUED, TaskStatus.RUNNING},
            status=TaskStatus.FAILED,
            updates={
                "error": {
                    "code": "ASSET_INGEST_FAILED",
                    "message": str(error),
                    "type": type(error).__name__,
                },
            },
        )
        _publish_remote_task_event(services, task, event_type="task.failed")


def _start_remote_task(
    services: CreatorFileServices,
    *,
    project_id: str,
    key: str,
    url: str,
    requested_name: str,
    attach_source: bool,
    scope: str,
) -> None:
    task_id = _stable_id("task", project_id, key, scope)
    identity = (project_id, task_id)
    running = _REMOTE_INGEST_TASKS.get(identity)
    if running is not None and not running.done():
        return
    background = asyncio.create_task(
        asyncio.to_thread(
            _run_remote_task_sync,
            services,
            project_id=project_id,
            key=key,
            url=url,
            requested_name=requested_name,
            attach_source=attach_source,
            scope=scope,
        ),
        name=f"creator-remote-asset:{project_id}:{task_id}",
    )
    _REMOTE_INGEST_TASKS[identity] = background

    def completed(task: asyncio.Task[None]) -> None:
        if _REMOTE_INGEST_TASKS.get(identity) is task:
            _REMOTE_INGEST_TASKS.pop(identity, None)
        if not task.cancelled():
            task.exception()

    background.add_done_callback(completed)


def _translate(error: BaseException) -> None:
    if isinstance(error, IdempotencyConflictError):
        raise ConflictError("Idempotency-Key 已用于不同的 Asset 请求") from error
    if isinstance(error, IdempotencyStateConflictError):
        raise ConflictError("Asset idempotency 状态冲突") from error
    if isinstance(error, AssetFileError):
        raise StorageIntegrityError(str(error)) from error
    raise error


@router.post("/assets", status_code=status.HTTP_202_ACCEPTED)
async def ingest_asset(
    project_id: str,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    services: CreatorFileServices = Depends(project_file_services),
) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = next(
            (
                value
                for _, value in form.multi_items()
                if isinstance(value, UploadFile)
            ),
            None,
        )
        if upload is None:
            raise ValidationError("multipart assets 请求缺少 file")
        key = resolve_idempotency_key(
            idempotency_key,
            stable_client_id=str(form.get("clientRequestId") or ""),
        )
        post_action = str(form.get("postIngestAction") or "NONE")
        name = Path(upload.filename or "upload.bin").name
        media_type = (
            upload.content_type
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )
        _validate_local_video_upload(
            name=name,
            media_type=media_type,
            size_bytes=upload.size,
        )
        payload = await upload.read()
        _validate_local_video_upload(
            name=name,
            media_type=media_type,
            size_bytes=len(payload),
        )
        input_item: _AssetInput | _StagedAssetInput = _AssetInput(
            name=name,
            content=payload,
            media_type=media_type,
        )
        remote_url: str | None = None
    else:
        body = TextOrUrlAssetRequest.model_validate(await request.json())
        key = resolve_idempotency_key(
            idempotency_key,
            stable_client_id=body.client_request_id,
        )
        post_action = body.post_ingest_action
        name = body.name.strip() or "asset"
        if body.kind == "text":
            payload = body.value.encode("utf-8")
            media_type = "text/plain; charset=utf-8"
            input_item = _AssetInput(
                name=name,
                content=payload,
                media_type=media_type,
            )
            remote_url = None
        else:
            remote_url = body.value
    if post_action not in {"NONE", "ATTACH_SOURCE"}:
        raise ValidationError("postIngestAction 非法")
    try:
        if remote_url is not None:
            remote_url = _validate_public_remote_url(remote_url)
            indexed_item = await asyncio.to_thread(
                _register_remote_asset_sync,
                services,
                project_id=project_id,
                key=key,
                url=remote_url,
                requested_name=name,
                attach_source=post_action == "ATTACH_SOURCE",
                scope="POST-assets",
            )
            task = await asyncio.to_thread(
                _ensure_remote_task,
                services,
                project_id=project_id,
                key=key,
                url=remote_url,
                requested_name=name,
                attach_source=post_action == "ATTACH_SOURCE",
                scope="POST-assets",
            )
            if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                _start_remote_task(
                    services,
                    project_id=project_id,
                    key=key,
                    url=remote_url,
                    requested_name=name,
                    attach_source=post_action == "ATTACH_SOURCE",
                    scope="POST-assets",
                )
            return {
                "assetId": indexed_item["assetId"],
                "taskId": task.task_id,
                "status": task.status.value,
                "progress": task.progress,
                "assetVersionId": indexed_item["assetVersionId"],
                "result": task.result,
                "error": task.error,
            }
        else:
            result, replayed = await asyncio.to_thread(
                _ingest_many_sync,
                services,
                project_id=project_id,
                key=key,
                inputs=[input_item],
                attach_source=post_action == "ATTACH_SOURCE",
                scope="POST-assets",
            )
    except BaseException as error:
        _translate(error)
    item = result["items"][0]
    return {
        "assetId": item["assetId"],
        "taskId": result["taskId"],
        "status": result["status"],
        "progress": result["progress"],
        "assetVersionId": item["assetVersionId"],
        "result": {"items": result["items"], "idempotentReplay": replayed},
        "error": None,
    }


@router.post("/asset-imports", status_code=status.HTTP_202_ACCEPTED)
async def import_assets(
    project_id: str,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    services: CreatorFileServices = Depends(project_file_services),
) -> dict[str, Any]:
    if not request.headers.get("content-type", "").startswith(
        "multipart/form-data",
    ):
        raise ValidationError("asset-imports 只接受 multipart")
    form = await request.form()
    key = resolve_idempotency_key(
        idempotency_key,
        stable_client_id=str(form.get("clientRequestId") or ""),
    )
    post_action = str(form.get("postIngestAction") or "NONE")
    if post_action not in {"NONE", "ATTACH_SOURCE"}:
        raise ValidationError("postIngestAction 非法")
    uploads = [
        value
        for _, value in form.multi_items()
        if isinstance(value, UploadFile)
    ]
    if not uploads:
        raise ValidationError("文件夹导入至少包含一个文件")
    inputs: list[_AssetInput | _StagedAssetInput] = []
    for index, upload in enumerate(uploads):
        name = Path(upload.filename or f"item-{index + 1}").name
        media_type = (
            upload.content_type
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )
        _validate_local_video_upload(
            name=name,
            media_type=media_type,
            size_bytes=upload.size,
        )
        content = await upload.read()
        _validate_local_video_upload(
            name=name,
            media_type=media_type,
            size_bytes=len(content),
        )
        inputs.append(
            _AssetInput(
                name=name,
                content=content,
                media_type=media_type,
            ),
        )
    try:
        result, _replayed = await asyncio.to_thread(
            _ingest_many_sync,
            services,
            project_id=project_id,
            key=key,
            inputs=inputs,
            attach_source=post_action == "ATTACH_SOURCE",
            scope="POST-asset-imports",
        )
    except BaseException as error:
        _translate(error)
    import_id = _stable_id("asset-import", project_id, key)
    record = AssetImportRecord(
        importId=import_id,
        taskId=result["taskId"],
        projectId=project_id,
        status="SUCCEEDED",
        progress=1.0,
        items=[
            AssetImportItemRecord(
                name=item["name"],
                assetVersionId=item["assetVersionId"],
                checksum=item["checksum"],
            )
            for item in result["items"]
        ],
        createdAt=datetime.now(UTC),
    )
    await asyncio.to_thread(
        AtomicJsonRecordStore(
            services.projects.project_root(project_id)
            / "runtime"
            / "asset-imports"
            / f"{import_id}.json",
            AssetImportRecord,
        ).write,
        record,
    )
    return {"importId": import_id, "taskId": result["taskId"], "eventSeq": 0}


@router.get("/asset-imports/{import_id}")
async def get_asset_import(
    project_id: str,
    import_id: str,
    services: CreatorFileServices = Depends(project_file_services),
) -> dict[str, Any]:
    try:
        record = await asyncio.to_thread(
            AtomicJsonRecordStore(
                services.projects.project_root(project_id)
                / "runtime"
                / "asset-imports"
                / f"{import_id}.json",
                AssetImportRecord,
            ).read,
        )
    except RecordNotFoundError as error:
        raise NotFoundError("asset import 不存在") from error
    if record.project_id != project_id or record.import_id != import_id:
        raise StorageIntegrityError("asset import identity 损坏")
    return record.model_dump(
        mode="json",
        by_alias=True,
        exclude={"project_id", "created_at"},
    )


@router.get("/asset-imports/{import_id}/events")
async def get_asset_import_events(
    project_id: str,
    import_id: str,
    after: int = Query(0, ge=0),
    services: CreatorFileServices = Depends(project_file_services),
) -> dict[str, Any]:
    del after
    await get_asset_import(project_id, import_id, services)
    return {"items": []}


def _asset_version(project: Any, asset_id: str, version_id: str | None):
    candidates = [
        item
        for item in project.assets.source_versions_by_id.values()
        if item.logical_asset_id == asset_id
        and (version_id is None or item.version_id == version_id)
    ]
    if not candidates:
        raise NotFoundError("AssetVersion 不存在")
    return max(candidates, key=lambda item: item.created_at)


@router.get("/assets/{asset_id}/content")
async def get_asset_content(
    project_id: str,
    asset_id: str,
    version_id: str | None = Query(None, alias="versionId"),
    services: CreatorFileServices = Depends(project_file_services),
) -> StreamingResponse:
    try:
        project = await asyncio.to_thread(services.projects.read, project_id)
    except Exception as error:
        raise NotFoundError("Project 不存在") from error
    version = _asset_version(project.project, asset_id, version_id)
    if version.file_id is None:
        cache = await asyncio.to_thread(
            resolve_remote_cache,
            services.projects.project_root(project_id),
            version,
            ProjectExecutionStore(services.root).list_tasks(project_id),
        )
        if cache is None:
            raise NotFoundError("远程 Asset 本地缓存尚未完成")
        return StreamingResponse(
            cache.path.open("rb"),
            media_type=version.media_type,
            headers={
                "Content-Length": str(cache.size_bytes),
                "Content-Disposition": inline_content_disposition(
                    version.name,
                    version.media_type,
                ),
                "ETag": f'"sha256:{cache.sha256}"',
            },
        )
    indexed = project.project.assets.files_by_id[version.file_id]
    try:
        stream = await asyncio.to_thread(
            AssetFileStore(
                services.projects.project_root(project_id),
            ).open_verified,
            indexed,
        )
    except AssetFileError as error:
        raise StorageIntegrityError(str(error)) from error
    return StreamingResponse(
        stream,
        media_type=version.media_type,
        headers={
            "Content-Length": str(indexed.size_bytes),
            "Content-Disposition": inline_content_disposition(
                version.name,
                version.media_type,
            ),
            "ETag": f'"sha256:{indexed.sha256}"',
        },
    )


__all__ = ["router"]
