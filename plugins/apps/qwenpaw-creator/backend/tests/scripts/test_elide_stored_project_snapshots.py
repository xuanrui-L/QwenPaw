# -*- coding: utf-8 -*-
"""The history-collapse script must store exactly what the runtime projects."""

# pylint: disable=protected-access

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.file_agent_runtime import driver
from services.runtime_files.jsonl_store import DurableJsonlStore
from services.runtime_files.models import CreatorMessageRecord

from scripts import elide_stored_project_snapshots as elide

pytestmark = pytest.mark.unit

PROJECT_ID = "project-under-test"
FILLER = "x" * 4096


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

    collapsed = _read(path)
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
    assert path.stat().st_size < sum(
        len(_snapshot_text(index, f"etag-{index}")) for index in range(1, 4)
    )


def test_a_collapsed_file_is_left_alone_on_the_next_run(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    path = _seed_session(tmp_path, 3)
    assert _run(monkeypatch, "--data-root", str(tmp_path)) == 0
    after_first = path.read_bytes()
    backups = sorted((tmp_path / elide.BACKUP_SUBDIR).rglob("messages.jsonl"))
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

    backup = tmp_path / elide.BACKUP_SUBDIR / path.relative_to(tmp_path)
    assert backup.read_bytes() == before
    assert path.read_bytes() != before


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


def test_a_file_growing_midflight_is_refused(tmp_path) -> None:
    path = _seed_session(tmp_path, 2)
    decision = elide._decide(path)
    assert decision is not None and decision.changed
    store = DurableJsonlStore(path, CreatorMessageRecord)
    store.append(_snapshot_message(seq=9, generation=9))

    with pytest.raises(RuntimeError, match="grew while being collapsed"):
        elide._rewrite(decision)

    # The refusal leaves the durable file and its lock directory intact.
    assert [item.message_seq for item in _read(path)][-1] == 9
    assert not path.with_name(f".{path.name}.eliding").exists()
