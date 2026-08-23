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
import builtins
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
_MAX_RANGE_ITEMS = 1_000
_SOURCE_NAME = "browser_use_code"


def _bounded_range(*args: int) -> range:
    """Return a small range suitable for one browser-operation program.

    The bridge runs beside the Creator API server rather than in the host
    Browser tool's disposable worker process.  Bounding a model-authored loop
    keeps accidental or prompt-injected CPU work from monopolising the event
    loop, where ``asyncio.wait_for`` cannot pre-empt synchronous Python.
    """
    value = range(*args)
    if len(value) > _MAX_RANGE_ITEMS:
        raise LiveOperationError(
            "range is limited to "
            f"{_MAX_RANGE_ITEMS} items in live-operation code",
        )
    return value


_SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "Exception",
        "RuntimeError",
        "TypeError",
        "ValueError",
        "TimeoutError",
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "filter",
        "float",
        "int",
        "isinstance",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "print",
        "repr",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    )
}
_SAFE_BUILTINS["range"] = _bounded_range


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
    return await _run_browser_code_isolated(
        compiled,
        run_root=run_root,
        run_id=run_id,
        identity=identity,
        fps=fps,
        max_width=max_width,
        max_height=max_height,
        max_take_seconds=max_take_seconds,
        timeout_seconds=timeout_seconds,
    )


async def _run_browser_code_isolated(
    compiled: Any,
    *,
    run_root: Path,
    run_id: str,
    identity: str,
    fps: int,
    max_width: int,
    max_height: int,
    max_take_seconds: float,
    timeout_seconds: float,
) -> LiveOperationRun:
    """Decorate only this task's browser links for one complete invocation."""
    workspace = workspace_dir(run_root, run_id)
    outcome = LiveOperationRun()
    recorder = TakeRecorder(
        workspace=workspace,
        fps=fps,
        max_width=max_width,
        max_height=max_height,
        max_duration_seconds=max_take_seconds,
    )
    links, owned_playwright = _recording_links(recorder)
    from qwenpaw.browser.runtime.links import scoped_links

    # Engines bind a link at connect time. A ContextVar overlay makes that
    # decoration visible only to this asyncio task, so sibling workspaces can
    # operate the same provider concurrently without their facts crossing.
    try:
        with scoped_links(links):
            session = await LiveBrowserSession.connect(identity=identity)
            selected = session.control_link
            if not isinstance(selected, RecordingControlLink):
                await session.close()
                raise LiveOperationError(
                    "the selected browser backend could not be isolated for "
                    "recording",
                )
            active_page = _ActivePage()
            stdout = io.StringIO()
            try:
                namespace: dict[str, Any] = {
                    "__name__": "__browser_use__",
                    "Browser": _BoundBrowser(session, active_page),
                    "recorder": AgentRecorder(session, recorder, active_page),
                }
                value = await asyncio.wait_for(
                    _execute(compiled, namespace, output=stdout),
                    timeout=timeout_seconds,
                )
                if value is not None:
                    outcome.result_repr = _clip(repr(value), 2_000)
            except TimeoutError as exc:
                raise LiveOperationError(
                    f"browser code exceeded {timeout_seconds:g} seconds",
                ) from exc
            except Exception as exc:  # noqa: BLE001 - model-code boundary
                raise LiveOperationError(
                    "browser code failed: " f"{type(exc).__name__}: {exc}",
                ) from exc
            finally:
                # A take the model forgot to stop still becomes usable footage
                # rather than being discarded with the captured frames.
                with contextlib.suppress(Exception):
                    await recorder.stop_if_recording()
                outcome.takes = recorder.takes
                outcome.screenshots = selected.screenshots
                outcome.output = _clip(stdout.getvalue(), _MAX_OUTPUT_CHARS)
                await session.close()
    finally:
        # This in-process filming provider belongs to the current event loop.
        # Closing it here prevents Playwright state from leaking into another
        # Creator workspace's loop, while the ordinary Browser runtime keeps
        # its own long-lived worker plane unchanged.
        with contextlib.suppress(Exception):
            await owned_playwright.close_all()
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
        if name.startswith("_"):
            raise LiveOperationError(
                f"private browser attribute {name!r} is unavailable in "
                "live-operation code",
            )
        return getattr(self._session.browser, name)


