# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Retry timing for the paid media lanes, and what counts as worth retrying.

The measured failure this replaces: sixteen storyboard image nodes each hit
``429`` on the same second, retried on an identical linear schedule
(10s, 20s), and spent their whole three-attempt budget inside 31 seconds while
the provider's throttle window was measured in minutes. Five other nodes came
back ``503`` from nginx and were abandoned on their first attempt, because the
lane only recognised ``429``.
"""

import json

import pytest

from models.image.base import _is_retryable_response
from models.provider_errors import is_rate_limit_text
from models.retry_timing import (
    MAX_RETRY_AFTER_SECONDS,
    backoff_seconds,
    retry_after_seconds,
)

NGINX_503 = (
    "<html>\r\n<head><title>503 Service Temporarily Unavailable</title>"
    '</head>\r\n<body bgcolor="white">\r\n<center><h1>503 Service '
    "Temporarily Unavailable</h1></center>\r\n<hr><center>nginx</center> "
    "</body>\r\n</html>"
)

# A deterministic client fault the proxy reports as a retryable 502.
GATEWAY_DETERMINISTIC_502 = json.dumps(
    {
        "code": "ASP.UPSTREAM.ERROR",
        "message": "InvalidParameter",
        "retryable": True,
        "request_id": "d5507d1b-1059-9635-989e-e1f0c1b6b62e",
    },
    ensure_ascii=False,
)

GATEWAY_CREDITS_403 = json.dumps(
    {
        "code": "ASP.BIZ.CREDITS_INSUFFICIENT",
        "message": "模型 Credits 不足，请先使用贡献值兑换",
        "retryable": False,
    },
    ensure_ascii=False,
)


class _Response:
    """The three attributes the retry decision reads off an httpx response."""

    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.headers = {}

    def json(self) -> dict:
        payload = json.loads(self.text)
        if not isinstance(payload, dict):
            raise ValueError("not an object")
        return payload


# -- the provider's stated wait ----------------------------------------


def test_provider_stated_wait_is_read_in_case_insensitively() -> None:
    assert retry_after_seconds({"Retry-After": "45"}) == 45.0
    assert retry_after_seconds({"retry-after": "45"}) == 45.0


def test_absurd_or_malformed_waits_are_treated_as_silence() -> None:
    # An absurd delay parks a paid task longer than re-dispatching it costs.
    assert retry_after_seconds({"Retry-After": "86400"}) == (
        MAX_RETRY_AFTER_SECONDS
    )
    # The HTTP-date form is not sent by any provider in use; guessing at a
    # clock comparison would be worse than falling back to our own schedule.
    assert retry_after_seconds({"Retry-After": "Wed, 21 Oct 2026"}) == 0.0
    assert retry_after_seconds({"Retry-After": "0"}) == 0.0
    assert retry_after_seconds({"Retry-After": "-5"}) == 0.0
    assert retry_after_seconds({}) == 0.0
    assert retry_after_seconds(None) == 0.0


# -- our own schedule ---------------------------------------------------


def test_backoff_grows_exponentially_and_stops_at_the_cap() -> None:
    schedule = [
        backoff_seconds(attempt, base=10, jitter_ratio=0)
        for attempt in range(4)
    ]
    assert schedule == [10, 20, 40, 80]
    assert backoff_seconds(9, base=10, jitter_ratio=0) == 300.0


def test_backoff_honours_retry_after_over_the_schedule() -> None:
    assert (
        backoff_seconds(
            0,
            base=10,
            headers={"Retry-After": "120"},
        )
        == 120.0
    )


def test_jitter_spreads_a_fanout_without_losing_the_schedule() -> None:
    drawn = {backoff_seconds(1, base=10) for _ in range(40)}
    # Within a bounded band, so the budget stays predictable …
    assert all(20.0 <= value <= 25.0 for value in drawn)
    # … but not one value, which is what made every node retry in lockstep.
    assert len(drawn) > 1


# -- what counts as worth another paid attempt --------------------------


def test_unenveloped_server_errors_are_retryable() -> None:
    # The regression this fixes: an nginx 503 has no gateway code, so the
    # status alone decides, and it used to end the task on its first attempt.
    assert _is_retryable_response(_Response(503, NGINX_503))
    assert _is_retryable_response(_Response(429, "too many requests"))
    assert _is_retryable_response(_Response(502, ""))


def test_a_gateway_code_no_longer_overrides_the_status() -> None:
    # The proxy is fixing its stamping on its side, so a deterministic-looking
    # code no longer vetoes the status: a 502 retries. A 4xx still does not -
    # that is the status rule, not the code.
    assert _is_retryable_response(_Response(502, GATEWAY_DETERMINISTIC_502))
    assert not _is_retryable_response(_Response(403, GATEWAY_CREDITS_403))


def test_client_errors_and_successes_are_never_retried() -> None:
    assert not _is_retryable_response(_Response(400, "bad request"))
    assert not _is_retryable_response(_Response(404, "not found"))
    assert not _is_retryable_response(_Response(200, "{}"))


# -- the throttle signal the scheduler holds dispatch on ----------------


def test_rate_limit_signal_recognises_both_wordings() -> None:
    assert is_rate_limit_text(
        "Image generation failed: rate limited after all retries",
    )
    assert is_rate_limit_text(
        "Image generation failed with status 429: slow down",
    )
    assert is_rate_limit_text("Video submit hit a rate limit")
    assert not is_rate_limit_text(NGINX_503)
    assert not is_rate_limit_text(GATEWAY_CREDITS_403)
    assert not is_rate_limit_text("")


def test_rate_limit_text_is_narrower_than_retryability() -> None:
    # A 503 is retryable for this node but says nothing about the other
    # branches, so it must not hold the whole project.
    assert _is_retryable_response(_Response(503, NGINX_503))
    assert not is_rate_limit_text("Image generation failed with status 503")


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_lane_decision_matches_shared_status_rule(status: int) -> None:
    assert _is_retryable_response(_Response(status, "no envelope here"))
