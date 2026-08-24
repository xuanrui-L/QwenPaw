# -*- coding: utf-8 -*-
"""Idempotency guards for the operator-only R2V materialization repair."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.recover_completed_r2v_materialization import (
    _reopen_materialization_state,
)


class _State(SimpleNamespace):
    def model_dump(self, *, mode: str) -> dict:
        assert mode == "python"
        return dict(vars(self))


@pytest.mark.parametrize("phase", ["FAILED", "PROVIDER_SUCCEEDED"])
def test_recovery_can_reopen_first_attempt_and_interrupted_rerun(
    phase,
) -> None:
    result = {"status": "SUCCEEDED", "url": "https://cdn.example/video.mp4"}
    state = _State(
        phase=phase,
        provider_task_id="provider-1",
        provider_result=result,
        last_error="download failed",
        materialize_owner="stale-owner",
        materialize_claim_token="stale-claim",
        materialize_claimed_at_epoch=1.0,
        materialize_heartbeat_at_epoch=2.0,
        materialize_claim_expires_at_epoch=3.0,
    )

    reopened = _reopen_materialization_state(
        state,
        provider_task_id="provider-1",
        provider_result=result,
    )

    assert reopened["phase"] == "PROVIDER_SUCCEEDED"
    assert reopened["last_error"] is None
    for field in (
        "materialize_owner",
        "materialize_claim_token",
        "materialize_claimed_at_epoch",
        "materialize_heartbeat_at_epoch",
        "materialize_claim_expires_at_epoch",
    ):
        assert reopened[field] is None


def test_recovery_still_fails_closed_when_provider_identity_changes() -> None:
    result = {"status": "SUCCEEDED", "url": "https://cdn.example/video.mp4"}
    state = _State(
        phase="PROVIDER_SUCCEEDED",
        provider_task_id="provider-other",
        provider_result=result,
    )

    with pytest.raises(RuntimeError, match="state changed"):
        _reopen_materialization_state(
            state,
            provider_task_id="provider-1",
            provider_result=result,
        )