def _recording_links(
    recorder: TakeRecorder,
) -> tuple[tuple[RecordingControlLink, ...], Any]:
    """Wrap each available variant without changing the global registry."""
    from qwenpaw.browser.control_link.playwright.adapter import (
        PlaywrightControlLink,
    )
    from qwenpaw.browser.runtime.links import registered_links

    owned_playwright = PlaywrightControlLink()
    wrapped: list[RecordingControlLink] = []
    seen: set[str] = set()
    # Prefer a provider created on this event loop. Other variants remain
    # available for explicitly selected Chrome/CDP identities, but the global
    # Playwright singleton is skipped once this variant has been wrapped.
    for inner in (owned_playwright, *registered_links()):
        variant = str(getattr(inner, "variant", ""))
        if not variant or variant in seen:
            continue
        seen.add(variant)
        wrapped.append(
            RecordingControlLink(
                inner,
                manifest_source=lambda: recorder.manifest,
                elapsed_ms=recorder.elapsed_ms,
            ),
        )
    if not wrapped:
        raise LiveOperationError("no browser control link is available")
    return tuple(wrapped), owned_playwright


def _compile(source: str) -> Any:
    """Compile module-level async code, mirroring the host browser tool."""
    try:
        tree = ast.parse(source, filename=_SOURCE_NAME, mode="exec")
        _ModelCodeValidator().visit(tree)
        return compile(
            tree,
            _SOURCE_NAME,
            "exec",
            flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        )
    except SyntaxError as exc:
        raise LiveOperationError(f"code has a syntax error: {exc}") from exc


class _ModelCodeValidator(ast.NodeVisitor):
    """Keep agent code on the declared operation objects.

    Unlike the host Browser tool's disposable worker process, Creator runs
    beside API keys and Project state. Imports and private-object traversal
    would turn a website prompt injection into backend code execution, so this
    bridge exposes ordinary Python control flow but no process escape surface.
    """

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        del node
        raise LiveOperationError(
            "imports are unavailable in live-operation code; Browser, "
            "desktop, and recorder are already in scope",
        )

    visit_ImportFrom = visit_Import

    def _reject_definition(self, kind: str) -> None:
        raise LiveOperationError(
            f"{kind} definitions are unavailable in live-operation code; "
            "use top-level await and bounded control flow with the provided "
            "Browser, desktop, and recorder objects",
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        del node
        self._reject_definition("function")

    def visit_AsyncFunctionDef(  # noqa: N802
        self,
        node: ast.AsyncFunctionDef,
    ) -> None:
        del node
        self._reject_definition("async function")

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        del node
        self._reject_definition("lambda")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        del node
        self._reject_definition("class")

    def visit_While(self, node: ast.While) -> None:  # noqa: N802
        del node
        raise LiveOperationError(
            "while loops are unavailable in live-operation code; use a "
            "bounded for loop or the Browser SDK's wait methods",
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if node.attr.startswith("_"):
            raise LiveOperationError(
                "private attributes are unavailable in live-operation code",
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if node.id.startswith("__"):
            raise LiveOperationError(
                "dunder names are unavailable in live-operation code",
            )


async def _execute(
    compiled: Any,
    namespace: dict[str, Any],
    *,
    output: io.StringIO | None = None,
) -> Any:
    started = time.monotonic()
    safe_builtins = dict(_SAFE_BUILTINS)
    if output is not None:

        def captured_print(
            *values: Any,
            sep: str = " ",
            end: str = "\n",
            flush: bool = False,
        ) -> None:
            builtins.print(*values, sep=sep, end=end, file=output)
            if flush:
                output.flush()

        safe_builtins["print"] = captured_print
    namespace.setdefault("__builtins__", safe_builtins)
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
