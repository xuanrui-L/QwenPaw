# -*- coding: utf-8 -*-
"""Async application facade for the synchronous filesystem primitives."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import logging
from pathlib import Path
import threading
from typing import Any, Mapping

from services.runtime_files.field_blocks import FieldBlockStore
from services.runtime_files import (
    MessageChannel,
    MessageClassification,
    RuntimeGoalNotFound,
    RuntimeSessionNotFound,
)
from services.runtime_files.session_store import ProjectRuntimeSessionStore

from .commit import ProjectCommitBoundary, ProjectCommitResult
from .jq_transform import JqProjectTransformer
from .poller import ProjectPoller, ProjectSnapshotCacheEntry
from .recovery import (
    CreatorRecoveryReport,
    ProjectCommitRecoveryCoordinator,
    ProjectRecoveryReport,
)
from .review import (
    CreatorReviewRecoveryReport,
    ProjectReviewService,
    ReviewDecisionItem,
    ReviewDecisionJournalState,
    ReviewRejectionAction,
    ReviewRejectionFeedback,
    render_rejection_feedback_message,
)
from .store import ProjectSnapshot, ProjectStore


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CreatorFileServices:
    root: Path
    projects: ProjectStore
    commits: ProjectCommitBoundary
    poller: ProjectPoller
    reviews: ProjectReviewService
    sessions: ProjectRuntimeSessionStore
    jq: JqProjectTransformer
    recovery: ProjectCommitRecoveryCoordinator
    startup_recovery: CreatorRecoveryReport
    startup_review_recovery: CreatorReviewRecoveryReport

    @classmethod
    def create(cls, root: Path) -> CreatorFileServices:
        projects = ProjectStore(root)
        recovery = ProjectCommitRecoveryCoordinator(projects)
        startup_recovery = recovery.recover_all()
        reviews = ProjectReviewService(projects)
        # Review decisions may depend on a compensating Project transaction.
        # Project journals therefore converge first, followed by Review facts.
        startup_review_recovery = reviews.recover_all()
        services = cls(
            root=root,
            projects=projects,
            commits=ProjectCommitBoundary(projects),
            poller=ProjectPoller(projects),
            reviews=reviews,
            sessions=ProjectRuntimeSessionStore(root),
            jq=JqProjectTransformer(),
            recovery=recovery,
            startup_recovery=startup_recovery,
            startup_review_recovery=startup_review_recovery,
        )
        services.recover_review_rejection_feedback_messages()
        return services

    def recover_project(
        self,
        project_id: str,
        *,
        _lifecycle_lock_held: bool = False,
    ) -> ProjectRecoveryReport:
        return self.recovery.recover_project(
            project_id,
            _lifecycle_lock_held=_lifecycle_lock_held,
        )

    def recover_all(self) -> CreatorRecoveryReport:
        return self.recovery.recover_all()

    def blocks(self, project_id: str) -> FieldBlockStore:
        return FieldBlockStore(
            self.projects.project_root(project_id)
            / "runtime"
            / "locks"
            / "fields",
        )

    async def snapshot(
        self,
        project_id: str,
        *,
        force: bool = False,
    ) -> ProjectSnapshotCacheEntry:
        return await asyncio.to_thread(
            self.poller.poll_once,
            project_id,
            force=force,
        )

    async def commit_candidate(
        self,
        *,
        base: ProjectSnapshot,
        candidate: Mapping[str, Any],
        **metadata: Any,
    ) -> ProjectCommitResult:
        result = await asyncio.to_thread(
            self.commits.commit,
            base=base,
            candidate=candidate,
            **metadata,
        )
        await asyncio.to_thread(self.poller.note_commit, result.snapshot)
        return result

    async def apply_jq(
        self,
        *,
        project_id: str,
        program: str,
        string_args: Mapping[str, str] | None = None,
        json_args: Mapping[str, Any] | None = None,
        **metadata: Any,
    ) -> ProjectCommitResult:
        # No Project lock is held during jq/model execution.  The later commit
        # merges touched values against the then-current authority.
        base = await asyncio.to_thread(self.projects.read, project_id)
        candidate = await asyncio.to_thread(
            self.jq.transform,
            base.project.model_dump(mode="json"),
            program,
            string_args=string_args,
            json_args=json_args,
        )
        return await self.commit_candidate(
            base=base,
            candidate=candidate,
            **metadata,
        )

    async def active_review(self, project_id: str):
        return await asyncio.to_thread(self.reviews.active, project_id)

    async def active_reviews(self, project_id: str) -> list:
        return await asyncio.to_thread(self.reviews.all_pending, project_id)

    async def decide_review(
        self,
        *,
        project_id: str,
        review_id: str,
        decision_token: str,
        decisions: list[ReviewDecisionItem],
        rejection_feedback: ReviewRejectionFeedback | None = None,
        decision_id: str | None = None,
        _lifecycle_lock_held: bool = False,
    ):
        result = await asyncio.to_thread(
            self.reviews.decide,
            project_id=project_id,
            review_id=review_id,
            decision_token=decision_token,
            decisions=decisions,
            rejection_feedback=rejection_feedback,
            decision_id=decision_id,
            _lifecycle_lock_held=_lifecycle_lock_held,
        )
        current = await asyncio.to_thread(self.projects.read, project_id)
        await asyncio.to_thread(self.poller.note_commit, current)
        return result

    async def publish_review_rejection_feedback(
        self,
        *,
        project_id: str,
        review_id: str,
        decision_id: str,
    ) -> ReviewRejectionAction | None:
        """Append one idempotent Runtime message for a finalized rejection."""

        return await asyncio.to_thread(
            self._publish_review_rejection_feedback,
            project_id=project_id,
            review_id=review_id,
            decision_id=decision_id,
        )

    def recover_review_rejection_feedback_messages(self) -> None:
        """Replay the feedback outbox after a process crash.

        A crash can happen after the Review journal is finalized but before
        its Session message is appended. The deterministic client message ID
        makes replay safe even when the first append actually succeeded.
        """

        for project_id in self.projects.discover_project_ids():
            try:
                journals = self.reviews.finalized_rejection_feedback_journals(
                    project_id,
                )
            except Exception:
                logger.exception(
                    "failed to discover rejection feedback for Project %s",
                    project_id,
                )
                continue
            for journal in journals:
                try:
                    self._publish_review_rejection_feedback(
                        project_id=project_id,
                        review_id=journal.review_id,
                        decision_id=journal.decision_id,
                    )
                except Exception:
                    logger.exception(
                        "failed to recover rejection feedback %s "
                        "for Project %s",
                        journal.decision_id,
                        project_id,
                    )

    def _publish_review_rejection_feedback(
        self,
        *,
        project_id: str,
        review_id: str,
        decision_id: str,
    ) -> ReviewRejectionAction | None:
        """Synchronous implementation shared by HTTP and startup recovery."""

        journal = self.reviews.get_decision_journal(
            project_id,
            review_id,
            decision_id,
        )
        if journal.state is not ReviewDecisionJournalState.FINALIZED:
            return None
        feedback = journal.rejection_feedback
        text = render_rejection_feedback_message(journal)
        if feedback is None or text is None:
            return None
        try:
            session = self.sessions.get_project_session_snapshot(project_id)
        except RuntimeSessionNotFound:
            # Unit-created/legacy Projects may have Reviews but no Runtime
            # Session. The feedback remains durable in the decision journal.
            return feedback.action

        messages = self.sessions.list_messages(
            project_id,
            session.session_id,
            after_seq=0,
            limit=None,
        )
        originating = next(
            (
                item
                for item in messages
                if item.message_seq
                == journal.review_before.request_message_seq
            ),
            None,
        )
        conversation_id = (
            originating.conversation_id if originating is not None else None
        )
        if conversation_id is None and session.active_goal_id is not None:
            try:
                goal = self.sessions.get_goal(
                    project_id,
                    session.active_goal_id,
                )
                conversation_id = goal.conversation_id
            except RuntimeGoalNotFound:
                pass
        if conversation_id is None:
            conversations = self.sessions.list_conversations(
                project_id,
                session.session_id,
            )
            default = next(
                (item for item in conversations if item.is_default),
                conversations[0] if conversations else None,
            )
            if default is None:
                return feedback.action
            conversation_id = default.conversation_id

        digest = sha256(
            f"{project_id}\0{review_id}\0{decision_id}".encode("utf-8"),
        ).hexdigest()
        self.sessions.append_message(
            project_id,
            session.session_id,
            conversation_id,
            role=(
                "user"
                if feedback.action is ReviewRejectionAction.UNDO_AND_REGENERATE
                else "system"
            ),
            content_parts=[{"type": "text", "text": text}],
            message_id=f"message-review-feedback-{digest}",
            client_message_id=f"review-feedback-{digest}",
            source="review_rejection_feedback",
            channel=MessageChannel.RUNTIME,
            classification=(
                MessageClassification.REVIEW_REVISE
                if feedback.action is ReviewRejectionAction.UNDO_AND_REGENERATE
                else MessageClassification.REVIEW_COMMENT
            ),
            metadata={
                "reviewId": review_id,
                "decisionId": decision_id,
                "rejectionFeedback": feedback.model_dump(
                    mode="json",
                    by_alias=True,
                ),
                "targets": [
                    item.model_dump(mode="json")
                    for item in journal.rejection_targets
                ],
            },
        )
        return feedback.action


_registry_lock = threading.RLock()
_registry: dict[Path, CreatorFileServices] = {}


def creator_file_services(root: Path) -> CreatorFileServices:
    resolved = root.expanduser().resolve()
    with _registry_lock:
        services = _registry.get(resolved)
        if services is None:
            services = CreatorFileServices.create(resolved)
            _registry[resolved] = services
        return services


def clear_creator_file_service_registry() -> None:
    """Test/process-shutdown hook; durable state remains on disk."""

    with _registry_lock:
        for services in _registry.values():
            services.poller.stop()
        _registry.clear()


__all__ = [
    "CreatorFileServices",
    "clear_creator_file_service_registry",
    "creator_file_services",
]
