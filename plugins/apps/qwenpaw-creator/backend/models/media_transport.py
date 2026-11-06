# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long,too-many-return-statements
import asyncio
import base64
import hashlib
import io
import ipaddress
import mimetypes
import re
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse, urlunsplit

import httpx
from PIL import Image, ImageOps

from models import config as model_config
from services.runtime_files.safe_remote_download import safe_download_bytes
from utils.logger import setup_logger
from utils.paths import local_path_from_file_url, media_path_from_url

logger = setup_logger("models.media_transport")

OSS_POLICY_URL = "https://dashscope.aliyuncs.com/api/v1/uploads"
DEFAULT_CREATOR_MEDIA_BUCKET = "creator-store"
WAN_MEDIA_PREFIX = "wan_media"
SD2_MEDIA_PREFIX = "sd2_media"
GROUNDING_LENS_MEDIA_PREFIX = "grounding_lens"
UGUU_UPLOAD_URL = "https://uguu.se/upload"
UGUU_UPLOAD_MAX_ATTEMPTS = 5
UGUU_RETRY_BACKOFF_CAP_SECONDS = 10.0
# When configured, Creator OSS keeps Lens media private behind a short-lived
# signed URL; callers use Uguu only when OSS configuration is entirely absent.
GROUNDING_LENS_SIGNED_URL_EXPIRES_SECONDS = 15 * 60
DASHSCOPE_TEMP_UPLOAD_MAX_BYTES = 1024 * 1024 * 1024
DASHSCOPE_TEMP_UPLOAD_CACHE_SECONDS = 47 * 60 * 60
# Ark (Volcengine) Seedance accepts Base64 data URLs for reference images:
# a single image must stay below 30MB and the request body below 64MB.
SEEDANCE_REFERENCE_IMAGE_MAX_BYTES = 30 * 1024 * 1024
REFERENCE_IMAGE_DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024

# Inline images step down this ladder (longest edge in px, JPEG quality)
# until the encoded bytes fit the caller's budget. Providers bill vision
# tokens rather than request bytes, so a downscaled image still answers the
# question whereas a hard refusal leaves the understanding record silently
# empty. Mirrors the video clip ladder in services.media.source_observation.
IMAGE_INLINE_LADDER = (
    (2048, 85),
    (1536, 80),
    (1024, 75),
    (768, 70),
)
# A video *generation* reference is the visual basis of the output rather
# than a question to answer, so it keeps more detail than the VLM ladder
# above. The 1600px floor is a provider constraint, not a quality
# preference: HappyHorse r2v rejects a short side under 400px, and 1600
# still leaves 640px at the widest legal 2.5:1 aspect ratio.
VIDEO_REFERENCE_INLINE_LADDER = (
    (4096, 92),
    (3200, 88),
    (2560, 85),
    (2048, 82),
    (1600, 78),
)

_dashscope_temp_upload_cache: dict[
    tuple[str, int, int, str, str],
    tuple[str, float],
] = {}
_dashscope_temp_upload_locks: dict[
    tuple[str, int, int, str, str],
    asyncio.Lock,
] = {}
_dashscope_credential_tokens: dict[str, str] = {}
# Process-local salt for credential cache tokens; never persisted.
_CREDENTIAL_TOKEN_SALT = uuid.uuid4().bytes


async def get_oss_policy(model: str = "wan2.7-r2v") -> dict:
    api_key = model_config.get_oss_policy_api_key()
    if not api_key:
        raise RuntimeError(
            "creator_media_oss.policy_api_key or OSS_POLICY_API_KEY is required for OSS upload policy",
        )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            OSS_POLICY_URL,
            params={"action": "getPolicy", "model": model},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        return resp.json().get("data", resp.json())


async def upload_file_to_oss(
    file_content: bytes,
    filename: str,
    policy_data: dict,
) -> str:
    """Upload a file to OSS using the policy credentials. Returns oss:// URL."""
    upload_host = policy_data["upload_host"]
    upload_dir = policy_data["upload_dir"]
    key = f"{upload_dir}/{filename}"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            upload_host,
            data={
                "OSSAccessKeyId": policy_data["oss_access_key_id"],
                "Signature": policy_data["signature"],
                "policy": policy_data["policy"],
                "x-oss-object-acl": policy_data.get(
                    "x_oss_object_acl",
                    "default",
                ),
                "x-oss-forbid-overwrite": policy_data.get(
                    "x_oss_forbid_overwrite",
                    "true",
                ),
                "key": key,
                "success_action_status": "200",
            },
            files={"file": (filename, file_content)},
        )
        resp.raise_for_status()

    return f"oss://{key}"


def _dashscope_transport_filename(path: Path, media_type: str) -> str:
    filename = path.name or "selected-media"
    if not Path(filename).suffix:
        extension = mimetypes.guess_extension(media_type) or ".bin"
        filename = f"{filename}{extension}"
    return _safe_filename(filename)


def _credential_cache_token(api_key: str) -> str:
    """Opaque per-process stand-in for *api_key* inside cache keys.

    Keys are salted PBKDF2 digests so raw credentials never persist in
    process memory maps and cannot be cheaply brute-forced; values are
    random tokens that keep entries isolated.
    """
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        api_key.encode("utf-8"),
        _CREDENTIAL_TOKEN_SALT,
        50_000,
    ).hex()
    if len(_dashscope_credential_tokens) >= 256:
        _dashscope_credential_tokens.clear()
    return _dashscope_credential_tokens.setdefault(digest, uuid.uuid4().hex)


