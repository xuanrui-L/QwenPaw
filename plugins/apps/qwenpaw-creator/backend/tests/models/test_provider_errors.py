# -*- coding: utf-8 -*-
# pylint: disable=protected-access
# flake8: noqa: E501
"""Provider errors, retry eligibility, and backoff."""

from __future__ import annotations

import json

import pytest

from models.image.base import _is_retryable_response
from models.provider_errors import (
    CLASS_PERMANENT,
    CLASS_QUOTA,
    CLASS_TRANSIENT,
    CLASS_UNKNOWN,
    classify_gateway_error,
    gateway_error_code,
    gateway_request_id,
    is_gateway_quota_error,
    is_rate_limit_text,
    retryable_for_status,
)
from models.retry_timing import (
    MAX_RETRY_AFTER_SECONDS,
    backoff_seconds,
    retry_after_seconds,
)
from services.file_agent_runtime.work_scheduler import (
    _is_transient_dispatch_error,
    _provider_error_suffix,
)
from services.media_files.transient_errors import (
    is_transient_error_message,
    is_transient_task_error,
    is_unclassified_failure,
)
from utils.exceptions import ModelError

# Wrapping matters: the raising layer appends its own context and the raw
# provider body, so the code is never the whole string.
UPSTREAM_ENVELOPE = (
    'Image generation failed with status 502: {"code": "ASP.UPSTREAM.ERROR", '
    '"message": "InvalidParameter: Failed to download the reference media", '
    '"retryable": true, "request_id": "d5507d1b-1059-9635-989e-e1f0c1b6b62e"}. '
    "Check creator_image_model configuration."
)

CREDITS_ENVELOPE = (
    'Chat completion failed with status 403: {"code": '
    '"ASP.BIZ.CREDITS_INSUFFICIENT", "message": "模型 Credits 不足，请先使用'
    '贡献值兑换", "retryable": false, "request_id": '
    '"bcc611fa-c5da-4190-8e3b-73aff40f4444"}'
)

SIZE_ENVELOPE = (
    '{"code": "ASP.SYS.INTERNAL_ERROR", "message": "internal error", '
    '"retryable": true}'
)

# Same refusal as CREDITS_ENVELOPE, but as an OpenAI-compatible client raises
# it on the Agent main loop: the body is re-serialised as a Python repr, so
# every quote is a single quote and ``False`` is capitalised. Captured from a
# run whose project session died in a retry loop.
CREDITS_REPR_ENVELOPE = (
    "Creator AgentScope model request failed: Error code: 403 - "
    "{'error': {'code': 'ASP.BIZ.CREDITS_INSUFFICIENT', "
    "'message': '模型 Credits 不足，请先使用贡献值兑换', "
    "'retryable': False, 'type': 'BUSINESS'}, 'request_id': "
    "'70491bf2-9f12-4e43-82c1-bab4a321f647'}"
)


def test_oversized_body_is_classified_unknown() -> None:
    # The classifier still reads the envelope as unknown; only it no longer
    # vetoes a retry, so a 500 is retried on its status.
    assert classify_gateway_error(SIZE_ENVELOPE) == CLASS_UNKNOWN
    assert retryable_for_status(500, SIZE_ENVELOPE) is True


def test_credits_refusal_is_its_own_class() -> None:
    assert classify_gateway_error(CREDITS_ENVELOPE) == CLASS_QUOTA
    assert is_gateway_quota_error(CREDITS_ENVELOPE) is True
    # Permanent for this request, but not a reason to wall the node: the fix
    # is a top-up, not an edit to the prompt.
    assert is_transient_error_message(CREDITS_ENVELOPE) is False


