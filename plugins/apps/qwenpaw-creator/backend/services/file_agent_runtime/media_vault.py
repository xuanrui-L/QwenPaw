# -*- coding: utf-8 -*-
"""Durable home for inline media the context guard moves out of a request.

Compaction is not allowed to drop the contract message carrying a screenshot,
so the guard demotes the payload to a pointer instead. That pointer is only
honest while the bytes behind it can still be read back: one content-addressed
write per demoted block, keyed by the SHA-256 of the *decoded* payload, which
is the same digest the pointer advertises.

Nothing here raises at the call site. Losing the durable copy has to degrade
to a pointer without recall, never turn a request that would have worked into
one that fails.
"""

from __future__ import annotations

import base64
import binascii
from hashlib import sha256
from typing import Callable

from domain.errors import StorageIntegrityError, ValidationError
from services.workspace.content_store import ContentStore
from utils.logger import setup_logger

logger = setup_logger("creator.media_vault")

# Blobs are immutable and keyed by content, so the same image demoted in two
# turns of one project occupies exactly one copy on disk.
NAMESPACE = "blob"

MediaVault = Callable[[str], "str | None"]


def split_data_url(url: str) -> tuple[str, bytes] | None:
    """Return ``(mime, payload)`` for a base64 ``data:`` URL, else ``None``."""
    header, _, encoded = url.partition(";base64,")
    if not encoded:
        return None
    try:
        payload = base64.b64decode("".join(encoded.split()), validate=True)
    except (binascii.Error, ValueError):
        # A payload with padding or characters outside the alphabet is not
        # worth a second guess; the pointer still records what it was.
        return None
    mime = header[5:] if header.startswith("data:") else ""
    return mime.split(";")[0].strip() or "application/octet-stream", payload


def digest_for(payload: bytes) -> str:
    return sha256(payload).hexdigest()


# A handle is the only thing a model gets back after demotion, so recall has
# to work from it alone. These signatures cover the containers the Creator
# runtime inlines as media; anything else is returned as opaque bytes rather
# than guessed at, which a provider rejects with an error the model cannot
# read any better than the pointer it came from.
_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"ID3", "audio/mpeg"),
    (b"RIFF", "audio/wav"),
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"%PDF", "application/pdf"),
)


def sniff_mime(payload: bytes) -> str:
    """Best-effort media type from the leading bytes of a stored payload."""
    for magic, media_type in _MAGIC_SIGNATURES:
        if payload.startswith(magic):
            return media_type
    # WEBP is "RIFF" plus a four-byte size and the "WEBP" tag, and MP4 carries
    # the box length before "ftyp": both need the header read, not matched.
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    if payload[4:8] == b"ftyp":
        return "video/mp4"
    return "application/octet-stream"


def normalize_handle(handle: str) -> str:
    """Strip the ``sha256:`` a pointer advertises, leaving the bare digest."""
    value = (handle or "").strip().lower()
    if value.startswith("sha256:"):
        value = value[len("sha256:") :]
    return value


def looks_like_digest(handle: str) -> bool:
    """Whether a pointer's handle can name a stored blob at all.

    Checked here rather than by opening the store, so a handle the model
    retyped wrong is rejected without touching the filesystem.
    """
    digest = normalize_handle(handle)
    return len(digest) == 64 and all(
        char in "0123456789abcdef" for char in digest
    )


def _open_store(store: ContentStore | None) -> ContentStore:
    # Constructing on demand keeps the data root resolution out of import time
    # and lets a caller or a test hand in its own directory.
    return store if store is not None else ContentStore()


def store_data_url(
    url: str,
    *,
    store: ContentStore | None = None,
) -> str | None:
    """Persist one demoted payload and return its 64-hex digest handle."""
    decoded = split_data_url(url)
    if decoded is None:
        return None
    _, payload = decoded
    digest = digest_for(payload)
    try:
        _open_store(store).put_bytes(payload, namespace=NAMESPACE)
    except (StorageIntegrityError, ValidationError, OSError) as exc:
        logger.warning(
            "media vault could not store %s bytes as %s: %s",
            len(payload),
            digest[:12],
            exc,
        )
        return None
    return digest


def load_data_url(
    digest: str,
    mime: str,
    *,
    store: ContentStore | None = None,
) -> str | None:
    """Rebuild a ``data:`` URL from its stored bytes, or ``None`` if absent."""
    try:
        payload = _open_store(store).read_bytes(digest, namespace=NAMESPACE)
    except (StorageIntegrityError, ValidationError, OSError) as exc:
        logger.warning("media vault could not read %s: %s", digest[:12], exc)
        return None
    return (
        f"data:{mime or sniff_mime(payload)};"
        f"base64,{base64.b64encode(payload).decode('ascii')}"
    )


def load_by_handle(
    handle: str,
    *,
    mime: str = "",
    store: ContentStore | None = None,
) -> tuple[str, int] | None:
    """Return ``(data_url, decoded_bytes)`` for one pointer, or ``None``.

    The type is read back off the bytes unless the caller already knows it,
    so a handle on its own is enough to restore what the model can look at.
    A handle the model retyped wrong is refused here, before any store is
    opened: reaching this function is not the same as naming real bytes.
    """
    digest = normalize_handle(handle)
    if not looks_like_digest(digest):
        return None
    try:
        payload = _open_store(store).read_bytes(digest, namespace=NAMESPACE)
    except (StorageIntegrityError, ValidationError, OSError) as exc:
        logger.warning("media vault could not read %s: %s", digest[:12], exc)
        return None
    body = base64.b64encode(payload).decode("ascii")
    declared = (mime or "").strip()
    url = f"data:{declared or sniff_mime(payload)};base64,{body}"
    return url, len(payload)


def make_vault(
    store: ContentStore | None = None,
) -> MediaVault:
    """Bind a store so the context guard can stay free of storage details."""

    def vault(url: str) -> str | None:
        return store_data_url(url, store=store)

    return vault


__all__ = [
    "MediaVault",
    "digest_for",
    "load_by_handle",
    "load_data_url",
    "looks_like_digest",
    "make_vault",
    "normalize_handle",
    "sniff_mime",
    "split_data_url",
    "store_data_url",
]
