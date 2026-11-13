# -*- coding: utf-8 -*-
"""Replay filtering: noise out, lifecycle intact, cursor intact."""

from __future__ import annotations

import json

import pytest

from services.runtime_files.models import SessionEventRecord
from services.runtime_files.replay_filter import (
    REPLAY_SKIP_TYPES,
    filter_replay_events,
    replay_filter_enabled,
    replay_noise_line,
    replay_result_max_bytes,
)

pytestmark = pytest.mark.unit

SESSION = "session-1"
PROJECT = "project-1"


def _event(seq: int, event_type: str, payload: dict | None = None):
    return SessionEventRecord(
        event_id=f"event-{seq}",
        project_id=PROJECT,
        creator_session_id=SESSION,
        event_seq=seq,
        event_type=event_type,
        actor="file_agent_runtime",
        payload=payload or {},
    )


def test_historical_deltas_are_dropped_and_lifecycle_survives() -> None:
    events = [
        _event(1, "agent.run.started", {"runId": "run-1"}),
        _event(2, "agent.message_delta", {"runId": "run-1", "delta": "你好"}),
        _event(3, "subagent.message_delta", {"runId": "run-1", "delta": "x"}),
        _event(4, "message.completed", {"runId": "run-1", "text": "你好"}),
        _event(5, "agent.tool_completed", {"runId": "run-1"}),
    ]
    kept = filter_replay_events(events, active_run_ids=())
    assert [event.event_type for event in kept] == [
        "agent.run.started",
        "message.completed",
        "agent.tool_completed",
    ]
    # The seqs jump, which is exactly why the client cursor compares with `>`.
    assert [event.event_seq for event in kept] == [1, 4, 5]


def test_in_flight_run_keeps_its_deltas() -> None:
    """An unfinished message has no completed event: its text exists only as
    deltas, and the 「正在思考」 state reads them too."""
    events = [
        _event(
            1, "agent.message_delta", {"runId": "run-live", "delta": "前半"}
        ),
        _event(2, "agent.message_delta", {"runId": "run-done", "delta": "别"}),
    ]
    kept = filter_replay_events(events, active_run_ids={"run-live"})
    assert [event.event_seq for event in kept] == [1]


def test_parent_run_id_also_grants_the_exemption() -> None:
    events = [
        _event(
            1,
            "subagent.message_delta",
            {"parentRunId": "agent-run-live", "delta": "y"},
        ),
    ]
    assert (
        len(
            filter_replay_events(
                events,
                active_run_ids={"agent-run-live"},
            ),
        )
        == 1
    )


def test_oversized_result_is_summarized_not_dropped() -> None:
    pointers = [
        f"/assets/artifact_versions_by_id/artifact-version-{i}"
        for i in range(900)
    ]
    big = {"changedPointers": pointers, "other": "z" * 4096}
    events = [
        _event(
            7,
            "subagent.tool_completed",
            {"runId": "run-1", "toolCallId": "call-1", "result": big},
        ),
    ]
    kept = filter_replay_events(events, result_max_bytes=32 * 1024)
    assert len(kept) == 1, "the terminal event itself must never disappear"
    result = kept[0].payload["result"]
    assert result["replayTruncated"] is True
    assert result["changedPointerCount"] == 900
    assert "changedPointers" not in result
    assert result["bytes"] == len(json.dumps(big, ensure_ascii=False))
    # Identity fields the UI does read stay untouched.
    assert kept[0].payload["toolCallId"] == "call-1"
    assert kept[0].event_type == "subagent.tool_completed"


def test_small_result_passes_through_untouched() -> None:
    payload = {"runId": "run-1", "result": {"changedPointers": ["/name"]}}
    kept = filter_replay_events(
        [_event(1, "subagent.tool_completed", payload)],
        result_max_bytes=32 * 1024,
    )
    assert kept[0].payload["result"] == {"changedPointers": ["/name"]}