def test_a_repr_serialised_envelope_still_classifies() -> None:
    """The main loop hands over a repr, not JSON, and must not read as blank.

    A regex that only accepts double quotes finds no envelope here, so the
    caller falls back to its status rule and the provider's own
    ``retryable: False`` is discarded - which is how one Credits refusal
    turned into repeated failures inside two seconds.
    """
    assert gateway_error_code(CREDITS_REPR_ENVELOPE) == (
        "ASP.BIZ.CREDITS_INSUFFICIENT"
    )
    assert classify_gateway_error(CREDITS_REPR_ENVELOPE) == CLASS_QUOTA
    assert is_gateway_quota_error(CREDITS_REPR_ENVELOPE) is True
    assert gateway_request_id(CREDITS_REPR_ENVELOPE) == (
        "70491bf2-9f12-4e43-82c1-bab4a321f647"
    )
    assert retryable_for_status(403, CREDITS_REPR_ENVELOPE) is False


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ASP.UPSTREAM.TIMEOUT", CLASS_TRANSIENT),
        ("ASP.COMM.RATE_LIMITED", CLASS_TRANSIENT),
        ("ASP.BIZ.MODEL_NOT_ALLOWED", CLASS_PERMANENT),
        ("ASP.PROXY.API_KEY_INVALID", CLASS_PERMANENT),
        ("ASP.BIZ.CREDITS_INSUFFICIENT", CLASS_QUOTA),
        ("ASP.SOMETHING.NEW", CLASS_UNKNOWN),
    ],
)
def test_classification_keys_on_the_code(code: str, expected: str) -> None:
    body = f'{{"code": "{code}", "retryable": true}}'
    assert classify_gateway_error(body) == expected
    # Classification keeps reading the code, but it no longer decides retries:
    # a 502 is retried on its status whatever the code says. The provider's own
    # flag is likewise ignored here.
    assert retryable_for_status(502, body) is True


def test_providers_without_an_envelope_keep_the_status_rule() -> None:
    """Bailian/Ark publish no envelope, so 5xx and 429 stay retryable."""
    assert classify_gateway_error("upstream connect error") == ""
    assert retryable_for_status(503, "upstream connect error") is True
    assert retryable_for_status(429, "too many requests") is True
    assert retryable_for_status(400, "bad request") is False
    assert is_transient_error_message("status 503: upstream busy") is True


def test_a_persisted_retryable_flag_is_now_taken_at_face_value() -> None:
    """The envelope used to veto the stored flag; it no longer does.

    The proxy is fixing its ``retryable`` stamping on its side, so a persisted
    ``True`` now means what it says even when the message carries an envelope.
    """
    assert (
        is_transient_task_error(
            {"message": UPSTREAM_ENVELOPE, "retryable": True},
        )
        is True
    )
    assert (
        is_transient_task_error(
            {"message": "socket hang up", "retryable": True},
        )
        is True
    )
    assert is_transient_task_error({"message": "", "retryable": True}) is True


def test_request_id_is_lifted_out_for_handover() -> None:
    assert (
        gateway_request_id(UPSTREAM_ENVELOPE)
        == "d5507d1b-1059-9635-989e-e1f0c1b6b62e"
    )
    suffix = _provider_error_suffix(Exception(UPSTREAM_ENVELOPE))
    assert "provider_code=ASP.UPSTREAM.ERROR" in suffix
    assert "provider_request_id=d5507d1b-1059-9635-989e-e1f0c1b6b62e" in suffix
    # A provider that publishes nothing adds no noise to the user's message.
    assert _provider_error_suffix(Exception("socket hang up")) == ""


def test_model_error_from_a_gateway_4xx_is_marked_permanent() -> None:
    """The raising layers must not default a client fault to retryable."""
    error = ModelError(
        f"Video task submission failed with status 400: {UPSTREAM_ENVELOPE}",
        retryable=retryable_for_status(400, UPSTREAM_ENVELOPE),
    )
    assert error.retryable is False
    transient = ModelError(
        "Video task submission failed with status 503: upstream busy",
        retryable=retryable_for_status(503, "upstream busy"),
    )
    assert transient.retryable is True


