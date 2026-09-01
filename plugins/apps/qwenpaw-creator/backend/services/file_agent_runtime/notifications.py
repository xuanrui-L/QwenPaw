# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Runtime→Agent notification bus (steer / inject delivery primitives).

Background work (the work-graph scheduler, and later asynchronous
specialists) reports back to the mainline Agent through this bus.  Two
delivery primitives, aligned with the Codex subagent-notification and
DeepSeek-Harness inject/steer models:

- ``steer`` (NEXT_STEP events): the fact requires an Agent action and no
  other mechanism will bring the Agent back.  It becomes one durable
  user-role RUNTIME-channel session message immediately (idempotent by
  ``client_message_id``) and wakes the dispatcher: an idle session starts
  a run within one poll interval, a busy session consumes it right after
  the current run — the mainline is never interrupted mid-run.

- ``inject`` (QUIET events): informational progress that must not spend a
  model run on its own.  It is staged in the per-project notification
  outbox and rides along with the next steer message or the end-of-run
  resume digest.

The session message log stays the single inbox; the outbox is only a
staging area whose content always ends up folded into inbox messages.

Delivery is idempotent end to end: every event carries a ``request_id``
anchored to the underlying fact (node fingerprint, project generation,
specialist run id), deduplicated once against the outbox history for
quiet events and once against the session log's ``client_message_id``
for steer messages.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
from typing import Any, Callable, Mapping

from services.runtime_files import (
    MessageChannel,
    MessagePayloadConflict,
    RuntimeSessionNotFound,
)
from services.runtime_files.errors import RecordNotFoundError
from services.runtime_files.notification_store import (
    NotificationOutboxRecord,
    NotificationOutboxStore,
)
from utils.logger import setup_logger

logger = setup_logger("creator.notifications")


NOTIFICATION_SOURCE = "runtime_notification"

# Runtime-authored user messages that keep an unattended session moving.
# A steer counts the session tail's consecutive run of these sources as a
# hard fuse: past the cap the bus stops waking the Agent and downgrades to
# the outbox until a human message resets the streak.
RUNTIME_AUTONOMOUS_SOURCES = frozenset(
    {
        "yolo_auto_resume",
        "prompt_contract_resume",
        "mainline_resume",
        NOTIFICATION_SOURCE,
    },
)

NOTIFY_AUTONOMOUS_HARD_CAP = 10

# Escape valve for hard-cap parked NEXT_STEP events.  The cap stops
# autonomous steers, and a fully idle session has no future run whose
# turn boundary could inject the parked outbox — without a valve those
# events would wait forever for a human.  Once a parked NEXT_STEP event
# has aged past the cooldown on an idle session, the dispatcher may
# deliver one batched flush message, at most ``NOTIFY_IDLE_FLUSH_BUDGET``
# times since the last human message: the flush→run→park loop stays
# bounded even when every flush spawns work that parks again.
NOTIFY_IDLE_FLUSH_BUDGET = 2
NOTIFY_IDLE_FLUSH_COOLDOWN_SECONDS = 120.0

# Window of tail messages inspected for the hard fuse; the streak resets on
# any human message, so a bounded window never miscounts a longer streak
# than the cap it guards.
_FUSE_TAIL_WINDOW = 200


class RuntimeEventKind(StrEnum):
    SUBAGENT_TERMINAL = "subagent_terminal"
    NODE_DETERMINISTIC_FAILURE = "node_deterministic_failure"
    NODE_TRANSIENT_CAP_EXHAUSTED = "node_transient_cap_exhausted"
    GRAPH_ALL_DONE = "graph_all_done"
    COMPOSE_COMPLETED = "compose_completed"
    NODE_DISPATCH_STARTED = "node_dispatch_started"
    NODE_SUCCEEDED = "node_succeeded"
    NODE_GATED = "node_gated"


class NotificationLevel(StrEnum):
    NEXT_STEP = "next_step"
    QUIET = "quiet"