def test_durable_records_are_not_mutated() -> None:
    big = {"changedPointers": ["p"] * 5000}
    event = _event(1, "agent.tool_completed", {"result": big})
    filter_replay_events([event], result_max_bytes=1024)
    assert event.payload["result"] == big, "the read model must stay intact"


def test_switch_defaults_on_and_env_disables(monkeypatch) -> None:
    monkeypatch.delenv("CREATOR_SSE_REPLAY_FILTER", raising=False)
    assert replay_filter_enabled() is True
    for value in ("0", "false", "off", "NO"):
        monkeypatch.setenv("CREATOR_SSE_REPLAY_FILTER", value)
        assert replay_filter_enabled() is False
    monkeypatch.setenv("CREATOR_SSE_REPLAY_FILTER", "1")
    assert replay_filter_enabled() is True


def test_result_ceiling_reads_env_with_fallback(monkeypatch) -> None:
    monkeypatch.delenv("CREATOR_SSE_REPLAY_RESULT_MAX_BYTES", raising=False)
    assert replay_result_max_bytes() == 32 * 1024
    monkeypatch.setenv("CREATOR_SSE_REPLAY_RESULT_MAX_BYTES", "4096")
    assert replay_result_max_bytes() == 4096
    monkeypatch.setenv("CREATOR_SSE_REPLAY_RESULT_MAX_BYTES", "nonsense")
    assert replay_result_max_bytes() == 32 * 1024
    monkeypatch.setenv("CREATOR_SSE_REPLAY_RESULT_MAX_BYTES", "0")
    assert replay_result_max_bytes() == 32 * 1024


def test_only_delta_types_are_in_the_skip_set() -> None:
    # Widening this set is how the replay would start losing correctness.
    assert set(REPLAY_SKIP_TYPES) == {
        "agent.message_delta",
        "subagent.message_delta",
    }


def test_byte_classifier_ignores_a_nested_event_type_in_a_result() -> None:
    """A lifecycle record whose result embeds a delta JSON is not noise.

    Review #243 r4548690038: the byte classifier used to substring-match
    ``"event_type":"agent.message_delta"`` anywhere on the line, so a
    ``subagent.tool_completed`` envelope whose ``result`` field carried a
    legitimate JSON blob with that same key pair was dropped as noise
    -- losing the persistent tool state that a fresh view rebuilds its
    ToolCallCard from. The marker is now anchored to the outer record's
    canonical key ordering (``event_type`` immediately followed by
    ``message_id``), which a nested result cannot reproduce.
    """

    outer_lifecycle = json.dumps(
        {
            "record": {
                "actor": "file_agent_runtime",
                "created_at": "2026-11-13T00:00:00+00:00",
                "creator_session_id": SESSION,
                "event_id": "event-2",
                "event_seq": 2,
                "event_type": "subagent.tool_completed",
                "message_id": None,
                "payload": {
                    "runId": "run-1",
                    "toolCallId": "call-1",
                    # A result that legitimately carries the very bytes
                    # the loose marker used to match anywhere on the line.
                    "result": json.dumps(
                        {
                            "event_type": "agent.message_delta",
                            "delta": "nested",
                        },
                    ),
                },
                "project_id": PROJECT,
                "round_id": None,
            },
            "seq": 2,
            "written_at": "2026-11-13T00:00:00+00:00",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert replay_noise_line(outer_lifecycle) is False, (
        "a tool_completed whose result embeds an event_type JSON must "
        "not be classified as streaming noise -- the whole lifecycle "
        "record would disappear from the replay"
    )

    # An actual outer delta is still classified as noise.
    outer_delta = json.dumps(
        {
            "record": {
                "actor": "file_agent_runtime",
                "created_at": "2026-11-13T00:00:00+00:00",
                "creator_session_id": SESSION,
                "event_id": "event-3",
                "event_seq": 3,
                "event_type": "agent.message_delta",
                "message_id": "msg-1",
                "payload": {"runId": "run-1", "delta": "hi"},
                "project_id": PROJECT,
                "round_id": None,
            },
            "seq": 3,
            "written_at": "2026-11-13T00:00:00+00:00",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert replay_noise_line(outer_delta) is True
