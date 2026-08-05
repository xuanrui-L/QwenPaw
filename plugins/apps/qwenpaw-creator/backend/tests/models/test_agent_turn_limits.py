# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from models import config as creator_config
from models.config import (
    DEFAULT_MAINLINE_MAX_MODEL_TURNS,
    DEFAULT_SPECIALIST_MAX_MODEL_TURNS,
    get_mainline_max_model_turns,
    get_specialist_max_model_turns,
)


pytestmark = pytest.mark.unit


def _patch_user_config(monkeypatch, data: dict) -> None:
    monkeypatch.setattr(creator_config, "_get_user_config", lambda: data)


def test_turn_limits_default_without_config(monkeypatch) -> None:
    _patch_user_config(monkeypatch, {})
    assert get_mainline_max_model_turns() == DEFAULT_MAINLINE_MAX_MODEL_TURNS
    assert (
        get_specialist_max_model_turns() == DEFAULT_SPECIALIST_MAX_MODEL_TURNS
    )


def test_turn_limits_read_agent_runtime_section(monkeypatch) -> None:
    _patch_user_config(
        monkeypatch,
        {
            "agent_runtime": {
                "mainline_max_model_turns": 48,
                "specialist_max_model_turns": 20,
            },
        },
    )
    assert get_mainline_max_model_turns() == 48
    assert get_specialist_max_model_turns() == 20


def test_turn_limits_reject_invalid_values(monkeypatch) -> None:
    _patch_user_config(
        monkeypatch,
        {
            "agent_runtime": {
                "mainline_max_model_turns": 0,
                "specialist_max_model_turns": True,
            },
        },
    )
    assert get_mainline_max_model_turns() == DEFAULT_MAINLINE_MAX_MODEL_TURNS
    assert (
        get_specialist_max_model_turns() == DEFAULT_SPECIALIST_MAX_MODEL_TURNS
    )