# Measured on 2026-09-17: the proxy invented a name for an old condition.
# Concurrency throttling arrives as a 429 whose text says "please retry
# later", while the envelope reports retryable:false. No table can hold a
# code that did not exist when the table was written, so an unlisted code
# defers to the status the proxy now passes through.
CONCURRENCY_ENVELOPE = (
    "Text model 请求失败 [protocol=OpenAI-compatible "
    "model=qwen3.8-flash] HTTP 429: "
    '{"error":{"code":"ASP.BIZ.TOO_MANY_CONCURRENT_REQUESTS",'
    '"message":"并发计费任务过多，请稍后重试","retryable":false,'
    '"type":"BUSINESS"},'
    '"request_id":"96eebdd1-76fd-4b94-b668-3ac889244b83"}'
)


def test_an_unlisted_code_defers_to_the_passed_through_status() -> None:
    # An unrecognised code never overrode the status either way; the status is
    # now the sole authority for every code.
    assert classify_gateway_error(CONCURRENCY_ENVELOPE) == CLASS_TRANSIENT
    assert is_transient_error_message(CONCURRENCY_ENVELOPE) is True
    assert retryable_for_status(429, CONCURRENCY_ENVELOPE) is True


def test_a_4xx_status_still_walls_an_unlisted_code() -> None:
    # Only the status changes: a 4xx for an unknown code stays a wall, so
    # deferring to the status does not turn into "retry everything".
    assert (
        classify_gateway_error(
            CONCURRENCY_ENVELOPE.replace("HTTP 429", "HTTP 400"),
        )
        == CLASS_UNKNOWN
    )


def test_a_measured_wrapper_no_longer_walls_at_502() -> None:
    # The old guardrail let this code veto the 502; with the proxy fixing its
    # stamping, the status wins and the node is retried instead of walled.
    assert classify_gateway_error(UPSTREAM_ENVELOPE) == CLASS_UNKNOWN
    assert is_transient_error_message(UPSTREAM_ENVELOPE) is True


def test_a_severed_stream_is_transient_without_any_envelope() -> None:
    # An httpx transport error carries no body: nothing was billed because the
    # response never completed, which is what makes the retry safe.
    assert (
        is_transient_error_message(
            "Text model request failed [protocol=AgentScope Platform] "
            "RemoteProtocolError: peer closed connection without sending "
            "complete message body (incomplete chunked read)",
        )
        is True
    )


# Verbatim from a stalled project: the lane writes its status as "HTTP 504" and
# nginx words its page as "504 Gateway Time-out".
GATEWAY_TIMEOUT_504 = (
    "Text model 请求失败 [protocol=OpenAI-compatible model=qwen3.8-flash "
    "endpoint=https://platform-pre.agentscope.io/v1/chat/completions] "
    "HTTP 504: 上游响应: <html>\n<head><title>504 Gateway Time-out</title>"
    '</head>\n<body bgcolor="white">\n<center><h1>504 Gateway Time-out'
    "</h1></center>\n<hr><center>nginx</center>\n</body>\n</html>"
)


def test_a_gateway_timeout_is_transient_despite_the_hyphen() -> None:
    """The one character that walled a whole project.

    The marker table carried "gateway timeout" and "status 504", but nginx
    writes "Gateway Time-out" and the lane prefixes its message with "HTTP 504",
    so nothing matched. The scheduler filed the node as a deterministic failure
    it would never retry - while the retryable flag persisted on that same
    failure said true, because that path reads the status properly. A message
    that names its own status now decides by status, not by substring.
    """

    assert is_transient_error_message(GATEWAY_TIMEOUT_504) is True


def test_a_named_status_does_not_rescue_a_client_error() -> None:
    # The status rule only speaks for temporary codes; a 4xx stays permanent
    # whether or not the message names it.
    assert (
        is_transient_error_message(
            "Image generation failed with status 400: Green net check failed",
        )
        is False
    )
    assert (
        is_transient_error_message(
            "Text model 请求失败 HTTP 403: 上游响应: forbidden",
        )
        is False
    )


# What nginx actually writes for a read timeout, verbatim in its wording.
NGINX_504_PAGE = (
    "<html>\r\n<head><title>504 Gateway Time-out</title></head>\r\n"
    '<body bgcolor="white">\r\n<center><h1>504 Gateway Time-out</h1>'
    "</center>\r\n<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n"
)


