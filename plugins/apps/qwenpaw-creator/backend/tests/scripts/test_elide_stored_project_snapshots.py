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


def _staged(path: Path) -> tuple[Path, Path]:
    return (
        path.with_name(f".{path.name}.eliding"),
        path.with_name(f".{path.name}.eliding-plan.json"),
    )


def _message(seq: int, text: str, *, snapshot=False) -> CreatorMessageRecord:
    return CreatorMessageRecord(
        message_id=f"message-{seq}",
        project_id=PROJECT_ID,
        creator_session_id="session-1",
        conversation_id="conversation-1",
        message_seq=seq,
        role="tool" if snapshot else "user",
        content_parts=[{"type": "text", "text": text}],
        source="runtime_action_result" if snapshot else "user",
        metadata={"toolName": "read_project", "tool": "read_project"}
        if snapshot
        else {},
    )


def _snapshot(seq: int) -> CreatorMessageRecord:
    generation = seq - 1
    data = {
        "project": {
            "project_id": PROJECT_ID,
            "generation": generation,
            "timelines": {"items": {"tl:1": {"synopsis": FILLER}}},
        },
        "generation": generation,
        "etag": f"etag-{seq}",
    }
    return _message(seq, json.dumps(data), snapshot=True)


@pytest.fixture
def session(tmp_path, monkeypatch):
    path = (
        tmp_path
        / f"project-{PROJECT_ID}"
        / "runtime"
        / "sessions"
        / "session-1"
        / "messages.jsonl"
    )
    path.parent.mkdir(parents=True)
    store = DurableJsonlStore(path, CreatorMessageRecord)
    store.append(_message(1, "把第三集改成雨夜"))
    for seq in range(2, 5):
        store.append(_snapshot(seq))

    def run(*options: str) -> None:
        monkeypatch.setattr(
            "sys.argv",
            [
                "elide_stored_project_snapshots",
                "--data-root",
                str(tmp_path),
                *options,
            ],
        )
        assert elide.main() == 0

    return path, store, run


@pytest.mark.parametrize(
    "options",
    [("--dry-run",), ("--project-id", "someone-else")],
)
def test_reporting_and_project_scope_do_not_write(
    session, tmp_path, capsys, options
):
    path, _, run = session
    before = path.read_bytes()
    run(*options)
    assert path.read_bytes() == before
    assert not any(staged.exists() for staged in _staged(path))
    assert not (tmp_path / elide.BACKUP_SUBDIR).exists()
    if options == ("--dry-run",):
        assert "2 receipt(s)" in capsys.readouterr().out


def test_elision_preserves_identity_and_keeps_a_backup(
    session, tmp_path, capsys
):
    path, store, run = session
    before = path.read_bytes()
    original = store.read_records()
    candidate, plan = _staged(path)
    run()

    assert path.read_bytes() == before
    assert candidate.stat().st_size < len(before)
    rows = DurableJsonlStore(candidate, CreatorMessageRecord).read_records()
    assert [(row.message_seq, row.message_id) for row in rows] == [
        (row.message_seq, row.message_id) for row in original
    ]
    assert rows[0] == original[0]
    assert rows[-1] == original[-1]
    for row in rows[1:-1]:
        receipt = json.loads(row.content_parts[0].text)
        assert receipt["supersededProjectSnapshot"] is True
        assert (
            receipt["resultKind"] == driver._PROJECT_CHANGE_RECEIPT_RESULT_KIND
        )
        assert receipt["toolName"] == "read_project"
        assert "project" not in receipt

    payload = json.loads(plan.read_text(encoding="utf-8"))
    assert payload["target"] == str(path)
    assert payload["expected_last_seq"] == 4
    (backup,) = (tmp_path / elide.BACKUP_SUBDIR).rglob(
        f"*{elide.BACKUP_SUFFIX}"
    )
    assert gzip.decompress(backup.read_bytes()) == before

    assert (
        session_elision._apply_candidate_plan(plan) == f"swapped {path.name}"
    )
    collapsed = path.read_bytes()
    capsys.readouterr()
    run()
    assert path.read_bytes() == collapsed
    assert "nothing to collapse" in capsys.readouterr().out
    assert list(
        (tmp_path / elide.BACKUP_SUBDIR).rglob(f"*{elide.BACKUP_SUFFIX}")
    ) == [backup]


def test_a_file_growing_midflight_is_refused(session):
    path, store, run = session
    candidate, plan = _staged(path)
    run()
    store.append(_snapshot(9))
    updated = path.read_bytes()

    outcome = session_elision._apply_candidate_plan(plan)

    assert outcome == f"refused: {path.name} moved after the child read it"
    assert path.read_bytes() == updated
    assert not candidate.exists()
    assert not plan.exists()
