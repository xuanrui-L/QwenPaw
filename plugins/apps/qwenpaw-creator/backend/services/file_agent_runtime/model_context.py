# -*- coding: utf-8 -*-
"""Bound ephemeral provider input without changing durable conversations."""

from __future__ import annotations

from copy import deepcopy
import base64
import binascii
import hashlib
import json
import os
import re
from typing import Any, Callable, Mapping, Sequence

from services.project_files.model_view import (
    json_bytes,
    json_text,
    project_snapshot_view,
)

try:
    # The host already prices media the way vision models read it: Qwen-VL 28px
    # patches, audio by duration. Reusing that estimate keeps one answer for
    # "what will this cost" instead of a plugin-local guess that drifts.
    from qwenpaw.agents.context.visual_compression.pipeline.budget import (
        count_text_tokens as _host_count_text_tokens,
    )
    from qwenpaw.agents.utils.media_token_estimate import (
        estimate_data_url_tokens as _host_estimate_data_url_tokens,
    )

    _HOST_CONTEXT_ESTIMATOR_AVAILABLE = True
except ImportError:  # pragma: no cover - standalone plugin installs
    _host_count_text_tokens = None
    _host_estimate_data_url_tokens = None
    _HOST_CONTEXT_ESTIMATOR_AVAILABLE = False

# A conservative serialized-input guard, not a claimed tokenizer count or
# provider context-window size. Leave room for SDK formatting and output.
MODEL_INPUT_BYTES = 384 * 1024
HISTORY_BYTES = 128 * 1024

# Providers bill inline media as vision tokens, not as request bytes. Counting
# a ``data:`` URL by its Base64 text length charges a 2MB image ~2.7MB against
# a 384KB budget, and because media-bearing user messages are contract content
# that compaction may not drop, the request is refused locally and never
# reaches the model — the understanding record just comes back empty. Charge a
# representative text-equivalent instead: decoded bytes / 128, clamped.
# Calibrated against a measured 2MB image costing ~2.5K vision tokens, which is
# roughly 10KB of text budget; the clamp keeps a huge clip from being treated
# as free.
_INLINE_MEDIA_BYTES_DIVISOR = 128
_INLINE_MEDIA_MIN_BYTES = 8 * 1024
_INLINE_MEDIA_MAX_BYTES = 512 * 1024

# The absolute ceiling above predates a fatal arithmetic: it was larger than
# MODEL_INPUT_BYTES itself, so one big screenshot could outgrow the entire
# input budget on its own. Nothing in compaction can reclaim it either, since
# the message carrying it is contract content, which means the request was
# refused locally and never reached the provider. The ceiling therefore has to
# derive from the budget actually in force: a single block must always stay
# small enough that dropping every tool exchange leaves it somewhere to live.
MEDIA_CLAMP_RATIO_ENV = "CREATOR_MEDIA_CLAMP_RATIO"
DEFAULT_MEDIA_CLAMP_RATIO = 0.5
# Tuning the share down is legitimate; tuning it to nothing is not. A ratio
# small enough to charge a screenshot for a few bytes would make every block
# free, which is the failure this guard exists to catch.
MIN_MEDIA_CLAMP_RATIO = 0.05
# Only the ceiling is derived from the budget. The floor stays absolute, so a
# huge clip is never treated as free just because the budget happens to be
# small: several floor-priced blocks must still be able to overflow it.


def _media_clamp_ratio() -> float:
    """Share of the input budget one inline media part may occupy."""
    raw = (os.environ.get(MEDIA_CLAMP_RATIO_ENV) or "").strip()
    if not raw:
        return DEFAULT_MEDIA_CLAMP_RATIO
    try:
        ratio = float(raw)
    except ValueError:
        return DEFAULT_MEDIA_CLAMP_RATIO
    # A nonsensical setting must not disable the guard it exists to provide.
    if not 0 < ratio <= 1:
        return DEFAULT_MEDIA_CLAMP_RATIO
    return min(1.0, max(MIN_MEDIA_CLAMP_RATIO, ratio))


MEDIA_OBSERVATION_DEGRADE_ENV = "CREATOR_MEDIA_OBSERVATION_DEGRADE_ENABLED"

# The input window a token budget can be measured against. A model class in
# the SDK carries a default that has nothing to do with whichever model is
# configured here, so the only accepted source is one an operator wrote down:
# undeclared means the byte guard stays the sole limit.
MODEL_CONTEXT_TOKENS_ENV = "CREATOR_MODEL_CONTEXT_TOKENS"


