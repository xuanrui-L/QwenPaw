# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access,too-many-statements,unused-argument
# pylint: disable=use-implicit-booleaness-not-comparison
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256

import httpx
from fastapi import FastAPI
import pytest

from api import file_asset_routes, file_media_routes
from api.dependencies import creator_error_handler, project_file_services
from api.file_asset_routes import (
    _assert_supported_source_upload,
    _media_kind,
    _media_kind_compatible,
    _reusable_source_version,
    _validate_public_remote_url,
    router,
)
from api.file_media_routes import router as media_router
from domain.errors import CreatorError, ValidationError
from domain.enums import (
    SpecialistRole,
    SpecialistRunStatus,
    TaskKind,
    TaskStatus,
)
from services.media import (
    source_memory,
    source_observation,
    source_video_reader,
)
from services.media_files.keyframe_cache import CachedKeyframe
from services.project_files.facade import CreatorFileServices
from services.project_files.models import (
    ElementLocation,
    I2VCreation,
    Project,
    TimelineElement,
    TimelineSpan,
)
from services.runtime_files import safe_remote_download
from services.runtime_files.execution_models import (
    SpecialistRunRecord,
    TaskAttemptStatus,
    TaskRecord,
)
from services.runtime_files.execution_store import ProjectExecutionStore


class _ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.yield_count = 0

    def __iter__(self):
        for chunk in self._chunks:
            self.yield_count += 1
            yield chunk


def test_media_kind_classifies_readable_documents_by_extension() -> None:
    # Documents classify by extension even under legacy MIME types; AV
    # prefixes win for raster media, SVG routes to the document reader.
    expectations = {
        ("application/pdf", "script.pdf"): "document",
        ("text/csv", "budget.csv"): "document",
        ("application/vnd.ms-powerpoint", "deck.ppt"): "document",
        ("video/mp4", "clip.mp4"): "video",
        ("image/png", "frame.png"): "image",
        ("image/svg+xml", "icon.svg"): "document",
        ("application/octet-stream", "logo.svg"): "document",
        ("text/x-unknown", "blob.unknownext"): "text",
        ("application/zip", "bundle.zip"): "other",
    }
    for (media_type, name), expected in expectations.items():
        assert _media_kind(media_type, name) == expected, name


def test_source_upload_rejects_unreadable_binary_formats() -> None:
    # Opaque blobs without a renderer cannot enter Source Intelligence
    # and must be refused at the upload boundary with a readable hint.
    with pytest.raises(ValidationError) as excinfo:
        _assert_supported_source_upload(
            "unsupported.exe",
            "application/octet-stream",
        )
    assert "不支持的来源素材格式" in str(excinfo.value)
    assert "unsupported.exe" in str(excinfo.value)
    # Readable creative material passes untouched. 3D models are
    # renderable documents since the model3d renderer landed.
    _assert_supported_source_upload("script.pdf", "application/pdf")
    _assert_supported_source_upload("budget.csv", "text/csv")
    _assert_supported_source_upload("clip.mp4", "video/mp4")
    _assert_supported_source_upload("prop.glb", "model/gltf-binary")


