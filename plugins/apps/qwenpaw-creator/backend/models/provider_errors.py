# -*- coding: utf-8 -*-
"""Classify provider gateway errors by error code, not by wording.

The AgentScope model proxy (platform-pre) wraps upstream failures in its own
envelope and stamps ``retryable: true`` on errors that are demonstrably
deterministic. Measured samples (all of them returned in ~0.1s):

- missing required field in the body -> 502 ``ASP.UPSTREAM.ERROR``,
  ``retryable: true``, deterministic
- unresolvable reference-image host -> 502 ``ASP.UPSTREAM.ERROR``,
  ``retryable: true``, deterministic
- body over the gateway limit (8MB) -> 500 ``ASP.SYS.INTERNAL_ERROR``,
  ``retryable: true``, deterministic
- model not on the allowlist -> 400 ``ASP.BIZ.MODEL_NOT_ALLOWED``,
  ``retryable: false`` (correct)
- Credits exhausted -> 403 ``ASP.BIZ.CREDITS_INSUFFICIENT``,
  ``retryable: false`` (correct, but it reads like a permission failure)

Trusting the field means an unattended run re-sends requests that can never
succeed - each of which may still be billed upstream.  So classification here
keys on ``code``, and a code whose semantics are known (configuration, quota,
or the two measured-above wrappers) never defers to the status.

For a code that is not in any table the status code decides: the proxy now
passes the upstream status through, so 429/5xx is retryable. Inventing a new
name for a retryable condition used to wall the node - ``429
ASP.BIZ.TOO_MANY_CONCURRENT_REQUESTS`` arrived with ``retryable: false`` and
the wording "please retry later", and no table row can be expected for a code
that was not yet issued.
"""

from __future__ import annotations

import re

# The envelope may be embedded in a larger message (callers append their own
# context and the raw provider body), so the code is extracted by pattern
# rather than by parsing the whole string as JSON. Both quote styles are
# accepted because an OpenAI-compatible client re-serialises the body as a
# Python repr when it raises: ``Error code: 403 - {'error': {'code': ...}}``.
# Matching only JSON quotes silently classified every main-loop model failure
# as "no envelope", which threw away the provider's own ``retryable`` answer.
_CODE_RE = re.compile(r"""['"]code['"]\s*:\s*['"](ASP\.[A-Z0-9_.]+)['"]""")
_REQUEST_ID_RE = re.compile(
    r"""['"]request_id['"]\s*:\s*['"]([0-9a-fA-F-]{8,})['"]""",
)
# The status the caller appended (``HTTP 429: …``) or the provider's own
# wording (``… failed with status 502 …``). Message-only callers have no other
# way back to the status, because a persisted task error keeps just text.
_STATUS_RE = re.compile(r"\b(?:http|status)\s+(\d{3})\b", re.IGNORECASE)

_RATE_LIMIT_PHRASE_RE = re.compile(
    r"\brate[- ]limit(?:ed|ing|s)?\b",
    re.IGNORECASE,
)

# Wrappers that were measured stamping a 5xx on a client-side mistake, so they
# stay non-retryable even though their status would say otherwise.
GATEWAY_DETERMINISTIC_CODES = frozenset(
    {
        "ASP.UPSTREAM.ERROR",
        "ASP.SYS.INTERNAL_ERROR",
    },
)

# Only errors that are transient *because of the code itself* are retried.
GATEWAY_TRANSIENT_CODES = frozenset(
    {
        "ASP.UPSTREAM.UNAVAILABLE",
        "ASP.UPSTREAM.TIMEOUT",
        "ASP.UPSTREAM.CONNECTION_RESET",
        "ASP.COMM.RATE_LIMITED",
    },
)

# Configuration mistakes: retrying cannot fix them and the user must act.
GATEWAY_PERMANENT_CODES = frozenset(
    {
        "ASP.AUTH.UNAUTHORIZED",
        "ASP.PROXY.API_KEY_MISSING",
        "ASP.PROXY.API_KEY_INVALID",
        "ASP.BIZ.MODEL_NOT_ALLOWED",
        "ASP.COMM.NOT_FOUND",
    },
)

# Spending is stopped for the whole account, not for this request.
GATEWAY_QUOTA_CODES = frozenset({"ASP.BIZ.CREDITS_INSUFFICIENT"})

