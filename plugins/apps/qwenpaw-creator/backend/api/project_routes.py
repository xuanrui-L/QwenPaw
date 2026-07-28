# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=try-except-raise
"""File-native Project lifecycle routes.

``project.json`` and its Project-local ``runtime/`` tree are the only durable
authorities used here.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    Header,
    Query,
    Response,
    status,
    Request,
)
from fastapi.responses import StreamingResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.routing import Match
from starlette.types import Scope
from starlette.datastructures import UploadFile

from domain.errors import (
    ConflictError,
    StorageIntegrityError,
    ValidationError,
    BadRequestError,
)
from schemas.projects import (
    ExecutionPreauthorizationPolicy,
    ProjectCreateRequest,
    ProjectCreateResponse,
)
from services.file_agent_runtime import (
    interrupt_creator_agent_runtime,
    notify_creator_agent_runtime,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    ExecutionPreauthorization,
    Project,
    ProjectSettings,
)
from services.project_files.store import (
    ProjectAlreadyExists,
    ProjectIntegrityError,
    ProjectNotFound,
    ProjectStoreError,
    _safe_project_id,
)
from services.runtime_files.errors import RuntimeFileError
from services.runtime_files.idempotency_store import IdempotencyRecordStore
from services.runtime_files.locking import CrossProcessFileLock
from services.runtime_files.session_store import ProjectRuntimeBootstrap
from services.storage_root import require_creator_data_root
from services.project_files.serialization import load_project_json
from utils.logger import setup_logger
from .dependencies import (
    CreatorErrorRoute,
    project_file_services,
    resolve_idempotency_key,
)


logger = setup_logger("project_routes")

_CREATE_SCOPE = "POST /projects"


class _RemovedProjectPutRoute(CreatorErrorRoute):
    """Keep the removed whole-Project PUT absent instead of exposing a 405 alias."""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        match, child_scope = super().matches(scope)
        if match is Match.PARTIAL and scope.get("method") == "PUT":
            return Match.NONE, child_scope
        return match, child_scope


router = APIRouter(
    prefix="/projects",
    tags=["projects"],
    route_class=_RemovedProjectPutRoute,
)


def _stable_id(kind: str, identity: str) -> str:
    return f"{kind}-{uuid5(NAMESPACE_URL, f'qwenpaw-creator:{kind}:{identity}').hex}"


def _project_snapshot_id(project_id: str, generation: int = 0) -> str:
    return f"project-snapshot-{uuid5(NAMESPACE_URL, f'{project_id}:{generation}').hex}"


def _request_hash(request: ProjectCreateRequest) -> str:
    return IdempotencyRecordStore.request_hash(
        {
            "scope": _CREATE_SCOPE,
            "request": request.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            ),
        },
    )


def _settings(request: ProjectCreateRequest) -> ProjectSettings:
    preauthorization = (
        ExecutionPreauthorization.model_validate(
            request.execution_preauthorization.model_dump(mode="python"),
        )
        if request.execution_preauthorization is not None
        else None
    )
    return ProjectSettings(
        aspect_ratio=request.aspect_ratio,
        resolution=request.resolution,
        content_type=request.content_type,
        execution_preauthorization=preauthorization,
    )


def _header(project: Project) -> dict[str, Any]:
    preauthorization = project.settings.execution_preauthorization
    return {
        "id": project.project_id,
        "name": project.name,
        "description": project.description,
        "scenario": project.scenario,
        "aspectRatio": project.settings.aspect_ratio,
        "resolution": project.settings.resolution,
        "contentType": project.settings.content_type,
        **(
            {
                "executionPreauthorization": (
                    ExecutionPreauthorizationPolicy.model_validate(
                        preauthorization.model_dump(mode="python"),
                    ).model_dump(mode="json", by_alias=True)
                ),
            }
            if preauthorization is not None
            else {}
        ),
    }


def _existing_bootstrap(
    services: CreatorFileServices,
    *,
    project_id: str,
    expected_session_id: str,
    expected_conversation_id: str,
    request_hash: str,
) -> ProjectCreateResponse:
    services.projects.read(project_id)
    session = services.sessions.get_project_session(project_id)
    conversations = services.sessions.list_conversations(
        project_id,
        session.session_id,
    )
    defaults = [item for item in conversations if item.is_default]
    if len(defaults) != 1:
        raise StorageIntegrityError(
            "Project Runtime 必须且只能有一个默认 Conversation",
        )
    create_metadata = session.metadata.get("projectCreate")
    if not isinstance(create_metadata, dict):
        raise ConflictError("Project 已存在但缺少文件创建幂等记录")
    if create_metadata.get("requestHash") != request_hash:
        raise ConflictError("clientRequestId 已用于不同 Project payload")
    project_snapshot_id = create_metadata.get("projectSnapshotId")
    stored_response = create_metadata.get("response")
    if (
        session.session_id != expected_session_id
        or defaults[0].conversation_id != expected_conversation_id
        or not isinstance(project_snapshot_id, str)
        or not project_snapshot_id
    ):
        raise StorageIntegrityError("Project Runtime 创建记录与确定性身份不一致")
    try:
        response = ProjectCreateResponse.model_validate(stored_response)
    except PydanticValidationError as exc:
        raise StorageIntegrityError("Project 创建响应快照损坏") from exc
    if (
        response.project_id != project_id
        or response.creator_session_id != expected_session_id
        or response.conversation_id != expected_conversation_id
        or response.project_snapshot_id != project_snapshot_id
        or response.header.get("id") != project_id
    ):
        raise StorageIntegrityError("Project 创建响应快照身份不一致")
    return response


@router.get("")
async def list_projects(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort_by: Literal["updated_at", "created_at", "name"] = Query("updated_at"),
    sort_order: Literal["asc", "desc"] = Query("desc"),
    services: CreatorFileServices = Depends(project_file_services),
) -> dict[str, Any]:
    try:
        records = await asyncio.to_thread(
            services.projects.list,
            sort_by,
            sort_order,
        )
    except (ProjectIntegrityError, ProjectStoreError) as exc:
        raise StorageIntegrityError(str(exc)) from exc
    page = records[offset : offset + limit]
    return {
        "items": [
            {
                "projectId": item.project_id,
                "name": item.name,
                "description": item.description,
                "scenario": item.scenario,
                "aspectRatio": item.aspect_ratio,
                "resolution": item.resolution,
                "contentType": item.content_type,
                "createdAt": item.created_at,
                "updatedAt": item.updated_at,
            }
            for item in page
        ],
        "limit": limit,
        "offset": offset,
    }


@router.post(
    "",
    response_model=ProjectCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    request: ProjectCreateRequest,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    services: CreatorFileServices = Depends(project_file_services),
) -> ProjectCreateResponse:
    client_request_id = resolve_idempotency_key(
        idempotency_key,
        stable_client_id=request.client_request_id,
    )
    request_hash = _request_hash(request)
    project_id = _stable_id("project", client_request_id)
    session_id = _stable_id("session", client_request_id)
    conversation_id = _stable_id("conversation", client_request_id)
    goal_id = _stable_id("goal", client_request_id)
    message_id = _stable_id("message", client_request_id)
    project_snapshot_id = _project_snapshot_id(project_id)
    project = Project.new(
        project_id=project_id,
        name=request.name.strip(),
        description=request.description.strip(),
        scenario=request.scenario,
        settings=_settings(request),
    )
    initial_response = ProjectCreateResponse(
        projectId=project_id,
        creatorSessionId=session_id,
        conversationId=conversation_id,
        projectSnapshotId=project_snapshot_id,
        header=_header(project),
    )

    def operation() -> ProjectCreateResponse:
        # The name-uniqueness check and the create must share one
        # cross-process lock; per-project lifecycle locks have different
        # domains for different project ids.
        with CrossProcessFileLock(
            services.projects.root / ".project-names.lock",
        ):
            existing = services.projects.list()
            target_name = request.name.strip()
            if any(item.name == target_name for item in existing):
                raise ValidationError(f"项目名称「{target_name}」已存在，请使用其他名称")

            holder: list[ProjectRuntimeBootstrap] = []

            def initialize(staged_project_root) -> None:
                holder.append(
                    services.sessions.initialize_staged_project(
                        staged_project_root,
                        project_id,
                        session_id=session_id,
                        conversation_id=conversation_id,
                        session_metadata={
                            "projectCreate": {
                                "clientRequestId": client_request_id,
                                "requestHash": request_hash,
                                "projectSnapshotId": project_snapshot_id,
                                "response": initial_response.model_dump(
                                    mode="json",
                                    by_alias=True,
                                ),
                            },
                        },
                        initial_goal=request.initial_goal,
                        goal_id=goal_id
                        if request.initial_goal is not None
                        else None,
                        initial_message_id=(
                            message_id
                            if request.initial_goal is not None
                            else None
                        ),
                        initial_client_message_id=(
                            f"initial-goal:{client_request_id}"
                            if request.initial_goal is not None
                            else None
                        ),
                    ),
                )

            try:
                snapshot = services.projects.create(
                    project,
                    initialize_staged_project=initialize,
                )
            except ProjectAlreadyExists:
                return _existing_bootstrap(
                    services,
                    project_id=project_id,
                    expected_session_id=session_id,
                    expected_conversation_id=conversation_id,
                    request_hash=request_hash,
                )
        if len(holder) != 1:
            raise StorageIntegrityError("Project Runtime 未随 Project 原子创建")
        services.poller.note_commit(snapshot)
        # Return the response from the durable creation receipt as well.  This
        # makes the first call and every later replay byte-for-byte stable even
        # if project.json is subsequently edited.
        return _existing_bootstrap(
            services,
            project_id=project_id,
            expected_session_id=session_id,
            expected_conversation_id=conversation_id,
            request_hash=request_hash,
        )

    try:
        result = await asyncio.to_thread(operation)
    except (ConflictError, StorageIntegrityError):
        raise
    except (ProjectIntegrityError, ProjectStoreError, RuntimeFileError) as exc:
        raise StorageIntegrityError(str(exc)) from exc
    notify_creator_agent_runtime(project_id)
    response.status_code = status.HTTP_201_CREATED
    return result


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    services: CreatorFileServices = Depends(project_file_services),
) -> Response:
    # Deletion is naturally idempotent at the filesystem lifecycle boundary:
    # one process atomically renames the Project out of discovery and every
    # concurrent/replayed request observes the same absent state.
    resolve_idempotency_key(idempotency_key)
    await interrupt_creator_agent_runtime(
        project_id,
        superseded=False,
        reason="project_deleted",
    )
    try:
        await asyncio.to_thread(services.projects.delete, project_id)
    except ProjectNotFound:
        pass
    except (ProjectIntegrityError, ProjectStoreError) as exc:
        raise StorageIntegrityError(str(exc)) from exc
    services.poller.close(project_id)
    try:
        await asyncio.to_thread(
            services.poller.poll_once,
            project_id,
            force=True,
        )
    except ProjectNotFound:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{project_id}/export")
async def export_project(
    project_id: str,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    services: CreatorFileServices = Depends(project_file_services),
) -> StreamingResponse:
    #
    logger.info(f"exporting project:{project_id}")
    resolve_idempotency_key(idempotency_key)
    await interrupt_creator_agent_runtime(
        project_id,
        superseded=False,
        reason="exporting_project",
    )
    logger.debug("export project interrupted runtime")
    try:
        safe_id = _safe_project_id(project_id)
        return StreamingResponse(
            content=services.projects.export(project_id),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_id}.zip"',
            },
        )
    except Exception as e:
        logger.error(f"failed to export project {project_id}", exc_info=True)
        raise StorageIntegrityError(
            message=f"Failed to export project {project_id}: {str(e)}",
        ) from e


async def _run_import(upload) -> str:
    data_root = require_creator_data_root()
    imports_root = data_root / "imports"
    imports_root.mkdir(parents=True, exist_ok=True)

    uploaded_file = Path(upload.filename or "import.zip").name
    logger.info(f"importing project from {uploaded_file}")
    upload_bytes = await upload.read()
    logger.info(
        f"read bytes from uploaded zip {uploaded_file}, {len(upload_bytes)} bytes",
    )
    # Save the uploaded zip into the imports folder, then extract
    # it there so we can inspect project.json before publishing.
    temp_str = uuid4().hex
    saved_zip = imports_root / f"{temp_str}-{uploaded_file}"
    extract_dir = imports_root / f"{temp_str}"
    try:
        logger.info(f"writing zip bytes to {saved_zip}")
        saved_zip.write_bytes(upload_bytes)
        extract_dir.mkdir(mode=0o700)
        logger.info(f"unpacking zip from {saved_zip} to {extract_dir}")
        try:
            shutil.unpack_archive(
                str(saved_zip),
                extract_dir=extract_dir,
                format="zip",
            )
        except Exception as e:
            raise BadRequestError(
                f"failed to unpack uploaded file: {str(e)}",
            ) from e

        # extract_dir/project-xxx/, where project-xxx should be the only item in extract-dir
        dirs = []
        for d in extract_dir.iterdir():
            dirs.append(d)

        if not (
            len(dirs) == 1
            and dirs[0].is_dir()
            and dirs[0].name.startswith("project-")
            and dirs[0].name == _safe_project_id(dirs[0].name)
        ):
            raise BadRequestError(
                f"expecting only one project-* folder from unpacked file: {dirs}",
            )

        logger.info(f"looking for project.json in {dirs[0]}")
        # Load project.json from the extracted tree and read the
        # Project's project_id.
        project_json_path = None
        for f in dirs[0].iterdir():
            if f.is_file() and f.name == "project.json":
                project_json_path = f
                break

        if project_json_path is None:
            raise BadRequestError(
                "project.json not found in the uploaded data.",
            )

        logger.info(f"loading project obj from {project_json_path}")
        project_id = None
        try:
            project = load_project_json(project_json_path.read_bytes())
            project_id = str(project.project_id)
            if not project_id:
                raise ValueError(
                    f"project_id not found in {project_json_path}",
                )
        except Exception as e:
            raise BadRequestError(f"Invalid Project object: {str(e)}") from e

        logger.info(
            f"found project id in {project_json_path}: {project_id}",
        )

        target_project_dir = Path(data_root, dirs[0].name)
        if target_project_dir.exists():
            raise BadRequestError(
                f"project already exists {target_project_dir}",
            )
        # move the unpacked project-*** folder into creator data root so the
        # Project directory is published under its real project_id.
        shutil.move(dirs[0], data_root)
        logger.info(f"moved project folder {dirs[0]} to {data_root}")
        return project_id
    finally:
        saved_zip.unlink(missing_ok=True)
        shutil.rmtree(extract_dir, ignore_errors=True)
        logger.info(
            f"deleted temporary importing file and folder {saved_zip}, {extract_dir}",
        )


@router.post("/import")
async def import_project(
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    logger.info(f"import request, idempotency_key:{idempotency_key}")
    resolve_idempotency_key(idempotency_key)
    form = await request.form()
    logger.debug("import request, form is ready")
    for k, v in form.multi_items():
        logger.debug(
            f"import request form, k:{k}, v: type:{type(v)}, value:{v}",
        )
    upload = next(
        (
            value
            for _, value in form.multi_items()
            if isinstance(value, UploadFile)
        ),
        None,
    )
    if upload is None:
        raise BadRequestError("No uploaded file in the request")

    try:
        project_id = await _run_import(upload)
    except Exception as e:
        logger.error("failed to import project", exc_info=True)
        raise BadRequestError(f"Failed to import project: {str(e)}") from e
    return {"projectId": project_id}
