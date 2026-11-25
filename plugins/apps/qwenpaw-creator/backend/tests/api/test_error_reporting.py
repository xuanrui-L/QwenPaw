# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access,unused-argument
"""Creator error envelopes and error isolation when mounted by QwenPaw."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI, Query
from httpx import ASGITransport, AsyncClient

from api.dependencies import CreatorErrorRoute
from domain.errors import CreatorError
from services.runtime_files.errors import LockTimeoutError

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_ENTRYPOINT = WORKSPACE_ROOT / "qwenpaw-creator" / "backend" / "main.py"
QWENPAW_SOURCE = WORKSPACE_ROOT.parents[1] / "src"


@pytest.fixture(autouse=True)
def _restore_process_environment():
    """Undo the environment the loaded entrypoint exports.

    Importing the plugin entrypoint runs the host bootstrap, which mirrors
    the machine's real Creator model config into ``os.environ`` (TEXT_API_KEY,
    TTS_API_KEY, ...) without going through ``monkeypatch``.  Left in place,
    that credential makes every later test see a configured TTS backend and
    breaks their tool-set assertions.
    """
    before = dict(os.environ)
    yield
    for key in set(os.environ) - set(before):
        del os.environ[key]
    for key, value in before.items():
        os.environ[key] = value


def _load_plugin_entrypoint(monkeypatch):
    # Discard a partially loaded or foreign qwenpaw module before loading
    # the checked-out host source so this contract test is deterministic.
    for module_name in tuple(sys.modules):
        if module_name == "qwenpaw" or module_name.startswith("qwenpaw."):
            monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.syspath_prepend(str(QWENPAW_SOURCE))
    spec = importlib.util.spec_from_file_location(
        "qwenpaw_creator_plugin_host_contract",
        PLUGIN_ENTRYPOINT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_defaults_runtime_paths_to_qwenpaw_working_dir(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CREATOR_DATA_ROOT", "")
    monkeypatch.setenv("CREATOR_MODEL_CONFIG_PATH", "")
    monkeypatch.setenv("CREATOR_BINARY_DIR", "")
    module = _load_plugin_entrypoint(monkeypatch)

    working_dir = tmp_path / "qwenpaw-home"
    (
        data_root,
        model_config_path,
    ) = module.configure_creator_runtime_environment(
        working_dir=working_dir,
    )

    assert data_root == working_dir / "creator-runtime"
    assert model_config_path == data_root / "config" / "model_config.json"
    assert Path(module.os.environ["CREATOR_DATA_ROOT"]) == data_root
    assert (
        Path(module.os.environ["CREATOR_MODEL_CONFIG_PATH"])
        == model_config_path
    )
    assert (
        Path(module.os.environ["CREATOR_BINARY_DIR"])
        == data_root / "runtime-tools" / "bin"
    )
    assert data_root.is_dir()
    assert model_config_path.parent.is_dir()


def test_plugin_rejects_unsafe_runtime_path_configuration(
    tmp_path,
    monkeypatch,
) -> None:
    # A model config path escaping the runtime root fails closed.
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path / "creator-runtime"))
    monkeypatch.setenv(
        "CREATOR_MODEL_CONFIG_PATH",
        str(tmp_path / "outside" / "model_config.json"),
    )
    module = _load_plugin_entrypoint(monkeypatch)
    with pytest.raises(
        module.CreatorDataRootError,
        match="inside CREATOR_DATA_ROOT",
    ):
        module.configure_creator_runtime_environment(working_dir=tmp_path)


def test_real_qwenpaw_host_mount_keeps_creator_errors_local_and_structured(
    api_runtime_root,
    monkeypatch,
) -> None:
    module = _load_plugin_entrypoint(monkeypatch)
    from qwenpaw.plugins.api import PluginApi
    from qwenpaw.plugins.registry import PluginRegistry

    previous_registry = PluginRegistry._instance
    PluginRegistry._instance = None
    try:
        host = FastAPI()
        registry = PluginRegistry()
        registry.set_plugin_http_app(host)
        plugin_api = PluginApi(
            "qwenpaw-creator",
            config={},
            manifest={"id": "qwenpaw-creator"},
        )
        plugin_api.set_registry(registry)
        module.app.register(plugin_api)

        assert CreatorError not in host.exception_handlers
        registrations = registry.get_http_router_registrations()
        assert len(registrations) == 1
        assert registrations[0].prefix == "/qwenpaw-creator"
        assert registrations[0].routes
        # FastAPI 0.135+ keeps include_router contributions as optimized
        # _IncludedRouter nodes instead of flattening top-level APIRoutes. The
        # plugin's own route class plus the real request below are the stable
        # contract; inspecting the host's private flattened shape is not.
        assert module.creator_router.route_class is CreatorErrorRoute

        async def scenario():
            transport = ASGITransport(app=host, raise_app_exceptions=False)
            async with AsyncClient(
                transport=transport,
                base_url="http://plugin-host.test",
            ) as client:
                return await client.get(
                    "/api/qwenpaw-creator/projects/project-does-not-exist/project",
                )

        response = asyncio.run(scenario())
        assert response.status_code == 404
        body = response.json()
        assert {
            key: body[key]
            for key in ("code", "message", "retryable", "details")
        } == {
            "code": "NOT_FOUND",
            "message": "Project 不存在",
            "retryable": False,
            "details": {},
        }
        assert body["errorId"] == response.headers["X-Creator-Error-ID"]
        assert body["traceId"] == response.headers["X-Creator-Trace-ID"]
        assert body["requestId"] == response.headers["X-Request-ID"]
    finally:
        PluginRegistry._instance = previous_registry


def test_validation_lock_and_unexpected_errors_share_diagnostic_contract():
    app = FastAPI()
    router = APIRouter(route_class=CreatorErrorRoute)

    @router.get("/validation")
    async def validation(count: int = Query(...)) -> dict[str, int]:
        return {"count": count}

    @router.get("/lock")
    async def lock_failure() -> None:
        raise LockTimeoutError(
            Path("runtime/locks/session-runtime.lock"),
            10,
            phase="resource",
            waiter={"pid": 11},
            holder={"pid": 22},
        )

    @router.get("/unexpected")
    async def unexpected() -> None:
        raise ValueError("broken invariant")

    app.include_router(router)

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            return (
                await client.get("/validation?count=bad"),
                await client.get("/lock"),
                await client.get("/unexpected"),
            )

    validation_response, lock_response, unexpected_response = asyncio.run(
        scenario(),
    )
    validation_body = validation_response.json()
    assert validation_response.status_code == 422
    assert validation_body["details"]["issues"][0]["location"][-1] == "count"

    lock_body = lock_response.json()
    assert lock_response.status_code == 503
    assert lock_body["code"] == "RUNTIME_LOCK_TIMEOUT"
    assert lock_body["details"]["holder"]["pid"] == 22

    unexpected_body = unexpected_response.json()
    assert unexpected_response.status_code == 500
    assert unexpected_body["code"] == "INTERNAL_ERROR"
    assert unexpected_body["details"]["errorType"] == "ValueError"

    for response in (
        validation_response,
        lock_response,
        unexpected_response,
    ):
        body = response.json()
        assert body["errorId"] == response.headers["X-Creator-Error-ID"]
        assert body["traceId"] == response.headers["X-Creator-Trace-ID"]
        assert body["requestId"] == response.headers["X-Request-ID"]