def _install_remote_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chunks: list[bytes],
    headers: dict[str, str] | None = None,
) -> list[_ChunkStream]:
    streams: list[_ChunkStream] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        stream = _ChunkStream(chunks)
        streams.append(stream)
        return httpx.Response(
            200,
            headers=headers or {"content-type": "video/mp4"},
            stream=stream,
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(**kwargs):
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(safe_remote_download.httpx, "Client", client_factory)
    monkeypatch.setattr(
        safe_remote_download,
        "validate_public_remote_url",
        lambda value: value,
    )
    monkeypatch.setattr(
        file_asset_routes,
        "_validate_public_remote_url",
        lambda value: value,
    )
    monkeypatch.setattr(
        safe_remote_download,
        "validate_response_peer",
        lambda _response: None,
    )
    return streams


def _app(tmp_path):
    services = CreatorFileServices.create(tmp_path.resolve())
    services.projects.create(Project.new(project_id="project-1", name="One"))
    app = FastAPI()
    app.add_exception_handler(CreatorError, creator_error_handler)
    app.include_router(router)
    app.include_router(media_router)
    app.dependency_overrides[project_file_services] = lambda: services
    return app, services


def test_text_asset_is_published_indexed_attached_and_replayed(
    tmp_path,
    run_scenario,
) -> None:
    app, services = _app(tmp_path)
    payload = {
        "clientRequestId": "asset-1",
        "kind": "text",
        "name": "brief.txt",
        "value": "hello creator",
        "postIngestAction": "ATTACH_SOURCE",
    }

    async def scenario(client):
        headers = {"Idempotency-Key": "asset-1"}
        first = await client.post(
            "/projects/project-1/assets",
            headers=headers,
            json=payload,
        )
        replay = await client.post(
            "/projects/project-1/assets",
            headers=headers,
            json=payload,
        )
        content = await client.get(
            f"/projects/project-1/assets/{first.json()['assetId']}/content",
            params={"versionId": first.json()["assetVersionId"]},
        )
        media = await client.get(
            f"/media/assets/{first.json()['assetVersionId']}",
        )
        return first, replay, content, media

    first, replay, content, media = run_scenario(app, scenario)
    assert first.status_code == 202
    assert first.json()["status"] == "SUCCEEDED"
    assert replay.status_code == 202
    assert replay.json()["assetVersionId"] == first.json()["assetVersionId"]
    assert replay.json()["result"]["idempotentReplay"] is True
    assert content.status_code == 200
    assert content.content == b"hello creator"
    assert media.status_code == 200
    assert media.content == b"hello creator"

    project = services.projects.read("project-1").project
    assert len(project.assets.files_by_id) == 1
    assert len(project.assets.source_versions_by_id) == 1
    assert len(project.sources.sources.items) == 1
    assert project.generation == 1


def test_oss_source_uses_the_verified_runtime_relative_original(
    tmp_path,
    monkeypatch,
    run_scenario,
) -> None:
    app, _ = _app(tmp_path)
    content = b"image bytes in the project store"

    async def scenario(client):
        uploaded = await client.post(
            "/projects/project-1/assets",
            headers={"Idempotency-Key": "original"},
            data={"clientRequestId": "original", "postIngestAction": "NONE"},
            files={"file": ("窗台.png", content, "image/png")},
        )
        assert uploaded.status_code == 202
        version = uploaded.json()["assetVersionId"]
        url = f"/projects/project-1/media/source/{version}/oss-source"
        response = await client.get(url)
        assert response.status_code == 200
        relative = response.json()["relativePath"]
        assert relative.startswith("project-1/assets/")
        path = tmp_path / relative
        assert path.read_bytes() == content
        wrong_kind = await client.get(url.replace("/source/", "/artifact/"))
        assert wrong_kind.status_code == 404
        with monkeypatch.context() as patch:
            patch.setattr(file_asset_routes, "_MAX_OSS_MEDIA_BYTES", 1)
            oversized = await client.get(url)
            assert oversized.is_error and "500 MB" in oversized.text
        path.unlink()
        assert (await client.get(url)).is_error

    run_scenario(app, scenario)


def test_indexed_media_supports_byte_ranges_and_utf8_filenames(
    tmp_path,
    run_scenario,
) -> None:
    app, _services = _app(tmp_path)
    payload = {
        "clientRequestId": "asset-range-1",
        "kind": "text",
        "name": "猫咪成片.mp4",
        "value": "0123456789",
        "postIngestAction": "ATTACH_SOURCE",
    }

    async def scenario(client):
        created = await client.post(
            "/projects/project-1/assets",
            headers={"Idempotency-Key": "asset-range-1"},
            json=payload,
        )
        version_id = created.json()["assetVersionId"]
        ranged = await client.get(
            f"/media/assets/{version_id}",
            headers={"Range": "bytes=2-5"},
        )
        invalid = await client.get(
            f"/media/assets/{version_id}",
            headers={"Range": "bytes=99-100"},
        )
        return ranged, invalid

    ranged, invalid = run_scenario(app, scenario)
    assert ranged.status_code == 206
    assert ranged.content == b"2345"
    assert ranged.headers["accept-ranges"] == "bytes"
    assert ranged.headers["content-range"] == "bytes 2-5/10"
    disposition = ranged.headers["content-disposition"]
    assert disposition.startswith('inline; filename="media.mp4"; ')
    assert (
        "filename*=UTF-8''%E7%8C%AB%E5%92%AA%E6%88%90%E7%89%87.mp4"
        in disposition
    )
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == "bytes */10"


def test_remote_asset_streams_to_file_and_project_json_only_indexes_it(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    run_scenario,
) -> None:
    app, services = _app(tmp_path)
    chunks = [b"REMOTE_VIDEO_PAYLOAD_", b"STREAMED_WITHOUT_BODY_AGGREGATION"]
    payload = b"".join(chunks)
    monkeypatch.setenv("CREATOR_REMOTE_ASSET_MAX_BYTES", str(len(payload) + 1))
    streams = _install_remote_transport(
        monkeypatch,
        chunks=chunks,
        headers={
            "content-type": "video/mp4",
            "content-length": str(len(payload)),
        },
    )
    frame_path = tmp_path / "frame.jpg"
    frame_path.write_bytes(b"LOCAL_KEYFRAME")
    observed_frame_source: dict[str, object] = {}
    monkeypatch.setattr(
        file_media_routes,
        "ffmpeg_readiness",
        lambda: {"status": "ok", "path": "/fake/ffmpeg"},
    )

    def fake_materialize_keyframe(project_root, **kwargs):
        observed_frame_source.update(kwargs)
        assert project_root == services.projects.project_root("project-1")
        return CachedKeyframe(
            path=frame_path,
            sha256=sha256(frame_path.read_bytes()).hexdigest(),
            size_bytes=frame_path.stat().st_size,
            timestamp_seconds=float(kwargs["timestamp_seconds"]),
            width=int(kwargs["width"]),
        )

    monkeypatch.setattr(
        file_media_routes,
        "materialize_keyframe",
        fake_materialize_keyframe,
    )
    request_payload = {
        "clientRequestId": "remote-asset-1",
        "kind": "url",
        "name": "远程–视频.mp4",
        "value": "https://assets.example/source.mp4",
        "postIngestAction": "ATTACH_SOURCE",
    }

    async def scenario(client):
        headers = {"Idempotency-Key": "remote-asset-1"}
        first = await client.post(
            "/projects/project-1/assets",
            headers=headers,
            json=request_payload,
        )
        assert first.json()["status"] in {"QUEUED", "RUNNING"}
        await asyncio.gather(
            *list(file_asset_routes._REMOTE_INGEST_TASKS.values()),
        )
        replay = await client.post(
            "/projects/project-1/assets",
            headers=headers,
            json=request_payload,
        )
        cached = await client.get(
            f"/media/assets/{first.json()['assetVersionId']}",
        )
        oss_source = await client.get(
            f"/projects/project-1/media/source/{first.json()['assetVersionId']}/oss-source",
        )
        assert oss_source.status_code == 200
        relative = oss_source.json()["relativePath"]
        assert relative.startswith("project-1/runtime/asset-cache/")
        assert (tmp_path / relative).read_bytes() == payload
        direct = await client.get(
            f"/projects/project-1/assets/{first.json()['assetId']}/content",
            params={"versionId": first.json()["assetVersionId"]},
        )
        keyframe = await client.get(
            f"/media/assets/{first.json()['assetVersionId']}/frame",
            params={"timestamp": 4.5, "width": 640},
        )
        return first, replay, cached, direct, keyframe

    first, replay, cached, direct, keyframe = run_scenario(app, scenario)
    assert first.status_code == 202
    assert replay.status_code == 202
    assert first.json()["assetVersionId"].startswith("asset-version-")
    assert replay.json()["assetVersionId"] == first.json()["assetVersionId"]
    assert replay.json()["result"]["idempotentReplay"] is False
    assert cached.status_code == 200
    assert cached.content == payload
    assert direct.status_code == 200
    assert direct.content == payload
    assert keyframe.status_code == 200
    assert keyframe.content == b"LOCAL_KEYFRAME"
    assert keyframe.headers["x-creator-media-source"] == "local-keyframe-cache"
    assert observed_frame_source["timestamp_seconds"] == 4.5
    assert len(streams) == 1
    assert all(stream.yield_count == len(chunks) for stream in streams)

    snapshot = services.projects.read("project-1")
    assert snapshot.project.generation == 1
    assert snapshot.project.assets.files_by_id == {}
    version = snapshot.project.assets.source_versions_by_id[
        first.json()["assetVersionId"]
    ]
    assert version.file_id is None
    assert version.metadata["publicSourceUrl"] == request_payload["value"]
    project_root = services.projects.project_root("project-1")
    cache_item = replay.json()["result"]["items"][0]
    assert cache_item["cacheChecksum"] == sha256(payload).hexdigest()
    assert payload not in (project_root / "project.json").read_bytes()
    assert list((project_root / "assets" / ".staging").iterdir()) == []


def test_remote_asset_stream_limit_cleans_partial_staging(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app_instance, services = _app(tmp_path)
    monkeypatch.setenv("CREATOR_REMOTE_ASSET_MAX_BYTES", "5")
    _install_remote_transport(monkeypatch, chunks=[b"123", b"456"])
    file_store = file_asset_routes.AssetFileStore(
        services.projects.project_root("project-1"),
    )

    with pytest.raises(ValidationError, match="超过 5 bytes 限制"):
        file_asset_routes._download_remote_to_staging(
            "https://assets.example/too-large.mp4",
            file_store=file_store,
            staging_id="limit-test",
        )

    assert list(file_store.staging_root.iterdir()) == []


def test_local_video_route_enforces_its_own_limit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    api_request,
) -> None:
    app, services = _app(tmp_path)
    monkeypatch.setattr(file_asset_routes, "_MAX_LOCAL_VIDEO_UPLOAD_BYTES", 5)
    monkeypatch.setenv("CREATOR_REMOTE_ASSET_MAX_BYTES", "1000")

    response = api_request(
        app,
        "POST",
        "/projects/project-1/assets",
        headers={"Idempotency-Key": "local-video-too-large"},
        data={"clientRequestId": "local-video-too-large"},
        files={"file": ("source.mp4", b"123456", "video/mp4")},
    )
    assert response.status_code == 422
    assert "Local video upload exceeds 2 GiB" in response.text
    project = services.projects.read("project-1").project
    assert project.generation == 0
    assert project.assets.files_by_id == {}


def test_folder_import_commits_all_files_in_one_generation(
    tmp_path,
    run_scenario,
) -> None:
    app, services = _app(tmp_path)

    async def scenario(client):
        accepted = await client.post(
            "/projects/project-1/asset-imports",
            headers={"Idempotency-Key": "import-1"},
            data={
                "clientRequestId": "import-1",
                "postIngestAction": "ATTACH_SOURCE",
            },
            files=[
                ("files", ("a.txt", b"A", "text/plain")),
                ("files", ("b.txt", b"B", "text/plain")),
            ],
        )
        loaded = await client.get(
            f"/projects/project-1/asset-imports/{accepted.json()['importId']}",
        )
        return accepted, loaded

    accepted, loaded = run_scenario(app, scenario)
    assert accepted.status_code == 202
    assert loaded.status_code == 200
    assert loaded.json()["status"] == "SUCCEEDED"
    assert [item["name"] for item in loaded.json()["items"]] == [
        "a.txt",
        "b.txt",
    ]
    project = services.projects.read("project-1").project
    assert project.generation == 1
    assert len(project.assets.source_versions_by_id) == 2
    assert len(project.sources.sources.items) == 2


def test_remote_asset_url_validation_fails_closed_on_ssrf_vectors() -> None:
    # Literal and DNS-resolved private addresses, plus embedded credentials,
    # are refused; only clean public HTTP(S) URLs pass (fragment stripped).
    with pytest.raises(ValidationError, match="私有或保留网络"):
        _validate_public_remote_url("http://127.0.0.1/private")

    def private_resolver(*_args, **_kwargs):
        return [(2, 1, 6, "", ("10.0.0.8", 80))]

    with pytest.raises(ValidationError, match="私有或保留网络"):
        _validate_public_remote_url(
            "https://assets.example/video.mp4",
            resolver=private_resolver,
        )

    def public_resolver(*_args, **_kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    with pytest.raises(ValidationError, match="用户名或密码"):
        _validate_public_remote_url(
            "https://user:secret@assets.example/video.mp4",
            resolver=public_resolver,
        )
    assert (
        _validate_public_remote_url(
            "https://assets.example/video.mp4?version=1#ignored",
            resolver=public_resolver,
        )
        == "https://assets.example/video.mp4?version=1"
    )


def _session_app(tmp_path):
    """A ``project-1`` whose Runtime Session mirrors real project creation."""

    services = CreatorFileServices.create(tmp_path.resolve())
    services.projects.create(
        Project.new(project_id="project-1", name="One"),
        initialize_staged_project=lambda staged_root: (
            services.sessions.initialize_staged_project(
                staged_root,
                "project-1",
                session_id="session-1",
                conversation_id="conversation-1",
            )
        ),
    )
    app = FastAPI()
    app.add_exception_handler(CreatorError, creator_error_handler)
    app.include_router(router)
    app.include_router(media_router)
    app.dependency_overrides[project_file_services] = lambda: services
    return app, services


def _install_notification_capture(services, monkeypatch):
    # pylint: disable=import-outside-toplevel
    from types import SimpleNamespace

    from services.file_agent_runtime.notifications import (
        RuntimeNotificationBus,
    )

    wakes: list[str] = []
    bus = RuntimeNotificationBus(services, wake_dispatcher=wakes.append)
    monkeypatch.setattr(
        file_asset_routes,
        "get_creator_agent_runtime",
        lambda: SimpleNamespace(notifications=bus),
    )
    return wakes


def _uploaded_notifications(services):
    return [
        item
        for item in services.sessions.list_messages(
            "project-1",
            "session-1",
            after_seq=0,
            limit=None,
        )
        if item.metadata.get("notificationKind") == "source_assets_uploaded"
    ]


def test_asset_uploads_steer_the_agent_once_per_ingest(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    run_scenario,
) -> None:
    """One steer per ingest, gated on the session having a first message:
    launch-flow uploads stay silent (their refs ride the initial message),
    multipart replays stay silent, URL assets notify once the background
    download lands, and composer-staged uploads (notifyAgent=false) ride
    their outgoing message instead."""

    app, services = _session_app(tmp_path)
    _install_remote_transport(monkeypatch, chunks=[b"12", b"34"])
    wakes = _install_notification_capture(services, monkeypatch)

    async def scenario(client):
        # Launch flow: the session has no messages yet — stay silent.
        launch = await client.post(
            "/projects/project-1/assets",
            headers={"Idempotency-Key": "launch-upload"},
            data={
                "clientRequestId": "launch-upload",
                "postIngestAction": "NONE",
            },
            files={"file": ("cover.png", b"png-bytes-0", "image/png")},
        )
        assert launch.status_code == 202
        assert wakes == []
        assert (
            services.sessions.list_messages(
                "project-1",
                "session-1",
                after_seq=0,
                limit=None,
            )
            == []
        )
        services.sessions.append_message(
            "project-1",
            "session-1",
            "conversation-1",
            role="user",
            content_parts=[{"type": "text", "text": "做一条视频"}],
            source="user",
        )
        first = await client.post(
            "/projects/project-1/assets",
            headers={"Idempotency-Key": "upload-1"},
            data={
                "clientRequestId": "upload-1",
                "postIngestAction": "ATTACH_SOURCE",
            },
            files={"file": ("lulu.png", b"png-bytes", "image/png")},
        )
        replay = await client.post(
            "/projects/project-1/assets",
            headers={"Idempotency-Key": "upload-1"},
            data={
                "clientRequestId": "upload-1",
                "postIngestAction": "ATTACH_SOURCE",
            },
            files={"file": ("lulu.png", b"png-bytes", "image/png")},
        )
        remote = await client.post(
            "/projects/project-1/assets",
            headers={"Idempotency-Key": "remote-1"},
            json={
                "clientRequestId": "remote-1",
                "kind": "url",
                "name": "clip.mp4",
                "value": "https://assets.example/clip.mp4",
                "postIngestAction": "ATTACH_SOURCE",
            },
        )
        pending = list(file_asset_routes._REMOTE_INGEST_TASKS.values())
        if pending:
            await asyncio.gather(*pending)
        # Composer-staged uploads ride the outgoing message instead:
        # notifyAgent=false must stay silent.
        staged = await client.post(
            "/projects/project-1/assets",
            headers={"Idempotency-Key": "staged-1"},
            data={
                "clientRequestId": "staged-1",
                "postIngestAction": "ATTACH_SOURCE",
                "notifyAgent": "false",
            },
            files={"file": ("staged.png", b"png-bytes-2", "image/png")},
        )
        return first, replay, remote, staged

    first, replay, remote, staged = run_scenario(app, scenario)

    assert first.status_code == 202
    assert replay.status_code == 202
    assert remote.status_code == 202
    assert staged.status_code == 202
    notes = _uploaded_notifications(services)
    assert len(notes) == 2
    upload_note, remote_note = notes
    assert upload_note.role == "user"
    assert "lulu.png" in upload_note.content_parts[0].text
    assert upload_note.metadata["assetVersionRefs"] == [
        f"asset-version:{first.json()['assetVersionId']}",
    ]
    assert remote_note.metadata["assetVersionRefs"] == [
        f"asset-version:{remote.json()['assetVersionId']}",
    ]
    assert wakes == ["project-1", "project-1"]


class _RecordingRuntime:
    """Stand in for the process Creator runtime during asset deletion."""

    def __init__(self, *, owns_run: bool) -> None:
        self.owns_run = owns_run
        self.calls: list[tuple[str, str, str]] = []

    def cancel_specialist_run(
        self,
        project_id: str,
        run_id: str,
        *,
        reason: str,
    ) -> bool:
        self.calls.append((project_id, run_id, reason))
        return self.owns_run


def _seed_active_source_run(
    services: CreatorFileServices,
    version_id: str,
    run_id: str,
) -> str:
    """Create a QUEUED (active) SOURCE_INTELLIGENCE run for ``version_id``."""

    snapshot = services.projects.read("project-1")
    logical_id = snapshot.project.assets.source_versions_by_id[
        version_id
    ].logical_asset_id
    executions = ProjectExecutionStore(services.root)
    executions.create_specialist_run(
        SpecialistRunRecord(
            run_id=run_id,
            project_id="project-1",
            round_id="agent-round-del",
            role=SpecialistRole.SOURCE_INTELLIGENCE,
            status=SpecialistRunStatus.QUEUED,
            target_refs=[f"asset:{logical_id}"],
            input_generation=snapshot.generation,
            input_etag=snapshot.etag,
            related_run_id="agent-run-del",
            prompt_spec_id="file_project_json.source_intelligence.v1",
            caused_by_message_id="message-del",
            caused_by_message_seq=1,
            metadata={"parentActionId": "delegate-call-del"},
        ),
    )
    return run_id


def _upload_source(client, payload, key):
    return client.post(
        "/projects/project-1/assets",
        headers={"Idempotency-Key": key},
        json=payload,
    )


_TEXT_ASSET_PAYLOAD = {
    "clientRequestId": "asset-del-1",
    "kind": "text",
    "name": "brief.txt",
    "value": "hello creator",
    "postIngestAction": "ATTACH_SOURCE",
}


@pytest.mark.parametrize(
    "with_session",
    [False, True],
    ids=["no-session", "session"],
)
@pytest.mark.parametrize(
    "owns_run",
    [False, True],
    ids=["orphan", "live-worker"],
)
def test_delete_asset_settles_or_defers_the_source_run(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    run_scenario,
    with_session,
    owns_run,
) -> None:
    app, services = _app(tmp_path)
    runtime = _RecordingRuntime(owns_run=owns_run)
    monkeypatch.setattr(
        file_asset_routes,
        "get_creator_agent_runtime",
        lambda: runtime,
    )
    if with_session:
        session = services.sessions.create_project_runtime("project-1").session
    run_id = "spec-delete"

    async def scenario(client):
        created = await _upload_source(
            client,
            _TEXT_ASSET_PAYLOAD,
            "asset-del-1",
        )
        version_id = created.json()["assetVersionId"]
        _seed_active_source_run(services, version_id, run_id)
        return await client.delete(
            f"/projects/project-1/assets/{version_id}",
            headers={"Idempotency-Key": "del-source"},
        )

    deleted = run_scenario(app, scenario)
    assert deleted.status_code == 204
    assert runtime.calls == [("project-1", run_id, "source_asset_deleted")]
    run = ProjectExecutionStore(services.root).get_specialist_run(
        "project-1",
        run_id,
    )
    if owns_run:
        assert run.status is SpecialistRunStatus.QUEUED
    else:
        assert run.status is SpecialistRunStatus.CANCELLED
        assert run.metadata.get("cancelReason") == "SOURCE_ASSET_DELETED"

    if with_session:
        failed = [
            event
            for event in services.sessions.list_events(
                "project-1",
                session.session_id,
            )
            if event.event_type == "subagent.failed"
        ]
        if owns_run:
            assert not failed
        else:
            assert [event.payload["runId"] for event in failed] == [run_id]
            event = failed[0]
            assert event.actor == "file_agent_runtime"
            assert event.round_id == f"agent-round-{run_id}"
            assert event.payload["cancelled"] is True
            assert event.payload["error"] == "source_asset_deleted"
            assert event.payload["parentActionId"] == "delegate-call-del"
            assert (
                event.payload["role"]
                == SpecialistRole.SOURCE_INTELLIGENCE.value
            )


def test_delete_asset_settles_run_created_after_the_first_sweep(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    run_scenario,
) -> None:
    # A run whose durable record lands after the route's opening listing is
    # invisible to that loop; only the post-deletion re-check can settle it
    # and tell the frontend the activity is over.
    app, services = _app(tmp_path)
    runtime = _RecordingRuntime(owns_run=False)
    monkeypatch.setattr(
        file_asset_routes,
        "get_creator_agent_runtime",
        lambda: runtime,
    )
    session_id = services.sessions.create_project_runtime(
        "project-1",
    ).session.session_id
    real_list = ProjectExecutionStore.list_specialist_runs
    sweeps: list[list[str]] = []
    state = {"hiding": True}

    def hide_late_run(self, project_id):
        records = real_list(self, project_id)
        if state["hiding"]:
            # Simulate the race: the opening sweep cannot see the run that
            # the driver was writing at the very same moment.
            state["hiding"] = False
            records = [
                record for record in records if record.run_id != "spec-late"
            ]
        sweeps.append([record.run_id for record in records])
        return records

    monkeypatch.setattr(
        ProjectExecutionStore,
        "list_specialist_runs",
        hide_late_run,
    )

    async def scenario(client):
        created = await _upload_source(
            client,
            _TEXT_ASSET_PAYLOAD,
            "asset-del-1",
        )
        version_id = created.json()["assetVersionId"]
        _seed_active_source_run(services, version_id, "spec-early")
        _seed_active_source_run(services, version_id, "spec-late")
        deleted = await client.delete(
            f"/projects/project-1/assets/{version_id}",
            headers={"Idempotency-Key": "del-late"},
        )
        executions = ProjectExecutionStore(services.root)
        late = executions.get_specialist_run("project-1", "spec-late")
        events = services.sessions.list_events("project-1", session_id)
        return (
            deleted,
            late,
            [
                event.payload["runId"]
                for event in events
                if event.event_type == "subagent.failed"
            ],
        )

    deleted, late, notified = run_scenario(app, scenario)
    assert deleted.status_code == 204
    assert sweeps[0] == ["spec-early"]
    assert "spec-late" in sweeps[-1]
    assert late.status is SpecialistRunStatus.CANCELLED
    assert late.metadata.get("cancelReason") == "SOURCE_ASSET_DELETED"
    assert sorted(notified) == ["spec-early", "spec-late"]


def test_delete_asset_route_names_references_and_accepts_force(
    tmp_path,
    run_scenario,
) -> None:
    # The frontend branch depends on the route contract: a referenced asset
    # answers a structured 409 the UI can render by name, and the same delete
    # retried with ?force=true clears that reference instead of refusing.
    app, services = _app(tmp_path)

    async def scenario(client):
        # The ingest guard pairs clientRequestId with the Idempotency-Key, so
        # this asset keeps its own pair instead of reusing the shared payload.
        created = await _upload_source(
            client,
            {
                **_TEXT_ASSET_PAYLOAD,
                "clientRequestId": "asset-in-use-1",
            },
            "asset-in-use-1",
        )
        version_id = created.json()["assetVersionId"]
        base = services.projects.read("project-1")
        project = base.project.model_copy(deep=True)
        element = TimelineElement(
            element_id="el-shot-1",
            label="镜头一｜风雪定场",
            span=TimelineSpan(start_tick=0, duration_tick=3000),
            location=ElementLocation(),
            creation=I2VCreation(
                first_frame_version_id=version_id,
                video_prompt="雪花落满屋檐",
            ),
        )
        project.timelines.items["timeline:main"] = project.timelines.items[
            "timeline:main"
        ].model_copy(
            update={"elements_by_id": {element.element_id: element}},
        )
        services.projects.replace(
            "project-1",
            project.model_copy(
                update={
                    "generation": base.project.generation + 1,
                    "updated_at": datetime.now(timezone.utc),
                },
            ),
            base.etag,
        )

        conflict = await client.delete(
            f"/projects/project-1/assets/{version_id}",
            headers={"Idempotency-Key": "del-in-use"},
        )
        body = conflict.json()
        forced = await client.delete(
            f"/projects/project-1/assets/{version_id}?force=true",
            headers={"Idempotency-Key": "del-forced"},
        )
        after = services.projects.read("project-1").project
        return conflict, body, forced, version_id, after

    conflict, body, forced, version_id, after = run_scenario(app, scenario)

    assert conflict.status_code == 409
    assert body["code"] == "ASSET_IN_USE"
    references = body["details"]["timelineReferences"]
    assert references[0]["refKind"] == "i2v-first-frame"
    assert references[0]["elementLabel"] == "镜头一｜风雪定场"
    assert references[0]["detachable"] is True

    assert forced.status_code == 204
    assert version_id not in after.assets.source_versions_by_id
    assert (
        after.timelines.items["timeline:main"]
        .elements_by_id["el-shot-1"]
        .creation.first_frame_version_id
        is None
    )


class _RecordingObservationCancellation:
    """Stands in for the observation service inside the real registry."""

    def __init__(self, services: CreatorFileServices) -> None:
        self.services = services
        self.calls: list[tuple] = []

    async def cancel_for_asset(
        self,
        project_id: str,
        *,
        logical_asset_id: str | None = None,
        version_id: str | None = None,
    ) -> int:
        self.calls.append((project_id, logical_asset_id, version_id))
        # Deliberately settles nothing: the route's own durable CAS still has
        # to converge a Task no live worker owns in this process.
        return 0


def _seed_standalone_observation_task(
    services: CreatorFileServices,
    *,
    task_id: str,
    logical_asset_id: str,
    version_id: str,
) -> str:
    """One RUNNING observe_source_clip Task with no SpecialistRun behind it."""

    executions = ProjectExecutionStore(services.root)
    executions.create_task(
        TaskRecord(
            task_id=task_id,
            project_id="project-1",
            kind=TaskKind.OBSERVE_SOURCE_CLIP,
            request_fingerprint=f"observe-{task_id}",
            input_refs=[f"asset:{logical_asset_id}"],
            metadata={
                "targetRef": f"asset:{logical_asset_id}",
                "assetVersionId": version_id,
            },
        ),
    )
    executions.append_attempt(
        "project-1",
        task_id,
        event_id=f"{task_id}-a1-running",
        attempt_id=f"{task_id}-a1",
        status=TaskAttemptStatus.RUNNING,
        input={"assetVersionId": version_id},
    )
    return task_id


class _RecordingWorkerCancellation:
    """Stands in for one media owner service inside the real registry."""

    def __init__(self, services: CreatorFileServices) -> None:
        self.services = services
        self.calls: list[tuple] = []

    async def cancel_for_asset(
        self,
        project_id: str,
        *,
        logical_asset_id: str | None = None,
        version_id: str | None = None,
    ) -> int:
        self.calls.append((project_id, logical_asset_id, version_id))
        return 0


def test_delete_asset_revokes_media_workers_through_every_owner(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    run_scenario,
) -> None:
    # Settling a Task record is not cancelling its worker: the frame read and
    # the memory build each own an in-process coroutine that would otherwise
    # keep paying for model stages and publish artifacts built from bytes that
    # were just deleted. Every owner has to be reached, per Task kind.
    app, services = _app(tmp_path)
    observations = _RecordingWorkerCancellation(services)
    reads = _RecordingWorkerCancellation(services)
    memories = _RecordingWorkerCancellation(services)
    source_observation._SERVICES[str(services.root)] = observations
    source_video_reader._SERVICES[str(services.root)] = reads
    source_memory._SERVICES[str(services.root)] = memories
    monkeypatch.setattr(
        file_asset_routes,
        "get_creator_agent_runtime",
        lambda: None,
    )

    async def scenario(client):
        created = await _upload_source(
            client,
            _TEXT_ASSET_PAYLOAD,
            "asset-del-1",
        )
        version_id = created.json()["assetVersionId"]
        logical_id = (
            services.projects.read(
                "project-1",
            )
            .project.assets.source_versions_by_id[version_id]
            .logical_asset_id
        )
        deleted = await client.delete(
            f"/projects/project-1/assets/{version_id}",
            headers={"Idempotency-Key": "del-owner"},
        )
        return deleted, logical_id, version_id

    try:
        deleted, logical_id, version_id = run_scenario(app, scenario)
    finally:
        source_observation.clear_source_observation_service_registry()
        source_video_reader.clear_source_video_reader_service_registry()
        source_memory.clear_source_memory_service_registry()

    assert deleted.status_code == 204
    expected = [("project-1", logical_id, version_id)]
    assert observations.calls == expected
    assert (
        reads.calls == expected
    ), "the frame reader must be revoked through its own service"
    assert (
        memories.calls == expected
    ), "the memory build must be revoked through its own service"


def test_delete_asset_cancels_independent_observation_task(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    run_scenario,
) -> None:
    # A standalone media Task belongs to no SpecialistRun, so matching only
    # Task.run_id left it RUNNING against media that had just been deleted —
    # and its in-process worker kept running the VLM call, ready to publish an
    # observation of footage that no longer exists.
    app, services = _app(tmp_path)
    # Registered through the service registry (not by patching the route's
    # import) so the assertion is about the route's behaviour: a delete that
    # never reaches the owning service leaves the live VLM call running.
    observations = _RecordingObservationCancellation(services)
    source_observation._SERVICES[str(services.root)] = observations
    monkeypatch.setattr(
        file_asset_routes,
        "get_creator_agent_runtime",
        lambda: None,
    )

    async def scenario(client):
        created = await _upload_source(
            client,
            _TEXT_ASSET_PAYLOAD,
            "asset-del-1",
        )
        version_id = created.json()["assetVersionId"]
        logical_id = (
            services.projects.read(
                "project-1",
            )
            .project.assets.source_versions_by_id[version_id]
            .logical_asset_id
        )
        task_id = _seed_standalone_observation_task(
            services,
            task_id="observe-del",
            logical_asset_id=logical_id,
            version_id=version_id,
        )
        other_id = _seed_standalone_observation_task(
            services,
            task_id="observe-other",
            logical_asset_id="asset-other",
            version_id="version-other",
        )
        deleted = await client.delete(
            f"/projects/project-1/assets/{version_id}",
            headers={"Idempotency-Key": "del-observe"},
        )
        executions = ProjectExecutionStore(services.root)
        return (
            deleted,
            executions.get_task("project-1", task_id),
            executions.get_task("project-1", other_id),
            logical_id,
            version_id,
            list(observations.calls),
        )

    try:
        result = run_scenario(app, scenario)
    finally:
        source_observation.clear_source_observation_service_registry()

    deleted, task, other, logical_id, version_id, calls = result

    assert deleted.status_code == 204
    assert calls == [
        ("project-1", logical_id, version_id),
    ], "the live observation worker must be revoked through its service"
    assert task.status is TaskStatus.CANCELLED
    assert task.error == {
        "code": "SOURCE_ASSET_DELETED",
        "message": "Source asset was deleted",
    }
    assert (
        other.status is TaskStatus.RUNNING
    ), "a Task reading another asset must survive this deletion"


async def _post_image(client, *, key: str, name: str, content: bytes):
    """One composer-style multipart upload, silent towards the Agent."""

    return await client.post(
        "/projects/project-1/assets",
        headers={"Idempotency-Key": key},
        data={
            "clientRequestId": key,
            "postIngestAction": "ATTACH_SOURCE",
            "notifyAgent": "false",
        },
        files={"file": (name, content, "image/png")},
    )


@pytest.mark.parametrize("kind", ["image", "text"])
def test_renamed_reupload_reuses_bytes_and_keeps_the_original_asset(
    tmp_path,
    run_scenario,
    kind,
) -> None:
    app, services = _app(tmp_path)
    suffix = ".png" if kind == "image" else ".txt"
    name = "开场" + suffix
    content = _TEXT_ASSET_PAYLOAD["value"].encode()

    async def upload(client, key, filename):
        if kind == "image":
            return await _post_image(
                client,
                key=key,
                name=filename,
                content=content,
            )
        return await _upload_source(
            client,
            {**_TEXT_ASSET_PAYLOAD, "clientRequestId": key, "name": filename},
            key,
        )

    async def scenario(client):
        first = await upload(client, "dup-1", name)
        again = await upload(client, "dup-2", "开场-新名" + suffix)
        return first, again

    first, again = run_scenario(app, scenario)
    assert first.status_code == again.status_code == 202
    first, again = first.json(), again.json()
    assert first["reused"] is False
    assert again["reused"] is True
    assert again["assetVersionId"] == first["assetVersionId"]
    assert again["assetId"] == first["assetId"]
    item = again["result"]["items"][0]
    assert item["reused"] is True
    assert item["reusedName"] == name

    project = services.projects.read("project-1").project
    assert (
        len(project.assets.files_by_id)
        == len(project.sources.sources.items)
        == 1
    )
    assert project.generation == 1
    (version,) = project.assets.source_versions_by_id.values()
    assert version.name == name
    assert version.checksum == sha256(content).hexdigest()
    sources = (
        services.projects.project_root("project-1") / "assets" / "sources"
    )
    assert len(list(sources.iterdir())) == 1


def test_reusable_source_version_prefers_the_oldest_stored_match() -> None:
    # Projects created before dedup can hold several versions of one content
    # under different logical Assets. The pick decides which Asset the user
    # and the Agent are pointed at, so it may not depend on index order.
    files = {"file-old": {}, "file-new": {}}
    versions = {
        "version-new": {
            "version_id": "version-new",
            "checksum": "abc",
            "file_id": "file-new",
            "created_at": "2026-01-02T00:00:00+00:00",
        },
        "version-url": {
            "version_id": "version-url",
            "checksum": "abc",
            # A remote registration hashes the URL, never the bytes.
            "file_id": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        "version-old": {
            "version_id": "version-old",
            "checksum": "abc",
            "file_id": "file-old",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    }

    result = _reusable_source_version(
        files=files,
        versions=versions,
        checksum="abc",
    )
    assert len(result) == 2  # version-url excluded (remote, no file_id)
    assert result[0]["version_id"] == "version-old"
    assert result[1]["version_id"] == "version-new"
    assert (
        _reusable_source_version(
            files=files,
            versions=versions,
            checksum="other",
        )
        == []
    )


def test_batch_import_reuses_duplicate_within_batch(
    tmp_path,
    run_scenario,
) -> None:
    # A folder containing the same file twice enters as one Asset, while the
    # distinct file still lands: the fold happens per item, not per request.
    app, services = _app(tmp_path)

    async def scenario(client):
        accepted = await client.post(
            "/projects/project-1/asset-imports",
            headers={"Idempotency-Key": "import-dup"},
            data={
                "clientRequestId": "import-dup",
                "postIngestAction": "ATTACH_SOURCE",
            },
            files=[
                ("files", ("a.png", b"shared-bytes", "image/png")),
                ("files", ("b.png", b"other-bytes", "image/png")),
                ("files", ("a-copy.png", b"shared-bytes", "image/png")),
            ],
        )
        loaded = await client.get(
            f"/projects/project-1/asset-imports/{accepted.json()['importId']}",
        )
        return accepted, loaded

    accepted, loaded = run_scenario(app, scenario)

    assert accepted.status_code == 202
    assert loaded.status_code == 200
    items = loaded.json()["items"]
    assert [item["name"] for item in items] == ["a.png", "b.png", "a-copy.png"]
    assert (
        items[2]["assetVersionId"] == items[0]["assetVersionId"]
    ), "the repeated file folds onto the version the batch just published"
    assert len({item["assetVersionId"] for item in items}) == 2
    project = services.projects.read("project-1").project
    assert len(project.assets.source_versions_by_id) == 2
    assert len(project.sources.sources.items) == 2


def test_deleted_asset_uploads_fresh_when_reuploaded(
    tmp_path,
    run_scenario,
) -> None:
    # Dedup reads the Project's live index only: once an Asset is deleted, the
    # same bytes are new material again and must not be folded onto a ghost.
    app, services = _app(tmp_path)

    async def scenario(client):
        first = await _post_image(
            client,
            key="again-1",
            name="one.png",
            content=b"returning-bytes",
        )
        version_id = first.json()["assetVersionId"]
        deleted = await client.delete(
            f"/projects/project-1/assets/{version_id}",
        )
        replay = await _post_image(
            client,
            key="again-2",
            name="one.png",
            content=b"returning-bytes",
        )
        return first, deleted, replay

    first, deleted, replay = run_scenario(app, scenario)

    assert deleted.status_code == 204
    assert replay.status_code == 202
    assert replay.json()["reused"] is False
    assert replay.json()["assetVersionId"] != first.json()["assetVersionId"]
    project = services.projects.read("project-1").project
    assert list(project.assets.source_versions_by_id) == [
        replay.json()["assetVersionId"],
    ]
    assert len(project.sources.sources.items) == 1


@pytest.mark.parametrize(
    ("stored", "uploaded", "compatible"),
    [
        ("image", "image", True),
        ("audio", "audio", True),
        ("video", "video", True),
        ("image", "audio", False),
        ("audio", "video", False),
        ("video", "image", False),
        ("image", "other", False),
        *(
            ("other", kind, True)
            for kind in ("image", "audio", "video", "text")
        ),
    ],
)
def test_media_kind_compatibility(stored, uploaded, compatible):
    assert (
        _media_kind_compatible(stored=stored, uploaded=uploaded) is compatible
    )


@pytest.mark.parametrize(
    ("media_kind", "checksum", "expected"),
    [
        (None, "abc", ["image", "audio", "other"]),
        ("image", "abc", ["image", "other"]),
        ("audio", "abc", ["audio", "other"]),
        ("video", "abc", ["other"]),
        ("document", "abc", ["other"]),
        ("text", "abc", ["other"]),
        ("image", "xyz", []),
    ],
)
def test_reusable_source_versions_match_checksum_and_media_kind(
    media_kind,
    checksum,
    expected,
):
    kinds = ["image", "audio", "other"]
    versions = {
        kind: {
            "version_id": kind,
            "checksum": "abc",
            "file_id": kind,
            "media_kind": kind,
            "created_at": f"2026-01-0{index}T00:00:00+00:00",
        }
        for index, kind in enumerate(kinds, start=1)
    }
    found = _reusable_source_version(
        files=dict.fromkeys(kinds, {}),
        versions=versions,
        checksum=checksum,
        media_kind=media_kind,
    )
    assert [version["version_id"] for version in found] == expected