def _prune_dashscope_temp_upload_cache(now: float) -> None:
    expired = [
        key
        for key, (_, deadline) in _dashscope_temp_upload_cache.items()
        if deadline <= now
    ]
    for key in expired:
        _dashscope_temp_upload_cache.pop(key, None)
        lock = _dashscope_temp_upload_locks.get(key)
        if lock is not None and not lock.locked():
            _dashscope_temp_upload_locks.pop(key, None)


def _dashscope_temp_upload_cache_key(
    path: Path,
    *,
    api_key: str,
    model_name: str,
) -> tuple[str, int, int, str, str]:
    stat = path.stat()
    credential_fingerprint = _credential_cache_token(api_key)
    return (
        str(path.resolve()),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        model_name,
        credential_fingerprint,
    )


def _mask_key(key: str) -> str:
    if not key:
        return "(empty)"
    return "[redacted]"


def _fetch_dashscope_upload_policy(
    client: httpx.Client,
    *,
    api_key: str,
    model_name: str,
    size: int,
) -> dict:
    """Fetch the model-bound upload policy and enforce its size limit."""
    logger.info(
        "DashScope upload policy request | url=%s, model=%s, api_key=%s, size=%d",
        OSS_POLICY_URL,
        model_name,
        _mask_key(api_key),
        size,
    )
    policy_response = client.get(
        OSS_POLICY_URL,
        params={"action": "getPolicy", "model": model_name},
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    logger.info(
        "DashScope upload policy response | status=%d",
        policy_response.status_code,
    )
    policy_response.raise_for_status()
    payload = policy_response.json()
    policy = payload.get("data", payload)
    try:
        max_policy_bytes = int(
            float(policy["max_file_size_mb"]) * 1024 * 1024,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "DashScope upload policy missing max_file_size_mb",
        ) from exc
    if size > max_policy_bytes:
        raise RuntimeError(
            "media exceeds the current model-bound DashScope upload "
            f"policy ({size} bytes > {max_policy_bytes} bytes); provide a safe "
            "public nativeModelUrl/publicSourceUrl",
        )
    return policy


def _post_dashscope_temp_upload(
    client: httpx.Client,
    policy: dict,
    filename: str,
    file_source: object,
    media_type: str,
) -> str:
    """POST one payload to the policy's temporary OSS. Returns oss:// URL."""
    upload_dir = str(policy["upload_dir"]).rstrip("/")
    key = f"{upload_dir}/{uuid.uuid4().hex}-{filename}"
    form = {
        "OSSAccessKeyId": str(policy["oss_access_key_id"]),
        "Signature": str(policy["signature"]),
        "policy": str(policy["policy"]),
        "x-oss-object-acl": str(policy.get("x_oss_object_acl", "private")),
        "x-oss-forbid-overwrite": str(
            policy.get("x_oss_forbid_overwrite", "true"),
        ),
        "key": key,
        "success_action_status": "200",
    }
    upload_response = client.post(
        str(policy["upload_host"]),
        data=form,
        files={"file": (filename, file_source, media_type)},
    )
    upload_response.raise_for_status()
    return f"oss://{key}"


def _upload_local_file_to_dashscope_temp_sync(
    path: Path,
    *,
    api_key: str,
    model_name: str,
    media_type: str,
) -> str:
    """Stream one local file to DashScope's model-bound temporary OSS."""

    size = path.stat().st_size
    if size > DASHSCOPE_TEMP_UPLOAD_MAX_BYTES:
        raise RuntimeError(
            "DashScope temporary upload rejects files larger than 1GB; "
            "provide a safe public nativeModelUrl/publicSourceUrl instead",
        )
    logger.info(
        "DashScope temp upload start | path=%s, size=%d, model=%s, media_type=%s",
        path,
        size,
        model_name,
        media_type,
    )
    timeout = httpx.Timeout(connect=30.0, read=300.0, write=3600.0, pool=30.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        policy = _fetch_dashscope_upload_policy(
            client,
            api_key=api_key,
            model_name=model_name,
            size=size,
        )
        filename = _dashscope_transport_filename(path, media_type)
        with path.open("rb") as file_handle:
            result = _post_dashscope_temp_upload(
                client,
                policy,
                filename,
                file_handle,
                media_type,
            )
            logger.info("DashScope temp upload complete | result=%s", result)
            return result


async def upload_local_file_to_dashscope_temp(
    path: Path,
    *,
    api_key: str,
    model_name: str,
    media_type: str,
) -> str:
    """Return a cached 48-hour provider URL without reading the file into memory."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"selected local media does not exist: {resolved}",
        )
    if not api_key.strip() or not model_name.strip():
        raise RuntimeError(
            "DashScope temporary upload requires API key and model name",
        )
    cache_key = _dashscope_temp_upload_cache_key(
        resolved,
        api_key=api_key,
        model_name=model_name,
    )
    now = time.monotonic()
    _prune_dashscope_temp_upload_cache(now)
    cached = _dashscope_temp_upload_cache.get(cache_key)
    if cached is not None and cached[1] > now:
        return cached[0]
    lock = _dashscope_temp_upload_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _dashscope_temp_upload_cache.get(cache_key)
        if cached is not None and cached[1] > now:
            return cached[0]
        url = await asyncio.to_thread(
            _upload_local_file_to_dashscope_temp_sync,
            resolved,
            api_key=api_key,
            model_name=model_name,
            media_type=media_type,
        )
        _dashscope_temp_upload_cache[cache_key] = (
            url,
            now + DASHSCOPE_TEMP_UPLOAD_CACHE_SECONDS,
        )
        return url


def _reference_media_type(filename: str, content: bytes) -> str:
    media_type = mimetypes.guess_type(filename or "")[0]
    if not media_type:
        media_type = mimetypes.guess_type(
            f"reference{_suffix_from_magic(content)}",
        )[0]
    return media_type or "application/octet-stream"


def _upload_reference_bytes_to_dashscope_temp_sync(
    content: bytes,
    filename: str,
    *,
    api_key: str,
    model_name: str,
) -> str:
    size = len(content)
    if size > DASHSCOPE_TEMP_UPLOAD_MAX_BYTES:
        raise RuntimeError(
            "DashScope temporary upload rejects files larger than 1GB",
        )
    media_type = _reference_media_type(filename, content)
    logger.info(
        "DashScope reference upload start | filename=%s, size=%d, model=%s, media_type=%s",
        filename,
        size,
        model_name,
        media_type,
    )
    timeout = httpx.Timeout(connect=30.0, read=300.0, write=3600.0, pool=30.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        policy = _fetch_dashscope_upload_policy(
            client,
            api_key=api_key,
            model_name=model_name,
            size=size,
        )
        result = _post_dashscope_temp_upload(
            client,
            policy,
            _safe_filename(filename),
            content,
            media_type,
        )
        logger.info("DashScope reference upload complete | result=%s", result)
        return result


async def upload_reference_bytes_to_dashscope_temp(
    content: bytes,
    filename: str,
    *,
    api_key: str,
    model_name: str,
) -> str:
    """Upload in-memory reference media to DashScope's model-bound temp OSS.

    Official Bailian temporary-file upload (getPolicy + form POST). The URL is
    an ``oss://`` reference valid for 48 hours (files up to 1GB) and is only
    resolvable by the bound model when the request carries the
    ``X-DashScope-OssResourceResolve: enable`` header.
    """
    if not api_key.strip() or not model_name.strip():
        raise RuntimeError(
            "DashScope temporary upload requires API key and model name",
        )
    return await asyncio.to_thread(
        _upload_reference_bytes_to_dashscope_temp_sync,
        content,
        filename,
        api_key=api_key,
        model_name=model_name,
    )


# ── AgentScope model-proxy media upload ─────────────────────────────────────
# The proxy carries the DashScope wire format, but its ``sk-as-`` key has no
# identity on the Bailian temporary-upload API (measured on platform-pre:
# getPolicy answers 401 InvalidApiKey), so an ``oss://`` reference can never be
# minted for it. Its own endpoint returns an anonymously readable public URL,
# and the proxy fetches public URLs server-side (measured: an unreachable
# reference answers FAILED "Failed to download <url>" before any render).
PLATFORM_MEDIA_UPLOAD_PATH = "/v1/media/uploads"
# The endpoint sits behind a 32MB nginx envelope rather than the 1GB temp-OSS
# budget, so the smaller cap has to be enforced here instead of upstream.
PLATFORM_MEDIA_UPLOAD_MAX_BYTES = 30 * 1024 * 1024
# The response carries no expiry field. An hour is long enough to reuse one
# character sheet across every segment of a run, and short enough that a bucket
# sweep cannot strand a paid render on a dead reference URL.
PLATFORM_MEDIA_UPLOAD_CACHE_SECONDS = 60 * 60

_platform_media_upload_cache: dict[tuple[str, str], tuple[str, float]] = {}
_platform_media_upload_locks: dict[tuple[str, str], asyncio.Lock] = {}


def platform_media_upload_url(base_url: str) -> str:
    """Return the proxy's upload endpoint for one configured section base.

    Image and video bases are saved either as the API root (``/v1``) or as the
    full generation endpoint, so the path is rebuilt from the origin instead of
    appended to whatever the caller stored.
    """
    parsed = urlparse(base_url.strip())
    if not parsed.netloc:
        raise RuntimeError(
            f"cannot derive the media-upload endpoint from base URL: {base_url!r}",
        )
    scheme = parsed.scheme or "https"
    return f"{scheme}://{parsed.netloc}{PLATFORM_MEDIA_UPLOAD_PATH}"


def _prune_platform_media_upload_cache(now: float) -> None:
    expired = [
        key
        for key, (_, deadline) in _platform_media_upload_cache.items()
        if deadline <= now
    ]
    for key in expired:
        _platform_media_upload_cache.pop(key, None)
        lock = _platform_media_upload_locks.get(key)
        if lock is not None and not lock.locked():
            _platform_media_upload_locks.pop(key, None)


def _platform_media_upload_cache_key(
    *,
    identity: str,
    api_key: str,
) -> tuple[str, str]:
    """Cache identity for one upload.

    Unlike a Bailian temporary upload - which is bound to the model its policy
    was issued for - a proxy upload URL is model-agnostic and anonymously
    fetchable, so one entry can serve every consumer of the same bytes.
    """
    return identity, _credential_cache_token(api_key)


@dataclass(frozen=True, slots=True)
class _PinnedDestination:
    """One upload target whose address was validated before it was used.

    ``url`` carries a literal address, so the socket cannot be re-resolved to
    something the policy never saw after the check already ran;
    ``host_header`` and ``sni_hostname`` keep the configured endpoint's own
    name in the request and in the TLS handshake, so binding the address
    changes nothing the provider can observe.
    """

    url: str
    host_header: str
    sni_hostname: str
    addresses: tuple[str, ...]


def _addressed_url(upload_url: str, address: str) -> str:
    """Rewrite *upload_url*'s authority to the literal *address*."""
    parsed = urlparse(upload_url)
    port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    host = f"[{address}]" if ":" in address else address
    return urlunsplit(
        (
            parsed.scheme,
            f"{host}:{port}",
            parsed.path or "/",
            parsed.query,
            "",
        ),
    )


def _pin_destination(
    upload_url: str,
    destination_guard: Callable[[str], None],
) -> _PinnedDestination:
    """Resolve *upload_url* and bind it to an address the guard accepts.

    A guard that judges strings can only ever judge a hostname as a
    hostname, while what decides whether a request reaches a metadata service
    is the address the resolver hands back. Every answer is therefore handed
    to the same policy as a literal-address URL — one rule covering the
    configured base, the DNS answer and the connected peer — and the request
    is rewritten to a validated literal so no re-resolution can overrule it.
    A name that resolves to nothing is refused rather than guessed at.
    """
    parsed = urlparse(upload_url)
    host = (parsed.hostname or "").strip()
    if not host:
        raise RuntimeError(
            f"provider media upload has no host to bind: {upload_url!r}",
        )
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        # Nothing to resolve: the guard judged this literal already. A scoped
        # IPv6 zone names the interface the operator chose, while the socket
        # reports the bare address, so both spellings are the same peer.
        scope_free = host.split("%", 1)[0]
        accepted = (host,) if scope_free == host else (host, scope_free)
        return _PinnedDestination(upload_url, "", "", accepted)
    port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RuntimeError(
            f"provider media upload cannot resolve {host}: {exc}",
        ) from exc
    addresses: list[str] = []
    for record in records:
        address = str((record[4] or ("",))[0]).split("%", 1)[0]
        if address and address not in addresses:
            addresses.append(address)
    if not addresses:
        raise RuntimeError(
            f"provider media upload cannot resolve {host}: no address",
        )
    for address in addresses:
        destination_guard(_addressed_url(upload_url, address))
    return _PinnedDestination(
        url=_addressed_url(upload_url, addresses[0]),
        # The name as configured, minus any credentials a base URL might carry:
        # a Host header must name the server, and nothing else.
        host_header=parsed.netloc.rsplit("@", 1)[-1],
        sni_hostname=(host if parsed.scheme.casefold() == "https" else ""),
        addresses=tuple(addresses),
    )


def _assert_pinned_peer(
    response: Any,
    pin: _PinnedDestination,
    destination_guard: Callable[[str], None],
) -> None:
    """Re-check the address the socket actually connected to.

    The pin is what makes the pre-flight answer authoritative, but a transport
    that ignored it — or a proxy standing in between — still has to be
    caught, so the peer is read once per guarded upload and judged by the
    same policy. A transport exposing no network stream (a test double, an
    in-process app) has no address to check and is left to the pin.
    """
    extensions = getattr(response, "extensions", None)
    if not isinstance(extensions, dict):
        return
    stream = extensions.get("network_stream")
    get_extra_info = getattr(stream, "get_extra_info", None)
    if get_extra_info is None:
        return
    peer = get_extra_info("server_addr")
    if not isinstance(peer, tuple) or not peer:
        return
    address = str(peer[0]).split("%", 1)[0]
    if not address:
        return
    destination_guard(_addressed_url(pin.url, address))
    if address not in pin.addresses:
        raise RuntimeError(
            "provider media upload connected to "
            f"{address}, which its configured host {pin.host_header} was not "
            "resolved to; the payload was not sent further",
        )


def _upload_target_label(url: str, pin: _PinnedDestination | None) -> str:
    """Name an upload target the way its operator configured it.

    Binding the address puts a literal in the URL, which says nothing to
    whoever set the base URL and would leak one resolution into the message
    the UI shows. The configured authority is the useful label, and the path
    is unchanged by the pin.
    """
    if pin is None or not pin.host_header:
        return url
    parsed = urlparse(url)
    return urlunsplit(
        (parsed.scheme, pin.host_header, parsed.path, parsed.query, ""),
    )


def _post_platform_media_upload(
    client: httpx.Client,
    *,
    base_url: str,
    api_key: str,
    filename: str,
    file_source: object,
    media_type: str,
    refuse_redirects: bool = False,
    pin: _PinnedDestination | None = None,
    destination_guard: Callable[[str], None] | None = None,
) -> str:
    """Upload one payload through the proxy. Returns its public URL."""
    url = pin.url if pin is not None else platform_media_upload_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}"}
    bound: dict[str, Any] = {}
    if pin is not None:
        # The URL names a validated literal, so the endpoint still has to be
        # addressed by its own host and seen with its own certificate:
        # binding the address must not change what the provider is told.
        if pin.host_header:
            headers["Host"] = pin.host_header
        if pin.sni_hostname:
            bound["extensions"] = {"sni_hostname": pin.sni_hostname}
    response = client.post(
        url,
        headers=headers,
        files={"file": (filename, file_source, media_type)},
        **bound,
    )
    if pin is not None and destination_guard is not None:
        _assert_pinned_peer(response, pin, destination_guard)
    if refuse_redirects and 300 <= response.status_code < 400:
        # A redirect would carry this payload, and the bearer naming it, to a
        # host nobody configured. Only a caller that asked for the strict
        # policy gets a refusal; production keeps following its own proxy.
        raise RuntimeError(
            "provider media upload was redirected (HTTP "
            f"{response.status_code} -> "
            f"{response.headers.get('location', '')}); this caller does not "
            "follow uploads off the configured endpoint",
        )
    if response.status_code >= 400:
        raise RuntimeError(
            "provider media upload failed "
            f"(HTTP {response.status_code} {_upload_target_label(url, pin)})"
            f": {response.text[:300]}",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"provider media upload returned a non-JSON body from "
            f"{_upload_target_label(url, pin)}",
        ) from exc
    items = payload.get("items") if isinstance(payload, dict) else None
    first = items[0] if isinstance(items, list) and items else None
    public_url = str((first or {}).get("url") or "")
    if not public_url:
        raise RuntimeError(
            f"provider media upload returned no URL: {str(payload)[:200]}",
        )
    logger.info(
        "Provider media upload complete | filename=%s, url=%s",
        filename,
        public_url,
    )
    return public_url


def _upload_file_to_platform_media_sync(
    path: Path,
    *,
    api_key: str,
    base_url: str,
    media_type: str,
    destination_guard: Callable[[str], None] | None = None,
) -> str:
    """Stream one local file to the proxy's own media bucket."""
    size = path.stat().st_size
    if size > PLATFORM_MEDIA_UPLOAD_MAX_BYTES:
        raise RuntimeError(
            "provider media upload rejects files larger than "
            f"{PLATFORM_MEDIA_UPLOAD_MAX_BYTES} bytes ({size} given); shrink "
            "the reference or serve it from public storage",
        )
    timeout = httpx.Timeout(connect=30.0, read=120.0, write=300.0, pool=30.0)
    filename = _dashscope_transport_filename(path, media_type)
    upload_url = platform_media_upload_url(base_url)
    pin = None
    if destination_guard is not None:
        # Checked before the connection is opened, so a refused target never
        # sees the bearer token at all rather than failing afterwards.
        destination_guard(upload_url)
        pin = _pin_destination(upload_url, destination_guard)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=destination_guard is None,
    ) as client:
        with path.open("rb") as file_handle:
            return _post_platform_media_upload(
                client,
                base_url=base_url,
                api_key=api_key,
                filename=filename,
                file_source=file_handle,
                media_type=media_type,
                refuse_redirects=destination_guard is not None,
                pin=pin,
                destination_guard=destination_guard,
            )


