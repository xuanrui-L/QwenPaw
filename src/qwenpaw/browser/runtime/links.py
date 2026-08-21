# -*- coding: utf-8 -*-
"""Variant-to-control-link registry."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from .ports import ControlLink


_local: list["ControlLink"] = []
_external: Callable[[], Iterable["ControlLink"]] = lambda: ()
_scoped: ContextVar[tuple["ControlLink", ...]] = ContextVar(
    "browser_scoped_control_links",
    default=(),
)


def register_local(link: "ControlLink", *, priority: bool = False) -> None:
    """Register a first-party control link, optionally ahead of defaults."""
    if priority:
        _local.insert(0, link)
    else:
        _local.append(link)


def unregister_local(link: "ControlLink") -> None:
    """Remove a temporary first-party control link from the registry."""
    try:
        _local.remove(link)
    except ValueError:
        pass


@contextmanager
def scoped_links(links: Iterable["ControlLink"]) -> Iterator[None]:
    """Temporarily prefer links only inside the current async context.

    A feature that decorates a link for one operation must not mutate the
    process-wide registry: the host can run several workspaces concurrently,
    and a global priority link would intercept unrelated browser sessions.
    Context variables follow the current asyncio task while remaining isolated
    from sibling tasks and threads.
    """
    preferred = tuple(links)
    token = _scoped.set((*preferred, *_scoped.get()))
    try:
        yield
    finally:
        _scoped.reset(token)


def bind_external(
    source: Callable[[], Iterable["ControlLink"]],
) -> None:
    """Bind a live source of externally contributed control links."""
    global _external
    _external = source


def registered_links() -> tuple["ControlLink", ...]:
    """Snapshot every currently registered control link."""
    return (*_scoped.get(), *_local, *_external())


def link_for(variant: str) -> "ControlLink | None":
    """Return the current control link for a browser variant, if any."""
    for link in registered_links():
        if link is not None and getattr(link, "variant", None) == variant:
            return link
    return None


async def availability() -> dict[str, bool]:
    """Return the current provider facts for identity adjudication."""
    for link in registered_links():
        probe = getattr(link, "probe_availability", None)
        if callable(probe):
            return dict(await probe())
    return {
        variant: bool(
            link_for(variant) is not None and link_for(variant).is_available(),
        )
        for variant in ("chrome", "cdp", "playwright")
    }
