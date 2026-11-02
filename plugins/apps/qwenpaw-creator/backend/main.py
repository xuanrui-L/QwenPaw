# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=wrong-import-order,wrong-import-position,ungrouped-imports
"""QwenPaw Creator — PawApp backend entry point.

Exports a :class:`PawApp` instance named ``app`` (also aliased as
``plugin``) which the PluginLoader discovers and registers. All Creator
REST routes are mounted under ``/api/qwenpaw-creator`` via the PawApp
router prefix contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from functools import partial
from pathlib import Path

from qwenpaw.pawapp import PawApp

logger = logging.getLogger("qwenpaw").getChild("plugin.qwenpaw_creator")

BACKEND_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = BACKEND_DIR.parent

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from utils.env import load_project_env  # noqa: E402

load_project_env()

from api.router import router as creator_router  # noqa: E402
from api.file_asset_routes import (  # noqa: E402
    drain_remote_ingest_tasks,
    reset_remote_ingest_admission,
)
from api.file_execution_routes import (  # noqa: E402
    drain_timeline_render_jobs,
)
from services.file_agent_runtime.registry import (  # noqa: E402
    start_creator_agent_runtime,
    stop_creator_agent_runtime,
)
from services.media_files import (  # noqa: E402
    shutdown_file_media_execution_services,
    start_file_media_execution_services,
)
from services.media_files.motion_engine import ensure_vendor_libs  # noqa: E402
from services.media_files.task_scratch import (
    budget_from_environment,
    release_orphaned_scratch,
)  # noqa: E402
from services.observability import mem_probe, trace_event  # noqa: E402
from services.project_files.facade import (  # noqa: E402
    CreatorFileServices,
    clear_creator_file_service_registry,
    creator_file_services,
    mark_runtime_preparing,
)
from services.project_files.transaction_gc import (  # noqa: E402
    collect_terminal_transactions,
)
from services.runtime_files.runtime_dependencies import (  # noqa: E402
    CreatorBinaryDependencyError,
    ensure_creator_runtime_dependencies,
)
from services.runtime_files.session_elision import (  # noqa: E402
    elide_stored_conversations,
)
from services.storage_root import (  # noqa: E402
    CreatorDataRootError,
    require_creator_data_root,
)
from services.source_analysis import (  # noqa: E402
    recover_interrupted_source_analysis,
    shutdown_source_analysis_services,
)
from services.media.source_memory import (  # noqa: E402
    recover_interrupted_source_memory,
)
from services.media.source_observation import (  # noqa: E402
    drain_source_observation_services,
    recover_interrupted_source_observations,
)
from services.media.source_video_reader import (  # noqa: E402
    drain_source_video_reader_services,
    recover_interrupted_source_video_reads,
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


app = PawApp("QwenPaw Creator", app_id="qwenpaw-creator")
app.include_router(creator_router)

# Creator file runtime handle kept for the lifetime of the app.
_file_services: CreatorFileServices | None = None

# A background maintenance pass has to be referenced or the loop may collect it
# mid-sweep.
_maintenance_task: "asyncio.Task[None] | None" = None

# Long enough for recovery and the first page to settle, short enough that a
# customer's reclaimed space arrives while they are still looking at the list.
_MAINTENANCE_DELAY_SECONDS = 20.0


async def _sweep_orphaned_scratch(
    services: CreatorFileServices,
    budget,
) -> None:
    """Collect scratch a killed process left behind, without gating startup.

    A finished Task releases its own scratch through the execution store the
    moment it turns terminal, so this is only a safety net for the window
    between materialize and release; everything it cannot prove finished stays.
    """

    try:
        sweep = await asyncio.to_thread(
            partial(release_orphaned_scratch, services, budget=budget),
        )
    except Exception:  # noqa: BLE001 - cleanup must never gate startup
        logger.warning("orphaned task scratch sweep failed", exc_info=True)
        return
    if sweep.released_directories:
        logger.info(
            "orphaned task scratch released: projects=%s dirs=%s files=%s "
            "bytes=%s stoppedEarly=%s",
            sweep.projects,
            sweep.released_directories,
            sweep.removed_files,
            sweep.removed_bytes,
            sweep.stopped_early,
        )
    if sweep.skipped_projects:
        logger.warning(
            "scratch reclaim withheld pending confirmation: projects=%s "
            "bytes=%s (confirm through the storage page)",
            sweep.skipped_projects,
            sweep.skipped_bytes,
        )


async def _collect_settled_transactions(
    services: CreatorFileServices,
    budget,
) -> None:
    """Drop commit history no reader can use, without gating startup.

    Runs after the scratch sweep, which stamps the reclaim baseline this sweep
    measures against. Recovery has already reconciled every journal by now, so
    anything still non-terminal is a transaction the next commit must see.
    """

    try:
        sweep = await asyncio.to_thread(
            partial(collect_terminal_transactions, services, budget=budget),
        )
    except Exception:  # noqa: BLE001 - cleanup must never gate startup
        logger.warning("terminal transaction sweep failed", exc_info=True)
        return
    if sweep.removed_transactions:
        logger.info(
            "terminal transactions released: projects=%s swept=%s "
            "transactions=%s bytes=%s stoppedEarly=%s",
            sweep.projects,
            sweep.swept_projects,
            sweep.removed_transactions,
            sweep.removed_bytes,
            sweep.stopped_early,
        )
    if sweep.skipped_projects:
        logger.warning(
            "transaction reclaim withheld pending confirmation: projects=%s "
            "bytes=%s (confirm through the storage page)",
            sweep.skipped_projects,
            sweep.skipped_bytes,
        )


async def _run_storage_maintenance(
    services: CreatorFileServices,
) -> None:
    """Free settled history and collapse stale echoes, after the first paint.

    None of this is awaited by the startup hook any more.  Deleting a tree that
    arrived with 24 GB of receipts and scratch is measured in minutes, and a
    boot that waits those minutes looks exactly like a hang to the person who
    just upgraded -- so the pass runs later, on one budget shared by both
    sweeps, and whatever it could not reach is left for the next boot.  The
    conversation collapse reads whole streams and is therefore handed its own
    child process.
    """

    await asyncio.sleep(_MAINTENANCE_DELAY_SECONDS)
    budget = budget_from_environment()
    await _sweep_orphaned_scratch(services, budget)
    await _collect_settled_transactions(services, budget)
    await _elide_stored_conversations(services)


async def _elide_stored_conversations(
    services: CreatorFileServices,
) -> None:
    """Collapse legacy project echoes inside stored conversations.

    Where the two sweeps above walk directories, this one reads whole session
    streams, so it hands each stream to a child process: a 606 MB stream costs
    its reader about four times its size, and that bill belongs to a process the
    app can lose without dying with it.  Each pass records where it left every
    file, so the next boot pays a stat instead of a re-read.
    """

    try:
        summary = await elide_stored_conversations(services)
    except Exception:  # noqa: BLE001 - cleanup must never gate the app
        logger.warning("stored conversation elision failed", exc_info=True)
        return
    for failure in summary.failures:
        logger.warning("stored conversation elision failed: %s", failure)
    if summary.oversize_files:
        logger.warning(
            "stored conversations left uncollapsed as too large to risk: "
            "files=%s bytes=%s (raise %s only on an install with the room)",
            summary.oversize_files,
            summary.oversize_bytes,
            "CREATOR_MESSAGE_ELISION_MAX_BYTES",
        )
    if summary.saved_bytes:
        logger.info(
            "stored conversation echoes collapsed: projects=%s files=%s "
            "bytes=%s",
            summary.projects,
            summary.files,
            summary.saved_bytes,
        )


@app.hook("startup", priority=90)
async def _startup() -> None:  # pylint: disable=too-many-statements
    global _file_services, _maintenance_task
    try:
        configure_creator_runtime_environment()
        data_root = require_creator_data_root()
        log_path = configure_creator_file_logging(data_root)
        mem_probe.start_probe(data_root)
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
        await asyncio.to_thread(ensure_vendor_libs)
        # Recovery scans every Project and takes minutes on a large tree.  The
        # event loop has to keep answering while it runs, and requests that
        # arrive in that window get a 503 instead of queueing behind the same
        # registry lock the recovery holds.
        mark_runtime_preparing(True)
        try:
            services = await asyncio.to_thread(
                creator_file_services,
                data_root,
            )
        finally:
            mark_runtime_preparing(False)
        await asyncio.to_thread(
            recover_interrupted_source_analysis,
            services,
        )
        reset_remote_ingest_admission()
        mem_probe.stage("runtime.prepared")
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
        await asyncio.to_thread(recover_interrupted_source_memory, services)
        # Observation/read workers live only in process memory: fail their
        # orphaned QUEUED/RUNNING Tasks closed so waiters see a retryable
        # error instead of polling a record no worker will ever finish.
        await asyncio.to_thread(
            recover_interrupted_source_observations,
            services,
        )
        await asyncio.to_thread(
            recover_interrupted_source_video_reads,
            services,
        )
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
    _maintenance_task = asyncio.create_task(_run_storage_maintenance(services))
    # Loud, not silent: explicitly set review env vars take full control
    # and the settings-center toggles are ignored — a field incident had
    # review running with the UI switched off because a stale env stayed
    # injected in the launch command.
    from models.config import forced_review_env_overrides

    overrides = forced_review_env_overrides()
    if overrides:
        logger.warning(
            "Review tiers forced by environment (settings-center toggles "
            "are ignored for these): %s",
            ", ".join(f"{k}={v}" for k, v in overrides.items()),
        )
    trace_event(
        "creator.runtime.started",
        component="pawapp",
        attributes={
            "dataRoot": str(data_root),
            "dependencyCount": len(dependency_status),
        },
    )
    logger.info(
        "QwenPaw Creator native tool status: %s",
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
    configured = [
        key
        for key in (
            "TEXT_API_KEY",
            "VLM_API_KEY",
            "ASR_API_KEY",
            "TTS_API_KEY",
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
    global _file_services, _maintenance_task
    trace_event(
        "creator.runtime.stopping",
        component="pawapp",
    )
    _file_services = None
    if _maintenance_task is not None:
        # A sweep -- or a collapse child holding a rewritten session file --
        # must not outlive the app that owns the data root.  Cancelling the
        # task kills the child too; see _run_one.
        _maintenance_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _maintenance_task
        _maintenance_task = None
    try:
        await drain_remote_ingest_tasks()
    finally:
        try:
            await drain_timeline_render_jobs()
        finally:
            try:
                await _shutdown_file_runtime()
            finally:
                mem_probe.stop_probe()


async def _shutdown_file_runtime() -> None:
    """Stop the media/agent runtime layers in dependency order."""

    try:
        await _drain_source_media_services()
    finally:
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


async def _drain_source_media_services() -> None:
    """Terminalize in-flight observation/read Tasks before shutdown."""

    try:
        await drain_source_observation_services()
    finally:
        await drain_source_video_reader_services()


# The 'plugin' variable is what PluginLoader looks for.
plugin = app
