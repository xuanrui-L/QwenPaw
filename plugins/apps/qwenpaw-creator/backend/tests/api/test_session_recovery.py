# -*- coding: utf-8 -*-
"""Interactive session replay and cancellation recovery."""

from __future__ import annotations

import asyncio
from functools import partial

import pytest
from api import file_session_routes
from api.file_session_routes import stream_events
from domain.enums import (
    SpecialistRole,
    SpecialistRunStatus,
    TaskKind,
    TaskStatus,
)
from services.runtime_files.execution_models import (
    SpecialistRunRecord,
    TaskRecord,
)
from services.runtime_files.execution_store import ProjectExecutionStore
from test_file_runtime_api import _app

from services.runtime_files import ProjectRuntimeSessionStore
from services.runtime_files.jsonl_store import DurableJsonlStore
from runtime_files.test_session_store import (
    CONVERSATION_ID,
    PROJECT_ID,
    SESSION_ID,
    _activate_runtime,
    _admit,
    _append,
    _boundary_file,
    _runtime,
    _text,
)
from services.runtime_files import JsonlCorruptionError
from runtime_files.test_storage_primitives import EventRecord


def _append_agent_event(services, event_type, payload):
    return services.sessions.append_event(
        "project-1",
        "session-1",
        event_type=event_type,
        actor="agent",
        payload=payload,
    )


async def _event_stream(services, *, disconnect_after=4):
    class Connection:
        polls = 0

        async def is_disconnected(self):
            self.polls += 1
            return self.polls > disconnect_after

    response = await stream_events(
        "project-1",
        Connection(),
        after=0,
        last_event_id=None,
        services=services,
    )
    return response.body_iterator


def _parse_sse(chunk):
    fields = dict(
        line.split(": ", 1) for line in chunk.splitlines() if ": " in line
    )
    return (
        int(fields.get("id", "0")),
        fields.get("event", ""),
        fields.get("data", ""),
    )


async def _replay(services, *, disconnect_after=4):
    replay = []
    async for chunk in await _event_stream(
        services,
        disconnect_after=disconnect_after,
    ):
        event = _parse_sse(chunk)
        if event[0] or event[1]:
            replay.append(event)
    return replay


def test_live_streaming_delta_past_the_replay_boundary_is_delivered(
    tmp_path,
) -> None:
    """A live delta must survive a long-lived SSE connection.

    Review #243 blocking #1: ``replay_noise_line`` was passed to the
    reader without a boundary, so it kept dropping every streaming
    delta for the whole life of the connection -- a returning user saw
    the replay up to the tail seq, then silence while a fresh reply
    generated in front of them. The fix scopes the filter to the tail
    at connect time; anything appended after that point reaches the
    client verbatim.
    """

    _app_value, services, _snapshot, _bootstrap = _app(tmp_path)
    append = partial(_append_agent_event, services)

    # Pre-replay: a lifecycle event + an old delta that the filter is
    # allowed to drop. Boundary is the tail at connect time (=2).
    append("agent.run.started", {"runId": "run-old"})
    append(
        "agent.message_delta",
        {
            "runId": "run-old",
            "messageId": "m-old",
            "deltaIndex": 0,
            "delta": "旧",
        },
    )

    async def scenario():
        iterator = await _event_stream(services, disconnect_after=40)
        seen = [_parse_sse(await anext(iterator))[:2]]
        # Cross the original replay boundary before publishing the live delta.
        append(
            "agent.message_delta",
            {
                "runId": "run-live",
                "messageId": "m-live",
                "deltaIndex": 0,
                "delta": "live",
            },
        )
        async for chunk in iterator:
            seq, kind, _ = _parse_sse(chunk)
            if seq or kind:
                seen.append((seq, kind))
            if kind == "agent.message_delta":
                break
        return seen

    assert asyncio.run(scenario()) == [
        (1, "agent.run.started"),
        (3, "agent.message_delta"),
    ]


