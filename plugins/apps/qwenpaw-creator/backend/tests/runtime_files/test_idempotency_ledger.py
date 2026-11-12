# -*- coding: utf-8 -*-
"""The open-reservation ledger that keeps GC from deleting a live witness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.runtime_files import idempotency_ledger

pytestmark = pytest.mark.unit


def _record(root: Path, name: str, record_id: str, status: str) -> Path:
    path = root / f"{name}.json"
    payload = {"record_id": record_id, "status": status}
    if status == "COMPLETED":
        payload["response"] = {"ok": True}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_ledger_tracks_open_reservations(tmp_path) -> None:
    root = tmp_path / "idempotency"

    assert idempotency_ledger.open_transaction_ids(root) == frozenset()

    idempotency_ledger.mark_open(root, "txn-1")
    idempotency_ledger.mark_open(root, "txn-2")
    assert idempotency_ledger.open_transaction_ids(root) == frozenset(
        {"txn-1", "txn-2"},
    )

    idempotency_ledger.mark_closed(root, "txn-1")
    assert idempotency_ledger.open_transaction_ids(root) == frozenset(
        {"txn-2"},
    )


def test_a_damaged_ledger_is_rebuilt_from_the_records(tmp_path) -> None:
    """An unreadable set is rebuilt, not trusted and not left empty.

    Trusting a partial set would delete a transaction that an IN_PROGRESS
    reservation still needs as the only durable witness for its response.
    """

    root = tmp_path / "idempotency"
    root.mkdir(parents=True)
    _record(root, "rec-open", "txn-open", "IN_PROGRESS")
    _record(root, "rec-done", "txn-done", "COMPLETED")
    ledger = root / idempotency_ledger.LEDGER_FILE_NAME
    ledger.write_text("{corrupt", encoding="utf-8")

    assert idempotency_ledger.open_transaction_ids(root) == frozenset(
        {"txn-open"},
    )
    # The rebuild is persisted, so the next sweep reads instead of scanning.
    assert "txn-open" in json.loads(ledger.read_text(encoding="utf-8"))["open"]


def test_a_failed_ledger_write_invalidates_the_file(
    tmp_path,
    monkeypatch,
) -> None:
    """A half-written ledger is worse than none, so it is dropped instead.

    The reservation the failed write would have recorded is lost from the set,
    which is exactly the dangerous direction -- so the file is invalidated and
    the next reader rebuilds from the records themselves.
    """

    root = tmp_path / "idempotency"
    root.mkdir(parents=True)
    _record(root, "rec", "txn-open", "IN_PROGRESS")

    def boom(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(idempotency_ledger.os, "replace", boom)
    idempotency_ledger.mark_open(root, "txn-new")  # must not raise
    monkeypatch.undo()

    assert idempotency_ledger.open_transaction_ids(root) == frozenset(
        {"txn-open"},
    )


def test_an_unreadable_record_is_still_treated_as_open(tmp_path) -> None:
    """Fail toward keeping: a damaged head cannot prove the request settled."""

    root = tmp_path / "idempotency"
    root.mkdir(parents=True)
    (root / "rec.json").write_bytes(
        b'{"record_id": "txn-mangled", "status": "IN_PROGRESS"',
    )

    assert idempotency_ledger.open_transaction_ids(root) == frozenset(
        {"rec"},
    )
