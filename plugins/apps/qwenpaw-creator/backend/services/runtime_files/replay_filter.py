# -*- coding: utf-8 -*-
"""Replay-side filtering for the durable SSE event stream.

A fresh view replays the whole event log on purpose: the durable session
snapshot carries no terminal projection for ``subagentActivities`` or run
cancellation, so clipping the window silently downgrades an accepted-then-
failed delegation to a success (CR #7). The replay is a correctness
requirement, not a size choice.

What it needs is the lifecycle. What it also carries is streaming noise.
Measured on one field session (101.2 MB / 77,261 records):

    agent.message_delta        60,496 条   43.2 MB
    subagent.message_delta     10,446 条    9.4 MB
    subagent.tool_completed       209 条   43.0 MB  (single `result` field)

91.8% of the records are deltas the client discards by design: the
``ingestEvents`` window keeps the last 500 events, and ``AgentDock`` reads
``completedText ?? deltas``, so they are transmitted, parsed and dropped.
The remaining volume sits in one ``result`` field of
``subagent.tool_completed`` (largest single record: 3.6 MB), which no
component renders: it is stored on the tool call and passed through the
presentation layer unread.

Filtering by event type therefore removes the bytes without touching the one
thing the replay exists to rebuild.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, Iterable, Sequence

from services.runtime_files.models import SessionEventRecord

#: Types dropped from a replay when their run is no longer active.
REPLAY_SKIP_TYPES = frozenset(
    {
        "agent.message_delta",
        "subagent.message_delta",
    },
)

#: Byte markers of those types, matched on the raw line before any parsing.
#: Canonical JSON writes no space after the colon; both spellings are matched
#: so a future encoder change degrades to "parse it", never to "keep it".
REPLAY_SKIP_LINE_MARKERS: tuple[bytes, ...] = tuple(
    pattern
    for name in sorted(REPLAY_SKIP_TYPES)
    for pattern in (
        f'"event_type":"{name}"'.encode("ascii"),
        f'"event_type": "{name}"'.encode("ascii"),
    )
)


def replay_noise_line(line: bytes) -> bool:
    """Whether one raw line is streaming noise the replay is going to drop.

    Classifying before parsing matters more than the bytes the drop saves: a
    field session's 146 MB stream holds 70,942 deltas, and parsing each one to
    throw it away kept a single replay busy for over 90 seconds, which is what
    made every other endpoint on the same process wait 7-23 seconds while a
    Project page was open.
    """

    return any(marker in line for marker in REPLAY_SKIP_LINE_MARKERS)


def make_replay_noise_line(
    active_run_ids: Iterable[str] = (),
) -> "Callable[[bytes], bool]":
    """Byte-level classifier that spares deltas of still-active runs.

    Review #243 P2: the filter is used before parsing so it cannot see
    ``event.payload``, but the parse-time ``filter_replay_events`` already
    exempts non-terminal runs because a live reply has no durable
    message yet. Applying the byte-level drop to those deltas first
    meant the exemption never got the chance to fire, and a returning
    user saw the partial text of an in-flight reply disappear until
    ``message.completed`` landed. The factory bakes the active run ids
    into a substring test on the raw bytes; the ids come from the
    session's non-terminal mainline run and every non-terminal
    specialist, mirroring the parse-time set exactly.

    An empty active set keeps the original behaviour byte for byte.
    """

    ids = sorted({run_id for run_id in active_run_ids if run_id})
    if not ids:
        return replay_noise_line
    active_markers: tuple[bytes, ...] = tuple(
        pattern
        for run_id in ids
        for key in ("runId", "parentRunId")
        for pattern in (
            f'"{key}":"{run_id}"'.encode("utf-8"),
            f'"{key}": "{run_id}"'.encode("utf-8"),
        )
    )

    def is_noise(line: bytes) -> bool:
        if not any(marker in line for marker in REPLAY_SKIP_LINE_MARKERS):
            return False
        return not any(marker in line for marker in active_markers)

    return is_noise


FILTER_ENV = "CREATOR_SSE_REPLAY_FILTER"
RESULT_MAX_BYTES_ENV = "CREATOR_SSE_REPLAY_RESULT_MAX_BYTES"
DEFAULT_RESULT_MAX_BYTES = 32 * 1024


def replay_filter_enabled() -> bool:
    """Master switch; ``0`` restores the pre-filter replay byte for byte."""

    raw = os.environ.get(FILTER_ENV, "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def replay_result_max_bytes() -> int:
    """Tool results above this are replaced by a summary during replay."""

    raw = os.environ.get(RESULT_MAX_BYTES_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_RESULT_MAX_BYTES
    except ValueError:
        return DEFAULT_RESULT_MAX_BYTES
    return value if value > 0 else DEFAULT_RESULT_MAX_BYTES


def _run_id(payload: dict[str, Any]) -> str | None:
    return payload.get("runId") or payload.get("parentRunId")


def _summarize_result(result: Any) -> dict[str, Any]:
    """Keep the shape discoverable, drop the bytes nobody renders."""

    summary: dict[str, Any] = {"replayTruncated": True}
    if isinstance(result, dict):
        summary["keys"] = sorted(result)
        pointers = result.get("changedPointers")
        if isinstance(pointers, list):
            summary["changedPointerCount"] = len(pointers)
    elif isinstance(result, list):
        summary["itemCount"] = len(result)
    summary["bytes"] = len(json.dumps(result, ensure_ascii=False))
    return summary


def filter_replay_events(
    events: Sequence[SessionEventRecord],
    *,
    active_run_ids: Iterable[str] = (),
    result_max_bytes: int = DEFAULT_RESULT_MAX_BYTES,
) -> list[SessionEventRecord]:
    """Drop historical streaming deltas and summarize oversized results.

    ``active_run_ids`` is the correctness guard: a run that is still in flight
    has no ``message.completed`` yet, and dropping its deltas would blank the
    partially written text and the 「正在思考」 state that reads
    ``thinkingDeltas`` non-empty with ``deltas`` empty. Pass the run ids that
    were non-terminal when the connection opened — erring towards a larger set
    only means a few extra deltas, never a missing one.
    """

    active = {run_id for run_id in active_run_ids if run_id}
    kept: list[SessionEventRecord] = []
    for event in events:
        payload = event.payload or {}
        if event.event_type in REPLAY_SKIP_TYPES:
            if _run_id(payload) in active:
                kept.append(event)
            continue
        result = payload.get("result")
        if isinstance(result, (dict, list)) and (
            len(json.dumps(result, ensure_ascii=False)) > result_max_bytes
        ):
            kept.append(
                event.model_copy(
                    update={
                        "payload": {
                            **payload,
                            "result": _summarize_result(result),
                        },
                    },
                ),
            )
            continue
        kept.append(event)
    return kept