@pytest.mark.parametrize(
    ("status", "index_unavailable"),
    [
        (SpecialistRunStatus.RUNNING_MODEL, False),
        (SpecialistRunStatus.FAILED, False),
        (SpecialistRunStatus.RUNNING_MODEL, True),
    ],
)
def test_replay_preserves_specialist_text_without_a_completed_message(
    tmp_path,
    status,
    index_unavailable,
    monkeypatch,
) -> None:
    """Both replay filters retain partial replies from live and failed workers."""

    _app_value, services, _snapshot, _bootstrap = _app(tmp_path)
    append = partial(_append_agent_event, services)

    append(
        "agent.run.started",
        {"runId": "run-active"},
    )
    append(
        "subagent.message_delta",
        {
            "runId": "run-active",
            "messageId": "m-active",
            "deltaIndex": 0,
            "delta": "prefix",
        },
    )
    # Same boundary-internal delta from a run that is NOT in the active
    # set at connect -- still should be dropped as replay noise.
    append(
        "subagent.message_delta",
        {
            "runId": "run-settled",
            "messageId": "m-settled",
            "deltaIndex": 0,
            "delta": "should-stay-dropped",
        },
    )

    append(
        "subagent.message_completed",
        {
            "runId": "run-settled",
            "messageId": "m-settled",
            "text": "Complete reply",
        },
    )
    append("agent.message_delta", {"runId": "finished-main", "delta": "old"})
    append(
        "subagent.tool_completed", {"result": {"text": "x" * (1024 * 1024)}}
    )
    if status == SpecialistRunStatus.FAILED:
        append(
            "subagent.failed",
            {"runId": "run-active", "error": "stream interrupted"},
        )

    executions = ProjectExecutionStore(services.root)
    for run_id, final_status in (
        ("run-active", status),
        ("run-settled", SpecialistRunStatus.SUCCEEDED),
    ):
        executions.create_specialist_run(
            SpecialistRunRecord(
                run_id=run_id,
                project_id="project-1",
                round_id=run_id,
                role=SpecialistRole.VISUAL_DEVELOPMENT,
                input_generation=_snapshot.generation,
                input_etag=_snapshot.etag,
            )
        )
        executions.transition_specialist_run(
            "project-1",
            run_id,
            expected_status=SpecialistRunStatus.QUEUED,
            status=SpecialistRunStatus.RUNNING_MODEL,
        )
        if final_status != SpecialistRunStatus.RUNNING_MODEL:
            executions.transition_specialist_run(
                "project-1",
                run_id,
                expected_status=SpecialistRunStatus.RUNNING_MODEL,
                status=final_status,
            )

    if index_unavailable:

        def fail_read(*_args):
            raise OSError("specialist index unavailable")

        monkeypatch.setattr(
            ProjectExecutionStore,
            "list_specialist_runs",
            fail_read,
        )

    events = asyncio.run(_replay(services))
    seqs = [seq for seq, _, _ in events]
    assert 1 in seqs  # run.started, always kept
    # The active run's delta is delivered (fix). Without the change,
    # seq 2 was filtered here while the parse-time exemption never
    # got to run.
    active_delta = next(
        (data for seq, kind, data in events if seq == 2),
        None,
    )
    assert active_delta is not None, (
        f"active-run delta was dropped before the parse-time filter "
        f"could exempt it: {events}"
    )
    assert "prefix" in active_delta
    # An available index lets the replay discard the settled run's duplicate.
    assert (3 in seqs) is index_unavailable
    assert 5 not in seqs
    summary = next(data for seq, _, data in events if seq == 6)
    assert "replayTruncated" in summary
    assert len(summary) < 1024


