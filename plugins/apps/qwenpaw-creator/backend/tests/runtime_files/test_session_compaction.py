# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,protected-access,unused-argument
"""Compacting durable session records and filtering replay events."""

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.media_files import task_scratch
from services.runtime_files import session_elision
from services.runtime_files.jsonl_store import DurableJsonlStore
from services.runtime_files.models import (
    CreatorMessageRecord,
    MessageContentPart,
    SessionEventRecord,
)
from services.runtime_files.replay_filter import (
    REPLAY_SKIP_TYPES,
    filter_replay_events,
    replay_filter_enabled,
    replay_noise_line,
    replay_result_max_bytes,
)

pytestmark = pytest.mark.unit

PROJECT = "project-" + "a" * 32
SESSION = "session-1"
MESSAGE_FILE = f"{PROJECT}/runtime/sessions/{SESSION}/messages.jsonl"


def _conversation(root: Path, *, size: int) -> Path:
    path = root / MESSAGE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{}" + b"x" * max(0, size - 2))
    return path


def _services(root: Path) -> SimpleNamespace:
    return SimpleNamespace(root=root)


def _elide(root: Path):
    return asyncio.run(
        session_elision.elide_stored_conversations(_services(root)),
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch):
    """Record every child the service would start, and fake its result."""

    recorded: list[list[str]] = []

    async def _fake(
        data_root: Path,
        paths: list[Path],
        ceiling: int,
    ) -> tuple[bool, str]:
        recorded.append(
            [str(data_root), *[str(p) for p in paths], f"ceiling={ceiling}"],
        )
        return True, ""

    monkeypatch.setattr(session_elision, "_run_one", _fake)
    return recorded


def test_an_oversized_conversation_is_handed_to_a_child_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The server never holds the stream -- a child reads it, or nothing does.

    Field note: one Project carried 606 MB of conversation across 2952
    messages, and reading that whole inside the process serving it is what
    ran the customer out of memory.
    """

    path = _conversation(
        tmp_path,
        size=session_elision.MIN_ELISION_BYTES + 4096,
    )
    visited: list[list[str]] = []

    async def _collapse(
        data_root: Path,
        paths: list[Path],
        ceiling: int,
    ) -> tuple[bool, str]:
        with path.open("r+") as handle:
            handle.truncate(session_elision.MIN_ELISION_BYTES)
        visited.append([str(data_root), *[str(p) for p in paths]])
        return True, ""

    monkeypatch.setattr(session_elision, "_run_one", _collapse)

    summary = _elide(tmp_path)

    assert summary.saved_bytes == 4096
    assert summary.projects == 1
    assert summary.files == 1
    # The candidate list goes to the child verbatim -- not a bare Project
    # scope, which would let an oversize sibling in the same directory be
    # dragged into the same read.
    assert visited == [[str(tmp_path), str(path)]]


def test_a_project_is_visited_once_until_it_grows_again(
    tmp_path: Path,
    calls,
):
    """The ledger keeps a boot cheap on history that cannot shrink."""

    _conversation(
        tmp_path,
        size=session_elision.MIN_ELISION_BYTES + 4096,
    )

    _elide(tmp_path)
    assert len(calls) == 1

    _elide(tmp_path)
    assert len(calls) == 1

    # New turns arrive: the file outgrows where the last pass left it.
    path = tmp_path / MESSAGE_FILE
    path.write_bytes(
        b"{}" + b"x" * (session_elision.MIN_ELISION_BYTES + 8192),
    )
    _elide(tmp_path)
    assert len(calls) == 2


def test_a_small_conversation_never_pays_for_a_process(
    tmp_path: Path,
    calls,
):
    _conversation(tmp_path, size=4096)

    summary = _elide(tmp_path)

    assert summary.projects == 0
    assert calls == []


def test_the_env_switch_leaves_stored_history_alone(
    tmp_path: Path,
    calls,
    monkeypatch: pytest.MonkeyPatch,
):
    _conversation(
        tmp_path,
        size=session_elision.MIN_ELISION_BYTES + 4096,
    )
    monkeypatch.setenv("CREATOR_MESSAGE_ELISION_ENABLED", "0")

    summary = _elide(tmp_path)

    assert summary.files == 0
    assert calls == []


def test_a_failed_child_is_not_retried_until_the_file_grows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An attempt that died leaves a floor, so a boot does not chase it.

    Without it, every restart would re-read the one file large enough to kill
    the attempt -- the exact cost this whole path exists to avoid.
    """

    path = _conversation(
        tmp_path,
        size=session_elision.MIN_ELISION_BYTES + 4096,
    )

    async def _die(
        data_root: Path,
        paths: list[Path],
        ceiling: int,
    ) -> tuple[bool, str]:
        return False, "Killed"

    monkeypatch.setattr(session_elision, "_run_one", _die)

    first = _elide(tmp_path)
    second = _elide(tmp_path)

    assert first.failures == [f"{PROJECT}: Killed"]
    assert first.saved_bytes == 0
    assert second.failures == []
    assert path.stat().st_size == session_elision.MIN_ELISION_BYTES + 4096


