# -*- coding: utf-8 -*-
"""The history-collapse script must store exactly what the runtime projects."""

# pylint: disable=protected-access

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from services.file_agent_runtime import driver
from services.runtime_files import session_elision
from services.runtime_files.jsonl_store import DurableJsonlStore
from services.runtime_files.models import CreatorMessageRecord

from scripts import elide_stored_project_snapshots as elide

pytestmark = pytest.mark.unit

PROJECT_ID = "project-under-test"
FILLER = "x" * 4096


def _candidate(path: Path) -> Path:
    """The staged collapse the script leaves beside the live file."""

    return path.with_name(f".{path.name}.eliding")


def _plan(path: Path) -> Path:
    """The swap plan: last seq the script read + candidate digest."""

    return path.with_name(f".{path.name}.eliding-plan.json")


def _snapshot_text(generation: int, etag: str) -> str:
    return json.dumps(
        {
            "project": {
                "project_id": PROJECT_ID,
                "generation": generation,
                "timelines": {"items": {"tl:1": {"synopsis": FILLER}}},
            },
            "generation": generation,
            "etag": etag,
        },
    )


def _snapshot_message(seq: int, generation: int) -> CreatorMessageRecord:
    return CreatorMessageRecord(
        message_id=f"message-{seq}",
        project_id=PROJECT_ID,
        creator_session_id="session-1",
        conversation_id="conversation-1",
        message_seq=seq,
        role="tool",
        content_parts=[
            {
                "type": "text",
                "text": _snapshot_text(generation, f"etag-{seq}"),
            },
        ],
        source="runtime_action_result",
        metadata={"toolName": "read_project", "tool": "read_project"},
    )


def _seed_session(tmp_path: Path, count: int) -> Path:
    directory = (
        tmp_path
        / f"project-{PROJECT_ID}"
        / "runtime"
        / "sessions"
        / "session-1"
    )
    directory.mkdir(parents=True)
    path = directory / "messages.jsonl"
    store = DurableJsonlStore(path, CreatorMessageRecord)
    store.append(
        CreatorMessageRecord(
            message_id="message-0",
            project_id=PROJECT_ID,
            creator_session_id="session-1",
            conversation_id="conversation-1",
            message_seq=1,
            role="user",
            content_parts=[{"type": "text", "text": "把第三集改成雨夜"}],
            source="user",
        ),
    )
    for index in range(count):
        store.append(_snapshot_message(seq=index + 2, generation=index + 1))
    return path


def _read(path: Path) -> list[CreatorMessageRecord]:
    return DurableJsonlStore(path, CreatorMessageRecord).read_records()


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["elide_stored_project_snapshots", *argv])
    return elide.main()


