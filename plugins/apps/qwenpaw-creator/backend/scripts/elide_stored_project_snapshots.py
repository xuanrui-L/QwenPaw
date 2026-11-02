# -*- coding: utf-8 -*-
"""Collapse project echoes that stored conversation history no longer needs.

``messages.jsonl`` used to append every ``read_project`` / ``jq_project``
result verbatim, so a 50-element Project left ~400 KB of intermediate
snapshots behind forever (observed: 96% of a 284 MB session file, re-read
in full on every page open).
The read path stopped showing those bytes long ago:
``_elide_stale_snapshots`` already collapses every superseded snapshot to a
mutation receipt and bounds the newest one before the model sees it, and new
writes store that bounded view directly (``CREATOR_TOOL_ECHO_RECEIPT``).  This
script only recovers surplus that is already invisible -- each record is
rewritten to exactly the text the runtime was already substituting for it.

Nothing is deleted, no sequence is renumbered, ``written_at`` carries over, and
every file is copied to a backup root first.  A file that grows while the
script works is refused, not merged.
"""

# This repair path deliberately reuses the runtime's own private projection
# helpers, so the stored bytes stay identical to what the model is already
# shown.  Duplicating them here would let the two drift apart.
# pylint: disable=protected-access

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# The service imports below follow the path bootstrap on purpose: an operator
# runs this file directly, where backend/ is not on sys.path until those two
# lines put it there.
# pylint: disable=wrong-import-position
from services.file_agent_runtime import driver  # noqa: E402
from services.runtime_files.atomic_store import (  # noqa: E402
    canonical_json_bytes,
)
from services.runtime_files.jsonl_store import (  # noqa: E402
    DurableJsonlStore,
    JsonlEnvelope,
)
from services.runtime_files.models import (  # noqa: E402
    CreatorMessageRecord,
    MessageContentPart,
)

BACKUP_SUBDIR = "elide-backups"
LINE_NEWLINE_BYTES = 1


@dataclass(frozen=True)
class SessionDecision:
    """One session file decided on, with the size collapsing would leave."""

    path: Path
    envelopes: list[JsonlEnvelope]
    records: list[CreatorMessageRecord]
    before_bytes: int
    after_bytes: int
    collapsed: int
    bounded: int

    @property
    def saved_bytes(self) -> int:
        return self.before_bytes - self.after_bytes

    @property
    def changed(self) -> bool:
        return self.after_bytes != self.before_bytes


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="CREATOR_DATA_ROOT holding the project-*/runtime/sessions trees",
    )
    parser.add_argument(
        "--project-id",
        help="restrict the sweep to one Project directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would collapse without writing anything",
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        help=(
            "where to copy each file first "
            f"(default: <data-root>/{BACKUP_SUBDIR})"
        ),
    )
    return parser.parse_args()


def _session_files(data_root: Path, project_id: str | None) -> list[Path]:
    scope = f"project-{project_id}" if project_id else "project-*"
    return sorted(
        path
        for path in data_root.glob(
            f"{scope}/runtime/sessions/*/messages.jsonl",
        )
        if path.is_file() and path.stat().st_size
    )


def _collapse_one(
    message: CreatorMessageRecord,
    receipts: dict[int, str],
) -> CreatorMessageRecord:
    """Return the record as the runtime would already have projected it."""

    text = receipts.get(message.message_seq)
    if text is None:
        return message
    stored = message.content_parts[0].text
    if len(message.content_parts) == 1 and stored == text:
        return message  # an earlier run already collapsed this one
    receipt = not driver._project_snapshot_from_text(text)
    return message.model_copy(
        update={
            "content_parts": [MessageContentPart(type="text", text=text)],
            "metadata": {
                **message.metadata,
                "resultKind": (
                    driver._PROJECT_CHANGE_RECEIPT_RESULT_KIND
                    if receipt
                    else driver._PROJECT_SNAPSHOT_RESULT_KIND
                ),
            },
        },
    )


def _line_bytes(envelope: JsonlEnvelope) -> int:
    return len(canonical_json_bytes(envelope)) + LINE_NEWLINE_BYTES