@pytest.mark.parametrize("endpoint", ["interrupt", "tasks/memory/cancel"])
def test_stop_signals_a_recovered_memory_build(
    tmp_path, monkeypatch, endpoint
):
    from httpx import ASGITransport, AsyncClient
    from services.media.source_memory import source_memory_service

    app, services, _, _ = _app(tmp_path)
    memory = source_memory_service(services)
    memory.executions.create_task(
        TaskRecord(
            task_id="memory",
            project_id="project-1",
            kind=TaskKind.SOURCE_MEMORY_BUILD,
            request_fingerprint="memory",
            metadata={
                "targetRef": "asset:source",
                "analysisVersionId": "index",
                "assetVersionId": "version",
                "sourceChecksum": "source",
                "durationMs": 1_500_000,
                "localPath": str(tmp_path / "clip.mp4"),
            },
        )
    )

    async def scenario():
        started = asyncio.Event()

        async def drive(_job):
            started.set()
            await asyncio.Future()

        monkeypatch.setattr(memory, "_drive", drive)
        await memory.resume_interrupted()
        await asyncio.wait_for(started.wait(), 5)
        worker = memory._jobs["memory"]
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/projects/project-1/{endpoint}",
                headers={"Idempotency-Key": "cancel-memory"},
                json={"reason": "stop"},
            )
        assert response.status_code == 202
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(worker), 2)
        for _ in range(100):
            if (
                memory.executions.get_task("project-1", "memory").status
                is TaskStatus.CANCELLED
            ):
                break
            await asyncio.sleep(0.01)
        assert (
            memory.executions.get_task("project-1", "memory").status
            is TaskStatus.CANCELLED
        )

    asyncio.run(scenario())


def test_lifecycle_event_embedding_an_event_record_survives_the_filter(
    tmp_path,
    monkeypatch,
) -> None:
    """A tool_completed whose result carries a whole event is delivered.

    Review #243 r4548690038 follow-up: the byte-level classifier used to
    substring-match ``"event_type":"<type>"`` (then even with an adjacent
    ``message_id``), so a ``subagent.tool_completed`` whose ``result``
    legitimately embeds an event record -- the reviewer's exact probe:
    ``{"event_type":"agent.message_delta","message_id":"nested-example"}``
    -- was dropped as noise at the byte layer, before the parse-time
    filter (which reads the real outer event type and would have kept
    it) ever ran. The client lost the persistent tool state it rebuilds
    its ToolCallCard from. The classifier now inspects only the
    envelope's own record at depth 1.
    """

    _app_value, services, _snapshot, _bootstrap = _app(tmp_path)
    append = partial(_append_agent_event, services)

    append("agent.run.started", {"runId": "run-1"})
    append(
        "subagent.tool_completed",
        {
            "runId": "run-1",
            "toolCallId": "call-1",
            # A result that embeds a full event record, so the line
            # reproduces both keys in canonical order -- the exact
            # shape the loose matcher used to fall for.
            "result": {
                "event_seq": 99,
                "event_type": "agent.message_delta",
                "message_id": "nested-example",
                "delta": "copied",
            },
        },
    )
    append("agent.run.completed", {"runId": "run-1"})
    # A genuine historical delta with no active run is still dropped,
    # proving the fix did not just disable the classifier.
    append(
        "agent.message_delta",
        {"runId": "run-settled", "messageId": "m", "delta": "gone"},
    )

    monkeypatch.setattr(
        file_session_routes,
        "_active_specialist_run_ids",
        lambda *_args, **_kwargs: set(),
    )

    events = asyncio.run(_replay(services))
    kinds = [kind for _, kind, _ in events]
    assert "subagent.tool_completed" in kinds, (
        f"a tool_completed embedding an event record was dropped as "
        f"replay noise: {events}"
    )
    # The genuine settled-run delta with no active run is still filtered.
    assert "agent.message_delta" not in kinds