EVENT_LEVELS: dict[RuntimeEventKind, NotificationLevel] = {
    RuntimeEventKind.SUBAGENT_TERMINAL: NotificationLevel.NEXT_STEP,
    RuntimeEventKind.NODE_DETERMINISTIC_FAILURE: NotificationLevel.NEXT_STEP,
    RuntimeEventKind.NODE_TRANSIENT_CAP_EXHAUSTED: (
        NotificationLevel.NEXT_STEP
    ),
    RuntimeEventKind.GRAPH_ALL_DONE: NotificationLevel.NEXT_STEP,
    RuntimeEventKind.COMPOSE_COMPLETED: NotificationLevel.NEXT_STEP,
    RuntimeEventKind.NODE_DISPATCH_STARTED: NotificationLevel.QUIET,
    RuntimeEventKind.NODE_SUCCEEDED: NotificationLevel.QUIET,
    RuntimeEventKind.NODE_GATED: NotificationLevel.QUIET,
}


_STEER_HEADER = "【系统自动消息 · Runtime 通知】"
_DIGEST_HEADER = "【Runtime 进度速报】自动执行进展（进度信息，不是新的用户指令，不要为其重复生成）："
_STEER_FOOTER = (
    "本消息由 Runtime 自动发出，不是新的用户修改意见；请基于当前 Project 实际状态决定验证、修复或确认完成，不要重复已完成的工作。"
)


def render_steer_message(
    event_text: str,
    quiet_records: list[NotificationOutboxRecord],
) -> str:
    """Deterministically render one steer message (byte-stable per input)."""

    lines = [_STEER_HEADER]
    if quiet_records:
        lines.append(_DIGEST_HEADER)
        lines.extend(f"- {record.text}" for record in quiet_records)
        lines.append("")
    lines.append(event_text)
    lines.append("")
    lines.append(_STEER_FOOTER)
    return "\n".join(lines)


def render_resume_digest(
    quiet_records: list[NotificationOutboxRecord],
) -> str:
    """Digest prefix folded into a resume message; empty when nothing pends."""

    if not quiet_records:
        return ""
    lines = [_DIGEST_HEADER]
    lines.extend(f"- {record.text}" for record in quiet_records)
    lines.append("")
    return "\n".join(lines)


_IDLE_FLUSH_HEADER = (
    "自动执行此前因连续自主消息达到上限而暂停主动推进；"
    "会话已空闲一段时间，按空闲兜底策略补投递以下挂起通知。"
)


def render_idle_flush_message(
    records: list[NotificationOutboxRecord],
) -> str:
    """Deterministically render one idle-flush message (byte-stable)."""

    actionable = [item for item in records if item.level == "next_step"]
    quiet = [item for item in records if item.level != "next_step"]
    lines = [_STEER_HEADER, _IDLE_FLUSH_HEADER, ""]
    if actionable:
        lines.append("【待处理事项】")
        lines.extend(f"- {record.text}" for record in actionable)
        lines.append("")
    if quiet:
        lines.append(_DIGEST_HEADER)
        lines.extend(f"- {record.text}" for record in quiet)
        lines.append("")
    lines.append(_STEER_FOOTER)
    return "\n".join(lines)