def _upload_bytes_to_platform_media_sync(
    content: bytes,
    filename: str,
    *,
    api_key: str,
    base_url: str,
    media_type: str,
    destination_guard: Callable[[str], None] | None = None,
) -> str:
    if len(content) > PLATFORM_MEDIA_UPLOAD_MAX_BYTES:
        raise RuntimeError(
            "provider media upload rejects payloads larger than "
            f"{PLATFORM_MEDIA_UPLOAD_MAX_BYTES} bytes ({len(content)} given)",
        )
    timeout = httpx.Timeout(connect=30.0, read=120.0, write=300.0, pool=30.0)
    upload_url = platform_media_upload_url(base_url)
    pin = None
    if destination_guard is not None:
        destination_guard(upload_url)
        pin = _pin_destination(upload_url, destination_guard)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=destination_guard is None,
    ) as client:
        return _post_platform_media_upload(
            client,
            base_url=base_url,
            api_key=api_key,
            filename=_safe_filename(filename),
            file_source=content,
            media_type=media_type,
            refuse_redirects=destination_guard is not None,
            pin=pin,
            destination_guard=destination_guard,
        )


async def upload_local_file_to_platform_media(
    path: Path,
    *,
    api_key: str,
    base_url: str,
    media_type: str,
    destination_guard: Callable[[str], None] | None = None,
) -> str:
    """Upload a local file through the proxy and cache its public URL.

    *destination_guard* is for callers that must not touch an address their
    own policy forbids. It is applied to the upload target, to every address
    the resolver returns for it, and to the peer the socket actually used,
    and the request is then sent to a validated literal address so nothing
    can be re-resolved after the policy ran; the client also refuses
    redirects, so a proxy answering 302 cannot move the payload somewhere
    else. Leaving it unset keeps the production behaviour and the shared
    cache intact.
    """
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"selected local media does not exist: {resolved}",
        )
    if not api_key.strip():
        raise RuntimeError("provider media upload requires an API key")
    stat = resolved.stat()
    cache_key = _platform_media_upload_cache_key(
        identity=(
            f"{resolved}|{int(stat.st_size)}|{int(stat.st_mtime_ns)}"
            + ("|strict" if destination_guard is not None else "")
        ),
        api_key=api_key,
    )
    return await _cached_platform_media_upload(
        cache_key,
        lambda: _upload_file_to_platform_media_sync(
            resolved,
            api_key=api_key,
            base_url=base_url,
            media_type=media_type,
            destination_guard=destination_guard,
        ),
    )