@pytest.mark.parametrize(
    "task_status",
    [TaskStatus.RUNNING, TaskStatus.CANCELLED],
)
def test_stop_cleanup_settles_media_run_after_worker_is_gone(
    tmp_path,
    task_status,
) -> None:
    from api.file_session_routes import _cancel_active_project_tasks_sync

    _app_instance, services, snapshot, _bootstrap = _app(tmp_path)
    executions = ProjectExecutionStore(services.root)
    run = executions.create_run(
        SpecialistRunRecord(
            run_id="run-stopped-image",
            project_id="project-1",
            round_id="round-image",
            role=SpecialistRole.R2V_GENERATION_DIRECTOR,
            input_generation=snapshot.generation,
            input_etag=snapshot.etag,
            metadata={"commandType": "GENERATE_STORYBOARD_IMAGE"},
        ),
    )
    executions.transition_run(
        "project-1",
        run.run_id,
        expected_status=SpecialistRunStatus.QUEUED,
        status=SpecialistRunStatus.RUNNING_MODEL,
    )
    executions.transition_run(
        "project-1",
        run.run_id,
        expected_status=SpecialistRunStatus.RUNNING_MODEL,
        status=SpecialistRunStatus.WAITING_RUNTIME,
    )
    task = executions.create_task(
        TaskRecord(
            task_id="task-stopped-image",
            project_id="project-1",
            run_id=run.run_id,
            round_id=run.round_id,
            input_generation=snapshot.generation,
            input_etag=snapshot.etag,
            kind=TaskKind.IMAGE_GENERATION,
            request_fingerprint="stopped-image",
        ),
    )
    executions.transition_task(
        "project-1",
        task.task_id,
        expected_status=TaskStatus.QUEUED,
        status=task_status,
    )

    # No worker remains to execute its finally block. Repeated stop must also
    # repair a Task already cancelled by a previous interrupted cleanup.
    _cancel_active_project_tasks_sync(services, "project-1")
    _cancel_active_project_tasks_sync(services, "project-1")

    assert (
        executions.get_task("project-1", task.task_id).status
        is TaskStatus.CANCELLED
    )
    stopped_run = executions.get_run("project-1", run.run_id)
    assert stopped_run.status is SpecialistRunStatus.CANCELLED
    assert stopped_run.final_marker == "CANCELLED"
    assert services.projects.read("project-1").etag == snapshot.etag


def test_sse_replay_drops_historical_deltas_and_keeps_lifecycle(tmp_path):
    """The replay exists to rebuild lifecycle state (CR #7), but streaming
    deltas inside it are discarded by the client by design. The endpoint must
    stop shipping the noise without ever losing a lifecycle event."""
    _app_value, services, _snapshot, _bootstrap = _app(tmp_path)
    for event_type, payload in (
        ("agent.run.started", {"runId": "run-1"}),
        ("agent.message_delta", {"runId": "run-1", "delta": "x"}),
        ("subagent.message_delta", {"runId": "run-1", "delta": "y"}),
        ("message.completed", {"runId": "run-1", "text": "xy"}),
        ("agent.tool_completed", {"runId": "run-1", "toolCallId": "c-1"}),
    ):
        _append_agent_event(services, event_type, payload)

    replay = asyncio.run(_replay(services, disconnect_after=2))
    assert [(seq, kind) for seq, kind, _ in replay] == [
        (1, "agent.run.started"),
        (4, "message.completed"),
        (5, "agent.tool_completed"),
    ]


def _spy_full_reads(monkeypatch) -> list[str]:
    """Record whole-stream reads, so a paging test can prove it skipped one."""

    reads: list[str] = []
    original = DurableJsonlStore._read_bytes_unlocked

    def spy(self):  # type: ignore[no-untyped-def]
        reads.append(self.path.name)
        return original(self)

    monkeypatch.setattr(DurableJsonlStore, "_read_bytes_unlocked", spy)
    return reads


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cursor", "first"),
    [(None, 26), (26, 21)],
    ids=["tail", "older-page"],
)
def test_recent_messages_page_from_cursor_without_reading_whole_stream(
    tmp_path,
    monkeypatch,
    cursor,
    first,
):
    _, _, store, _ = _runtime(tmp_path)
    for index in range(30):
        _append(store, "user", f"message {index}")
    reads = _spy_full_reads(monkeypatch)

    page = store.list_recent_messages(
        PROJECT_ID,
        SESSION_ID,
        limit=5,
        conversation_id=CONVERSATION_ID,
        **({"before_seq": cursor} if cursor is not None else {}),
    )

    assert [item.message_seq for item in page] == list(range(first, first + 5))
    assert not reads


