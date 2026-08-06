# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long,broad-except
"""Creator feedback service: OTel span export + JSONL recording.

This module provides an independent feedback system for the Creator plugin,
integrating with AgentTrack for OTel span export and storing records locally.
"""

from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

logger = logging.getLogger("qwenpaw.creator.feedback")

APP_NAME = "qwenpaw-creator"

# ── User identity (read from dogfooding-bundle's SSO data) ──────────────────
_user_account_path_cache: tuple[float, dict[str, str]] | None = None


def _read_dogfooding_user_account() -> dict[str, str]:
    """Read user_account.json from dogfooding-bundle's SSO data.

    Returns dict with 'user_account' (employee ID) and 'user_name'.
    Uses mtime-based cache to avoid repeated file reads.
    """
    global _user_account_path_cache
    path = Path.home() / ".qwenpaw" / "dogfooding" / "user_account.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"user_account": "", "user_name": ""}

    if _user_account_path_cache is not None:
        cached_mtime, cached_data = _user_account_path_cache
        if mtime == cached_mtime:
            return cached_data

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        result = {
            "user_account": str(data.get("user_account", "")).strip(),
            "user_name": str(data.get("user_name", "")).strip(),
        }
    except Exception as exc:
        logger.debug("Failed to read dogfooding user_account.json: %s", exc)
        result = {"user_account": "", "user_name": ""}

    _user_account_path_cache = (mtime, result)
    return result


def _normalize_emp_id(user_id: str) -> str:
    """Normalize employee ID, dropping fallback sentinels."""
    value = str(user_id or "").strip()
    if not value or value.lower() in ("default", ""):
        return ""
    return value


# ── ContextVar for conversation context ─────────────────────────────────────
_conv_context: ContextVar[dict[str, str]] = ContextVar(
    "creator_feedback_context",
    default={},
)


def set_conversation_context(
    project_id: str,
    conversation_id: str,
    session_id: str = "",
) -> None:
    """Set conversation context at _run_message() entry.

    Automatically reads user identity from dogfooding-bundle's SSO data.
    """
    user_data = _read_dogfooding_user_account()
    _conv_context.set({
        "project_id": project_id,
        "conversation_id": conversation_id,
        "session_id": session_id,
        "user_id": user_data["user_account"],
        "user_name": user_data["user_name"],
    })


def get_conversation_context() -> dict[str, str]:
    """Get current conversation context."""
    return _conv_context.get()


# ── AgentTrack initialization ───────────────────────────────────────────────
_agenttrack_initialized = False


def init_agenttrack() -> None:
    """Initialize AgentTrack SDK for OTel span export."""
    global _agenttrack_initialized
    if _agenttrack_initialized:
        return

    try:
        from agenttrack.sdk import AgentTrack
        from traceloop.sdk.instruments import Instruments

        AgentTrack.init(
            app_name=APP_NAME,
            block_instruments={Instruments.TERMINAL_BENCH},
        )
        _agenttrack_initialized = True

        # Read the actual resolved PID from OTel resource attributes
        resolved_pid = "APP_NAME"
        try:
            from opentelemetry import trace as otel_trace

            provider = otel_trace.get_tracer_provider()
            resource = getattr(provider, "resource", None)
            if resource:
                resolved_pid = resource.attributes.get("service.name")
                logger.info("Resolved PID from OTel resource: %s", resolved_pid)
        except Exception:
            pass

        logger.info(
            "================================================\n"
            "  AgentTrack initialized for Creator\n"
            "  PID (resolved): %s\n"
            "  OTel endpoint: sunfire-ingestion-pt-na610.alibaba-inc.com:4318\n"
            "  Feedback recording: ENABLED\n"
            "  JSONL path: %s/records.jsonl\n"
            "================================================",
            resolved_pid,
            _feedback_dir(),
        )
    except ImportError as exc:
        logger.warning(
            "AgentTrack SDK not available: %s. OTel span export disabled.",
            exc,
        )
    except Exception as exc:
        logger.error("Failed to initialize AgentTrack: %s", exc, exc_info=True)


def is_agenttrack_available() -> bool:
    """Check if AgentTrack is initialized."""
    return _agenttrack_initialized


