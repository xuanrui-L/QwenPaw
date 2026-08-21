# -*- coding: utf-8 -*-
"""Creator-side Computer Use approval that stays in step with the host.

The bundled computer-use tool answers native approval requests only inside a
main-repo chat session, so Creator — whose agent runs outside that session —
cannot simply reuse it. Creator therefore asks the user itself, but the fact
of what is allowed lives in one place: the host's own ComputerUseAccessStore.
An app the user already granted persistently there is honored here without a
second prompt, and a decision made here is written back to the same store, so
both entry points show one consistent grant state.

The store lives in the computer-use bundle, which is only importable once the
host has loaded it, so it is bound lazily. Everything else is injectable and
unit-tested against a fake store.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


class AccessStore(Protocol):
    """The slice of the host access store this coordinator relies on."""

    def resolve(self, request: Any) -> Any:
        ...

    def record_session(self, request: Any, *, allowed: bool) -> None:
        ...

    def record_persistent(self, request: Any) -> None:
        ...


@dataclass(frozen=True, slots=True)
class DesktopApprovalRequest:
    """One request to operate a named desktop application."""

    session_id: str
    canonical_app_id: str
    display_name: str
    risk: str = "unknown"
    warning: str = ""


class ApprovalOutcome:
    """The decision plus where it came from, for honest reporting."""

    def __init__(self, allowed: bool, source: str) -> None:
        self.allowed = allowed
        self.source = source


# A prompt callback returns (allowed, remember_persistently). Creator supplies
# one backed by its runtime authorization subsystem; tests supply a stub.
AskUser = Callable[[DesktopApprovalRequest], "tuple[bool, bool]"]


def load_host_access_store() -> AccessStore | None:
    """Bind the host's Computer Use access store if the bundle is loaded."""
    try:
        from computer_use.access import (  # type: ignore[import-not-found]
            get_computer_use_access_store,
        )
    except Exception:  # noqa: BLE001 - bundle absent outside a loaded host
        return None
    try:
        return get_computer_use_access_store()
    except Exception:  # noqa: BLE001 - never let approval wiring crash a run
        logger.debug("host access store unavailable", exc_info=True)
        return None


class DesktopApprovalCoordinator:
    """Decide desktop-action approvals, single-sourced to the host store."""

    def __init__(
        self,
        ask_user: AskUser,
        *,
        store: AccessStore | None = None,
    ) -> None:
        self._ask_user = ask_user
        # Explicit store for tests; otherwise the host store, bound lazily so a
        # deployment without the bundle simply prompts every time.
        self._store = store if store is not None else load_host_access_store()

    def decide(self, request: DesktopApprovalRequest) -> ApprovalOutcome:
        """Return an approval, honoring an existing host grant first."""
        existing = self._resolve_existing(request)
        if existing is not None:
            return existing
        try:
            allowed, remember = self._ask_user(request)
        except (
            Exception
        ):  # noqa: BLE001 - a failed prompt denies, never crashes
            logger.debug("desktop approval prompt failed", exc_info=True)
            return ApprovalOutcome(False, "prompt_error")
        self._record(request, allowed=allowed, remember=remember)
        return ApprovalOutcome(allowed, "creator")

    def _resolve_existing(
        self,
        request: DesktopApprovalRequest,
    ) -> ApprovalOutcome | None:
        if self._store is None:
            return None
        try:
            decision = self._store.resolve(request)
        except Exception:  # noqa: BLE001 - a broken lookup just re-prompts
            logger.debug("access store resolve failed", exc_info=True)
            return None
        if decision is None:
            return None
        allowed = bool(getattr(decision, "allowed", False))
        source = str(getattr(decision, "source", "host"))
        return ApprovalOutcome(allowed, source)

    def _record(
        self,
        request: DesktopApprovalRequest,
        *,
        allowed: bool,
        remember: bool,
    ) -> None:
        if self._store is None:
            return
        try:
            self._store.record_session(request, allowed=allowed)
            # "Always allow this app" is the only persistent grant, and only a
            # positive one — a refusal must never become a standing block
            # that silently denies the user later.
            if allowed and remember:
                self._store.record_persistent(request)
        except Exception:  # noqa: BLE001 - recording is best-effort
            logger.debug("access store record failed", exc_info=True)


__all__ = [
    "AccessStore",
    "ApprovalOutcome",
    "AskUser",
    "DesktopApprovalCoordinator",
    "DesktopApprovalRequest",
    "load_host_access_store",
]