@pytest.mark.unit
def test_message_at_reads_only_the_named_line(tmp_path, monkeypatch):
    # A review follow-up already names the seq of the request it belongs to.
    # Locating that message used to parse every record the session ever wrote,
    # which on the field's long sessions meant hundreds of MB per lookup.
    _, _, store, _ = _runtime(tmp_path)
    for index in range(30):
        _append(store, "user", f"message {index}")
    reads = _spy_full_reads(monkeypatch)

    found = store.message_at(PROJECT_ID, SESSION_ID, message_seq=7)

    assert found is not None
    assert found.message_seq == 7
    assert not reads
    # A seq that names nothing is answered without touching the disk.
    assert store.message_at(PROJECT_ID, SESSION_ID, message_seq=0) is None


@pytest.mark.unit
def test_find_recent_message_answers_existence_without_the_stream(
    tmp_path,
    monkeypatch,
):
    # "Was this pause notice already filed" is an existence question.  The walk
    # is exhaustive -- a miss really means absent -- but it never materializes
    # the stream, which is what a 606 MB history could not afford.
    _, _, store, _ = _runtime(tmp_path)
    for index in range(20):
        _append(store, "user", f"noise {index}")
    marker = _append(
        store,
        "assistant",
        "filed",
        metadata={"executionPauseKey": "pause-key-1"},
    )
    reads = _spy_full_reads(monkeypatch)

    found = store.find_recent_message(
        PROJECT_ID,
        SESSION_ID,
        where=lambda item: item.metadata.get("executionPauseKey")
        == "pause-key-1",
    )

    assert found is not None
    assert found.message_seq == marker.message.message_seq
    assert (
        store.find_recent_message(
            PROJECT_ID,
            SESSION_ID,
            where=lambda item: item.metadata.get("executionPauseKey")
            == "never-filed",
        )
        is None
    )
    assert not reads


@pytest.mark.unit
def test_last_message_seq_tracks_the_head_without_reading_the_stream(
    tmp_path,
    monkeypatch,
):
    # The YOLO fuse asks "did anything land since I decided?" once per second.
    # Answering that with list_messages rebuilt the whole history every tick.
    _, _, store, _ = _runtime(tmp_path)
    assert store.last_message_seq(PROJECT_ID, SESSION_ID) == 0
    _append(store, "user", "first")
    _append(store, "assistant", "reply")
    reads = _spy_full_reads(monkeypatch)

    head = store.last_message_seq(PROJECT_ID, SESSION_ID)

    assert head == 2
    assert not reads
    _append(store, "user", "third")
    assert store.last_message_seq(PROJECT_ID, SESSION_ID) == 3


@pytest.mark.unit
def test_list_recent_messages_filters_conversation_while_scanning(tmp_path):
    # The conversation filter has to live in the scan: a page of one
    # conversation must not be truncated by newer messages of another.
    _, _, store, _ = _runtime(tmp_path)
    other = store.create_conversation(
        PROJECT_ID,
        SESSION_ID,
        conversation_id="conversation-2",
        title="Other",
    )
    for index in range(4):
        _append(store, "user", f"first {index}")
        store.append_message(
            PROJECT_ID,
            SESSION_ID,
            other.conversation_id,
            role="user",
            content_parts=_text(f"second {index}"),
        )

    page = store.list_recent_messages(
        PROJECT_ID,
        SESSION_ID,
        limit=3,
        conversation_id=CONVERSATION_ID,
    )

    # Sequences 1, 3, 5, 7 belong to the default conversation.
    assert [item.message_seq for item in page] == [3, 5, 7]
    assert {item.conversation_id for item in page} == {CONVERSATION_ID}


@pytest.mark.unit
def test_list_recent_events_finds_the_admission_in_the_tail_window(
    tmp_path,
    monkeypatch,
):
    _, _, store, _ = _runtime(tmp_path)
    admitted = _admit(store, "request-tail")
    store.append_event(
        PROJECT_ID,
        SESSION_ID,
        event_type="message.accepted",
        actor="user",
        message_id=admitted.message.message_id,
    )
    for index in range(40):
        store.append_event(
            PROJECT_ID,
            SESSION_ID,
            event_type="task.progress_updated",
            payload={"index": index},
        )
    reads = _spy_full_reads(monkeypatch)

    found = store.list_recent_events(
        PROJECT_ID,
        SESSION_ID,
        limit=1,
        event_type="message.accepted",
        message_id=admitted.message.message_id,
        max_scan=200,
    )

    assert [item.message_id for item in found] == [admitted.message.message_id]
    # A window too small answers "not in the tail" rather than walking back to
    # the head: the send route turns that into an explicit full-scan fallback.
    assert (
        store.list_recent_events(
            PROJECT_ID,
            SESSION_ID,
            limit=1,
            event_type="message.accepted",
            message_id=admitted.message.message_id,
            max_scan=5,
        )
        == []
    )
    assert not reads


