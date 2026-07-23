# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=wrong-import-order,wrong-import-position
"""QwenPaw Creator — PawApp backend entry point.

Exports a :class:`PawApp` instance named ``app`` (also aliased as
``plugin``) which the PluginLoader discovers and registers. All Creator
REST routes are mounted under ``/api/qwenpaw-creator`` via the PawApp
router prefix contract.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from qwenpaw.pawapp import PawApp

logger = logging.getLogger("qwenpaw").getChild("plugin.qwenpaw_creator")

BACKEND_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = BACKEND_DIR.parent
CONFIG_TOOLS = {
    "creator_text_model": {
        "description": "QwenPaw Creator text model configuration",
        "icon": "📝",
    },
    "creator_vlm_model": {
        "description": "QwenPaw Creator native multimodal understanding configuration",
        "icon": "👁️",
    },
    "creator_asr_model": {
        "description": "QwenPaw Creator speech-to-text configuration",
        "icon": "🎙️",
    },
    "creator_image_model": {
        "description": "QwenPaw Creator image generation configuration",
        "icon": "🎨",
    },
    "creator_video_model": {
        "description": "QwenPaw Creator reference-to-video configuration",
        "icon": "🎬",
    },
    "creator_media_oss": {
        "description": "QwenPaw Creator OSS/media configuration",
        "icon": "🪣",
    },
}

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from utils.env import load_project_env  # noqa: E402

load_project_env()

from api.router import router as creator_router  # noqa: E402
from services.file_agent_runtime.registry import (  # noqa: E402
    start_creator_agent_runtime,
    stop_creator_agent_runtime,
)
from services.media_files import (  # noqa: E402
    shutdown_file_media_execution_services,
    start_file_media_execution_services,
)
from services.observability import trace_event  # noqa: E402
from services.project_files.facade import (  # noqa: E402
    CreatorFileServices,
    clear_creator_file_service_registry,
    creator_file_services,
)
from services.runtime_files.runtime_dependencies import (  # noqa: E402
    CreatorBinaryDependencyError,
    ensure_creator_runtime_dependencies,
)
from services.storage_root import (  # noqa: E402
    CreatorDataRootError,
    require_creator_data_root,
)
from services.source_analysis import (  # noqa: E402
    recover_interrupted_source_analysis,
    shutdown_source_analysis_services,
)
from utils.logger import configure_creator_file_logging  # noqa: E402


def configure_creator_runtime_environment(
    *,
    working_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Provision Creator paths below QwenPaw's working directory by default.

    Explicit environment values remain authoritative, but they must preserve
    the backend's absolute external-root contract and keep model configuration
    inside the selected Creator data root.
    """

    if working_dir is None:
        from qwenpaw.constant import WORKING_DIR

        working_dir = Path(WORKING_DIR)

    configured_root = os.environ.get("CREATOR_DATA_ROOT", "").strip()
    data_root = (
        Path(configured_root).expanduser()
        if configured_root
        else working_dir / "creator-runtime"
    )
    if not data_root.is_absolute():
        raise CreatorDataRootError(
            "CREATOR_DATA_ROOT must be an absolute path",
        )
    data_root = data_root.resolve(strict=False)

    configured_model_path = os.environ.get(
        "CREATOR_MODEL_CONFIG_PATH",
        "",
    ).strip()
    model_config_path = (
        Path(configured_model_path).expanduser()
        if configured_model_path
        else data_root / "config" / "model_config.json"
    )
    if not model_config_path.is_absolute():
        raise CreatorDataRootError(
            "CREATOR_MODEL_CONFIG_PATH must be an absolute path",
        )
    model_config_path = model_config_path.resolve(strict=False)
    if not model_config_path.is_relative_to(data_root):
        raise CreatorDataRootError(
            "CREATOR_MODEL_CONFIG_PATH must be inside CREATOR_DATA_ROOT",
        )

    data_root.mkdir(parents=True, exist_ok=True)
    model_config_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["CREATOR_DATA_ROOT"] = str(data_root)
    os.environ["CREATOR_MODEL_CONFIG_PATH"] = str(model_config_path)
    configured_binary_dir = os.environ.get("CREATOR_BINARY_DIR", "").strip()
    if configured_binary_dir:
        binary_dir = Path(configured_binary_dir).expanduser()
        if not binary_dir.is_absolute():
            raise CreatorDataRootError(
                "CREATOR_BINARY_DIR must be an absolute path",
            )
        binary_dir = binary_dir.resolve(strict=False)
    else:
        binary_dir = data_root / "runtime-tools" / "bin"
    binary_dir.mkdir(parents=True, exist_ok=True)
    os.environ["CREATOR_BINARY_DIR"] = str(binary_dir)
    return data_root, model_config_path