class RuntimeNotificationBus:
    """Route runtime events into the session inbox without disturbing runs."""

    def __init__(
        self,
        services: Any,
        *,
        wake_dispatcher: Callable[[str], None],
        store: NotificationOutboxStore | None = None,
    ) -> None:
        self.services = services
        self.store = store or NotificationOutboxStore(services.root)
        self._wake_dispatcher = wake_dispatcher
        # Projects whose idle-flush budget is exhausted, keyed to the
        # session's last_message_seq at exhaustion: the poll-driven
        # dispatcher probes every tick, and without this memo each tick
        # would repeat the budget tail scan and its log line. Any new
        # session message moves the seq and re-evaluates once.
        self._idle_flush_blocked: dict[str, int] = {}

    # -- public API ------------------------------------------------------

    async def notify(
        self,
        project_id: str,
        *,
        kind: RuntimeEventKind,
        request_id: str,
        text: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        level = EVENT_LEVELS.get(kind)
        if level is None:
            raise ValueError(
                f"runtime event kind has no declared level: {kind!r}",
            )
        if level is NotificationLevel.NEXT_STEP:
            await self.steer(
                project_id,
                kind=kind,
                request_id=request_id,
                text=text,
                payload=payload,
            )
        else:
            await self.inject(
                project_id,
                kind=kind,
                request_id=request_id,
                text=text,
                payload=payload,
            )

    async def inject(
        self,
        project_id: str,
        *,
        kind: RuntimeEventKind,
        request_id: str,
        text: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Stage a quiet event; it rides along with the next delivery."""

        try:
            await asyncio.to_thread(
                self.store.append_pending,
                project_id,
                kind=kind.value,
                level=EVENT_LEVELS[kind].value,
                request_id=request_id,
                text=text,
                payload=dict(payload or {}),
            )
        except RecordNotFoundError:
            logger.info(
                "notification dropped, project gone: %s %s",
                project_id,
                request_id,
            )

    async def steer(
        self,
        project_id: str,
        *,
        kind: RuntimeEventKind,
        request_id: str,
        text: str,
        payload: Mapping[str, Any] | None = None,
    ) -> bool:
        """Deliver one next-step event as a durable user message + wake.

        Returns ``True`` when a message is durably present for this
        ``request_id`` (freshly appended or an idempotent replay).
        Assign-then-append: pending quiet records are claimed under the
        notification lock first, the message is rendered from the claimed
        set (byte-stable per ``client_message_id``), appended outside the
        lock, then the claimed records settle to DRAINED.
        """

        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:24]
        client_message_id = f"notif-{kind.value}-{digest}"
        try:
            session = await asyncio.to_thread(
                self.services.sessions.get_project_session_snapshot,
                project_id,
            )
        except RuntimeSessionNotFound:
            logger.info(
                "steer skipped, project %s has no runtime session",
                project_id,
            )
            return False
        conversation_id = await asyncio.to_thread(
            self._default_conversation_id,
            project_id,
            session.session_id,
        )
        if conversation_id is None:
            logger.warning(
                "steer skipped, project %s has no conversation",
                project_id,
            )
            return False
        if await asyncio.to_thread(
            self._autonomous_streak_exhausted,
            project_id,
            session,
        ):
            logger.warning(
                "steer downgraded to outbox for %s (%d consecutive "
                "runtime-authored messages without a human): %s",
                project_id,
                NOTIFY_AUTONOMOUS_HARD_CAP,
                request_id,
            )
            await self.inject(
                project_id,
                kind=kind,
                request_id=request_id,
                text=text,
                payload=payload,
            )
            return False
        try:
            claimed = await asyncio.to_thread(
                self.store.assign,
                project_id,
                client_message_id,
            )
        except RecordNotFoundError:
            return False
        message_text = render_steer_message(text, claimed)
        metadata = {
            "notificationKind": kind.value,
            "requestId": request_id,
            **dict(payload or {}),
        }
        try:
            await asyncio.to_thread(
                self.services.sessions.append_message,
                project_id,
                session.session_id,
                conversation_id,
                role="user",
                content_parts=[{"type": "text", "text": message_text}],
                client_message_id=client_message_id,
                source=NOTIFICATION_SOURCE,
                channel=MessageChannel.RUNTIME,
                metadata=metadata,
            )
        except MessagePayloadConflict:
            # The same request_id already landed with different folded
            # progress (an end-of-run digest claimed the records between
            # our crash and this replay). The fact is durably delivered.
            logger.info(
                "steer replay converged on existing message: %s %s",
                project_id,
                client_message_id,
            )
        except Exception:  # pylint: disable=broad-except
            # Delivery failure must never break the producer (scheduler
            # dispatch, specialist finalizer). Claimed records stay
            # ASSIGNED and are rescued by the next end-of-run digest.
            logger.exception(
                "steer delivery failed for %s %s",
                project_id,
                request_id,
            )
            return False
        await asyncio.to_thread(
            self.store.settle,
            project_id,
            client_message_id,
        )
        self._wake_dispatcher(project_id)
        return True

    async def drain_into_resume(
        self,
        project_id: str,
        *,
        assigned_to: str,
    ) -> str:
        """Claim pending progress for an end-of-run resume message.

        Also rescues records a crashed earlier drain left ASSIGNED.  The
        caller appends its resume message, then calls
        :meth:`settle_resume` with the same identity.
        """

        try:
            claimed = await asyncio.to_thread(
                self.store.assign,
                project_id,
                assigned_to,
                include_stale_assigned=True,
            )
        except RecordNotFoundError:
            return ""
        return render_resume_digest(claimed)

    async def settle_resume(
        self, project_id: str, *, assigned_to: str
    ) -> None:
        try:
            await asyncio.to_thread(
                self.store.settle,
                project_id,
                assigned_to,
            )
        except RecordNotFoundError:
            pass

    async def inject_pending_into_run(
        self,
        project_id: str,
        *,
        run_id: str,
    ) -> str:
        """Render staged progress into a live run's next model turn.

        The records turn INJECTED (no durable session message); the
        caller settles them when the run succeeds or reopens them when it
        fails, and a restart sweep reopens any survivors.
        """

        try:
            claimed = await asyncio.to_thread(
                self.store.mark_injected,
                project_id,
                run_id,
            )
        except RecordNotFoundError:
            return ""
        return render_resume_digest(claimed)

    async def settle_injected(
        self,
        project_id: str,
        *,
        run_id: str,
        success: bool,
    ) -> None:
        try:
            if success:
                await asyncio.to_thread(
                    self.store.settle,
                    project_id,
                    f"injected-{run_id}",
                )
            else:
                await asyncio.to_thread(
                    self.store.reopen_injected,
                    project_id,
                    run_id=run_id,
                )
        except RecordNotFoundError:
            pass

    async def reopen_all_injected(self, project_id: str) -> None:
        """Startup sweep: any surviving INJECTED record belongs to a dead run."""

        try:
            await asyncio.to_thread(
                self.store.reopen_injected,
                project_id,
            )
        except RecordNotFoundError:
            pass

    async def cancel_pending(self, project_id: str) -> None:
        """Drop undelivered progress: a human hard stop took over."""

        try:
            await asyncio.to_thread(self.store.cancel_pending, project_id)
        except RecordNotFoundError:
            pass

    async def has_flush_candidates(self, project_id: str) -> bool:
        """Cheap probe: an aged, undelivered NEXT_STEP event exists."""

        try:
            outstanding = await asyncio.to_thread(
                self.store.undelivered_records,
                project_id,
            )
        except RecordNotFoundError:
            return False
        return self._flush_anchor(outstanding) is not None

    async def flush_pending_on_idle(self, project_id: str) -> bool:
        """Deliver hard-cap parked events on an idle session (bounded).

        The caller (dispatcher reconcile) guarantees the session is idle:
        no active run, no queued user messages, no active review, no
        in-flight specialists.  Delivery follows the same assign-then-
        append discipline as :meth:`steer`, anchored to the newest parked
        NEXT_STEP record so a crash replay converges on the same
        ``client_message_id``.  Undelivered quiet records ride along.
        """

        try:
            outstanding = await asyncio.to_thread(
                self.store.undelivered_records,
                project_id,
            )
        except RecordNotFoundError:
            return False
        anchor = self._flush_anchor(outstanding)
        if anchor is None:
            return False
        request_id = f"idleflush-{anchor.request_id}"
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:24]
        client_message_id = f"notif-idle-flush-{digest}"
        try:
            session = await asyncio.to_thread(
                self.services.sessions.get_project_session_snapshot,
                project_id,
            )
        except RuntimeSessionNotFound:
            return False
        if self._idle_flush_blocked.get(project_id) == session.last_message_seq:
            return False
        self._idle_flush_blocked.pop(project_id, None)
        conversation_id = await asyncio.to_thread(
            self._default_conversation_id,
            project_id,
            session.session_id,
        )
        if conversation_id is None:
            return False
        if await asyncio.to_thread(
            self._idle_flush_budget_exhausted,
            project_id,
            session,
        ):
            self._idle_flush_blocked[project_id] = session.last_message_seq
            logger.info(
                "idle flush skipped for %s: budget of %d used since the "
                "last human message; parked notifications wait for a human",
                project_id,
                NOTIFY_IDLE_FLUSH_BUDGET,
            )
            return False
        try:
            claimed = await asyncio.to_thread(
                self.store.assign,
                project_id,
                client_message_id,
                include_stale_assigned=True,
            )
        except RecordNotFoundError:
            return False
        if not claimed:
            return False
        message_text = render_idle_flush_message(claimed)
        metadata = {
            "notificationKind": "idle_flush",
            "requestId": request_id,
            "idleFlush": True,
        }
        try:
            await asyncio.to_thread(
                self.services.sessions.append_message,
                project_id,
                session.session_id,
                conversation_id,
                role="user",
                content_parts=[{"type": "text", "text": message_text}],
                client_message_id=client_message_id,
                source=NOTIFICATION_SOURCE,
                channel=MessageChannel.RUNTIME,
                metadata=metadata,
            )
        except MessagePayloadConflict:
            logger.info(
                "idle flush replay converged on existing message: %s %s",
                project_id,
                client_message_id,
            )
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "idle flush delivery failed for %s %s",
                project_id,
                request_id,
            )
            return False
        await asyncio.to_thread(
            self.store.settle,
            project_id,
            client_message_id,
        )
        logger.warning(
            "idle flush delivered %d parked notification(s) for %s: %s",
            len(claimed),
            project_id,
            request_id,
        )
        self._wake_dispatcher(project_id)
        return True

    # -- internals ---------------------------------------------------------

    def _default_conversation_id(
        self,
        project_id: str,
        session_id: str,
    ) -> str | None:
        conversations = self.services.sessions.list_conversations(
            project_id,
            session_id,
        )
        default = next(
            (item for item in conversations if item.is_default),
            conversations[0] if conversations else None,
        )
        return default.conversation_id if default is not None else None

    def _autonomous_streak_exhausted(
        self,
        project_id: str,
        session: Any,
    ) -> bool:
        after_seq = max(0, session.last_message_seq - _FUSE_TAIL_WINDOW)
        messages = self.services.sessions.list_messages(
            project_id,
            session.session_id,
            after_seq=after_seq,
            limit=None,
        )
        streak = 0
        for item in reversed(messages):
            if item.role != "user":
                continue
            if item.source in RUNTIME_AUTONOMOUS_SOURCES:
                streak += 1
                if streak >= NOTIFY_AUTONOMOUS_HARD_CAP:
                    return True
                continue
            break
        return False

    def _flush_anchor(
        self,
        outstanding: list[NotificationOutboxRecord],
    ) -> NotificationOutboxRecord | None:
        """Newest undelivered NEXT_STEP record aged past the cooldown.

        The age gate doubles as the idleness signal: had any run happened
        since the record parked, its turn boundary or end-of-run digest
        would have drained it already.
        """

        candidates = [
            record
            for record in outstanding
            if record.level == "next_step"
        ]
        if not candidates:
            return None
        anchor = max(
            candidates,
            key=lambda record: (record.created_at, record.record_id),
        )
        age = (datetime.now(UTC) - anchor.created_at).total_seconds()
        if age < NOTIFY_IDLE_FLUSH_COOLDOWN_SECONDS:
            return None
        return anchor

    def _idle_flush_budget_exhausted(
        self,
        project_id: str,
        session: Any,
    ) -> bool:
        after_seq = max(0, session.last_message_seq - _FUSE_TAIL_WINDOW)
        messages = self.services.sessions.list_messages(
            project_id,
            session.session_id,
            after_seq=after_seq,
            limit=None,
        )
        flushes = 0
        for item in reversed(messages):
            if item.role != "user":
                continue
            if item.source not in RUNTIME_AUTONOMOUS_SOURCES:
                break
            if item.metadata.get("idleFlush"):
                flushes += 1
                if flushes >= NOTIFY_IDLE_FLUSH_BUDGET:
                    return True
        return False


__all__ = [
    "EVENT_LEVELS",
    "NOTIFICATION_SOURCE",
    "NOTIFY_AUTONOMOUS_HARD_CAP",
    "NOTIFY_IDLE_FLUSH_BUDGET",
    "NOTIFY_IDLE_FLUSH_COOLDOWN_SECONDS",
    "NotificationLevel",
    "RUNTIME_AUTONOMOUS_SOURCES",
    "RuntimeEventKind",
    "RuntimeNotificationBus",
    "render_idle_flush_message",
    "render_resume_digest",
    "render_steer_message",
]