def test_the_id_handed_to_the_script_resolves_the_very_directory(
    tmp_path: Path,
):
    """The seam between this service and the script, pinned to the script.

    Project directories are named by the full id, while the script takes an id
    and prepends ``project-`` itself.  Getting that wrong would aim the repair
    at a directory that does not exist and still report a clean run.
    """

    path = _conversation(tmp_path, size=2048)
    module = _script_module()

    found = module._session_files(
        tmp_path,
        PROJECT.removeprefix("project-"),
    )
    assert found == [path]
    # The backups sit outside every Project, so the profile has to count them
    # under the very name the script writes them with.
    assert module.BACKUP_SUBDIR == task_scratch.ELISION_BACKUP_DIR_NAME


def _script_module():
    """Load the repair script the product would spawn, in this process."""

    spec = importlib.util.spec_from_file_location(
        "elide_stored_project_snapshots",
        Path(session_elision.__file__).resolve().parents[2]
        / session_elision.ELISION_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The script declares dataclasses, which resolve their module by name.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        del sys.modules[spec.name]


def test_an_oversize_stream_is_reported_instead_of_risked(
    tmp_path: Path,
    calls,
    monkeypatch: pytest.MonkeyPatch,
):
    """A 606 MB stream costs its reader 2.3 GB at the peak.

    Measured, not guessed: collapsing the field Project's stream peaked at
    2,314 MB, and that bill lands in the same container as the app.  Anything
    above the ceiling is counted and left exactly where it is.
    """

    monkeypatch.setenv(
        "CREATOR_MESSAGE_ELISION_MAX_BYTES",
        str(1024 * 1024),
    )
    _conversation(
        tmp_path,
        size=session_elision.MIN_ELISION_BYTES + 4096,
    )

    summary = _elide(tmp_path)

    assert summary.oversize_files == 1
    assert summary.oversize_bytes == session_elision.MIN_ELISION_BYTES + 4096
    assert summary.files == 0
    assert calls == []


def test_the_child_is_offered_to_the_oom_killer_before_the_app():
    """When the container limit is reached, the cleanup should lose."""

    script = Path("/x/elide.py")
    candidate = Path("/root/project-abc/runtime/sessions/s/messages.jsonl")
    direct = [
        sys.executable,
        str(script),
        "--data-root",
        "/root",
        "--session-file",
        str(candidate),
        "--max-bytes",
        "67108864",
    ]

    command = session_elision._spawn_argv(
        script,
        Path("/root"),
        [candidate],
        64 * 1024 * 1024,
    )

    if sys.platform == "linux":
        assert command[:2] == ["/bin/sh", "-c"]
        assert command[3] == "creator-elision"
        assert command[4] == session_elision.CHILD_OOM_SCORE_ADJ
        assert command[5:] == direct
    else:
        assert command == direct


def test_a_second_collapse_replaces_the_backup_it_no_longer_needs(
    tmp_path: Path,
):
    """A fixed backup name made a second collapse impossible.

    The repair now runs from the product on every boot, so refusing to write
    over an existing backup meant a stream that grew back could never be
    trimmed again -- and a plain copy of it cost more than the collapse saved.
    """

    module = _script_module()
    path = _conversation(tmp_path, size=4096)
    original = path.read_bytes()
    backup_root = tmp_path / module.BACKUP_SUBDIR

    first = module._backup(path, tmp_path, backup_root)
    second = module._backup(path, tmp_path, backup_root)

    assert first != second
    assert not first.exists()
    assert gzip.open(second, "rb").read() == original
    assert sorted(backup_root.rglob("*.gz")) == [second]


def test_cancelling_the_pass_kills_its_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown must not orphan a child that is rewriting a session file.

    The collapse holds 2.3 GB and a temp file for the duration of a pass; a
    process that survives its parent would keep rewriting bytes in a data root
    nobody is serving any more.
    """

    import subprocess
    import time

    slow = tmp_path / "slow-child.py"
    slow.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(session_elision, "ELISION_SCRIPT", slow)

    async def _go() -> None:
        task = asyncio.create_task(
            session_elision._run_one(tmp_path, [tmp_path / "unused.jsonl"], 1),
        )
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_go())
    time.sleep(0.5)

    # pgrep exits 1 when nothing matches, which is exactly the outcome wanted.
    probe = subprocess.run(
        ["pgrep", "-f", str(slow)],
        capture_output=True,
        check=False,
    )
    assert probe.returncode == 1


def test_the_size_ceiling_stands_even_for_a_confirmed_project(
    tmp_path: Path,
    calls,
    monkeypatch: pytest.MonkeyPatch,
):
    """Confirming reclaim does not buy a 2.3 GB peak in the background.

    The confirmation lifts the deletion cap, where a person is deciding about
    their own bytes.  It does not lift the collapse ceiling: that one exists
    because the reader pays ~4x the stream in resident memory, and a background
    pass must never be able to spend that on its own authority.
    """

    monkeypatch.setenv("CREATOR_MESSAGE_ELISION_MAX_BYTES", str(1024 * 1024))
    _conversation(
        tmp_path,
        size=session_elision.MIN_ELISION_BYTES + 4096,
    )
    task_scratch.confirm_project_reclaim(
        tmp_path / PROJECT,
        now=datetime.now(timezone.utc),
    )

    summary = _elide(tmp_path)

    assert summary.oversize_files == 1
    assert summary.files == 0
    assert calls == []


def test_a_small_candidate_does_not_drag_an_oversize_sibling_into_the_child(
    tmp_path: Path,
    calls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child receives the parent's filtered list, not a Project scope.

    Regression for review #243 P1: with ``--project-id`` the script
    enumerated every non-empty ``messages.jsonl`` under the Project and
    read each one whole, so a 9 MiB candidate sitting next to a 606 MiB
    stream collapsed both in the same child -- the ceiling the parent
    had already applied was silently bypassed. Passing ``--session-file``
    verbatim plus ``--max-bytes`` closes the window, and the child
    re-checks size before opening a stream in case it grew in between.
    """

    monkeypatch.setenv(
        "CREATOR_MESSAGE_ELISION_MAX_BYTES",
        str(session_elision.MIN_ELISION_BYTES + 10000),
    )
    small = tmp_path / MESSAGE_FILE
    small.parent.mkdir(parents=True, exist_ok=True)
    small.write_bytes(
        b"{}" + b"x" * (session_elision.MIN_ELISION_BYTES + 4096 - 2),
    )
    oversize = (
        tmp_path
        / PROJECT
        / "runtime"
        / "sessions"
        / "session-2"
        / "messages.jsonl"
    )
    oversize.parent.mkdir(parents=True, exist_ok=True)
    oversize.write_bytes(
        b"{}" + b"x" * (session_elision.MIN_ELISION_BYTES + 20000 - 2),
    )

    summary = _elide(tmp_path)

    # The parent classified them; the child must see only the small one.
    assert summary.oversize_files == 1
    assert summary.oversize_bytes == session_elision.MIN_ELISION_BYTES + 20000
    assert len(calls) == 1
    listed = calls[0]
    assert str(small) in listed
    assert str(oversize) not in listed


def test_the_child_refuses_a_file_that_grew_past_the_ceiling(
    tmp_path: Path,
) -> None:
    """``--session-file`` is trusted only while ``--max-bytes`` still holds.

    The parent listed this path before spawning. Between then and the
    child's ``_decide`` a stream can grow into the danger zone; the child
    must not read it just because it was named.
    """

    module = _script_module()
    path = _conversation(tmp_path, size=4096)

    args = SimpleNamespace(
        session_files=[path],
        max_bytes=1024,
        project_id=None,
    )

    assert module._selected_files(args, tmp_path) == []

    args.max_bytes = 8192
    assert module._selected_files(args, tmp_path) == [
        path.resolve(strict=False),
    ]


def _message(seq: int) -> CreatorMessageRecord:
    return CreatorMessageRecord(
        message_id=f"msg-{seq}",
        project_id="project-1",
        creator_session_id="session-1",
        conversation_id="conversation-1",
        message_seq=seq,
        role="user",
        content_parts=[MessageContentPart(type="text", text=f"text {seq}")],
    )


def _live_store(tmp_path):
    path = tmp_path / "messages.jsonl"
    store = DurableJsonlStore(path, CreatorMessageRecord)
    for seq in (1, 2, 3):
        store.append(_message(seq))
    return path, store


def _stage(tmp_path, path, seqs):
    candidate = path.with_name(".messages.jsonl.eliding")
    writer = DurableJsonlStore(candidate, CreatorMessageRecord)
    for seq in seqs:
        writer.append(_message(seq))
    plan = path.with_name(".messages.jsonl.eliding-plan.json")
    plan.write_text(
        json.dumps(
            {
                "target": str(path),
                "candidate": str(candidate),
                "expected_last_seq": 3,
                "sha256": session_elision.sha256_of_file(candidate),
                "bytes": candidate.stat().st_size,
            },
        ),
        encoding="utf-8",
    )
    return candidate, plan


def test_a_message_appended_after_the_child_read_survives_the_swap(
    tmp_path,
) -> None:
    """The deterministic regression review #243 asked for.

    The child staged its collapse against seq 3; the parent accepted a fourth
    message before applying the plan. Swapping then would delete confirmed
    input and hand seq 4 to the next append, so the plan must be refused and
    the live file left exactly as the parent wrote it.
    """

    path, store = _live_store(tmp_path)
    candidate, plan = _stage(tmp_path, path, [1])

    store.append(_message(4))

    note = session_elision._apply_candidate_plan(plan)

    assert note.startswith("refused")
    assert [item.seq for item in store.read_all()] == [1, 2, 3, 4]
    assert not candidate.exists()
    assert not plan.exists()


def test_an_unchanged_live_file_is_swapped_to_the_candidate(tmp_path) -> None:
    path, store = _live_store(tmp_path)
    candidate, plan = _stage(tmp_path, path, [1])

    note = session_elision._apply_candidate_plan(plan)

    assert note.startswith("swapped")
    assert [item.seq for item in store.read_all()] == [1]
    assert not candidate.exists()
    assert not plan.exists()


def test_a_candidate_changed_on_disk_is_refused(tmp_path) -> None:
    path, store = _live_store(tmp_path)
    candidate, plan = _stage(tmp_path, path, [1])
    candidate.write_bytes(candidate.read_bytes() + b"tampered\n")

    note = session_elision._apply_candidate_plan(plan)

    assert note.startswith("refused")
    assert [item.seq for item in store.read_all()] == [1, 2, 3]
    assert not plan.exists()


REPLAY_PROJECT = "project-1"


def _event(seq: int, event_type: str, payload: dict | None = None):
    return SessionEventRecord(
        event_id=f"event-{seq}",
        project_id=REPLAY_PROJECT,
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
        _event(1, "agent.message_delta", {"runId": "run-live", "delta": "前半"}),
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

    Review #243 r4548690038 follow-up: matching
    ``"event_type":"<type>","message_id":`` anywhere on the line was
    still not an outer anchor -- a result that copies a whole event
    record reproduces both keys in canonical order and would drop the
    lifecycle envelope. The classifier now reads the *record's own*
    ``event_type`` at depth 1, so any embedded event-shaped object --
    whether serialized as a nested dict or as a JSON string -- can no
    longer trigger the drop. Adjacent fields prove nothing about
    hierarchy; depth does.
    """

    def _envelope(record: dict, seq: int) -> bytes:
        return json.dumps(
            {
                "record": record,
                "seq": seq,
                "written_at": "2026-11-13T00:00:00+00:00",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    embedded_event = {
        "actor": "file_agent_runtime",
        "created_at": "2026-11-13T00:00:00+00:00",
        "creator_session_id": SESSION,
        "event_id": "event-nested",
        "event_seq": 99,
        # The reviewer's strengthened probe: an inner object that
        # reproduces the very adjacent pair the previous fix anchored
        # on, produced simply by copying a message into the tool output.
        "event_type": "agent.message_delta",
        "message_id": "nested-example",
        "payload": {"runId": "run-1", "delta": "nested"},
        "project_id": REPLAY_PROJECT,
        "round_id": None,
    }

    # Case 1: the embedded event is a real nested object (dict), so
    # canonical sort_keys emits ``event_seq, event_type, message_id``
    # with no escaping -- the pair the loose matcher keyed on.
    nested_object = _envelope(
        {
            "actor": "file_agent_runtime",
            "created_at": "2026-11-13T00:00:00+00:00",
            "creator_session_id": SESSION,
            "event_id": "event-2",
            "event_seq": 2,
            "event_type": "subagent.tool_completed",
            "message_id": None,
            "payload": {"runId": "run-1", "result": embedded_event},
            "project_id": REPLAY_PROJECT,
            "round_id": None,
        },
        seq=2,
    )
    assert replay_noise_line(nested_object) is False, (
        "a tool_completed whose result embeds a full event record must "
        "not be classified as streaming noise"
    )

    # Case 2: the same embedded event serialized as a JSON string.
    nested_string = _envelope(
        {
            "actor": "file_agent_runtime",
            "created_at": "2026-11-13T00:00:00+00:00",
            "creator_session_id": SESSION,
            "event_id": "event-2b",
            "event_seq": 2,
            "event_type": "subagent.tool_completed",
            "message_id": None,
            "payload": {
                "runId": "run-1",
                "result": json.dumps(embedded_event, sort_keys=True),
            },
            "project_id": REPLAY_PROJECT,
            "round_id": None,
        },
        seq=2,
    )
    assert replay_noise_line(nested_string) is False

    # A real outer delta is still classified as noise.
    outer_delta = _envelope(
        {
            "actor": "file_agent_runtime",
            "created_at": "2026-11-13T00:00:00+00:00",
            "creator_session_id": SESSION,
            "event_id": "event-3",
            "event_seq": 3,
            "event_type": "agent.message_delta",
            "message_id": "msg-1",
            "payload": {"runId": "run-1", "delta": "hi"},
            "project_id": REPLAY_PROJECT,
            "round_id": None,
        },
        seq=3,
    )
    assert replay_noise_line(outer_delta) is True