def test_the_text_lane_wording_still_reads_back_as_transient() -> None:
    """A contract between the error wording and the retry classifier.

    The rule reads a status back out of prose, so the prose is load-bearing.
    Rewording ``HTTP {code}`` - the exact kind of edit that let a gateway
    timeout be filed as a deterministic failure and wall a project - would make
    the classifier stop seeing a status at all, and the regression would show up
    as a stalled project rather than a failing test. Building the real error
    here turns that edit into a red test.
    """
    from models.text_model import _http_error

    class _Response:
        status_code = 504
        text = NGINX_504_PAGE

    error = _http_error(
        _Response(),
        protocol="OpenAI-compatible",
        model_name="qwen3.8-flash",
        url="https://platform-pre.agentscope.io/v1/chat/completions",
    )

    assert "HTTP 504" in str(error)
    assert is_transient_error_message(str(error)) is True
    # The flag persisted alongside the message has to agree with the classifier,
    # because the two used to contradict each other on this very failure.
    assert error.retryable is True


def test_an_unrecognised_failure_is_retried_by_the_scheduler_only() -> None:
    """The floor, placed exactly where it belongs.

    The two verdicts are not symmetric at the dispatch wall: a wrong
    "transient" spends a bounded budget and then stops with an honest message,
    while a wrong "deterministic" walls a paid node until somebody notices -
    that is how one hyphen in nginx's "504 Gateway Time-out" stalled a project.
    A durable media slot is not the same bet, so an unknown wording still does
    not reopen one there: resubmitting an interrupted one-shot paid render is
    spend with no possible outcome.
    """
    unknown = "provider answered something we have never seen"

    assert is_unclassified_failure(unknown) is True
    assert _is_transient_dispatch_error(Exception(unknown)) is True
    # The media-side verdict stays conservative.
    assert is_transient_error_message(unknown) is False


def test_a_structural_refusal_is_never_retried_however_worded() -> None:
    # The floor must not swallow the codes that name an input the model cannot
    # accept; they used to be checked only after the transient branch, which the
    # permissive default would have made unreachable.
    exc = ModelError("provider refused this request", retryable=False)
    exc.code = "IMAGE_REFERENCE_BUDGET_EXCEEDED"  # type: ignore[attr-defined]

    assert is_unclassified_failure(str(exc)) is True
    assert _is_transient_dispatch_error(exc) is False


def test_configuration_and_capability_failures_stay_deterministic() -> None:
    """What the floor must not swallow: causes with no request behind them.

    These carry no HTTP status precisely because nothing was sent, so no
    structured signal exists to veto them and the wording is all there is.
    Retrying a missing key is pure spend with no possible outcome.
    """

    for message in (
        "Creator text model API key 未配置：请在模型配置弹窗中填写。",
        "Image generation failed: check creator_image_model configuration.",
        "Anthropic Messages protocol does not support video input.",
        "Never resubmit an interrupted one-shot image provider call.",
    ):
        assert is_transient_error_message(message) is False
        assert is_unclassified_failure(message) is False
        assert _is_transient_dispatch_error(Exception(message)) is False


def test_a_persisted_flag_is_read_as_a_claim_not_a_veto() -> None:
    """``retryable`` means "the lane believed this was retryable".

    The executors persist ``bool(getattr(exc, "retryable", False))``, so a bare
    exception that never carried the attribute lands as ``false`` exactly like a
    refusal the lane did call permanent. Treating that as a veto would close
    retry slots on plain connection failures, so only a true counts as an answer
    and anything else falls back to the wording.
    """

    assert (
        is_transient_task_error({"message": "All connection attempts failed"})
        is True
    )
    assert (
        is_transient_task_error(
            {"message": "All connection attempts failed", "retryable": False},
        )
        is True
    )
    assert (
        is_transient_task_error(
            {"message": "provider answered something odd", "retryable": True},
        )
        is True
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