async def upload_reference_bytes_to_platform_media(
    content: bytes,
    filename: str,
    *,
    api_key: str,
    base_url: str,
    media_type: str,
    destination_guard: Callable[[str], None] | None = None,
) -> str:
    """Upload in-memory reference media through the proxy.

    See ``upload_local_file_to_platform_media`` for *destination_guard*.
    """
    if not api_key.strip():
        raise RuntimeError("provider media upload requires an API key")
    digest = hashlib.sha256(content).hexdigest()
    cache_key = _platform_media_upload_cache_key(
        identity=(
            f"sha256:{digest}|{filename}"
            + ("|strict" if destination_guard is not None else "")
        ),
        api_key=api_key,
    )
    return await _cached_platform_media_upload(
        cache_key,
        lambda: _upload_bytes_to_platform_media_sync(
            content,
            filename,
            api_key=api_key,
            base_url=base_url,
            media_type=media_type,
            destination_guard=destination_guard,
        ),
    )


async def _cached_platform_media_upload(
    cache_key: tuple[str, str],
    upload,
) -> str:
    """Run *upload* at most once per cache key, concurrently safe."""
    now = time.monotonic()
    _prune_platform_media_upload_cache(now)
    cached = _platform_media_upload_cache.get(cache_key)
    if cached is not None and cached[1] > now:
        return cached[0]
    lock = _platform_media_upload_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _platform_media_upload_cache.get(cache_key)
        if cached is not None and cached[1] > now:
            return cached[0]
        url = await asyncio.to_thread(upload)
        _platform_media_upload_cache[cache_key] = (
            url,
            now + PLATFORM_MEDIA_UPLOAD_CACHE_SECONDS,
        )
        return url


