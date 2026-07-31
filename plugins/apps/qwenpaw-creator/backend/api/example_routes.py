# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Plugin-bundled inspiration example Projects.

Examples ship with the plugin under ``backend/examples/``: one
``manifest.json`` plus one exported Project archive per example.  Opening an
example lazily materializes its archive into ``CREATOR_DATA_ROOT`` under the
fixed project id recorded in the manifest.  A ``BUILTIN_EXAMPLE_MARKER`` file
is staged inside the Project directory before publication, so the example is
excluded from the user's project listing atomically with its appearance.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends

from domain.errors import (
    BadRequestError,
    NotFoundError,
    StorageIntegrityError,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.store import (
    BUILTIN_EXAMPLE_MARKER,
    UnsafeProjectPath,
    _safe_project_id,
)
from services.runtime_files.locking import CrossProcessFileLock
from services.storage_root import require_creator_data_root
from utils.logger import setup_logger

from .dependencies import CreatorErrorRoute, project_file_services
from .project_routes import _validate_import_archive

logger = setup_logger("example_routes")

router = APIRouter(
    prefix="/examples",
    tags=["examples"],
    route_class=CreatorErrorRoute,
)


def examples_root() -> Path:
    """Bundled examples directory; module-level so tests can monkeypatch."""

    return Path(__file__).resolve().parent.parent / "examples"


def _load_manifest() -> list[dict[str, Any]]:
    """Return manifest entries whose archive actually shipped with the plugin.

    A missing or malformed manifest yields an empty catalogue instead of an
    error: the home page simply hides the inspiration section.
    """

    root = examples_root()
    manifest_path = root / "manifest.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = raw.get("examples") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    catalogue: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        example_id = entry.get("id")
        title = entry.get("title")
        description = entry.get("description")
        project_id = entry.get("projectId")
        archive = entry.get("archive")
        if not all(
            isinstance(value, str) and value
            for value in (example_id, title, description, project_id, archive)
        ):
            continue
        try:
            _safe_project_id(project_id)
        except UnsafeProjectPath:
            continue
        # Archive names are plain file names inside the examples directory.
        if Path(archive).name != archive:
            continue
        if not (root / archive).is_file():
            continue
        catalogue.append(
            {
                "id": example_id,
                "title": title,
                "description": description,
                "projectId": project_id,
                "archive": archive,
            },
        )
    return catalogue


def _public_item(entry: dict[str, Any], data_root: Path) -> dict[str, Any]:
    return {
        "id": entry["id"],
        "title": entry["title"],
        "description": entry["description"],
        "projectId": entry["projectId"],
        "installed": (
            data_root / entry["projectId"] / "project.json"
        ).is_file(),
    }


@router.get("")
async def list_examples() -> dict[str, Any]:
    data_root = require_creator_data_root()

    def catalogue() -> list[dict[str, Any]]:
        # Manifest parsing and per-entry installed checks are filesystem
        # I/O, so the whole catalogue is built off the event loop.
        return [_public_item(entry, data_root) for entry in _load_manifest()]

    return {"items": await asyncio.to_thread(catalogue)}


def _materialize_example(entry: dict[str, Any], data_root: Path) -> str:
    """Idempotently publish the example Project from its bundled archive."""

    project_id = entry["projectId"]
    target = data_root / project_id
    if (target / "project.json").is_file():
        return project_id

    archive_path = examples_root() / entry["archive"]
    # Pure ZipInfo-level filesystem preflight; no request-scoped state, so
    # it is safe to run inside asyncio.to_thread worker threads.
    _validate_import_archive(archive_path)

    staging_root = data_root / ".example-staging"
    staging_root.mkdir(mode=0o700, exist_ok=True)
    extract_dir = staging_root / uuid4().hex
    extract_dir.mkdir(mode=0o700)
    try:
        try:
            shutil.unpack_archive(
                str(archive_path),
                extract_dir=extract_dir,
                format="zip",
            )
        except Exception as exc:
            raise StorageIntegrityError(
                f"内置示例归档无法解包: {entry['id']}",
            ) from exc
        staged = extract_dir / project_id
        if not (staged / "project.json").is_file():
            raise StorageIntegrityError(
                f"内置示例归档缺少 {project_id}/project.json: {entry['id']}",
            )
        # The marker is staged before publication so the example can never be
        # observed without it (and thus never leaks into the project listing).
        (staged / BUILTIN_EXAMPLE_MARKER).write_text(
            json.dumps({"exampleId": entry["id"]}) + "\n",
            encoding="utf-8",
        )
        # Serialize with project creation so a same-id race publishes once.
        with CrossProcessFileLock(data_root / ".example-install.lock"):
            if not (target / "project.json").is_file():
                try:
                    os.rename(staged, target)
                except OSError as exc:
                    if not (target / "project.json").is_file():
                        raise StorageIntegrityError(
                            f"内置示例发布失败: {entry['id']}",
                        ) from exc
        logger.info(
            "materialized builtin example %s as %s",
            entry["id"],
            project_id,
        )
        return project_id
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


@router.post("/{example_id}/open")
async def open_example(
    example_id: str,
    services: CreatorFileServices = Depends(project_file_services),
) -> dict[str, Any]:
    entries = await asyncio.to_thread(_load_manifest)
    entry = next(
        (item for item in entries if item["id"] == example_id),
        None,
    )
    if entry is None:
        raise NotFoundError(f"内置示例不存在: {example_id}")
    try:
        project_id = await asyncio.to_thread(
            _materialize_example,
            entry,
            require_creator_data_root(),
        )
    except BadRequestError as exc:
        # A corrupt bundled archive is a plugin-install integrity problem,
        # not a caller mistake.
        raise StorageIntegrityError(
            f"内置示例归档已损坏: {example_id}（{exc.message}）",
        ) from exc
    # Prime the poll cache so the project page loads without a first-poll miss.
    try:
        await asyncio.to_thread(services.poller.poll_once, project_id)
    except Exception:  # pragma: no cover - cache warming is best effort
        logger.warning("example poll priming failed", exc_info=True)
    return {"projectId": project_id, "installed": True}