def test_dry_run_reports_without_writing(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    path = _seed_session(tmp_path, 3)
    before = path.read_bytes()

    assert _run(monkeypatch, "--data-root", str(tmp_path), "--dry-run") == 0

    assert path.read_bytes() == before
    assert "2 receipt(s)" in capsys.readouterr().out
    assert not (tmp_path / elide.BACKUP_SUBDIR).exists()


def test_superseded_snapshots_become_the_receipts_the_runtime_already_shows(
    tmp_path,
    monkeypatch,
) -> None:
    path = _seed_session(tmp_path, 3)
    original = _read(path)

    assert _run(monkeypatch, "--data-root", str(tmp_path)) == 0

    # The script stages a candidate and never swaps the live file itself:
    # the runtime parent owns the append path's write boundary and does
    # the swap after re-verifying the plan (review #243 P1). The durable
    # file is therefore untouched here, and the collapsed shape lives in
    # the candidate.
    assert [item.message_seq for item in _read(path)] == [
        item.message_seq for item in original
    ]
    collapsed = _read(_candidate(path))
    assert [item.message_seq for item in collapsed] == [
        item.message_seq for item in original
    ]
    assert [item.message_id for item in collapsed] == [
        item.message_id for item in original
    ]
    # The user turn is untouched; every superseded echo is a receipt.
    assert collapsed[0].content_parts[0].text == "把第三集改成雨夜"
    receipts = [item.content_parts[0].text for item in collapsed[1:-1]]
    for receipt in receipts:
        payload = json.loads(receipt)
        assert payload["supersededProjectSnapshot"] is True
        assert (
            payload["resultKind"] == driver._PROJECT_CHANGE_RECEIPT_RESULT_KIND
        )
        assert payload["toolName"] == "read_project"
        assert "project" not in payload
    # The newest echo stays the authoritative materialized snapshot.
    assert "resultKind" not in collapsed[-1].metadata
    assert json.loads(collapsed[-1].content_parts[0].text)["project"]
    assert _candidate(path).stat().st_size < sum(
        len(_snapshot_text(index, f"etag-{index}")) for index in range(1, 4)
    )
    # The plan names what the parent must re-verify before swapping.
    plan = json.loads(_plan(path).read_text(encoding="utf-8"))
    assert plan["target"] == str(path)
    assert plan["expected_last_seq"] == 4


def test_a_collapsed_file_is_left_alone_on_the_next_run(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    path = _seed_session(tmp_path, 3)
    assert _run(monkeypatch, "--data-root", str(tmp_path)) == 0
    # The parent side of the protocol: re-verify the plan and swap the
    # staged candidate in. Only after that swap does "already collapsed"
    # become observable to the next script run.
    assert session_elision._apply_candidate_plan(_plan(path)) == (
        f"swapped {path.name}"
    )
    after_first = path.read_bytes()
    backups = sorted(
        (tmp_path / elide.BACKUP_SUBDIR).rglob(f"*{elide.BACKUP_SUFFIX}"),
    )
    capsys.readouterr()

    assert _run(monkeypatch, "--data-root", str(tmp_path)) == 0

    assert path.read_bytes() == after_first
    assert "nothing to collapse" in capsys.readouterr().out
    assert len(backups) == 1


def test_the_original_file_is_backed_up_before_the_swap(
    tmp_path,
    monkeypatch,
) -> None:
    path = _seed_session(tmp_path, 2)
    before = path.read_bytes()

    assert _run(monkeypatch, "--data-root", str(tmp_path)) == 0

    # The backup is compressed and timestamped now: one copy per pass, named
    # by the moment it was taken, and it must still give the bytes back. It
    # is taken before the candidate is even staged, so no matter when the
    # parent swaps, the pre-collapse bytes are already aside.
    backups = sorted(
        (tmp_path / elide.BACKUP_SUBDIR).rglob(f"*{elide.BACKUP_SUFFIX}"),
    )
    assert len(backups) == 1
    with gzip.open(backups[0], "rb") as handle:
        assert handle.read() == before
    # The script itself never rewrites the live file -- the staged
    # candidate carries the collapse and waits for the parent's swap.
    assert path.read_bytes() == before
    assert _candidate(path).read_bytes() != before


def test_a_second_project_is_outside_the_requested_scope(
    tmp_path,
    monkeypatch,
) -> None:
    path = _seed_session(tmp_path, 2)
    before = path.read_bytes()

    assert (
        _run(
            monkeypatch,
            "--data-root",
            str(tmp_path),
            "--project-id",
            "someone-else",
        )
        == 0
    )

    assert path.read_bytes() == before


def test_a_file_growing_midflight_is_refused(
    tmp_path,
    monkeypatch,
) -> None:
    """A stream that moves between staging and swap is never overwritten.

    The growth check moved to the parent with the two-phase protocol: the
    script records the last seq it read in the plan, and the swap re-reads
    the live tail inside the append path's own lock (review #243 P1). A
    message appended after the script ran makes the plan stale; the parent
    refuses, discards the candidate, and says why.
    """

    path = _seed_session(tmp_path, 2)
    assert _run(monkeypatch, "--data-root", str(tmp_path)) == 0
    plan = _plan(path)
    assert plan.exists()

    store = DurableJsonlStore(path, CreatorMessageRecord)
    store.append(_snapshot_message(seq=9, generation=9))

    outcome = session_elision._apply_candidate_plan(plan)

    assert outcome == f"refused: {path.name} moved after the child read it"
    # The refusal leaves the durable file intact -- including the message
    # that arrived mid-flight -- and cleans both staged artifacts up.
    assert [item.message_seq for item in _read(path)][-1] == 9
    assert not _candidate(path).exists()
    assert not plan.exists()