def reference_transport_needs_upload_key(
    base_url: str,
    protocol: str = "",
) -> bool:
    """True when the reference transport for *base_url* must present a key.

    Both remote transports need one: the AgentScope proxy's own media upload
    and the DashScope model-bound temporary OSS. Only the inline Base64
    fallback is keyless. A caller that guards on the credential has to
    mirror the routing below, otherwise an uncredentialed request reaches a
    remote upload and is answered with 401.
    """
    # Local import: vlm_model imports this module at module level.
    from models.vlm_model import uses_dashscope_transport

    if model_config.is_agentscope_gateway(
        protocol=protocol,
        base_url=base_url,
    ):
        return True
    return uses_dashscope_transport(base_url, protocol)


async def upload_reference_file_for_provider(
    path: Path,
    *,
    api_key: str,
    model_name: str,
    media_type: str,
    base_url: str,
    protocol: str = "",
    destination_guard: Callable[[str], None] | None = None,
) -> str:
    """Return a URL the configured provider can fetch for one local file.

    Single decision point for the three reference transports, so a call site
    never has to know which storage the endpoint behind it accepts: the
    AgentScope proxy has no Bailian upload identity and takes its own public
    URL, a DashScope endpoint keeps the model-bound ``oss://`` temporary
    upload, and any other OpenAI-compatible gateway is served the bytes
    inline, because it can neither fetch a DashScope OSS object nor accept a
    Bailian upload credential.
    """
    if model_config.is_agentscope_gateway(
        protocol=protocol,
        base_url=base_url,
    ):
        return await upload_local_file_to_platform_media(
            path,
            api_key=api_key,
            base_url=base_url,
            media_type=media_type,
            destination_guard=destination_guard,
        )
    if not reference_transport_needs_upload_key(base_url, protocol):
        from models.vlm_model import (  # local import: see above
            transport_local_media_url,
        )

        # Reuse the VLM inline path so the size budget and the downscaling
        # ladder apply here too; a raw Base64 of a 200MB clip would otherwise
        # be assembled without limit.
        return await transport_local_media_url(
            path,
            api_key=api_key,
            model_name=model_name,
            media_type=media_type,
            base_url=base_url,
            protocol=protocol,
        )
    return await upload_local_file_to_dashscope_temp(
        path,
        api_key=api_key,
        model_name=model_name,
        media_type=media_type,
    )