def _envelope(
    store: DurableJsonlStore,
    seq: int,
    written_at: datetime,
    record: CreatorMessageRecord,
) -> JsonlEnvelope:
    return JsonlEnvelope(
        seq=seq,
        written_at=written_at,
        record=store._dump_record(record),
    )


def _decide(path: Path) -> SessionDecision | None:
    """Read one session file and decide what collapsing it would produce."""

    store = DurableJsonlStore(path, CreatorMessageRecord)
    envelopes = store.read_all()
    messages = [
        CreatorMessageRecord.model_validate(envelope.record)
        for envelope in envelopes
    ]
    receipts = driver._elide_stale_snapshots(messages)
    if not receipts:
        return None
    records = [_collapse_one(message, receipts) for message in messages]
    after_bytes = sum(
        _line_bytes(
            _envelope(
                store,
                envelope.seq,
                envelope.written_at,
                record,
            ),
        )
        for envelope, record in zip(envelopes, records)
    )
    return SessionDecision(
        path=path,
        envelopes=envelopes,
        records=records,
        before_bytes=path.stat().st_size,
        after_bytes=after_bytes,
        collapsed=sum(
            1
            for text in receipts.values()
            if not driver._project_snapshot_from_text(text)
        ),
        bounded=sum(
            1
            for text in receipts.values()
            if driver._project_snapshot_from_text(text)
        ),
    )


def _backup(path: Path, data_root: Path, backup_root: Path) -> Path:
    """Copy the durable file aside, keeping its path under the data root."""

    target = backup_root / path.relative_to(data_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise RuntimeError(
            f"backup already exists, refusing to overwrite: {target}",
        )
    shutil.copy2(path, target)
    return target


def _rewrite(decision: SessionDecision) -> None:
    """Swap the collapsed records in, under the store's own lock."""

    path = decision.path
    store = DurableJsonlStore(path, CreatorMessageRecord)
    temp = path.with_name(f".{path.name}.eliding")
    temp.unlink(missing_ok=True)
    writer = DurableJsonlStore(temp, CreatorMessageRecord)
    try:
        for envelope, record in zip(decision.envelopes, decision.records):
            writer.append(record, written_at=envelope.written_at)
        with store._lock():
            current = store.read_all()
            if not current or current[-1].seq != decision.envelopes[-1].seq:
                raise RuntimeError(
                    f"{path} grew while being collapsed; refusing to swap",
                )
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _verify(path: Path) -> None:
    """Reading the collapsed file must now project to the bytes it holds."""

    store = DurableJsonlStore(path, CreatorMessageRecord)
    messages = store.read_records()
    for seq, text in driver._elide_stale_snapshots(messages).items():
        message = next(item for item in messages if item.message_seq == seq)
        if message.content_parts[0].text != text:
            raise RuntimeError(
                f"{path} seq {seq} still projects differently than it stores",
            )


def _report(decision: SessionDecision, dry_run: bool) -> None:
    print(
        f"{decision.path}: {decision.collapsed} receipt(s) + "
        f"{decision.bounded} bounded snapshot(s), "
        f"{decision.before_bytes} -> {decision.after_bytes} bytes"
        + (" (dry run)" if dry_run else ""),
    )


def main() -> int:
    args = _arguments()
    data_root = args.data_root.expanduser().resolve()
    if not data_root.is_dir():
        print(f"data root is not a directory: {data_root}")
        return 2
    backup_root = (
        args.backup_root.expanduser().resolve()
        if args.backup_root
        else data_root / BACKUP_SUBDIR
    )

    touched = 0
    saved = 0
    for path in _session_files(data_root, args.project_id):
        decision = _decide(path)
        if decision is None or not decision.changed:
            continue
        touched += 1
        saved += decision.saved_bytes
        _report(decision, args.dry_run)
        if args.dry_run:
            continue
        backup = _backup(path, data_root, backup_root)
        _rewrite(decision)
        _verify(path)
        print(f"  backup: {backup}")

    if not touched:
        print("nothing to collapse: stored history is already bounded")
        return 0
    print(
        f"{touched} session file(s), {saved} bytes "
        f"{'to be ' if args.dry_run else ''}saved",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
