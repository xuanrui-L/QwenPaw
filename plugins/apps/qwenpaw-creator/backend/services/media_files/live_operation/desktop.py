# -*- coding: utf-8 -*-
"""Desktop live operation: drive a native app and record the screen.

This mirrors the browser bridge so the agent organizes the flow itself: it
writes async Python with ``desktop`` (observe/act on the native app, reusing
the host's Computer Use client) and ``recorder`` (system screen capture) in
scope, and only start/stop bounds are filmed.

Desktop control requires the Tauri host's native runtime, which is absent on
headless servers. The tool therefore always probes capability first and
degrades with a clear, actionable result instead of failing opaquely — a
static UI can be shown with a screenshot plus motion instead of a recording.
The native client lives in the computer-use bundle, importable only once the
host has loaded it, so it is bound lazily.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import uuid
from pathlib import Path
from typing import Any

from .bridge import LiveOperationError, LiveOperationRun, _clip, _compile
from .manifest import ActionFact, BoundingBox
from .recorder import RecordedTake
from .screen_recorder import (
    ScreenRecorder,
    ffmpeg_available,
    screen_capture_supported,
)
from .session import workspace_dir

logger = logging.getLogger(__name__)

_READ_ONLY_METHODS = frozenset(
    {"list_apps", "list_windows", "observe_window"},
)


def computer_use_status() -> dict[str, Any]:
    """Report every precondition for desktop operation, separately.

    Reported apart so a caller can tell a machine that will never support the
    feature from a host that simply has not offered a capability yet.
    """
    supported = screen_capture_supported()
    host_reachable = False
    platform_helper = False
    try:
        from qwenpaw.app.computer_use import HostRuntimeProvider

        runtime = HostRuntimeProvider.status()
        platform_helper = bool(runtime.supported_platform)
        host_reachable = bool(runtime.host_reachable)
    except Exception:  # noqa: BLE001 - runtime absent means simply unavailable
        logger.debug("computer-use runtime probe failed", exc_info=True)
    available = (
        supported
        and platform_helper
        and host_reachable
        and (ffmpeg_available())
    )
    return {
        "available": available,
        "screen_capture_supported": supported,
        "native_helper_platform": platform_helper,
        "host_reachable": host_reachable,
        "ffmpeg": ffmpeg_available(),
    }


def _unavailable_reason(status: dict[str, Any]) -> str:
    if (
        not status["native_helper_platform"]
        or not status["screen_capture_supported"]
    ):
        return (
            "desktop operation needs the native Computer Use helper, which "
            "exists only on Windows and macOS"
        )
    if not status["host_reachable"]:
        return (
            "the desktop host runtime is not reachable; desktop operation "
            "needs QwenPaw running on the desktop (Tauri host), not a "
            "headless server"
        )
    if not status["ffmpeg"]:
        return "ffmpeg is unavailable, so the screen cannot be recorded"
    return "desktop operation is unavailable in this environment"


def _load_native_client(session_id: str) -> Any:
    """Bind the host's Computer Use client if the bundle is loaded."""
    try:
        from computer_use.client import (  # type: ignore[import-not-found]
            ComputerUseClient,
        )
    except Exception as exc:  # noqa: BLE001 - bundle absent outside a host
        raise LiveOperationError(
            "the Computer Use client is unavailable; desktop operation needs "
            "the computer-use plugin loaded by the desktop host",
        ) from exc
    return ComputerUseClient(session_id)


