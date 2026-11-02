# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,protected-access,unused-argument
"""The product runs the conversation repair itself, in a supervised child."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import gzip
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.media_files import task_scratch
from services.runtime_files import session_elision

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

    async def _fake(data_root: Path, scope: str) -> tuple[bool, str]:
        recorded.append([str(data_root), scope])
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

    async def _collapse(data_root: Path, scope: str) -> tuple[bool, str]:
        with path.open("r+") as handle:
            handle.truncate(session_elision.MIN_ELISION_BYTES)
        visited.append([str(data_root), scope])
        return True, ""

    monkeypatch.setattr(session_elision, "_run_one", _collapse)

    summary = _elide(tmp_path)

    assert summary.saved_bytes == 4096
    assert summary.projects == 1
    assert summary.files == 1
    # The script re-adds the directory prefix, so it is handed the bare id.
    assert visited == [[str(tmp_path), PROJECT.removeprefix("project-")]]


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

    async def _die(data_root: Path, scope: str) -> tuple[bool, str]:
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
    direct = [
        sys.executable,
        str(script),
        "--data-root",
        "/root",
        "--project-id",
        "abc",
    ]

    command = session_elision._spawn_argv(script, Path("/root"), "abc")

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
        task = asyncio.create_task(session_elision._run_one(tmp_path, "x"))
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


def test_a_confirmed_project_lifts_the_size_ceiling(
    tmp_path: Path,
    calls,
    monkeypatch: pytest.MonkeyPatch,
):
    """The ceiling protects people who never asked; this person asked.

    Confirming also accepts the measured cost: the collapse peaks at about
    four times the stream, which is why the automatic pass refuses it.
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

    assert summary.oversize_files == 0
    assert calls == [[str(tmp_path), PROJECT.removeprefix("project-")]]
