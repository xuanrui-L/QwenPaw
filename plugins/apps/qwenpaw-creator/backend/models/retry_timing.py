# -*- coding: utf-8 -*-
"""Backoff timing shared by the paid media lanes.

Image and video each computed their own linear wait from a fixed base, so a
fan-out of identical requests retried in the same second and stayed inside the
provider's throttle window for its whole budget: the observed 429 storm burned
three attempts in 31 seconds while the window was measured in minutes.

Two rules fix that. ``Retry-After`` wins when the provider states a wait, and
everything else grows exponentially with jitter so concurrent callers spread
apart instead of re-colliding.
"""

from __future__ import annotations

import random
from typing import Mapping

#: Ceiling for a provider-supplied wait. A larger number parks a paid task
#: longer than simply dispatching it again would cost, so it is not honoured.
MAX_RETRY_AFTER_SECONDS = 300.0

#: Ceiling for our own exponential growth.
DEFAULT_BACKOFF_CAP_SECONDS = 300.0


def retry_after_seconds(headers: Mapping[str, str] | None) -> float:
    """The wait the provider asked for, or 0 when it said nothing.

    Only the delta-seconds form is read; no provider in use sends the
    HTTP-date variant. A missing or unparsable value is treated as silence
    rather than as an instruction, so a malformed header cannot stall a task.
    """

    if not headers:
        return 0.0
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return 0.0
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0
    if seconds <= 0:
        return 0.0
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def backoff_seconds(
    attempt: int,
    *,
    base: float,
    cap: float = DEFAULT_BACKOFF_CAP_SECONDS,
    jitter_ratio: float = 0.25,
    headers: Mapping[str, str] | None = None,
) -> float:
    """How long to wait before retrying ``attempt`` (0-based).

    The provider's stated wait wins: it knows the window, we do not.
    Otherwise the delay doubles from ``base`` and carries jitter, because a
    fan-out that sleeps in lockstep keeps hitting the same window and spends
    its whole budget without ever waiting it out.
    """

    requested = retry_after_seconds(headers)
    if requested > 0:
        return requested
    delay = min(base * (2 ** max(attempt, 0)), cap)
    if jitter_ratio > 0:
        delay += delay * jitter_ratio * random.random()
    return round(delay, 1)


__all__ = [
    "DEFAULT_BACKOFF_CAP_SECONDS",
    "MAX_RETRY_AFTER_SECONDS",
    "backoff_seconds",
    "retry_after_seconds",
]
