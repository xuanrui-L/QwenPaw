# -*- coding: utf-8 -*-
"""Per-project runtime notification outbox (quiet-event staging area).

The notification bus stages QUIET events here instead of the session
message log: any pending user message is consumed by the dispatcher
within one poll interval and starts a paid model run, so informational
progress must stay out of the inbox until it can ride along with a
NEXT_STEP message or an end-of-run digest.

Storage is one append-only JSONL stream per project
(``<project>/runtime/notifications/outbox.jsonl``).  Every state
transition appends a full new version of the record; the current state
of a record is its latest version.  Locking follows the minimal-lock
doctrine: composite read-modify-write operations hold one in-process
per-path operation lock, reads are lock-free.

Lock ordering discipline: the notification operation lock is never held
while acquiring any session-store lock.  Callers drain in two phases —
ASSIGN under the notification lock, then append the session message
outside it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from .errors import RecordNotFoundError
from .jsonl_store import DurableJsonlStore
from .locking import CrossProcessFileLock
from .models import (
    AwareDatetime,
    NonEmptyString,
    StrictRuntimeModel,
    utc_now,
)


NotificationRecordState = Literal[
    "PENDING",
    "ASSIGNED",
    "INJECTED",
    "DRAINED",
    "CANCELLED",
]


class NotificationOutboxRecord(StrictRuntimeModel):
    record_id: NonEmptyString
    project_id: NonEmptyString
    kind: NonEmptyString
    level: Literal["next_step", "quiet"]
    request_id: NonEmptyString
    text: NonEmptyString
    payload: dict[str, Any] = Field(default_factory=dict)
    state: NotificationRecordState = "PENDING"
    assigned_to: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class NotificationOutboxStore:
    """Durable staging store for runtime notification events."""

    def __init__(
        self,
        data_root: str | os.PathLike[str],
        *,
        lock_timeout_seconds: float | None = 10.0,
    ) -> None:
        raw_root = Path(data_root).expanduser()
        if not raw_root.is_absolute():
            raise ValueError("Runtime data root must be absolute")
        self.data_root = raw_root
        self.lock_timeout_seconds = lock_timeout_seconds

    # -- paths and locks -------------------------------------------------

    def _project_root(self, project_id: str) -> Path:
        return self.data_root / project_id

    def _outbox_path(self, project_id: str) -> Path:
        return (
            self._project_root(project_id)
            / "runtime"
            / "notifications"
            / "outbox.jsonl"
        )

    def _stream(
        self, project_id: str
    ) -> DurableJsonlStore[NotificationOutboxRecord]:
        return DurableJsonlStore(
            self._outbox_path(project_id),
            NotificationOutboxRecord,
            lock_timeout_seconds=self.lock_timeout_seconds,
        )

    def _op_lock(self, project_id: str) -> CrossProcessFileLock:
        return CrossProcessFileLock(
            self._project_root(project_id)
            / "runtime"
            / "locks"
            / "notifications.lock",
            timeout_seconds=self.lock_timeout_seconds,
        )

    def _require_project(self, project_id: str) -> None:
        if not (self._project_root(project_id) / "project.json").is_file():
            raise RecordNotFoundError(f"Project not found: {project_id}")

    # -- reads (lock-free) -----------------------------------------------

    def _current_versions(
        self,
        project_id: str,
    ) -> dict[str, NotificationOutboxRecord]:
        """Latest version per record_id, in first-seen (creation) order."""

        current: dict[str, NotificationOutboxRecord] = {}
        for record in self._stream(project_id).read_records():
            current[record.record_id] = record
        return current

    def seen_request_ids(self, project_id: str) -> set[str]:
        return {
            record.request_id
            for record in self._current_versions(project_id).values()
        }

    def pending_records(
        self,
        project_id: str,
    ) -> list[NotificationOutboxRecord]:
        return [
            record
            for record in self._current_versions(project_id).values()
            if record.state == "PENDING"
        ]

    def undelivered_records(
        self,
        project_id: str,
    ) -> list[NotificationOutboxRecord]:
        """PENDING plus ASSIGNED strays a crashed drain left behind."""

        return [
            record
            for record in self._current_versions(project_id).values()
            if record.state in {"PENDING", "ASSIGNED"}
        ]

    # -- writes ------------------------------------------------------------

    def append_pending(
        self,
        project_id: str,
        *,
        kind: str,
        level: Literal["next_step", "quiet"],
        request_id: str,
        text: str,
        payload: dict[str, Any] | None = None,
    ) -> NotificationOutboxRecord | None:
        """Stage one event; ``None`` when the request_id was seen before.

        Deduplication is against the full stream history so a restart or
        a repeated graph tick can never re-stage an already delivered
        fact.
        """

        with self._op_lock(project_id):
            self._require_project(project_id)
            if request_id in self.seen_request_ids(project_id):
                return None
            record = NotificationOutboxRecord(
                record_id=f"notif-record-{uuid4().hex}",
                project_id=project_id,
                kind=kind,
                level=level,
                request_id=request_id,
                text=text,
                payload=dict(payload or {}),
            )
            self._stream(project_id).append(record)
            return record

    def _transition(
        self,
        record: NotificationOutboxRecord,
        *,
        state: NotificationRecordState,
        assigned_to: str | None,
    ) -> NotificationOutboxRecord:
        updated = record.model_copy(
            update={
                "state": state,
                "assigned_to": assigned_to,
                "updated_at": utc_now(),
            },
        )
        self._stream(record.project_id).append(updated)
        return updated

    def assign(
        self,
        project_id: str,
        assigned_to: str,
        *,
        include_stale_assigned: bool = False,
    ) -> list[NotificationOutboxRecord]:
        """Atomically claim pending records for one delivery identity.

        Returns records already assigned to ``assigned_to`` (crash-replay
        path: the render must be byte-stable for the same identity) plus
        newly claimed PENDING records.  ``include_stale_assigned`` lets the
        end-of-run digest also rescue records a crashed earlier drain left
        ASSIGNED — re-delivering informational progress is harmless while
        losing it would strand the events forever.
        """

        with self._op_lock(project_id):
            claimed: list[NotificationOutboxRecord] = []
            for record in self._current_versions(project_id).values():
                if record.state == "ASSIGNED":
                    if record.assigned_to == assigned_to:
                        claimed.append(record)
                    elif include_stale_assigned:
                        claimed.append(
                            self._transition(
                                record,
                                state="ASSIGNED",
                                assigned_to=assigned_to,
                            ),
                        )
                elif record.state == "PENDING":
                    claimed.append(
                        self._transition(
                            record,
                            state="ASSIGNED",
                            assigned_to=assigned_to,
                        ),
                    )
            return claimed

    def settle(self, project_id: str, assigned_to: str) -> int:
        """Mark every record assigned to one delivery identity DRAINED."""

        with self._op_lock(project_id):
            settled = 0
            for record in self._current_versions(project_id).values():
                if (
                    record.state in {"ASSIGNED", "INJECTED"}
                    and record.assigned_to == assigned_to
                ):
                    self._transition(
                        record,
                        state="DRAINED",
                        assigned_to=assigned_to,
                    )
                    settled += 1
            return settled

    def mark_injected(
        self,
        project_id: str,
        run_id: str,
    ) -> list[NotificationOutboxRecord]:
        """Claim pending records for injection into one live run's turn.

        Injected records have no durable session message; the run's
        outcome decides their fate (settle -> DRAINED, failure/restart ->
        reopen to PENDING). Records already INJECTED for the same run are
        returned again so a retried turn renders the same digest.
        """

        assigned_to = f"injected-{run_id}"
        with self._op_lock(project_id):
            claimed: list[NotificationOutboxRecord] = []
            for record in self._current_versions(project_id).values():
                if (
                    record.state == "INJECTED"
                    and record.assigned_to == assigned_to
                ):
                    claimed.append(record)
                elif record.state == "PENDING":
                    claimed.append(
                        self._transition(
                            record,
                            state="INJECTED",
                            assigned_to=assigned_to,
                        ),
                    )
            return claimed

    def reopen_injected(
        self,
        project_id: str,
        *,
        run_id: str | None = None,
    ) -> int:
        """Return INJECTED records to PENDING (failed run or restart)."""

        assigned_to = f"injected-{run_id}" if run_id is not None else None
        with self._op_lock(project_id):
            reopened = 0
            for record in self._current_versions(project_id).values():
                if record.state != "INJECTED":
                    continue
                if assigned_to is not None and (
                    record.assigned_to != assigned_to
                ):
                    continue
                self._transition(
                    record,
                    state="PENDING",
                    assigned_to=None,
                )
                reopened += 1
            return reopened

    def cancel_pending(self, project_id: str) -> int:
        """Cancel undelivered records (hard stop: a human took over)."""

        with self._op_lock(project_id):
            cancelled = 0
            for record in self._current_versions(project_id).values():
                if record.state in {"PENDING", "ASSIGNED", "INJECTED"}:
                    self._transition(
                        record,
                        state="CANCELLED",
                        assigned_to=record.assigned_to,
                    )
                    cancelled += 1
            return cancelled


__all__ = [
    "NotificationOutboxRecord",
    "NotificationOutboxStore",
]