@pytest.mark.unit
def test_create_goal_reads_the_root_message_without_a_full_scan(
    tmp_path,
    monkeypatch,
):
    # A goal references exactly one message, but the old code materialized the
    # whole durable stream and indexed it in Python -- inside the exclusive
    # Project lock, where every 10s lock waiter pays for it.
    _, _, store, _ = _runtime(tmp_path)
    root = _append(store, "user", "objective")
    for index in range(30):
        _append(store, "user", f"message {index}")
    reads = _spy_full_reads(monkeypatch)

    goal = store.create_goal(
        PROJECT_ID,
        SESSION_ID,
        CONVERSATION_ID,
        goal_id="goal-read",
        root_message_seq=root.message.message_seq,
        intent="Create the Project",
    )

    assert goal.root_message_seq == 1
    # Recovery legitimately walks the (small) queued stream; the message stream
    # is the one that used to be materialized whole, and it must not be read as
    # a whole here.
    assert "messages.jsonl" not in reads


@pytest.mark.unit
def test_client_id_lookup_reaches_the_head_without_holding_the_stream(
    tmp_path,
    monkeypatch,
):
    _, _, store, _ = _runtime(tmp_path)
    oldest = _append(
        store,
        "user",
        "oldest",
        client_message_id="client-oldest",
    )
    for index in range(30):
        _append(store, "user", f"message {index}")
    reads = _spy_full_reads(monkeypatch)

    found = store._message_by_client_id_unlocked(
        PROJECT_ID,
        SESSION_ID,
        "client-oldest",
    )
    missing = store._message_by_client_id_unlocked(
        PROJECT_ID,
        SESSION_ID,
        "client-never-used",
    )

    assert found is not None
    assert found.message_id == oldest.message.message_id
    # "absent" still means absent everywhere: the replay walks to the head
    # of the stream, it just holds one page at a time instead of the whole
    # history.
    assert missing is None
    assert not reads


@pytest.mark.unit
def test_recovery_restores_a_boundary_beyond_one_replay_page(
    tmp_path,
    monkeypatch,
):
    # Recovery replays the message stream in pages to re-assert review
    # boundaries. The page window must not decide which history gets seen, so
    # force one record per page while the boundary sits on message 2.
    monkeypatch.setattr(
        "services.runtime_files.session_store._MESSAGE_REPLAY_PAGE",
        1,
    )
    root, snapshot, store, _bootstrap = _runtime(tmp_path)
    _activate_runtime(root, snapshot, store)
    _admit(store, "request-review", "change")
    for index in range(4):
        _append(store, "assistant", f"later {index}")
    boundary_path = _boundary_file(store, "request-review")
    boundary_path.unlink()

    recovered = ProjectRuntimeSessionStore(root).get_session(
        PROJECT_ID,
        SESSION_ID,
    )

    assert recovered.last_message_seq == 6
    assert boundary_path.is_file()


@pytest.mark.unit
def test_jsonl_read_record_at_targets_one_line(tmp_path):
    path = tmp_path / "events.jsonl"
    store = DurableJsonlStore(path, EventRecord)
    assert store.read_record_at(1) is None  # no journal file yet
    for value in range(5):
        store.append(EventRecord(worker=0, value=value))

    assert store.read_record_at(3).value == 2
    assert store.read_record_at(5).value == 4
    assert store.read_record_at(6) is None
    with pytest.raises(ValueError, match="must be positive"):
        store.read_record_at(0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("suffix", "bad_seq"),
    [
        (b'{"seq":2,"record":{"worker":0', False),
        (
            b'{"seq":9,"written_at":"2026-10-29T00:00:00+00:00","record":{"worker":0,"value":2}}\n',
            True,
        ),
    ],
    ids=["torn-tail", "wrong-sequence"],
)
def test_jsonl_read_record_at_checks_the_record_boundary(
    tmp_path, suffix, bad_seq
):
    path = tmp_path / "events.jsonl"
    store = DurableJsonlStore(path, EventRecord)
    store.append(EventRecord(worker=0, value=1))
    with path.open("ab") as handle:
        handle.write(suffix)

    assert store.read_record_at(1).value == 1
    if bad_seq:
        with pytest.raises(JsonlCorruptionError, match="line 2 has seq 9"):
            store.read_record_at(2)
    else:
        assert store.read_record_at(2) is None