def declared_context_tokens() -> int | None:
    """The context window declared for this deployment, or ``None``.

    Read per call, like the other knobs, so a restart-free change stays
    possible in tests and a value of 0 or junk means "not declared" rather
    than a budget that refuses every request.
    """
    raw = (os.environ.get(MODEL_CONTEXT_TOKENS_ENV) or "").strip()
    if not raw:
        return None
    try:
        tokens = int(raw)
    except ValueError:
        return None
    return tokens if tokens > 0 else None


def media_observation_degrade_enabled() -> bool:
    """Whether demotion may run at all; off restores the hard local refusal.

    Demotion is visible and recallable rather than silent, but a deployment
    that would rather take the failure and read the diagnostics can switch it
    off without a code change.
    """
    raw = (os.environ.get(MEDIA_OBSERVATION_DEGRADE_ENV) or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _media_charge_bounds(budget_bytes: int) -> tuple[int, int]:
    """Return the ``(floor, ceiling)`` one media part is charged within."""
    ceiling = min(
        _INLINE_MEDIA_MAX_BYTES,
        max(1, int(budget_bytes * _media_clamp_ratio())),
    )
    # A ceiling below the floor would silently un-floor the charge, so the
    # floor follows it down at most as far as the ceiling itself.
    floor = min(_INLINE_MEDIA_MIN_BYTES, ceiling)
    return floor, ceiling


def _data_url_charge(
    url: str,
    *,
    budget_bytes: int = MODEL_INPUT_BYTES,
) -> int:
    """Text-budget representative of one inline ``data:`` URL."""
    _, _, encoded = url.partition(";base64,")
    decoded_bytes = len(encoded) * 3 // 4
    charge = decoded_bytes // _INLINE_MEDIA_BYTES_DIVISOR
    floor, ceiling = _media_charge_bounds(budget_bytes)
    return max(floor, min(charge, ceiling))


def _inline_media_relief(
    node: Any,
    *,
    budget_bytes: int = MODEL_INPUT_BYTES,
) -> int:
    """Bytes the naive JSON count over-charges for inline media parts.

    Subtracted from ``json_bytes`` so both measurements agree on what one
    image or clip actually costs the provider.
    """
    if node is None or isinstance(node, str):
        # Neither can carry relief, and this runs over the whole history of
        # every context estimate.
        return 0
    if isinstance(node, dict):
        relief = 0
        for key, value in node.items():
            if (
                key == "url"
                and isinstance(value, str)
                and value.startswith("data:")
            ):
                # A data URL is ASCII by construction, so the character
                # count is the byte count. Only the hand-edited exception
                # pays for an encode, which would otherwise copy every
                # megabyte of the payload a second time just to measure it.
                raw = (
                    len(value)
                    if value.isascii()
                    else len(value.encode("utf-8"))
                )
                relief += max(
                    0,
                    raw - _data_url_charge(value, budget_bytes=budget_bytes),
                )
            elif isinstance(value, (dict, list)):
                # Only a mapping or a sequence can lead to a ``url`` key, so a
                # plain string of the history is not worth a call: a context
                # estimate walks the whole conversation on every turn.
                relief += _inline_media_relief(
                    value,
                    budget_bytes=budget_bytes,
                )
        return relief
    if isinstance(node, list):
        return sum(
            _inline_media_relief(item, budget_bytes=budget_bytes)
            for item in node
        )
    return 0


def _media_aware_bytes(
    value: Any,
    *,
    budget_bytes: int = MODEL_INPUT_BYTES,
) -> int:
    """Serialized size with inline media charged as vision tokens."""
    return json_bytes(value) - _inline_media_relief(
        value,
        budget_bytes=budget_bytes,
    )


# A block the guard demoted and the Agent then recalled is the runtime's own
# observation again: the pointer can answer for it a second time, so it stays
# as trimmable as any other runtime receipt. The Agent loop labels that
# injection with this source.
MEDIA_RECALL_SOURCE = "media_recall"

# These are runtime receipts carried in the provider's user role, not human
# requirements. Keep durable messages intact, but let old receipts leave the
# bounded history just like old tool results. Unknown sources fail closed.
_AUTOMATED_HISTORY_SOURCES = frozenset(
    {
        "run_review_feedback",
        "render_review_feedback",
        "runtime_notification",
        "mainline_resume",
        "prompt_contract_resume",
        "yolo_auto_resume",
        "review_approval_resume",
        MEDIA_RECALL_SOURCE,
    },
)


class ModelContextBudgetError(ValueError):
    """Local refusal carrying the accounting that produced it.

    The bare message used to be all a caller got, which left every report of
    "the session just errors out" guessing at whether text, tools or one
    oversized image was to blame. ``diagnostics`` names the culprit instead,
    so a caller can demote media knowingly rather than by accident.
    """

    def __init__(
        self,
        message: str,
        *,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


# Matches the same shape the platform estimator reads, so a block counted here
# and a block counted there are the same block.
_DATA_URL_PATTERN = re.compile(
    r"data:([^;,\"']*)?;base64,([A-Za-z0-9+/=]+)",
)
# Providers carry inline media in these part types. Anything not listed keeps
# its payload untouched, since rewriting an unknown shape can corrupt it.
_INLINE_MEDIA_PART_TYPES = frozenset(
    {
        "image_url",
        "image",
        "audio",
        "audio_url",
        "input_audio",
        "video",
        "video_url",
    },
)
# Called with a data URL, returns the 64-hex handle the bytes were stored
# under, or ``None`` when no durable copy exists. Supplied by the runtime that
# owns a data root, so this module keeps doing arithmetic and never touches
# the filesystem.
MediaVault = Callable[[str], "str | None"]


def _data_url_of(node: Any) -> str | None:
    """First inline ``data:`` URL carried by a content part, if any."""
    if isinstance(node, str):
        return node if node.startswith("data:") else None
    if isinstance(node, Mapping):
        direct = node.get("url")
        if isinstance(direct, str) and direct.startswith("data:"):
            return direct
        for value in node.values():
            if isinstance(value, (Mapping, list, tuple)):
                found = _data_url_of(value)
                if found is not None:
                    return found
    elif isinstance(node, (list, tuple)):
        for item in node:
            found = _data_url_of(item)
            if found is not None:
                return found
    return None


def _split_data_url(url: str) -> tuple[str, str]:
    header, _, encoded = url.partition(";base64,")
    mime = header[5:] if header.startswith("data:") else header
    return mime.split(";")[0], encoded


def _decoded_size(encoded: str) -> int:
    # Exact rather than approximate: a pointer advertises how many bytes its
    # handle stands for, and padding is the only thing separating a Base64
    # length from the payload behind it.
    return len(encoded) * 3 // 4 - encoded.count("=")


def _media_digest(url: str) -> str:
    """Content handle for a demoted block.

    Taken over the decoded bytes, which is the key the media vault stores
    under, so the pointer a model reads and the recall answering it cannot
    drift apart. A payload that refuses to decode still gets an identity over
    its text form instead of losing one.
    """
    _, encoded = _split_data_url(url)
    try:
        payload = base64.b64decode("".join(encoded.split()), validate=True)
    except (binascii.Error, ValueError):
        payload = encoded.encode("ascii", errors="ignore")
    return hashlib.sha256(payload).hexdigest()


def _demotion_stub(
    url: str,
    vault: "MediaVault | None" = None,
    *,
    human: bool = False,
) -> tuple[str, str | None]:
    """Pointer text, plus a durable handle when one could be written.

    A human attachment is announced as what it is. A model that finds an
    unexplained hole where a person's screenshot was can conclude the person
    withdrew it and re-plan around a requirement that never changed.
    """
    mime, encoded = _split_data_url(url)
    handle = vault(url) if vault is not None else None
    if handle:
        guidance = (
            f"handle=sha256:{handle}. "
            "Call recall_media with that handle to read it again."
        )
    else:
        guidance = (
            f"fingerprint=sha256:{_media_digest(url)}. "
            "Re-run the producing tool to observe it again."
        )
    label = (
        "user attachment moved out of this request"
        if human
        else "inline media left this context"
    )
    return (
        f"[{label}: {mime or 'unknown type'}, "
        f"{_decoded_size(encoded)} bytes. {guidance}]",
        handle,
    )


def _demote_in_node(
    node: Any,
    vault: "MediaVault | None" = None,
    *,
    human: bool = False,
) -> tuple[Any, list[tuple[str, str | None]]]:
    """Replace inline media under ``node`` with (data URL, handle) records."""
    removed: list[tuple[str, str | None]] = []
    if isinstance(node, str):
        if ";base64," not in node:
            return node, removed

        def replace(match: "re.Match[str]") -> str:
            stub, handle = _demotion_stub(
                match.group(0),
                vault,
                human=human,
            )
            removed.append((match.group(0), handle))
            return stub

        return _DATA_URL_PATTERN.sub(replace, node), removed
    if isinstance(node, Mapping):
        # Every value goes through the same walk, so a bare ``url`` holding a
        # payload turns into a stub instead of vanishing: a part the list
        # branch above did not recognize must still tell the model what left.
        out: dict[str, Any] = {}
        for key, value in node.items():
            replacement, inner = _demote_in_node(
                value,
                vault,
                human=human,
            )
            removed.extend(inner)
            out[key] = replacement
        return out, removed
    if isinstance(node, (list, tuple)):
        out_list: list[Any] = []
        for item in node:
            if (
                isinstance(item, Mapping)
                and str(item.get("type")) in _INLINE_MEDIA_PART_TYPES
            ):
                url = _data_url_of(item)
                if url is not None:
                    stub, handle = _demotion_stub(
                        url,
                        vault,
                        human=human,
                    )
                    out_list.append({"type": "text", "text": stub})
                    removed.append((url, handle))
                    continue
            replacement, inner = _demote_in_node(
                item,
                vault,
                human=human,
            )
            removed.extend(inner)
            out_list.append(replacement)
        return out_list, removed
    return node, removed


def collect_inline_media(
    value: Any,
    *,
    budget_bytes: int = MODEL_INPUT_BYTES,
) -> list[dict[str, Any]]:
    """Every inline block with the charge the budget actually applies to it.

    Shares ``_data_url_charge`` with the accounting path on purpose: an
    explanation that disagreed with what was being billed would be worse than
    no explanation at all.
    """
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            for match in _DATA_URL_PATTERN.finditer(node):
                mime, encoded = _split_data_url(match.group(0))
                found.append(
                    {
                        "mime": mime,
                        "decoded_bytes": _decoded_size(encoded),
                        "charge": _data_url_charge(
                            match.group(0),
                            budget_bytes=budget_bytes,
                        ),
                    },
                )
            return
        if isinstance(node, Mapping):
            for key, child in node.items():
                if (
                    key == "url"
                    and isinstance(child, str)
                    and child.startswith("data:")
                ):
                    mime, encoded = _split_data_url(child)
                    found.append(
                        {
                            "mime": mime,
                            "decoded_bytes": _decoded_size(encoded),
                            "charge": _data_url_charge(
                                child,
                                budget_bytes=budget_bytes,
                            ),
                        },
                    )
                elif isinstance(child, (Mapping, list, tuple)):
                    walk(child)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(value)
    return found


# Same convention the host falls back to, so a plugin running without the host
# package measures the same request the same way.
_TEXT_BYTES_PER_TOKEN = 4.0


def _text_tokens(text: str) -> int:
    if _host_count_text_tokens is not None:
        return _host_count_text_tokens(text)
    if not text:
        return 0
    return max(
        1,
        int(len(text.encode("utf-8")) / _TEXT_BYTES_PER_TOKEN + 0.5),
    )


def _data_url_tokens(url: str) -> int:
    if _host_estimate_data_url_tokens is not None:
        estimated = _host_estimate_data_url_tokens(url)
        if estimated is not None:
            return estimated
    # A payload the host estimator will not price (an exotic container) still
    # has to cost something, so the byte charge converts at the text rate.
    return max(
        1,
        int(
            _data_url_charge(url) / _TEXT_BYTES_PER_TOKEN + 0.5,
        ),
    )


def estimate_input_tokens(value: Any) -> int:
    """Tokens a provider input would cost, inline media priced as vision.

    Bytes and tokens disagree in both directions: a screenshot is megabytes of
    Base64 for a few thousand tokens, while dense CJK text is few bytes for
    many tokens. A guard watching only one of them will keep the wrong thing.
    """
    if value is None:
        return 0
    if isinstance(value, str):
        if not value:
            return 0
        total = 0
        cursor = 0
        for match in _DATA_URL_PATTERN.finditer(value):
            total += _text_tokens(value[cursor : match.start()])
            total += _data_url_tokens(match.group(0))
            cursor = match.end()
        return total + _text_tokens(value[cursor:])
    if isinstance(value, Mapping):
        total = 0
        for key, child in value.items():
            if (
                key == "url"
                and isinstance(child, str)
                and child.startswith("data:")
            ):
                # The same shape the byte accounting walks, so the two
                # measurements cannot diverge on which block is media.
                total += _data_url_tokens(child)
            else:
                total += estimate_input_tokens(child)
        return total
    if isinstance(value, (list, tuple)):
        return sum(estimate_input_tokens(item) for item in value)
    return 0


def _budget_diagnostics(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    *,
    max_bytes: int,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    payload = {"messages": messages, "tools": tools}
    blocks = collect_inline_media(payload, budget_bytes=max_bytes)
    by_role: dict[str, int] = {}
    for item in messages:
        role = str(item.get("role") or "?")
        by_role[role] = by_role.get(role, 0) + _media_aware_bytes(
            item,
            budget_bytes=max_bytes,
        )
    return {
        "max_bytes": max_bytes,
        "used_bytes": _media_aware_bytes(payload, budget_bytes=max_bytes),
        "raw_bytes": json_bytes(payload),
        "max_tokens": max_tokens,
        "input_tokens": estimate_input_tokens(payload),
        "role_bytes": by_role,
        "media_block_count": len(blocks),
        "media_charge_bytes": sum(int(b["charge"]) for b in blocks),
        # The most expensive blocks first: the one worth shrinking is nearly
        # always at the head, and a long list would bury it.
        "media_blocks": sorted(
            blocks,
            key=lambda b: int(b["charge"]),
            reverse=True,
        )[:5],
    }


# Recorded on the message that carried the block and kept through history
# compaction by the metadata whitelist below, so "what left this context"
# stays answerable several turns after the demotion happened.
DEMOTED_MEDIA_METADATA_KEY = "demotedMedia"
MAX_RECORDED_DEMOTIONS = 8


def _handle_record(url: str, handle: str, *, turn: Any) -> dict[str, Any]:
    """What a pointer promises: the bytes, the price, where they came from."""
    mime, encoded = _split_data_url(url)
    return {
        "handle": handle,
        "mime": mime or "application/octet-stream",
        "decodedBytes": _decoded_size(encoded),
        "estimatedTokens": _data_url_tokens(url),
        "messageSeq": turn,
    }


def _demotion_eligible(record: Mapping[str, Any]) -> bool:
    """Whether this message's media is the runtime's own observation.

    Tool results and the receipts the runtime carries in the user role can be
    observed again by re-running the tool that produced them. A requirement a
    person typed or uploaded cannot, so it stays out of this set.
    """
    return record.get("role") == "tool" or (
        record.get("role") == "user"
        and record.get("source") in _AUTOMATED_HISTORY_SOURCES
    )


def _record_demotions(
    record: dict[str, Any],
    removed: list[tuple[str, str | None]],
) -> list[dict[str, Any]]:
    """Attach the stored handles to the message that lost its media."""
    handles = [
        _handle_record(url, handle, turn=record.get("messageSeq"))
        for url, handle in removed
        if handle
    ]
    if not handles:
        return []
    metadata = dict(record.get("metadata") or {})
    previous = metadata.get(DEMOTED_MEDIA_METADATA_KEY)
    listed = [*handles, *(previous if isinstance(previous, list) else [])]
    metadata[DEMOTED_MEDIA_METADATA_KEY] = listed[:MAX_RECORDED_DEMOTIONS]
    record["metadata"] = metadata
    return handles


def demote_derived_inline_media(
    messages: Sequence[Mapping[str, Any]],
    *,
    budget_bytes: int = MODEL_INPUT_BYTES,
    vault: "MediaVault | None" = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Move inline media out of the messages compaction is allowed to trim.

    Compaction stops at contract content, so a refused request carrying a
    derived screenshot has no other relief available. This demotes only what
    can be observed again by re-running a tool; a human attachment is left to
    ``demote_human_inline_media``, which additionally requires a durable copy.

    ``vault`` writes a payload down and returns its handle. Without one the
    pointer still describes what left, just without a recall to offer.
    """
    result: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    untouched: list[dict[str, Any]] = []
    handles: list[dict[str, Any]] = []
    for item in messages:
        record = dict(item)
        blocks = collect_inline_media(
            record.get("content"),
            budget_bytes=budget_bytes,
        )
        if not blocks:
            result.append(record)
            continue
        if not _demotion_eligible(record):
            untouched.extend(blocks)
            result.append(record)
            continue
        content, removed = _demote_in_node(record.get("content"), vault)
        if removed:
            record["content"] = content
            demoted.extend(blocks)
            handles.extend(_record_demotions(record, removed))
        result.append(record)
    return result, {
        "demoted_blocks": len(demoted),
        "demoted_charge_bytes": sum(int(b["charge"]) for b in demoted),
        "stored_blocks": len(handles),
        "human_attachment_blocks": len(untouched),
        "handles": handles,
    }


def demote_human_inline_media(
    messages: Sequence[Mapping[str, Any]],
    *,
    vault: "MediaVault | None",
    budget_bytes: int = MODEL_INPUT_BYTES,
    one_step: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Last relief under real pressure: move a human attachment, or leave it.

    A message is demoted whole or not at all, because a partially shorn
    requirement reads like a person who changed their mind. A block whose
    bytes could not be stored keeps its payload: the budget is allowed to
    fail, an upload a human made is not allowed to disappear quietly.

    ``one_step`` moves the single most expensive attachment and stops. The
    caller re-checks the real budget after every step, so a request that fits
    once one picture has left does not lose the rest of a person's uploads.
    """
    result: list[dict[str, Any]] = [dict(item) for item in messages]
    candidates: list[tuple[int, int]] = []
    for index, record in enumerate(result):
        if _demotion_eligible(record):
            continue
        blocks = collect_inline_media(
            record.get("content"),
            budget_bytes=budget_bytes,
        )
        if blocks:
            candidates.append(
                (index, sum(int(block["charge"]) for block in blocks)),
            )

    moved: list[dict[str, Any]] = []
    handles: list[dict[str, Any]] = []
    refused = 0

    def attempt(index: int) -> bool:
        nonlocal refused
        record = result[index]
        blocks = collect_inline_media(
            record.get("content"),
            budget_bytes=budget_bytes,
        )
        content, removed = (
            _demote_in_node(record.get("content"), vault, human=True)
            if vault is not None
            else (None, [])
        )
        if not removed or not all(handle for _, handle in removed):
            refused += len(blocks)
            return False
        record["content"] = content
        moved.extend(blocks)
        handles.extend(_record_demotions(record, removed))
        return True

    ordered = (
        sorted(candidates, key=lambda item: -item[1])
        if one_step
        else candidates
    )
    for index, _charge in ordered:
        if attempt(index) and one_step:
            break
    return result, {
        "demoted_blocks": len(moved),
        "demoted_charge_bytes": sum(int(block["charge"]) for block in moved),
        "stored_blocks": len(handles),
        "refused_blocks": refused,
        "handles": handles,
    }


def _format_bytes(value: Any) -> str:
    return f"{int(value):,}"


def format_budget_failure(exc: ModelContextBudgetError) -> str:
    """Render a local refusal as something a user can act on."""
    info = exc.diagnostics
    if not info:
        return str(exc)
    lines = [
        str(exc),
        (
            f"预算 {_format_bytes(info.get('max_bytes', 0))} 字节，"
            f"实际 {_format_bytes(info.get('used_bytes', 0))} 字节"
            f"（未计媒体时 {_format_bytes(info.get('raw_bytes', 0))} 字节）。"
        ),
    ]
    token_limit = info.get("max_tokens")
    if token_limit:
        lines.append(
            "按模型窗口计：输入 "
            f"{_format_bytes(info.get('input_tokens', 0))} token，"
            f"上限 {_format_bytes(token_limit)} token。",
        )
    roles = info.get("role_bytes") or {}
    if roles:
        lines.append(
            "按角色占用："
            + "、".join(
                f"{role} {_format_bytes(size)}"
                for role, size in sorted(
                    roles.items(),
                    key=lambda kv: int(kv[1]),
                    reverse=True,
                )
            ),
        )
    blocks = info.get("media_blocks") or []
    if blocks:
        priciest = "、".join(
            (
                f"{block.get('mime') or '未知类型'} "
                f"{_format_bytes(block.get('decoded_bytes', 0))} 字节"
                f"→计费 {_format_bytes(block.get('charge', 0))}"
            )
            for block in blocks[:3]
        )
        lines.append(
            "内联媒体 "
            f"{info.get('media_block_count', len(blocks))} 块，"
            f"共计费 {_format_bytes(info.get('media_charge_bytes', 0))} 字节，"
            f"最贵的：{priciest}",
        )
        lines.append(
            "可压缩的工具交换已清空，超出部分由上述媒体或必要契约输入承担。",
        )
    return "\n".join(lines)


def media_could_bridge_the_gap(diagnostics: Mapping[str, Any]) -> bool:
    """Whether retiring inline media could plausibly close a refusal's gap.

    The accounting that names the overage also names what every inline block
    costs, so a request overflowing on contract text can be told apart from
    one overflowing on a screenshot. Only the second kind earns a human upload
    being moved: an attachment cannot shrink text by a byte, so demoting the
    whole batch spends the uploads on the way to the same failure.

    A refusal carrying no diagnostics answers ``True``, so the ladder runs
    exactly as it did before this gate existed.
    """
    if not diagnostics:
        return True
    used = int(diagnostics.get("used_bytes") or 0)
    ceiling = int(diagnostics.get("max_bytes") or 0)
    charged = int(diagnostics.get("media_charge_bytes") or 0)
    if used <= ceiling:
        # The token guard refused, not the byte guard. Media is still the only
        # thing this ladder can move, and a byte charge is not comparable to a
        # token gap, so the guard is not dismissed here.
        return True
    return charged >= used - ceiling


def format_demotion_record(
    entries: Sequence[tuple[str, Mapping[str, Any]]],
) -> str:
    """Say what the guard already gave up, so a refusal is locatable.

    A refusal that arrives after two demotion steps means the surviving input
    is not what the caller handed in. Without the record, "it keeps erroring"
    has no way to show which blocks left and where their bytes are.
    """
    parts: list[str] = []
    handles: list[str] = []
    for label, receipt in entries:
        count = int(receipt.get("demoted_blocks") or 0)
        stored = int(receipt.get("stored_blocks") or 0)
        if not count and not stored:
            continue
        parts.append(f"{label} {count} 块（已存档 {stored} 块）")
        for item in receipt.get("handles") or []:
            if isinstance(item, Mapping) and item.get("handle"):
                handles.append(str(item["handle"]))
    if not parts:
        return ""
    text = "媒体降级记录：" + "；".join(parts)
    if handles:
        shown = "、".join(f"sha256:{handle[:12]}" for handle in handles[:3])
        text += f"；句柄 {shown}" + (" 等" if len(handles) > 3 else "")
    return text


def _snapshot_content(content: Any) -> Mapping[str, Any] | None:
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                snapshot = _snapshot_content(part.get("text"))
                if snapshot is not None:
                    return snapshot
        return None
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except ValueError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("project"), dict):
        if payload["project"].get("project_id") and payload.get("etag"):
            return payload
    return None


def compact_conversation_history(
    history: list[dict[str, Any]],
    *,
    max_bytes: int = HISTORY_BYTES,
    max_tokens: int | None = None,
) -> list[dict[str, Any]]:
    result = deepcopy(history)
    latest_snapshot: int | None = None
    for index, item in enumerate(result):
        # Streaming/raw argument diagnostics belong to tracing, not context.
        item["metadata"] = {
            key: value
            for key, value in (item.get("metadata") or {}).items()
            if key
            in {
                "toolName",
                "tool",
                "resultKind",
                "transactionId",
                "reviewId",
                "status",
                # A pointer is the only place a demoted attachment can be
                # found from, so compaction must not strip it on the way.
                DEMOTED_MEDIA_METADATA_KEY,
            }
        }
        for part in item.get("content") or []:
            if isinstance(part, dict) and _snapshot_content(part.get("text")):
                latest_snapshot = index
    if _media_aware_bytes(result, budget_bytes=max_bytes) <= max_bytes and (
        max_tokens is None or estimate_input_tokens(result) <= max_tokens
    ):
        return result
    # Preserve every human goal/constraint and the newest state. Runtime
    # feedback also uses the user role; retaining every old review would make
    # a long production exceed the hard budget even after all tools are gone.
    # The current request/notification batch is outside this history view.
    removed = 0
    # Relief is a plain sum over list items and an elided slot contributes
    # zero, so carry it across the loop instead of re-walking every message
    # on each pass; json_bytes stays the loop's only full traversal, exactly
    # as it was before inline media entered the budget.
    relief = _inline_media_relief(result, budget_bytes=max_bytes)
    for index, item in enumerate(result):
        human_input = (
            item.get("role") == "user"
            and item.get("source") not in _AUTOMATED_HISTORY_SOURCES
        )
        if human_input or index == latest_snapshot:
            continue
        if json_bytes(result) - relief <= max_bytes - 256 and (
            max_tokens is None or estimate_input_tokens(result) <= max_tokens
        ):
            break
        result[index] = None
        relief -= _inline_media_relief(item, budget_bytes=max_bytes)
        removed += 1
    return [
        {
            "role": "system",
            "source": "context_compaction",
            "elidedMessageCount": removed,
        },
        *(item for item in result if item is not None),
    ]


def _shrink_continuation(
    content: str,
    max_bytes: int,
    latest_snapshot: Mapping[str, Any] | None,
    max_tokens: int | None = None,
) -> str:
    marker = "CONVERSATION_HISTORY_JSON="
    ending = "\n\nCURRENT_USER_REQUEST=\n"
    if marker not in content or ending not in content:
        return content
    opening, rest = content.split(marker, 1)
    raw, request = rest.split(ending, 1)
    try:
        history = json.loads(raw)
    except ValueError:
        return content
    if not isinstance(history, list):
        return content
    if latest_snapshot:
        for item in history:
            # Human messages may contain JSON too; never rewrite them.
            if item.get("role") != "tool":
                continue
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                previous = _snapshot_content(part.get("text"))
                if previous and _snapshot_supersedes(
                    latest_snapshot,
                    previous,
                ):
                    part["text"] = json_text(
                        {
                            "resultKind": "project_change_receipt",
                            "projectId": previous["project"]["project_id"],
                            "generation": previous.get("generation"),
                            "etag": previous.get("etag"),
                            "note": "A newer Project view is in this run.",
                        },
                    )
    return (
        opening
        + marker
        + json_text(
            compact_conversation_history(
                history,
                max_bytes=max_bytes,
                max_tokens=max_tokens,
            ),
        )
        + ending
        + request
    )


def _snapshot_supersedes(
    current: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> bool:
    current_generation = current.get("generation")
    previous_generation = previous.get("generation")
    return (
        current["project"]["project_id"] == previous["project"]["project_id"]
        and isinstance(current_generation, int)
        and isinstance(previous_generation, int)
        and current_generation >= previous_generation
    )


# Preserve complete protocol groups throughout each compaction stage.
# pylint: disable-next=too-many-branches
def prepare_model_messages(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    *,
    max_bytes: int = MODEL_INPUT_BYTES,
    max_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """Drop completed tool exchanges as units; retain user/system contracts.

    Large completed calls include their arguments, so deleting just an old
    tool result is insufficient. Both call and results leave together. A
    removed latest Project view is reattached separately for reconstruction.
    """
    result = deepcopy([dict(item) for item in messages])
    latest_snapshot = None
    for item in result:
        content = item.get("content")
        if item.get("role") != "tool":
            continue
        parts = (
            [{"type": "text", "text": content}]
            if isinstance(content, str)
            else content
        )
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            payload = _snapshot_content(part.get("text"))
            if payload:
                part["text"] = json_text(
                    project_snapshot_view(
                        payload,
                        max_bytes=min(64 * 1024, max_bytes // 4),
                    ),
                )
                latest_snapshot = json.loads(part["text"])
        if isinstance(content, str):
            item["content"] = parts[0]["text"]

    # A prior-run snapshot must not be pinned alongside its replacement.
    # That duplicate can evict the script, skills and references just read,
    # trapping long productions in repeated reconstruction instead of work.
    for item in result:
        content = item.get("content")
        if item.get("role") == "user" and isinstance(content, str):
            item["content"] = _shrink_continuation(
                content,
                min(HISTORY_BYTES, max_bytes // 4),
                latest_snapshot,
                None if max_tokens is None else max_tokens // 4,
            )

    def request_payload() -> dict[str, Any]:
        return {"messages": result, "tools": tools}

    def size() -> int:
        # Inline media is charged as vision tokens, not as Base64 text, so a
        # media-bearing contract message can fit alongside the tools schema.
        return _media_aware_bytes(request_payload(), budget_bytes=max_bytes)

    def over_budget() -> bool:
        if size() > max_bytes:
            return True
        # Bytes miss the mistake in both directions: a screenshot is megabytes
        # of Base64 for a few thousand vision tokens, while dense text is the
        # reverse. The declared window is the second guard, and it stays off
        # until an operator states it (see ``declared_context_tokens``).
        return max_tokens is not None and (
            estimate_input_tokens(request_payload()) > max_tokens
        )

    carried_snapshot = None
    while over_budget():
        removable = None
        for start, item in enumerate(result):
            if item.get("role") != "assistant":
                continue
            calls = item.get("tool_calls") or []
            end = start + 1
            if calls:
                while end < len(result) and result[end].get("role") == "tool":
                    end += 1
                wanted = {call.get("id") for call in calls}
                results_start = start + 1
                received = {
                    row.get("tool_call_id")
                    for row in result[results_start:end]
                }
                if wanted != received:
                    continue  # Never split an outstanding native tool group.
            removable = (start, end)
            break
        if removable is None:
            raise ModelContextBudgetError(
                "模型必要输入仍超过单次上下文预算；请缩小单次请求或分段读取项目。",
                diagnostics=_budget_diagnostics(
                    result,
                    tools,
                    max_bytes=max_bytes,
                    max_tokens=max_tokens,
                ),
            )
        start, end = removable
        for item in result[start:end]:
            payload = _snapshot_content(item.get("content"))
            if payload:
                carried_snapshot = latest_snapshot
        del result[start:end]
        # Keep only the newest carried view, and count it in the next check.
        result = [item for item in result if not item.get("_context_snapshot")]
        if carried_snapshot:
            result.append(
                {
                    "role": "user",
                    "_context_snapshot": True,
                    "content": "CURRENT_PROJECT_MODEL_VIEW="
                    + json_text(carried_snapshot),
                },
            )
    for item in result:
        item.pop("_context_snapshot", None)
    return result