# ── JSONL recording ─────────────────────────────────────────────────────────
def _feedback_dir() -> Path:
    """Return feedback data directory, creating it if necessary.

    Falls back to ~/.qwenpaw/creator-feedback/ if CREATOR_DATA_ROOT is not set.
    """
    try:
        from services.storage_root import require_creator_data_root

        data_root = require_creator_data_root()
        feedback_dir = data_root / "feedback"
    except Exception:
        # Fallback for test environments or when CREATOR_DATA_ROOT is not set
        feedback_dir = Path.home() / ".qwenpaw" / "creator-feedback"

    # Ensure directory exists
    feedback_dir.mkdir(parents=True, exist_ok=True)
    return feedback_dir


def _append_record(record: dict[str, Any]) -> None:
    """Append one JSON line to feedback records."""
    try:
        feedback_dir = _feedback_dir()
        target = feedback_dir / "records.jsonl"
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Failed to write feedback record: %s", exc)


# ── OTel span export ────────────────────────────────────────────────────────
def _emit_otel_span(
    span_name: str,
    attributes: dict[str, Any],
) -> str:
    """Create an OTel span and return its RPC ID."""
    if not _agenttrack_initialized:
        return ""

    try:
        from opentelemetry import trace as otel_trace

        tracer = otel_trace.get_tracer("qwenpaw.creator")
        with tracer.start_as_current_span(span_name) as span:
            if not span.is_recording():
                return ""

            for key, value in attributes.items():
                if value is not None:
                    if isinstance(value, (dict, list)):
                        span.set_attribute(key, json.dumps(value, ensure_ascii=False))
                    else:
                        span.set_attribute(key, value)

            # Force flush to ensure span is exported
            _force_flush_spans()

            # Read span context for correlation
            span_ctx = span.get_span_context()
            if getattr(span_ctx, "is_valid", False):
                return f"{span_ctx.span_id:016x}"
            return ""
    except Exception:
        logger.debug("OTel span export skipped", exc_info=True)
        return ""


def _force_flush_spans() -> None:
    """Best-effort flush so tracking spans reach the OTLP exporter quickly."""
    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider

        provider = otel_trace.get_tracer_provider()
        if not isinstance(provider, SDKTracerProvider):
            return
        provider.force_flush(timeout_millis=5000)
    except Exception:
        logger.debug("Span force_flush failed", exc_info=True)


