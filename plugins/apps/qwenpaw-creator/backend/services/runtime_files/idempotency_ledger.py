# -*- coding: utf-8 -*-
"""Which idempotency reservations have not settled, without a full search.

Transaction GC needs one answer before it deletes: does an IN_PROGRESS
reservation still rely on this transaction as its durable witness? A record is
addressed by (owner, scope, key) and its file name is a hash of those, so a
transaction id cannot be traced back to the record that references it.
Searching every record on each sweep would cost the project's whole command
history -- on the exact path this branch has been working to make cheaper.

So the paths that already touch a record keep a small ledger, and the ledger
fails in the safe direction:

- missing, damaged, or explicitly invalidated: rebuilt by scanning the records,
  which reads each file's head and parses in full only those still marked
  IN_PROGRESS;
- a stale entry left by a crash: the transaction survives one more sweep,
  which costs disk and never correctness;
- a lost entry is the dangerous one, because it would delete a witness an
  IN_PROGRESS request still needs. Any ledger write that fails therefore
  invalidates the file, so the next reader rebuilds from the records rather
  than trusting a set that may be missing something.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LEDGER_FILE_NAME = "_open-ledger.json"
LEDGER_SCHEMA = 1
_IN_PROGRESS_MARKERS = (
    b'"status": "IN_PROGRESS"',
    b'"status":"IN_PROGRESS"',
)
_SCAN_HEAD_BYTES = 4096


def _ledger_path(root: Path) -> Path:
    return root / LEDGER_FILE_NAME


def _read_ledger(root: Path) -> dict[str, str] | None:
    """The open set, or None when the ledger cannot be trusted."""

    try:
        raw = json.loads(_ledger_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != LEDGER_SCHEMA:
        return None
    entries = raw.get("open")
    if not isinstance(entries, dict):
        return None
    return {str(key): str(value) for key, value in entries.items()}


def _invalidate(root: Path) -> None:
    """Drop the ledger so the next reader rebuilds it from the records."""

    try:
        _ledger_path(root).unlink()
    except OSError:
        pass


def _write_ledger(root: Path, open_ids: dict[str, str]) -> bool:
    payload = {"schema": LEDGER_SCHEMA, "open": open_ids}
    target = _ledger_path(root)
    temp = target.with_name(f".{target.name}.tmp")
    try:
        root.mkdir(parents=True, exist_ok=True)
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp, target)
        return True
    except OSError as exc:
        logger.warning(
            "idempotency ledger write failed for %s: %s; invalidating it",
            root,
            exc,
        )
        _invalidate(root)
        return False
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def _record_id_of(path: Path) -> str | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(raw, dict) and isinstance(raw.get("record_id"), str):
        return raw["record_id"]
    return None


def _scan_open_ids(root: Path) -> dict[str, str]:
    """Rebuild the open set from the records themselves.

    Settled history is the overwhelming majority and costs a short read here:
    only a file whose head still carries the IN_PROGRESS marker is parsed, and
    one that cannot be parsed is kept anyway, which errs toward preserving the
    transaction it may have witnessed.
    """

    found: dict[str, str] = {}
    try:
        paths = sorted(root.glob("*.json"))
    except OSError:
        return found
    for path in paths:
        if path.name == LEDGER_FILE_NAME:
            continue
        try:
            with path.open("rb") as handle:
                head = handle.read(_SCAN_HEAD_BYTES)
        except OSError:
            continue
        if not any(marker in head for marker in _IN_PROGRESS_MARKERS):
            continue
        found[_record_id_of(path) or path.stem] = datetime.now(
            timezone.utc,
        ).isoformat()
    return found


def open_transaction_ids(root: Path) -> frozenset[str]:
    """Reservation ids that have not completed or failed yet."""

    open_ids = _read_ledger(root)
    if open_ids is None:
        open_ids = _scan_open_ids(root)
        _write_ledger(root, open_ids)
    return frozenset(open_ids)


def mark_open(root: Path, record_id: str) -> None:
    """Note a reservation that now depends on a durable transaction."""

    open_ids = _read_ledger(root)
    if open_ids is None:
        open_ids = _scan_open_ids(root)
    if record_id in open_ids:
        return
    open_ids[record_id] = datetime.now(timezone.utc).isoformat()
    _write_ledger(root, open_ids)


def mark_closed(root: Path, record_id: str) -> None:
    """Drop a settled reservation. A stale entry only ever costs disk."""

    open_ids = _read_ledger(root)
    if open_ids is None or record_id not in open_ids:
        return
    open_ids.pop(record_id, None)
    _write_ledger(root, open_ids)


__all__ = [
    "LEDGER_FILE_NAME",
    "mark_closed",
    "mark_open",
    "open_transaction_ids",
]
