# -*- coding: utf-8 -*-
"""Execute agent-written browser code with recording available.

The tool surface mirrors the main repository's browser tool: the model writes
module-level async Python, ``Browser`` is already in scope, and it works in a
perceive → act → verify loop. Creator adds one name, ``recorder``, so the
model decides for itself when footage is worth keeping — and therefore also
decides when no footage is needed at all.

Nothing about the flow is prescribed here. This module runs what the model
wrote, records the facts of what happened, and hands back both.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import logging
import time
from pathlib import Path
from typing import Any

from .manifest import TakeManifest
from .recorder import RecordedTake, TakeRecorder
from .recording_link import RecordingControlLink
from .session import LiveBrowserSession, LiveSessionError, workspace_dir

logger = logging.getLogger(__name__)

_MAX_OUTPUT_CHARS = 12_000
_SOURCE_NAME = "browser_use_code"


class LiveOperationError(RuntimeError):
    """The submitted browser code could not be run."""


class _ActivePage:
    """The page recording defaults to: the one most recently opened.

    Tracking the real SDK page object here — rather than the last page id the
    control link happened to see — means ``recorder.start()`` works right
    after ``browser.open(...)``, before any other operation has touched the
    page. Reusing the link's memory made the first recording depend on an
    incidental perceive/act call in between.
    """

    def __init__(self) -> None:
        self.page: Any = None


class AgentRecorder:
    """The ``recorder`` name the model sees inside its own code.

    Start and stop are explicit on purpose: filming only what a step actually
    needs is what keeps takes free of dead footage, and keeps the model's
    later reasoning about that footage cheap.
    """

    def __init__(
        self,
        session: LiveBrowserSession,
        recorder: TakeRecorder,
        active_page: "_ActivePage",
    ) -> None:
        self._session = session
        self._recorder = recorder
        self._active_page = active_page

    async def start(self, page: Any = None, *, label: str = "") -> str:
        """Begin a take on ``page`` (default: the page just opened)."""
        target = page if page is not None else self._active_page.page
        if target is None:
            raise LiveSessionError(
                "no page has been opened yet; open a page first: "
                'page = await browser.open("https://example.com")',
            )
        cdp = await self._session.cdp_session_for(target)
        return await self._recorder.start(cdp, label=label)

    async def stop(self) -> dict[str, Any]:
        """End the take and report what was filmed."""
        take = await self._recorder.stop()
        return {
            "take_id": take.take_id,
            "label": take.label,
            "summary": take.summary,
        }

    def is_recording(self) -> bool:
        """Whether a take is currently being filmed."""
        return self._recorder.recording


class LiveOperationRun:
    """One tool invocation: its takes, screenshots and printed output."""

    def __init__(self) -> None:
        self.takes: list[RecordedTake] = []
        self.screenshots: list[str] = []
        self.output: str = ""
        self.result_repr: str = ""


async def run_browser_code(
    code: str,
    *,
    run_root: Path,
    run_id: str,
    identity: str = "guest",
    fps: int = 25,
    max_width: int = 1280,
    max_height: int = 720,
    max_take_seconds: float = 300.0,
    timeout_seconds: float = 600.0,
) -> LiveOperationRun:
    """Run the model's browser code, returning everything it produced."""
    source = code.strip()
    if not source:
        raise LiveOperationError("code is empty")
    compiled = _compile(source)
    workspace = workspace_dir(run_root, run_id)
    outcome = LiveOperationRun()
    recorder = TakeRecorder(
        workspace=workspace,
        fps=fps,
        max_width=max_width,
        max_height=max_height,
        max_duration_seconds=max_take_seconds,
    )
    # The wrapper must be in place before the session connects: an engine
    # binds its control link once, so a link registered later would never see
    # the operations it is supposed to record.
    link = _install_recording_link(recorder)
    try:
        session = await LiveBrowserSession.connect(identity=identity)
    except BaseException:
        _remove_recording_link(link)
        raise
    active_page = _ActivePage()
    stdout = io.StringIO()
    try:
        namespace: dict[str, Any] = {
            "__name__": "__browser_use__",
            "Browser": _BoundBrowser(session, active_page),
            "recorder": AgentRecorder(session, recorder, active_page),
        }
        with contextlib.redirect_stdout(stdout):
            value = await asyncio.wait_for(
                _execute(compiled, namespace),
                timeout=timeout_seconds,
            )
        if value is not None:
            outcome.result_repr = _clip(repr(value), 2_000)
    except TimeoutError as exc:
        raise LiveOperationError(
            f"browser code exceeded {timeout_seconds:g} seconds",
        ) from exc
    finally:
        # A take the model forgot to stop still becomes usable footage rather
        # than being discarded together with the frames already captured.
        with contextlib.suppress(Exception):
            await recorder.stop_if_recording()
        outcome.takes = recorder.takes
        outcome.screenshots = link.screenshots
        outcome.output = _clip(stdout.getvalue(), _MAX_OUTPUT_CHARS)
        _remove_recording_link(link)
        await session.close()
    return outcome


class _BoundBrowser:
    """Expose ``Browser.connect()`` while reusing this run's live session.

    Opening a page is intercepted so recording can default to it; everything
    else falls through to the real SDK facade untouched.
    """

    def __init__(
        self,
        session: LiveBrowserSession,
        active_page: _ActivePage,
    ) -> None:
        self._session = session
        self._active_page = active_page

    async def connect(self, *, identity: str = "guest") -> "_BoundBrowser":
        del identity  # the run's session already carries the resolved identity
        return self

    async def open(self, url: str | None = None) -> Any:
        page = await self._session.browser.open(url)
        self._active_page.page = page
        return page

    async def present(self, url: str | None = None) -> Any:
        page = await self._session.browser.present(url)
        self._active_page.page = page
        return page

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session.browser, name)


def _install_recording_link(recorder: TakeRecorder) -> RecordingControlLink:
    """Put a recording wrapper in front of the active browser control link."""
    from qwenpaw.browser.runtime.links import (
        link_for,
        register_local,
    )

    from .session import ensure_control_link

    ensure_control_link()
    inner = link_for("playwright")
    if inner is None:
        raise LiveOperationError("no browser control link is available")
    if isinstance(inner, RecordingControlLink):
        return inner
    wrapper = RecordingControlLink(
        inner,
        manifest_source=lambda: recorder.manifest,
        elapsed_ms=recorder.elapsed_ms,
    )
    register_local(wrapper, priority=True)
    return wrapper


def _remove_recording_link(link: RecordingControlLink) -> None:
    from qwenpaw.browser.runtime.links import unregister_local

    with contextlib.suppress(Exception):
        unregister_local(link)


def _compile(source: str) -> Any:
    """Compile module-level async code, mirroring the host browser tool."""
    try:
        return compile(
            source,
            _SOURCE_NAME,
            "exec",
            flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        )
    except SyntaxError as exc:
        raise LiveOperationError(f"code has a syntax error: {exc}") from exc


async def _execute(compiled: Any, namespace: dict[str, Any]) -> Any:
    started = time.monotonic()
    outcome = eval(compiled, namespace)  # noqa: S307 - the model's own code
    if asyncio.iscoroutine(outcome):
        outcome = await outcome
    logger.info(
        "live operation code finished in %.1fs",
        time.monotonic() - started,
    )
    return outcome


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n… truncated at {limit} characters"


def collect_manifests(takes: list[RecordedTake]) -> list[TakeManifest]:
    return [take.manifest for take in takes]


__all__ = [
    "AgentRecorder",
    "LiveOperationError",
    "LiveOperationRun",
    "collect_manifests",
    "run_browser_code",
]
