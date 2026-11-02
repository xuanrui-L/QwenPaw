# -*- coding: utf-8 -*-
"""The process-wide media gate: one permit set, and no orphaned group."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from services.media_files import media_gate

pytestmark = pytest.mark.unit

FAKE_SLEEP_SECONDS = 0.15


@pytest.fixture(autouse=True)
def _fresh_gate():
    """The gate is built once from the environment; tests rebuild it."""

    media_gate.reset_gate()
    yield
    media_gate.reset_gate()


def _child_script(body: str) -> list[str]:
    return [sys.executable, "-c", body]


def test_gate_never_runs_more_media_processes_than_slots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Six spawns under a two-slot gate overlap in pairs, never more."""

    monkeypatch.setenv(media_gate.ENV_SLOTS, "2")
    media_gate.reset_gate()
    ledger = tmp_path / "overlaps.jsonl"
    ledger.write_text("", encoding="utf-8")

    _spawn_gated_runs(runs=6, ledger=ledger)

    assert _max_overlap(ledger) <= 2


_LEDGER_BODY = (
    "import json, sys, time\n"
    "path, seconds = sys.argv[1], float(sys.argv[2])\n"
    "for event in ('start', 'end'):\n"
    "    with open(path, 'a', encoding='utf-8') as handle:\n"
    "        handle.write(json.dumps(\n"
    "            {'event': event, 'at': time.time(), 'id': sys.argv[3]}\n"
    "        ) + chr(10))\n"
    "    if event == 'start':\n"
    "        time.sleep(seconds)\n"
)


def _max_overlap(ledger) -> int:
    """Largest number of fake media processes that were live at once."""

    events = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        events.append((row["at"], 1 if row["event"] == "start" else -1))
    # An end at the same instant as a start is not an overlap.
    events.sort(key=lambda item: (item[0], item[1]))
    live = peak = 0
    for _, delta in events:
        live += delta
        peak = max(peak, live)
    return peak


def test_active_count_tracks_the_permits_held() -> None:
    assert media_gate.active_count() == 0
    with media_gate.slot("first"):
        assert media_gate.active_count() == 1
    assert media_gate.active_count() == 0


def test_a_slot_is_never_taken_twice_by_the_same_thread() -> None:
    """A nested acquire would deadlock a saturated gate against its holder."""

    with media_gate.slot("outer"):
        with pytest.raises(RuntimeError, match="nests inside"):
            with media_gate.slot("inner"):
                pass
    assert media_gate.active_count() == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups only")
def test_timeout_takes_the_whole_group_down(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run only kills its direct child; the gate kills the group."""

    monkeypatch.setenv(media_gate.ENV_SLOTS, "1")
    media_gate.reset_gate()
    marker = tmp_path / "grandchild.pid"
    body = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        ")\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(str(child.pid))\n"
        "time.sleep(30)\n"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        media_gate.run(
            _child_script(body) + [str(marker)],
            timeout=0.5,
            check=False,
        )

    grandchild = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = subprocess.run(
            ["ps", "-o", "state=", "-p", str(grandchild)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        # A zombie has already given its memory back; only a live process
        # would still show up as running, sleeping or stopped.
        if not state or state[0] not in "RSDT":
            return
        time.sleep(0.05)
    pytest.fail(
        f"grandchild {grandchild} survived the timeout: state {state!r}",
    )


def test_slot_count_prefers_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(media_gate.ENV_SLOTS, "7")
    assert media_gate.slot_count() == 7
    monkeypatch.setenv(media_gate.ENV_SLOTS, "not-a-number")
    monkeypatch.setattr(media_gate, "container_memory_mb", lambda: None)
    assert media_gate.slot_count() == media_gate.MAX_SLOTS


def test_slot_count_shrinks_with_a_tight_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(media_gate.ENV_SLOTS, raising=False)
    monkeypatch.setattr(
        media_gate,
        "container_memory_mb",
        lambda: media_gate.DEFAULT_FFMPEG_PEAK_MB * 2.9,
    )
    assert media_gate.slot_count() == 2
    monkeypatch.setattr(media_gate, "container_memory_mb", lambda: 10.0)
    assert media_gate.slot_count() == media_gate.MIN_SLOTS


def test_run_forwards_the_subprocess_contract(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same return object, same exceptions, same captured streams."""

    monkeypatch.setenv(media_gate.ENV_SLOTS, "2")
    media_gate.reset_gate()
    marker = tmp_path / "ok.txt"

    completed = media_gate.run(
        _child_script(
            "import sys\n"
            "open(sys.argv[1], 'w', encoding='utf-8').write('written')\n"
            "print('to-stdout')\n",
        )
        + [str(marker)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == "to-stdout\n"
    assert marker.read_text(encoding="utf-8") == "written"

    with pytest.raises(subprocess.CalledProcessError) as failure:
        media_gate.run(
            _child_script("import sys; sys.exit(3)"),
            capture_output=True,
            timeout=30,
            check=True,
        )
    assert failure.value.returncode == 3


def _spawn_gated_runs(runs: int, ledger) -> None:
    """Run *runs* fake media processes through the gate from own threads."""

    results: list[str] = []

    def one(index: int) -> None:
        try:
            media_gate.run(
                _child_script(_LEDGER_BODY)
                + [str(ledger), str(FAKE_SLEEP_SECONDS), str(index)],
                timeout=30,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - reported through results
            results.append(f"{type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=one, args=(index,)) for index in range(runs)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not results, results