def _ensure_config_tools_registered() -> None:
    """Ensure CLI-installed Creator config tools exist for every agent."""
    try:
        from qwenpaw.config.config import (
            BuiltinToolConfig,
            ToolsConfig,
            load_agent_config,
            save_agent_config,
        )
        from qwenpaw.config.utils import load_config

        config = load_config()
        profiles = getattr(getattr(config, "agents", None), "profiles", {})
        for agent_id in profiles:
            agent_config = load_agent_config(agent_id)
            if not agent_config.tools:
                agent_config.tools = ToolsConfig()
            changed = False
            for tool_name, meta in CONFIG_TOOLS.items():
                if tool_name in agent_config.tools.builtin_tools:
                    continue
                agent_config.tools.builtin_tools[
                    tool_name
                ] = BuiltinToolConfig(
                    name=tool_name,
                    enabled=False,
                    description=meta["description"],
                    display_to_user=True,
                    async_execution=False,
                    icon=meta["icon"],
                )
                changed = True
            if changed:
                save_agent_config(agent_id, agent_config)
                logger.info(
                    "QwenPaw Creator config tools synced for agent '%s'",
                    agent_id,
                )
    except Exception as exc:
        logger.warning("QwenPaw Creator config tool sync skipped: %s", exc)


# ── PawApp definition ────────────────────────────────────────────────

app = PawApp("QwenPaw Creator", app_id="qwenpaw-creator")
app.include_router(creator_router)

# Creator file runtime handle kept for the lifetime of the app.
_file_services: CreatorFileServices | None = None


@app.hook("startup", priority=90)
async def _startup() -> None:
    global _file_services
    try:
        configure_creator_runtime_environment()
        data_root = require_creator_data_root()
        log_path = configure_creator_file_logging(data_root)
        trace_event(
            "creator.runtime.starting",
            component="pawapp",
            attributes={
                "dataRoot": str(data_root),
                "logPath": str(log_path),
            },
        )
        dependency_status = await asyncio.to_thread(
            ensure_creator_runtime_dependencies,
        )
        services = creator_file_services(data_root)
        recover_interrupted_source_analysis(services)
    except (CreatorDataRootError, CreatorBinaryDependencyError) as exc:
        trace_event(
            "creator.runtime.startup_failed",
            component="pawapp",
            status="error",
            attributes={
                "errorType": type(exc).__name__,
                "error": str(exc),
            },
        )
        logger.exception(
            "QwenPaw Creator runtime dependency preparation failed; startup aborted",
        )
        raise
    try:
        await start_file_media_execution_services(services)
        await start_creator_agent_runtime(services)
    except BaseException as exc:
        trace_event(
            "creator.runtime.startup_failed",
            component="pawapp",
            status="error",
            attributes={
                "errorType": type(exc).__name__,
                "error": str(exc),
            },
        )
        try:
            await shutdown_file_media_execution_services()
        finally:
            try:
                await stop_creator_agent_runtime()
            finally:
                clear_creator_file_service_registry()
        raise
    _file_services = services
    logger.info("QwenPaw Creator file runtime ready at %s", data_root)
    trace_event(
        "creator.runtime.started",
        component="pawapp",
        attributes={
            "dataRoot": str(data_root),
            "dependencyCount": len(dependency_status),
        },
    )
    logger.info(
        "QwenPaw Creator native tools ready: %s",
        {
            name: {
                "status": item.status,
                "path": item.path,
                "source": item.source,
            }
            for name, item in dependency_status.items()
        },
    )
    project_errors = services.startup_recovery.integrity_errors
    review_errors = services.startup_review_recovery.integrity_errors
    if project_errors or review_errors:
        logger.error(
            "Creator startup recovery completed with %d Project and %d Review "
            "integrity error(s); affected Projects remain fail-closed",
            len(project_errors),
            len(review_errors),
        )
    _ensure_config_tools_registered()
    configured = [
        key
        for key in (
            "TEXT_API_KEY",
            "VLM_API_KEY",
            "ASR_API_KEY",
            "IMAGE_API_KEY",
            "VIDEO_API_KEY",
            "OSS_POLICY_API_KEY",
            "OSS_ACCESS_KEY_ID",
            "OSS_ENDPOINT",
        )
        if os.environ.get(key)
    ]
    if configured:
        logger.info(
            "QwenPaw Creator env-backed config available: %s",
            configured,
        )
    else:
        logger.warning(
            "QwenPaw Creator is installed, but no env-backed generation "
            "config was found. Configure creator_text_model, creator_vlm_model, creator_asr_model, "
            "creator_image_model, creator_video_model, and creator_media_oss "
            "in QwenPaw Tools, or set the matching TEXT/IMAGE/VIDEO/OSS "
            "environment variables.",
        )


@app.hook("shutdown", priority=90)
async def _shutdown() -> None:
    global _file_services
    trace_event(
        "creator.runtime.stopping",
        component="pawapp",
    )
    _file_services = None
    try:
        await shutdown_file_media_execution_services()
    finally:
        try:
            await shutdown_source_analysis_services()
        finally:
            try:
                await stop_creator_agent_runtime()
            finally:
                clear_creator_file_service_registry()
                trace_event(
                    "creator.runtime.stopped",
                    component="pawapp",
                )


# The 'plugin' variable is what PluginLoader looks for.
plugin = app