async def upload_reference_bytes_for_provider(
    content: bytes,
    filename: str,
    *,
    api_key: str,
    model_name: str,
    base_url: str,
    protocol: str = "",
    media_type: str = "",
) -> str:
    """Return a URL the configured provider can fetch for in-memory media."""
    if model_config.is_agentscope_gateway(
        protocol=protocol,
        base_url=base_url,
    ):
        return await upload_reference_bytes_to_platform_media(
            content,
            filename,
            api_key=api_key,
            base_url=base_url,
            media_type=media_type or _reference_media_type(filename, content),
        )
    return await upload_reference_bytes_to_dashscope_temp(
        content,
        filename,
        api_key=api_key,
        model_name=model_name,
    )


# EXIF Orientation values that need a pixel transform. 1 and a missing tag
# both mean the stored pixels are already upright.
_EXIF_ORIENTATION_TAG = 0x0112


def upright_image(image: Image.Image) -> Image.Image:
    """Bake EXIF Orientation into the pixels.

    Re-encoding drops the tag, so without this a portrait phone photo
    reaches the model lying on its side: the only hint that viewers should
    rotate it is gone from the JPEG we hand over.
    """
    orientation = image.getexif().get(_EXIF_ORIENTATION_TAG)
    if isinstance(orientation, int) and 2 <= orientation <= 8:
        return ImageOps.exif_transpose(image)
    return image


def flatten_alpha(image: Image.Image) -> Image.Image:
    """Composite transparent pixels onto white before dropping the alpha.

    ``convert("RGB")`` discards the alpha channel and keeps whatever the
    encoder stored underneath the transparent pixels — usually black. UI
    captures and scanned documents are exactly the images that outgrow the
    inline budget, so without this a transparent-background page reaches the
    model as pale text on black, the one rendering it reads worst. Flattening
    also satisfies providers that reject an alpha channel outright (Wan r2v
    documents PNG without a transparency channel).

    A colour key counts as transparency too: ``tRNS`` is legal on RGB and L
    PNGs, and PIL applies it inside ``convert("RGBA")`` — measured on Pillow
    10, the declared floor, and on 13: a black key over black pixels yields
    alpha 0 and flattens to white, while a key that does not match the pixel
    keeps alpha 255.  The fast path therefore has to test for the key and not
    for the mode alone.
    """
    keyed = "transparency" in image.info
    if image.mode == "RGB" and not keyed:
        return image
    has_alpha = image.mode in {"RGBA", "LA"} or (
        keyed and image.mode in {"P", "RGB", "L"}
    )
    if not has_alpha:
        return image.convert("RGB")
    rgba = image.convert("RGBA")
    flat = Image.new("RGB", rgba.size, (255, 255, 255))
    flat.paste(rgba, mask=rgba.getchannel("A"))
    return flat