# ── LLM turn recording ──────────────────────────────────────────────────────
def record_llm_turn(
    *,
    user_message_id: str,
    assistant_message_id: str,
    model_name: str,
    prompt_messages: list[dict],
    response_content: str,
    tool_calls_count: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Record one LLM call (prompt + response) as OTel span + JSONL."""
    ctx = get_conversation_context()
    if not ctx:
        logger.debug("No conversation context, skipping LLM turn recording")
        return

    timestamp = time.time()
    user_id = ctx.get("user_id", "")
    user_name = ctx.get("user_name", "")
    emp_id = _normalize_emp_id(user_id)

    # Build JSONL record
    record: dict[str, Any] = {
        "record_type": "llm_turn",
        "timestamp": timestamp,
        "project_id": ctx["project_id"],
        "conversation_id": ctx["conversation_id"],
        "session_id": ctx.get("session_id", ""),
        "user_id": user_id,
        "user_name": user_name,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "model_name": model_name,
        "prompt": _serialize_messages(prompt_messages),
        "response": response_content,
        "tool_calls_count": tool_calls_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }

    # Build OTel attributes
    attributes: dict[str, Any] = {
        "alibaba.app.name": APP_NAME,
        "gen_ai.span.kind": "LLM",
        "gen_ai.conversation.id": ctx["conversation_id"],
        "gen_ai.request.model": model_name,
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "creator.user_message_id": user_message_id,
        "creator.assistant_message_id": assistant_message_id,
        "creator.prompt": record["prompt"],
        "creator.response": response_content[:2000],
    }
    # Add employee ID for OTel (matches dogfooding-bundle convention)
    if emp_id:
        attributes["alibaba.base.emp_id"] = emp_id
    if user_name:
        attributes["creator.user_name"] = user_name

    # Emit OTel span
    span_id = _emit_otel_span("agent_llm_call", attributes)
    if span_id:
        record["trajectory_span_id"] = span_id

    # Write JSONL
    _append_record(record)

    logger.debug(
        "Recorded LLM turn: conv=%s, msg=%s, model=%s, user=%s, tokens=%d+%d",
        ctx["conversation_id"],
        assistant_message_id,
        model_name,
        user_id or "anonymous",
        input_tokens,
        output_tokens,
    )


def _serialize_messages(messages: list[dict]) -> str:
    """Serialize prompt messages to JSON string."""
    try:
        return json.dumps(messages, ensure_ascii=False)
    except Exception:
        return str(messages)


# ── Feedback submission ─────────────────────────────────────────────────────
SCORE_LABELS = {
    "bad": 1,
    "fine": 2,
    "good": 3,
}

BAD_FEEDBACK_REASONS = [
    "没理解我的意图",
    "任务没有完成",
    "步骤太繁琐",
    "结果有误",
    "回复风格有问题",
    "存在安全风险",
    "响应太慢",
    "其他",
]


def submit_feedback(
    *,
    project_id: str,
    conversation_id: str,
    assistant_message_id: str,
    score_label: str,
    feedback_reason: str = "",
    feedback_comment: str = "",
) -> dict[str, Any]:
    """Submit user feedback for an assistant message."""
    if score_label not in SCORE_LABELS:
        raise ValueError(f"Invalid score_label: {score_label}")

    if score_label == "bad" and not feedback_reason.strip():
        raise ValueError("feedback_reason is required for 'bad' score")

    # Find the corresponding LLM turn record
    llm_turn = _find_llm_turn(assistant_message_id)
    model_name = llm_turn.get("model_name", "") if llm_turn else ""

    # Get user identity from context or LLM turn record
    ctx = get_conversation_context()
    user_id = ctx.get("user_id", "") or (llm_turn.get("user_id", "") if llm_turn else "")
    user_name = ctx.get("user_name", "") or (llm_turn.get("user_name", "") if llm_turn else "")
    emp_id = _normalize_emp_id(user_id)

    timestamp = time.time()

    # Build JSONL record
    record: dict[str, Any] = {
        "record_type": "feedback",
        "timestamp": timestamp,
        "project_id": project_id,
        "conversation_id": conversation_id,
        "assistant_message_id": assistant_message_id,
        "user_id": user_id,
        "user_name": user_name,
        "score_label": score_label,
        "score": SCORE_LABELS[score_label],
        "feedback_reason": feedback_reason.strip(),
        "feedback_comment": feedback_comment.strip(),
        "model_name": model_name,
    }

    # Build OTel attributes
    combined_comment = feedback_reason.strip()
    if feedback_comment.strip():
        if combined_comment:
            combined_comment += "; " + feedback_comment.strip()
        else:
            combined_comment = feedback_comment.strip()

    attributes: dict[str, Any] = {
        "alibaba.app.name": APP_NAME,
        "gen_ai.span.kind": "USER",
        "gen_ai.conversation.id": conversation_id,
        "feedback.score": SCORE_LABELS[score_label],
        "feedback.score_label": score_label,
        "feedback.comment": combined_comment,
        "feedback.response_id": assistant_message_id,
        "gen_ai.request.model": model_name,
    }
    # Add employee ID for OTel (matches dogfooding-bundle convention)
    if emp_id:
        attributes["alibaba.base.emp_id"] = emp_id
    if user_name:
        attributes["creator.user_name"] = user_name

    # Emit OTel span
    span_id = _emit_otel_span("agent_feedback", attributes)
    if span_id:
        record["trajectory_span_id"] = span_id

    # Write JSONL
    _append_record(record)

    logger.info(
        "Feedback submitted: conv=%s, msg=%s, score=%s, user=%s",
        conversation_id,
        assistant_message_id,
        score_label,
        user_id or "anonymous",
    )

    return record


def _find_llm_turn(assistant_message_id: str) -> dict[str, Any] | None:
    """Find the LLM turn record for a given assistant message ID."""
    records_file = _feedback_dir() / "records.jsonl"
    if not records_file.exists():
        return None

    try:
        with records_file.open("r", encoding="utf-8") as fh:
            # Read from end to find most recent match
            lines = fh.readlines()
            for line in reversed(lines):
                try:
                    record = json.loads(line.strip())
                    if (
                        record.get("record_type") == "llm_turn"
                        and record.get("assistant_message_id") == assistant_message_id
                    ):
                        return record
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    return None


def get_feedback(
    project_id: str,
    assistant_message_id: str,
) -> dict[str, Any] | None:
    """Get feedback for a specific assistant message."""
    records_file = _feedback_dir() / "records.jsonl"
    if not records_file.exists():
        return None

    try:
        with records_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line.strip())
                    if (
                        record.get("record_type") == "feedback"
                        and record.get("project_id") == project_id
                        and record.get("assistant_message_id") == assistant_message_id
                    ):
                        return record
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    return None


def get_feedback_reasons() -> list[str]:
    """Return the list of predefined feedback reasons."""
    return BAD_FEEDBACK_REASONS.copy()