CLASS_TRANSIENT = "transient"
CLASS_PERMANENT = "permanent"
CLASS_QUOTA = "quota"
CLASS_UNKNOWN = "unknown"


def gateway_error_code(text: str) -> str:
    """The gateway error code embedded in *text*, or "" when there is none."""
    match = _CODE_RE.search(text or "")
    return match.group(1) if match else ""


def gateway_request_id(text: str) -> str:
    """The provider's request id, so a failure can be handed over."""
    match = _REQUEST_ID_RE.search(text or "")
    return match.group(1) if match else ""


def status_code_in_text(text: str) -> int:
    """The HTTP status a message carries, or 0 when it carries none."""
    match = _STATUS_RE.search(text or "")
    return int(match.group(1)) if match else 0


def is_retryable_status(status_code: int) -> bool:
    """Whether a passed-through status describes a temporary condition."""
    return status_code == 429 or status_code >= 500


def is_rate_limit_text(text: str) -> bool:
    """Whether a failure reports throttling rather than a fault.

    Narrower than :func:`is_retryable_status` on purpose: a throttle is
    account-wide and expected to clear, while a 5xx says nothing about the
    other nodes in the same fan-out. Providers phrase it both ways - a bare
    status, or a lane-specific "rate limited" summary after their own retries -
    so both forms are recognised here.
    """

    if status_code_in_text(text) == 429:
        return True
    return bool(_RATE_LIMIT_PHRASE_RE.search(text or ""))


def classify_gateway_error(text: str) -> str:
    """Classify by code: transient / permanent / quota / unknown / "".

    "" means the text carries no gateway envelope at all, so the caller keeps
    whatever judgement it applies to its own provider (DashScope, Ark, …).
    """
    code = gateway_error_code(text)
    if not code:
        return ""
    if code in GATEWAY_QUOTA_CODES:
        return CLASS_QUOTA
    if code in GATEWAY_PERMANENT_CODES:
        return CLASS_PERMANENT
    if code in GATEWAY_DETERMINISTIC_CODES:
        return CLASS_UNKNOWN
    if code in GATEWAY_TRANSIENT_CODES:
        return CLASS_TRANSIENT
    # Anything else: the code is a name this build has not seen. Defer to the
    # status the proxy passes through instead of defaulting to a wall.
    return (
        CLASS_TRANSIENT
        if is_retryable_status(status_code_in_text(text))
        else CLASS_UNKNOWN
    )


def is_gateway_quota_error(text: str) -> bool:
    """True when the provider refuses to spend more Credits."""
    return classify_gateway_error(text) == CLASS_QUOTA


def is_gateway_transient(text: str) -> bool:
    """True only for codes that are transient by their own nature.

    Diagnostic use only - no retry path reads this any more. The gateway used
    to override the HTTP status with its ``code`` so a deterministic client
    fault dressed as a 5xx would not burn paid retries; the proxy is fixing
    that stamping on its side, so retry decisions now key on the status alone
    and this helper survives only for callers that want the classification.
    """
    return classify_gateway_error(text) == CLASS_TRANSIENT


def retryable_for_status(status_code: int, body: str = "") -> bool:
    """Retry decision for one HTTP failure.

    The status alone decides. An earlier revision let a gateway ``code``
    override the status - it stamped deterministic client faults as a 5xx to
    avoid paying for their retries - but the proxy is fixing that on its side,
    so we no longer second-guess the status here. ``body`` stays in the
    signature because callers still pass the raw response and the quota path
    (``is_gateway_quota_error``) reads codes from it separately.
    """
    _ = body
    return is_retryable_status(status_code)


__all__ = [
    "CLASS_PERMANENT",
    "CLASS_QUOTA",
    "CLASS_TRANSIENT",
    "CLASS_UNKNOWN",
    "GATEWAY_DETERMINISTIC_CODES",
    "GATEWAY_PERMANENT_CODES",
    "GATEWAY_QUOTA_CODES",
    "GATEWAY_TRANSIENT_CODES",
    "classify_gateway_error",
    "gateway_error_code",
    "gateway_request_id",
    "is_gateway_quota_error",
    "is_gateway_transient",
    "is_rate_limit_text",
    "is_retryable_status",
    "retryable_for_status",
    "status_code_in_text",
]