def downscale_image_to_budget(
    content: bytes,
    max_bytes: int,
    *,
    ladder: tuple[tuple[int, int], ...] = IMAGE_INLINE_LADDER,
    label: str = "",
) -> bytes | None:
    """Re-encode *content* down *ladder* until the JPEG fits *max_bytes*.

    Returns ``None`` when the bytes are not a decodable image, or when even
    the smallest rung stays over budget, so callers keep their own refusal
    instead of inheriting a transport error.

    The result is always JPEG whatever the source format was, so a caller
    that names the payload must use a ``.jpg`` suffix:
    ``_reference_media_type`` trusts the filename first and would otherwise
    label JPEG bytes with the source image's MIME type.

    The catch below is deliberately broad, and narrowing it would be a
    regression: PIL raises ``DecompressionBombError`` for a file over its
    pixel ceiling, and that type derives from ``Exception`` rather than
    ``OSError``, so an ``(OSError, ValueError)`` clause would let an
    oversized screenshot escape instead of reaching the refusal.
    """
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            image = upright_image(image)
            image = flatten_alpha(image)
            for max_edge, quality in ladder:
                frame = image.copy()
                frame.thumbnail((max_edge, max_edge), Image.LANCZOS)
                buffer = io.BytesIO()
                frame.save(
                    buffer,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                )
                payload = buffer.getvalue()
                if len(payload) <= max_bytes:
                    return payload
    except Exception as exc:  # pylint: disable=broad-except
        logger.info(
            "inline downscale unavailable for %s (%s)",
            label or f"{len(content)} bytes",
            exc,
        )
        return None
    return None


def reference_media_data_url(content: bytes, filename: str) -> str:
    """Inline reference media as a Base64 data URL for the Ark Seedance API.

    Volcengine's video-generation task API accepts ``data:<mime>;base64,...``
    reference images directly (the Ark File API's file_id is only usable by
    Chat/Responses), so no upload or extra storage configuration is needed.
    """
    if len(content) >= SEEDANCE_REFERENCE_IMAGE_MAX_BYTES:
        raise RuntimeError(
            "Seedance reference images must be smaller than 30MB; "
            "downscale the media or provide a public HTTPS URL",
        )
    media_type = _reference_media_type(filename, content)
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _creator_media_bucket() -> str:
    return model_config.get_oss_bucket(DEFAULT_CREATOR_MEDIA_BUCKET)


def _creator_media_public_base_url() -> str:
    return model_config.get_oss_public_base_url()


def creator_oss_readiness() -> dict:
    """Return explicit absent/ready/invalid Creator OSS readiness."""
    access_key_id = model_config.get_oss_access_key_id()
    access_key_secret = model_config.get_oss_access_key_secret()
    endpoint = model_config.get_oss_endpoint()
    configured = bool(
        access_key_id
        or access_key_secret
        or endpoint
        or model_config.get_oss_bucket("")
        or _creator_media_public_base_url(),
    )
    blockers = []
    if not access_key_id:
        blockers.append(
            "creator_media_oss.access_key_id or OSS_ACCESS_KEY_ID is required",
        )
    if not access_key_secret:
        blockers.append(
            "creator_media_oss.access_key_secret or OSS_ACCESS_KEY_SECRET is required",
        )
    if not endpoint:
        blockers.append(
            "creator_media_oss.endpoint or OSS_ENDPOINT is required",
        )
    return {
        "status": (
            "ready" if not blockers else "invalid" if configured else "absent"
        ),
        "bucket": _creator_media_bucket(),
        "endpoint_set": bool(endpoint),
        "access_key_set": bool(access_key_id and access_key_secret),
        "public_base_url_set": bool(_creator_media_public_base_url()),
        "blockers": blockers,
    }


def _safe_filename(filename: str) -> str:
    stem = Path(filename or "reference").stem or "reference"
    suffix = Path(filename or "").suffix.lower()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._") or "reference"
    if not suffix or not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
        suffix = ".bin"
    return f"{stem}{suffix}"


def _object_key(filename: str, backend: str) -> str:
    prefix = SD2_MEDIA_PREFIX if backend == "seedance2" else WAN_MEDIA_PREFIX
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{prefix}/{date}/{uuid.uuid4().hex}-{_safe_filename(filename)}"


def _public_url(endpoint: str, bucket: str, key: str) -> str:
    quoted_key = quote(key)
    public_base_url = _creator_media_public_base_url()
    if public_base_url:
        return f"{public_base_url.rstrip('/')}/{quoted_key}"
    endpoint_host = (
        endpoint.replace("https://", "").replace("http://", "").rstrip("/")
    )
    return f"https://{bucket}.{endpoint_host}/{quoted_key}"


def _upload_with_oss2(file_content: bytes, filename: str, backend: str) -> str:
    try:
        import oss2  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "oss2 package is required for creator-media uploads",
        ) from exc

    access_key_id = model_config.get_oss_access_key_id()
    access_key_secret = model_config.get_oss_access_key_secret()
    endpoint = model_config.get_oss_endpoint()
    if not access_key_id or not access_key_secret or not endpoint:
        readiness = creator_oss_readiness()
        raise RuntimeError("; ".join(readiness["blockers"]))

    bucket_name = _creator_media_bucket()
    key = _object_key(filename, backend)
    content_type = (
        mimetypes.guess_type(filename)[0] or "application/octet-stream"
    )
    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    headers = {
        "Content-Type": content_type,
        "x-oss-object-acl": "public-read",
    }
    bucket.put_object(key, file_content, headers=headers)
    return _public_url(endpoint, bucket_name, key)


async def upload_reference_media_to_creator_oss(
    file_content: bytes,
    filename: str,
    backend: str,
) -> str:
    """Upload reference media to creator-media and return a public HTTPS URL."""
    if backend not in {"wan", "seedance2"}:
        raise ValueError(
            f"Unsupported video backend for OSS upload: {backend}",
        )
    return await asyncio.to_thread(
        _upload_with_oss2,
        file_content,
        filename,
        backend,
    )


