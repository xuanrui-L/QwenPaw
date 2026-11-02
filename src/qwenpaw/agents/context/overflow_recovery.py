# -*- coding: utf-8 -*-
"""One-shot overflow recovery across model calls and stream consumption.

The agent supplies recovery that classifies the error, compacts context,
rebuilds input, and calls the model again. This module owns the retry boundary
and stream lifecycle; the recovery response is never wrapped for retry again.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from contextvars import ContextVar
from typing import Any

from ...providers.error_utils import extract_status_code
from ...providers.stream_progress import has_meaningful_stream_content

# Optional request-local custody. The observer retains streams without
# replacing them.
_stream_observer: ContextVar[
    Callable[[AsyncGenerator[Any, None]], None] | None
] = ContextVar("model_recovery_stream_observer", default=None)

# One classification for "the provider said this input was too long", shared
# by the agent loop and by any runtime that recovers from the same refusal.
# A bare 400 is deliberately insufficient: malformed tool schemas, unsupported
# parameters, and media errors must keep their existing handling.  Prefer the
# structured status code when the SDK exposes it, with the rendered exception
# as a compatibility fallback for gateways that wrap the original response.
CONTEXT_OVERFLOW_MARKERS: tuple[str, ...] = (
    "range of input length",
    "context length exceeded",
    "context_length_exceeded",
    "maximum context length",
    "maximum context window",
    "context window exceeded",
    "max input length",
    "input length should be",
    "input is too long",
    "prompt is too long",
    "prompt too long",
    "too many input tokens",
)
_CONTEXT_OVERFLOW_MARKER_GROUPS: tuple[tuple[str, ...], ...] = (
    ("tokens", "exceeds the available context size"),
    (
        "input token count",
        "exceeds the maximum number of tokens allowed",
    ),
    (
        "input token count",
        "model only supports up to",
    ),
)


def is_context_overflow_error(exc: Exception) -> bool:
    """Return whether *exc* is a provider refusal for an oversized input."""
    status = extract_status_code(exc)
    error_str = str(exc).lower()
    if status != 400 and "error code: 400" not in error_str:
        return False
    if any(marker in error_str for marker in CONTEXT_OVERFLOW_MARKERS):
        return True
    return any(
        all(marker in error_str for marker in marker_group)
        for marker_group in _CONTEXT_OVERFLOW_MARKER_GROUPS
    )


async def call_with_overflow_recovery(
    call_model: Callable[..., Awaitable[Any]],
    recover: Callable[[Exception], Awaitable[Any]],
    **kwargs: Any,
) -> Any:
    """Recover errors from invocation or pre-output stream consumption."""
    try:
        response = await call_model(**kwargs)
    except Exception as exc:
        response = await recover(exc)
    else:
        if inspect.isasyncgen(response):
            response = _stream_with_overflow_recovery(response, recover)
    if inspect.isasyncgen(response):
        if observer := _stream_observer.get():
            observer(response)
    return response


async def _stream_with_overflow_recovery(
    stream: AsyncGenerator[Any, None],
    recover: Callable[[Exception], Awaitable[Any]],
) -> AsyncGenerator[Any, None]:
    """Close consumed streams and never replay meaningful model output."""
    emitted = False
    try:
        async with aclosing(stream):
            async for chunk in stream:
                emitted = emitted or has_meaningful_stream_content(
                    chunk.content,
                )
                yield chunk
        return
    except Exception as exc:
        if emitted:
            raise
        response = await recover(exc)
    # Consume the retry directly so both failure phases share one attempt.
    if inspect.isasyncgen(response):
        async with aclosing(response):
            async for chunk in response:
                yield chunk
    else:
        yield response