@pytest.mark.unit
def test_forward_cursor_positions_at_the_cursor_without_reading_the_head(
    tmp_path,
    monkeypatch,
):
    """A reconnect used to walk every byte before its cursor.

    Field case: one session's events.jsonl reached 146 MB / 76,282 records, so
    every SSE reconnect paid a full scan just to find where to start. The
    cursor now positions by walking backwards, and the whole-file read path is
    never touched.
    """

    path = tmp_path / "events.jsonl"
    writer = DurableJsonlStore(path, EventRecord)
    for value in range(1, 2001):
        writer.append(EventRecord(worker=1, value=value))
    store = DurableJsonlStore(path, EventRecord)
    full_reads = _spy_full_reads(monkeypatch)
    reader = store.forward_reader(1900)
    assert reader.offset > 0
    page = reader.read(limit=50)
    assert [item.value for item in page] == list(range(1901, 1951))
    assert not full_reads


@pytest.mark.unit
def test_forward_cursor_skips_noise_lines_without_parsing_them(
    tmp_path,
    monkeypatch,
) -> None:
    """A replay that is going to drop a line must not pay to parse it.

    The field's 146 MB stream is 91.8% streaming deltas, and parsing them only
    to discard them kept one SSE connection busy for over 90 seconds -- which
    is what made every other endpoint wait 7-23 seconds beside it.
    """

    path = tmp_path / "events.jsonl"
    writer = DurableJsonlStore(path, EventRecord)
    for value in range(1, 201):
        writer.append(EventRecord(worker=9 if value % 2 else 1, value=value))
    store = DurableJsonlStore(path, EventRecord)

    validated: list[int] = []
    real_validate = store._validate_record  # pylint: disable=protected-access

    def counting(record, *, line_number):
        validated.append(line_number)
        return real_validate(record, line_number=line_number)

    monkeypatch.setattr(store, "_validate_record", counting)

    reader = store.forward_reader(
        0,
        skip_noise=lambda raw: b'"worker": 9' in raw or b'"worker":9' in raw,
    )
    values: list[int] = []
    while page := reader.read(limit=25):
        values.extend(item.value for item in page)

    assert values == [value for value in range(1, 201) if value % 2 == 0]
    # Half the lines were classified off the bytes: never parsed, never
    # validated, and the cursor still advanced past every one of them.
    assert len(validated) == 100


@pytest.mark.unit
def test_a_skipped_line_with_a_broken_seq_still_fails_closed(
    tmp_path,
) -> None:
    """Skipping never buys permission to stop proving contiguity.

    A cheap seq read that disagrees with the cursor falls back to the full
    parse, so a renumbered or replaced line is reported exactly as the strict
    path reports it.
    """

    path = tmp_path / "events.jsonl"
    store = DurableJsonlStore(path, EventRecord)
    for value in range(1, 6):
        store.append(EventRecord(worker=9, value=value))

    raw = path.read_bytes()
    broken = raw.replace(b'"seq": 3', b'"seq": 9').replace(
        b'"seq":3',
        b'"seq":9',
    )
    assert broken != raw  # the fixture found the seq to break
    path.write_bytes(broken)

    reader = DurableJsonlStore(path, EventRecord).forward_reader(
        0,
        skip_noise=lambda line: b'"worker": 9' in line
        or b'"worker":9' in line,
    )
    with pytest.raises(JsonlCorruptionError):
        while reader.read(limit=2):
            pass