class DesktopController:
    """The ``desktop`` name the model sees: observe and act on the app.

    Every method forwards to the host's native client, so the vocabulary and
    trust boundary are the host's, not a reimplementation. Observations expose
    the focused window's bounds, which recording crops to and which action
    coordinates are projected against.
    """

    def __init__(self, client: Any, recorder: ScreenRecorder) -> None:
        self._client = client
        self._recorder = recorder
        self._window_bounds: dict[str, Any] | None = None

    @property
    def window_bounds(self) -> dict[str, Any] | None:
        return self._window_bounds

    async def _execute(self, method: str, **params: Any) -> dict[str, Any]:
        bbox = _bounds_to_bbox(params.get("bounds"))
        manifest = self._recorder.manifest
        started_ms = self._recorder.elapsed_ms() if manifest else 0
        failed = False
        try:
            result = await self._client.execute(method, params)
        except BaseException:
            failed = True
            raise
        finally:
            if manifest is not None and method not in _READ_ONLY_METHODS:
                manifest.record(
                    ActionFact(
                        op=method,
                        t_start_ms=started_ms,
                        t_end_ms=self._recorder.elapsed_ms(),
                        target=str(params.get("element_id") or ""),
                        bbox=bbox,
                        failed=failed,
                    ),
                )
        if method == "observe_window":
            self._remember_bounds(result)
        return result

    def _remember_bounds(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        window = result.get("window")
        bounds = window.get("bounds") if isinstance(window, dict) else None
        if isinstance(bounds, dict):
            self._window_bounds = dict(bounds)

    async def list_apps(self) -> dict[str, Any]:
        return await self._execute("list_apps")

    async def list_windows(self, **params: Any) -> dict[str, Any]:
        return await self._execute("list_windows", **params)

    async def launch_app(self, name: str, **params: Any) -> dict[str, Any]:
        return await self._execute("launch_app", name=name, **params)

    async def observe_window(self, **params: Any) -> dict[str, Any]:
        return await self._execute("observe_window", **params)

    async def click(self, **params: Any) -> dict[str, Any]:
        return await self._execute("click", **params)

    async def type_text(self, text: str, **params: Any) -> dict[str, Any]:
        return await self._execute("type_text", text=text, **params)

    async def press_key(self, key: str, **params: Any) -> dict[str, Any]:
        return await self._execute("press_key", key=key, **params)

    async def scroll(self, **params: Any) -> dict[str, Any]:
        return await self._execute("scroll", **params)

    async def drag(self, **params: Any) -> dict[str, Any]:
        return await self._execute("drag", **params)

    async def invoke_element(self, **params: Any) -> dict[str, Any]:
        return await self._execute("invoke_element", **params)

    async def set_value(self, **params: Any) -> dict[str, Any]:
        return await self._execute("set_value", **params)

    async def close_window(self, **params: Any) -> dict[str, Any]:
        return await self._execute("close_window", **params)


class DesktopRecorderHandle:
    """The ``recorder`` name inside desktop code: start/stop a screen take."""

    def __init__(
        self,
        recorder: ScreenRecorder,
        controller: DesktopController,
    ) -> None:
        self._recorder = recorder
        self._controller = controller

    async def start(self, *, label: str = "", screen: str = "1") -> str:
        return await asyncio.to_thread(
            self._recorder.start,
            label=label,
            window_bounds=self._controller.window_bounds,
            screen=screen,
        )

    async def stop(self) -> dict[str, Any]:
        _output, manifest = await asyncio.to_thread(self._recorder.stop)
        return {
            "take_id": manifest.take_id,
            "label": manifest.label,
            "summary": manifest.summary(),
        }

    def is_recording(self) -> bool:
        return self._recorder.recording


async def run_computer_use_code(
    code: str,
    *,
    run_root: Path,
    run_id: str,
    session_id: str,
    fps: int = 25,
    max_take_seconds: float = 300.0,
    timeout_seconds: float = 600.0,
) -> LiveOperationRun:
    """Run desktop code, or return a clear degraded run when unavailable."""
    source = code.strip()
    if not source:
        raise LiveOperationError("code is empty")
    status = computer_use_status()
    outcome = LiveOperationRun()
    if not status["available"]:
        outcome.output = "computer_use unavailable: " + _unavailable_reason(
            status,
        )
        outcome.result_repr = repr(status)
        return outcome
    compiled = _compile(source)
    workspace = workspace_dir(run_root, run_id)
    recorder = ScreenRecorder(
        workspace=workspace,
        fps=fps,
        max_duration_seconds=max_take_seconds,
    )
    client = _load_native_client(session_id)
    controller = DesktopController(client, recorder)
    from qwenpaw.app.computer_use import set_current_computer_use_turn_id

    turn_token = set_current_computer_use_turn_id(
        f"creator-{uuid.uuid4().hex}",
    )
    stdout = io.StringIO()
    try:
        namespace: dict[str, Any] = {
            "__name__": "__computer_use__",
            "desktop": controller,
            "recorder": DesktopRecorderHandle(recorder, controller),
        }
        with contextlib.redirect_stdout(stdout):
            value = await asyncio.wait_for(
                _run(compiled, namespace),
                timeout=timeout_seconds,
            )
        if value is not None:
            outcome.result_repr = _clip(repr(value), 2_000)
    except TimeoutError as exc:
        raise LiveOperationError(
            f"desktop code exceeded {timeout_seconds:g} seconds",
        ) from exc
    finally:
        finished = await asyncio.to_thread(recorder.stop_if_recording)
        if finished is not None:
            video_path, manifest = finished
            outcome.takes.append(
                RecordedTake(
                    take_id=manifest.take_id,
                    label=manifest.label,
                    video_path=video_path,
                    manifest=manifest,
                ),
            )
        outcome.output = _clip(stdout.getvalue(), 12_000)
        with contextlib.suppress(Exception):
            await client.close()
        _reset_turn(turn_token)
    return outcome


def _run(compiled: Any, namespace: dict[str, Any]):
    async def _inner() -> Any:
        outcome = eval(
            compiled,
            namespace,
        )  # noqa: S307 - the model's own code
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        return outcome

    return _inner()


def _reset_turn(token: Any) -> None:
    from qwenpaw.app.computer_use import set_current_computer_use_turn_id

    # The host API takes a value, not a token; clearing to None ends the turn
    # binding for this dispatch.
    with contextlib.suppress(Exception):
        set_current_computer_use_turn_id(None)
    del token


def _bounds_to_bbox(bounds: Any) -> BoundingBox | None:
    if not isinstance(bounds, dict):
        return None
    try:
        return BoundingBox(
            float(bounds.get("x", bounds.get("left", 0))),
            float(bounds.get("y", bounds.get("top", 0))),
            float(bounds["width"]),
            float(bounds["height"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


__all__ = [
    "DesktopController",
    "DesktopRecorderHandle",
    "computer_use_status",
    "run_computer_use_code",
]
