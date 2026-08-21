# -*- coding: utf-8 -*-
"""One live browser session: the main-repo SDK plus a recording channel.

The agent drives the page through QwenPaw's own Browser SDK, unchanged — its
perception projection, semantic locators, identity adjudication and error
teaching all come from the main repository. Creator adds exactly two things
around it: a control link registered in-process so the SDK works inside the
Creator runtime, and a CDP channel used only to film the page.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PLAYWRIGHT_VARIANT = "playwright"


class LiveSessionError(RuntimeError):
    """A live browser session could not be established or used."""


def ensure_control_link() -> None:
    """Make the SDK usable in this process without touching the main repo.

    In the host the browser normally runs behind a worker plane that registers
    its own links. Creator drives the SDK directly, so the same first-party
    Playwright link is registered here through the main repo's public registry
    when nothing has claimed the variant yet.
    """
    from qwenpaw.browser.runtime.links import link_for, register_local

    if link_for(_PLAYWRIGHT_VARIANT) is not None:
        return
    from qwenpaw.browser.control_link.playwright.adapter import (
        PlaywrightControlLink,
    )

    register_local(PlaywrightControlLink())


class LiveBrowserSession:
    """A connected browser plus the CDP attachment used for recording."""

    def __init__(self, browser: Any) -> None:
        self._browser = browser
        self._cdp_sessions: dict[str, Any] = {}

    @classmethod
    async def connect(cls, *, identity: str = "guest") -> "LiveBrowserSession":
        """Connect through the main-repo SDK with recording attached."""
        ensure_control_link()
        from qwenpaw.browser import Browser

        return cls(await Browser.connect(identity=identity))

    @property
    def browser(self) -> Any:
        """The SDK facade handed to agent code verbatim."""
        return self._browser

    async def close(self) -> None:
        for session in self._cdp_sessions.values():
            try:
                await session.detach()
            except Exception:  # noqa: BLE001 - teardown must not mask results
                logger.debug("cdp detach failed", exc_info=True)
        self._cdp_sessions.clear()
        try:
            await self._browser.close()
        except Exception:  # noqa: BLE001 - the run's result already stands
            logger.debug("browser close failed", exc_info=True)

    async def cdp_session_for(self, page: Any) -> Any:
        """Return a filming channel for ``page``, creating it on first use.

        The channel is deliberately one-way: recording subscribes to frames
        and never issues an operation command, so what gets filmed is exactly
        what the agent did through the SDK.
        """
        page_id = str(getattr(page, "id", "") or "")
        if not page_id:
            raise LiveSessionError("cannot record a page without an id")
        existing = self._cdp_sessions.get(page_id)
        if existing is not None:
            return existing
        native = self._native_page(page_id)
        try:
            session = await native.context.new_cdp_session(native)
        except Exception as exc:  # noqa: BLE001 - surface an actionable cause
            raise LiveSessionError(
                "this browser backend cannot be filmed: "
                f"{type(exc).__name__}: {exc}",
            ) from exc
        self._cdp_sessions[page_id] = session
        return session

    def _native_page(self, page_id: str) -> Any:
        """Resolve the driver page behind one SDK page id.

        Filming needs the same page object the SDK is operating, which the
        control link owns. Reaching it here keeps the recording channel bound
        to the real session instead of opening a second browser that would
        show a different screen than the one being driven.
        """
        from qwenpaw.browser.runtime.links import link_for

        link = link_for(_PLAYWRIGHT_VARIANT)
        if link is None:
            raise LiveSessionError("no browser control link is registered")
        # pylint: disable-next=protected-access
        owner = self._browser._engine.session.owner  # noqa: SLF001
        resolver = getattr(link, "_page", None)
        if resolver is None:
            raise LiveSessionError(
                "the active browser backend exposes no page for recording",
            )
        try:
            return resolver((owner.workspace_id, owner.session_id), page_id)
        except Exception as exc:  # noqa: BLE001 - stale page or closed session
            raise LiveSessionError(
                f"the page to record is unavailable: {exc}",
            ) from exc


def workspace_dir(root: Path, run_id: str) -> Path:
    """Return the scratch directory takes are assembled in for one run."""
    target = root / "live_operation" / run_id
    target.mkdir(parents=True, exist_ok=True)
    return target


__all__ = [
    "LiveBrowserSession",
    "LiveSessionError",
    "ensure_control_link",
    "workspace_dir",
]
