# -*- coding: utf-8 -*-
"""Shared transient-failure classification for media execution services.

Transient provider failures may be retried on a derived durable slot;
deterministic rejections (safety refusals, validation errors) never are.
Markers are matched case-insensitively against the persisted task error.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from models.provider_errors import (
    is_retryable_status,
    status_code_in_text,
)

TRANSIENT_ERROR_MARKERS = (
    "connection",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "too many requests",
    # EBADF from a torn-down socket during download: the transport failed,
    # not the request — field runs showed httpx surfacing it mid-transfer.
    "bad file descriptor",
    # DNS resolution failures happen before any billable request leaves
    # the machine, so a bounded retry is free. A permanently wrong
    # base_url still surfaces: retry slots exhaust and the terminal
    # message tells the user to check the configuration. Field runs
    # (2026-08-07) showed one [Errno 8] blip locking three nodes.
    "nodename nor servname",  # macOS getaddrinfo EAI_NONAME
    "name or service not known",  # glibc getaddrinfo EAI_NONAME
    "temporary failure in name resolution",  # glibc EAI_AGAIN
    "getaddrinfo",
    "status 429",
    "status 502",
    "status 503",
    "status 504",
    # The gateway severing a response mid-flight leaves an httpx transport
    # error with no body, so there is no envelope and no status to read: the
    # wording is the only signal. Nothing was billed - the request never
    # completed - which is what makes a bounded retry safe here.
    "peer closed connection",
    "incomplete chunked read",
    # Legacy empty-detail records: before the provider labelled
    # httpx transport errors, WriteError/ReadError/ConnectError
    # stringified to nothing and persisted this exact degenerate
    # message. Only an empty ``str(exc)`` can produce it, so matching
    # it retroactively reopens nodes walled by a plain network blip
    # (field run 2026-08-10). Real config errors carry a detail and
    # never match.
    "image generation failed: . check",
)

# Causes a retry cannot fix, for the failures that carry neither a gateway
# envelope nor an HTTP status - configuration and capability problems, which
# have no status to read precisely because no request ever left the machine.
# :func:`is_unclassified_failure` treats them as settled, so the scheduler's
# retry-by-default floor never re-opens a misconfigured node.
PERMANENT_ERROR_MARKERS = (
    "api key",
    "configuration",
    "未配置",
    "does not support",
    "refusing resubmission",
    "never resubmit",
)

MAX_TRANSIENT_RETRY_SLOTS = 3


def is_transient_error_message(message: str) -> bool:
    """Whether a failure is worth another attempt.

    Two signals, most trusted first: an HTTP status the message names, then a
    short list of causes no retry can fix. Anything left over counts as
    transient.
    """

    # A gateway error code used to outrank everything below - the AgentScope
    # proxy stamped ``retryable: true`` on deterministic failures, so trusting
    # the status alone would burn paid retries on a request that could never
    # succeed. The proxy is fixing that stamping on its side, so the status is
    # now the authority and the envelope no longer vetoes a retry here.
    status = status_code_in_text(message)
    if status:
        # A message that names its own status needs no substring guess. The
        # old allowlist missed nginx's hyphenated "504 Gateway Time-out" - its
        # entries read "gateway timeout" and "status 504", and the lane writes
        # "HTTP 504" - so a gateway that merely took too long walled every
        # presentation node as deterministic.
        return is_retryable_status(status)
    folded = message.casefold()
    if any(marker in folded for marker in PERMANENT_ERROR_MARKERS):
        return False
    return any(marker in folded for marker in TRANSIENT_ERROR_MARKERS)


def is_unclassified_failure(message: str) -> bool:
    """Whether a failure carries no signal at all about its cause.

    True only when there is no HTTP status in the wording and
    no match in either marker table - which means the caller has learned
    nothing, as opposed to having learned that the fault is permanent. The
    scheduler retries exactly these, so that an unrecognised wording costs a
    bounded budget instead of walling a node forever: nginx writes "504 Gateway
    Time-out" with a hyphen, and that one character stalled a whole project.
    """

    text = str(message or "")
    if status_code_in_text(text):
        return False
    folded = text.casefold()
    return not any(
        marker in folded
        for marker in (*TRANSIENT_ERROR_MARKERS, *PERMANENT_ERROR_MARKERS)
    )


def is_transient_task_error(error: Mapping[str, Any] | None) -> bool:
    if not isinstance(error, Mapping):
        return False
    message = str(error.get("message") or "")
    # The persisted flag is whatever the raising layer believed. A gateway
    # envelope used to outrank it - the proxy stamped ``retryable: true`` on
    # deterministic failures, so honouring the flag first would re-open a retry
    # slot for a request that can never succeed - but the proxy is fixing that
    # stamping on its side, so the flag is taken at face value again.
    if error.get("retryable") is True:
        return True
    return is_transient_error_message(message)


def transient_retry_slot_key(idempotency_key: str, attempt: int) -> str:
    if attempt == 0:
        return idempotency_key
    # ":" keeps the derived key a safe runtime path segment, because r2v
    # persists the slot key as Task idempotency_key / caused_by_request_id.
    return f"{idempotency_key}:transient-retry-{attempt}"


__all__ = [
    "MAX_TRANSIENT_RETRY_SLOTS",
    "PERMANENT_ERROR_MARKERS",
    "TRANSIENT_ERROR_MARKERS",
    "is_transient_error_message",
    "is_transient_task_error",
    "is_unclassified_failure",
    "transient_retry_slot_key",
]