def _presign_upload_with_oss2(
    file_content: bytes,
    filename: str,
    expires_seconds: int,
) -> str:
    try:
        import oss2  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "oss2 package is required for creator-media uploads",
        ) from exc

    access_key_id = model_config.get_oss_access_key_id()
    access_key_secret = model_config.get_oss_access_key_secret()
    endpoint = model_config.get_oss_endpoint()
    if not access_key_id or not access_key_secret or not endpoint:
        readiness = creator_oss_readiness()
        raise RuntimeError("; ".join(readiness["blockers"]))

    bucket_name = _creator_media_bucket()
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = (
        f"{GROUNDING_LENS_MEDIA_PREFIX}/{date}/"
        f"{uuid.uuid4().hex}-{_safe_filename(filename)}"
    )
    content_type = (
        mimetypes.guess_type(filename)[0] or "application/octet-stream"
    )
    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    # Keep the bucket default (private) ACL: the object is only reachable
    # through the expiring signed URL below.
    bucket.put_object(
        key,
        file_content,
        headers={"Content-Type": content_type},
    )
    return bucket.sign_url("GET", key, expires_seconds)


async def upload_image_for_temporary_public_url(
    file_content: bytes,
    filename: str,
    *,
    expires_seconds: int = GROUNDING_LENS_SIGNED_URL_EXPIRES_SECONDS,
) -> str:
    """Upload an image privately and return a short-lived presigned URL."""
    return await asyncio.to_thread(
        _presign_upload_with_oss2,
        file_content,
        filename,
        expires_seconds,
    )


def _is_retryable_upload_response(response: httpx.Response) -> bool:
    return response.status_code == 429 or response.status_code >= 500


async def upload_image_to_uguu_for_temporary_public_url(
    file_content: bytes,
    filename: str,
    *,
    max_attempts: int = UGUU_UPLOAD_MAX_ATTEMPTS,
) -> str:
    """Upload one validated image to Uguu and return its temporary URL."""
    attempts = max(1, int(max_attempts))
    media_type = (
        mimetypes.guess_type(filename)[0] or "application/octet-stream"
    )
    last_error: Exception | None = None
    async with httpx.AsyncClient(
        timeout=60.0,
        follow_redirects=True,
    ) as client:
        for attempt in range(1, attempts + 1):
            try:
                response = await client.post(
                    UGUU_UPLOAD_URL,
                    files={
                        "files[]": (filename, file_content, media_type),
                    },
                )
                if _is_retryable_upload_response(response):
                    response.raise_for_status()
                response.raise_for_status()
                payload = response.json()
                files = (
                    payload.get("files") if isinstance(payload, dict) else None
                )
                public_url = (
                    str(files[0].get("url") or "").strip()
                    if isinstance(files, list)
                    and files
                    and isinstance(files[0], dict)
                    else ""
                )
                if not public_url.startswith("https://"):
                    raise RuntimeError(
                        "Uguu upload response did not contain a public HTTPS URL",
                    )
                return public_url
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if not _is_retryable_upload_response(exc.response):
                    raise
            if attempt >= attempts:
                break
            await asyncio.sleep(
                min(
                    UGUU_RETRY_BACKOFF_CAP_SECONDS,
                    float(2 ** (attempt - 1)),
                ),
            )
    raise RuntimeError(
        f"Uguu upload failed after {attempts} attempts: {last_error}",
    )


async def read_reference_media(
    url: str,
    *,
    max_bytes: int = REFERENCE_IMAGE_DOWNLOAD_MAX_BYTES,
) -> tuple[bytes, str]:
    """Read one bounded local or SSRF-safe public reference into memory."""

    if max_bytes <= 0:
        raise ValueError("reference media max_bytes must be positive")

    def read_local_bounded(media_path: Path) -> bytes:
        if media_path.stat().st_size > max_bytes:
            raise ValueError(
                f"reference media exceeds {max_bytes} bytes",
            )
        return media_path.read_bytes()

    if url.startswith("file://"):
        media_path = local_path_from_file_url(url)
        content = await asyncio.to_thread(read_local_bounded, media_path)
        filename = media_path.name or f"reference-{uuid.uuid4().hex}"
        if not Path(filename).suffix:
            filename += _suffix_from_magic(content)
        return content, filename
    if url.startswith("/generated/"):
        media_path = media_path_from_url(url)
        content = await asyncio.to_thread(read_local_bounded, media_path)
        return (
            content,
            media_path.name or f"reference-{uuid.uuid4().hex}.bin",
        )
    if url.startswith(("http://", "https://")):
        content = await asyncio.to_thread(
            safe_download_bytes,
            url,
            max_bytes=max_bytes,
            timeout=60.0,
        )
        filename = (
            Path(urlparse(url).path).name
            or f"reference-{uuid.uuid4().hex}.bin"
        )
        return content, filename
    raise ValueError(f"Unsupported reference media URL: {url}")


def validate_reference_image_bytes(content: bytes) -> None:
    """Reject empty, mislabeled, or truncated image-reference payloads."""
    try:
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            image.verify()
    except Exception as exc:
        raise ValueError("reference image cannot be decoded") from exc
    if width <= 0 or height <= 0:
        raise ValueError("reference image has invalid dimensions")


def _suffix_from_magic(content: bytes) -> str:
    """Recover a transport filename for suffix-free content-addressed blobs."""

    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    if len(content) >= 12 and content[4:8] == b"ftyp":
        return ".mp4"
    if content.startswith(b"\x1aE\xdf\xa3"):
        return ".webm"
    return ".bin"
