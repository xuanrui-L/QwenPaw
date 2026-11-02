# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access
"""The promise a demoted pointer makes has to be kept by this store."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

from services.file_agent_runtime import media_vault
from services.file_agent_runtime import driver as driver_module
from services.file_agent_runtime.driver import (
    FileAgentRuntimeError,
    FileCreatorAgentRuntime,
)
from services.workspace.content_store import ContentStore

pytestmark = pytest.mark.unit

# A payload with a real PNG header, so the type can be read back off the
# bytes exactly the way a provider would need it.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"frame" * 20_000


def _data_url(payload: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def test_a_stored_block_reads_back_byte_for_byte(tmp_path):
    """A pointer is only honest while the same bytes come back.

    The context guard rewrites a media part into a handle and calls it
    recallable. Recall has to deliver the original payload, not a re-encoded
    approximation of it, or the model is asked to trust a description of
    something nobody can check any more.
    """
    store = ContentStore(tmp_path)
    data_url = _data_url(_PNG_BYTES)

    handle = media_vault.store_data_url(data_url, store=store)
    restored = media_vault.load_by_handle(f"sha256:{handle}", store=store)

    assert handle == hashlib.sha256(_PNG_BYTES).hexdigest()
    assert restored is not None
    url, decoded_bytes = restored
    assert base64.b64decode(url.split(";base64,", 1)[1]) == _PNG_BYTES
    assert url.startswith("data:image/png;")
    assert decoded_bytes == len(_PNG_BYTES)


def test_the_same_image_demoted_twice_costs_one_copy(tmp_path):
    """Content addressing keeps one image from being stored twice."""
    store = ContentStore(tmp_path)
    data_url = _data_url(_PNG_BYTES)

    first = media_vault.store_data_url(data_url, store=store)
    second = media_vault.store_data_url(data_url, store=store)

    assert first == second
    # One stored copy, named by the digest the two pointers advertise.
    assert list(store.iter_digests(namespace="blob")) == [first]
    assert store.path_for(first).is_file()


def test_a_handle_that_is_not_a_digest_never_reaches_the_filesystem():
    """The model retypes pointers; a wrong one must not open paths."""
    assert media_vault.looks_like_digest("sha256:" + "a" * 64)
    assert media_vault.looks_like_digest("A" * 64)
    assert not media_vault.looks_like_digest("sha256:aaaa")
    assert not media_vault.looks_like_digest("../etc/passwd")
    assert not media_vault.looks_like_digest("z" * 64)
    assert media_vault.load_by_handle("z" * 64) is None


def test_a_failed_write_degrades_the_pointer_not_the_request(
    tmp_path,
    monkeypatch,
):
    """A vault that cannot write still leaves the caller a working pointer.

    Demotion exists to save a request that would otherwise be refused. Failing
    the write has to fall back to a pointer without recall, not turn a request
    that would have worked into an error the user sees.
    """

    def refuse(*_args, **_kwargs):
        raise OSError("data root is read-only")

    monkeypatch.setattr(ContentStore, "put_bytes", refuse)
    vault = media_vault.make_vault(ContentStore(tmp_path))

    assert vault(_data_url(_PNG_BYTES)) is None
    assert vault("https://example.com/a.png") is None
    assert vault("data:image/png;base64,not base64 at all") is None
    assert media_vault.split_data_url("https://example.com/a.png") is None


def test_an_unresolvable_data_root_stays_inside_the_vault(monkeypatch):
    """Opening the store is allowed to fail, and that failure never leaves.

    Every entry point resolves CREATOR_DATA_ROOT lazily, so a machine where it
    is unset or relative raises the moment a block is demoted. That is one more
    way the durable copy is missing: the turn keeps its pointer without a
    recall handle. Letting it escape would turn a request that would have
    worked into an error the user sees.
    """
    url = _data_url(_PNG_BYTES)
    monkeypatch.delenv("CREATOR_DATA_ROOT", raising=False)

    assert media_vault.store_data_url(url) is None
    assert media_vault.make_vault()(url) is None
    assert media_vault.load_by_handle("a" * 64) is None
    assert media_vault.load_data_url("a" * 64, "image/png") is None

    monkeypatch.setenv("CREATOR_DATA_ROOT", "relative/path")
    assert media_vault.store_data_url(url) is None
    assert media_vault.load_by_handle("a" * 64) is None


def _recall_runtime() -> FileCreatorAgentRuntime:
    # The recall path reads the vault and nothing else, so it can be exercised
    # without assembling a whole runtime.
    return FileCreatorAgentRuntime.__new__(FileCreatorAgentRuntime)


def test_a_pointer_is_answered_with_the_original_media_part(
    tmp_path,
    monkeypatch,
):
    """The tool has to hand back what the pointer promised.

    A recalled block arrives as a native media part on a user turn, not as
    text inside a tool result: no provider reads a Base64 blob out of a JSON
    string, so a recall that only described the image would leave the model
    looking at a caption and believing it had looked again.
    """
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    store = ContentStore(tmp_path)
    digest = media_vault.store_data_url(_data_url(_PNG_BYTES), store=store)

    async def scenario():
        runtime = _recall_runtime()
        result = await runtime._run_recall_media(
            arguments={"handles": [f"sha256:{digest}"]},
        )
        return result, await runtime._recalled_media_content(result=result)

    result, parts = asyncio.run(scenario())

    assert result["recalled"][0]["handle"] == digest
    assert result["recalled"][0]["mime"] == "image/png"
    assert parts[-1]["type"] == "image_url"
    restored = parts[-1]["image_url"]["url"]
    assert base64.b64decode(restored.split(";base64,", 1)[1]) == _PNG_BYTES
    # The payload never lands in the durable tool result itself.
    assert "base64" not in json.dumps(result)


def test_a_handle_nobody_stored_is_reported_missing_not_guessed(
    tmp_path,
    monkeypatch,
):
    """A wrong handle costs a re-run, a guessed image costs the whole run."""
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))

    async def scenario():
        return await _recall_runtime()._run_recall_media(
            arguments={"handles": ["sha256:" + "f" * 64, "../../etc/passwd"]},
        )

    with pytest.raises(FileAgentRuntimeError, match="no restorable bytes"):
        asyncio.run(scenario())


def test_a_block_too_large_to_fit_stays_a_pointer(tmp_path, monkeypatch):
    """Recall cannot trade one budget failure for another.

    The pointer already records the size, so restoring bytes the request could
    never carry would only send the caller back to the refusal that demoted
    them in the first place.
    """
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path.resolve()))
    monkeypatch.setattr(driver_module, "MAX_RECALL_MEDIA_BYTES", 1024)
    store = ContentStore(tmp_path)
    digest = media_vault.store_data_url(_data_url(_PNG_BYTES), store=store)

    async def scenario():
        return await _recall_runtime()._run_recall_media(
            arguments={"handles": [digest]},
        )

    with pytest.raises(FileAgentRuntimeError, match="no restorable bytes"):
        asyncio.run(scenario())
