# -*- coding: utf-8 -*-
"""剧本直登：共享落版本实现与主 Agent ``import_script_asset`` 工具。"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.project_files import agent_tools as agent_tools_module
from services.project_files import script_import as script_import_module
from services.project_files.agent_tools import (
    AgentProjectToolContext,
    AgentProjectToolError,
    AgentProjectTools,
    UnknownAgentProjectTool,
)
from services.project_files.assets import AssetFileStore
from services.project_files.commit import ProjectCommitBoundary
from services.project_files.models import IndexedFile, SourceAssetVersion
from services.project_files.script_artifacts import (
    script_file_relative_uri,
    timeline_script_slot_id,
)
from services.project_files.script_import import (
    IMPORT_SCRIPT_TOOL_NAME,
    ScriptImportError,
    import_script_tool_manifest,
    is_importable_script_text,
    normalize_script_text,
    publish_script_import,
)
from services.runtime_files.models import (
    ChangeOrigin,
    ReviewBoundary,
    ReviewPolicy,
)

from .conftest import PROJECT_ID, make_store, read_state

pytestmark = pytest.mark.unit

SCRIPT_TEXT = (
    "## 场 1 · 内景 · 旧宅大厅 · 夜\n\n"
    "林晚推门进来， Dust 在光柱里浮动。\n\n"
    "**林晚**（低声）：我回来了。\n\n"
    "> 她看见母亲遗留的钥匙压在鞋下。\n"
)

# 一份内容不同的改稿：归一化后 checksum / 版本 ID 均与 SCRIPT_TEXT 不同。
REVISED_TEXT = "## 场 1 · 改稿 B\n\n新的开场， Dust 在光柱里浮动。\n"


def _front_end_meta() -> dict[str, object]:
    return {
        "origin": ChangeOrigin.FRONTEND_EDIT,
        "review_policy": ReviewPolicy.AUTO_FIX,
        "caused_by_request_id": "script-import:request-1",
    }


def _publish(store, text: str = SCRIPT_TEXT, **kwargs):
    timeline_id = kwargs.pop("timeline_id", "timeline:main")
    return publish_script_import(
        store=store,
        commits=ProjectCommitBoundary(store),
        project_id=PROJECT_ID,
        timeline_id=timeline_id,
        raw_text=text,
        source_name=kwargs.pop("source_name", "我的剧本.md"),
        commit_metadata=kwargs.pop("commit_metadata", _front_end_meta()),
        **kwargs,
    )


def _ingest_asset(
    store,
    *,
    text: str = SCRIPT_TEXT,
    media_kind: str = "text",
    media_type: str = "text/markdown",
    version_id: str = "sv-import-1",
    file_id: str = "file-import-source-1",
    name: str = "我的剧本.md",
) -> None:
    """Publish one asset file and index it as a source asset version."""

    content = text.encode("utf-8")
    asset_store = AssetFileStore(store.project_root(PROJECT_ID))
    published = asset_store.publish(
        asset_store.stage_bytes(content, staging_id=f"stage-{file_id}"),
        f"assets/sources/{file_id}/{name}",
    )
    base = store.read(PROJECT_ID)
    candidate = base.project.model_copy(deep=True)
    candidate.assets.files_by_id[file_id] = IndexedFile(
        file_id=file_id,
        kind="source_original",
        relative_uri=published.relative_uri,
        sha256=published.sha256,
        size_bytes=published.size_bytes,
        media_type=media_type,
        created_at=datetime.now(UTC),
    )
    candidate.assets.source_versions_by_id[version_id] = SourceAssetVersion(
        version_id=version_id,
        logical_asset_id=f"source-{version_id}",
        name=name,
        file_id=file_id,
        checksum=published.sha256,
        media_kind=media_kind,
        media_type=media_type,
        created_at=datetime.now(UTC),
    )
    ProjectCommitBoundary(store).commit(
        base=base,
        candidate=candidate.model_dump(mode="json"),
        origin=ChangeOrigin.RUNTIME_TASK,
        review_policy=ReviewPolicy.AUTO_FIX,
    )


def _ingest_document_asset(
    store,
    *,
    name: str,
    media_type: str,
    version_id: str,
    file_id: str,
) -> None:
    """Index a Word/PDF asset: no text layer, only bytes on disk."""

    _ingest_asset(
        store,
        text="PK\x03\x04-not-really-a-document",
        media_kind="document",
        media_type=media_type,
        version_id=version_id,
        file_id=file_id,
        name=name,
    )


def _tools(store, *, context: AgentProjectToolContext | None = None):
    return AgentProjectTools(
        store,
        context=context
        or AgentProjectToolContext(
            origin="runtime_task",
            caused_by_request_id="request-1",
            caused_by_message_seq=1,
        ),
    )


def test_normalize_is_block_round_trip_and_rejects_blank() -> None:
    markdown, blocks = normalize_script_text(SCRIPT_TEXT)
    assert blocks == 4
    # 归一化幂等：前端块级渲染与后续 Agent 写入面对同一份基线。
    assert normalize_script_text(markdown) == (markdown, blocks)
    with pytest.raises(ScriptImportError, match="为空"):
        normalize_script_text("   \n  ")


def test_is_importable_script_text_only_accepts_readable_text() -> None:
    assert is_importable_script_text(
        media_kind="text",
        media_type="text/plain",
    )
    assert is_importable_script_text(
        media_kind="document",
        media_type="text/markdown",
    )
    assert not is_importable_script_text(
        media_kind="video",
        media_type="video/mp4",
    )
    # 结构化 JSON 不是剧本正文，不能被当成可读文稿直登。
    assert not is_importable_script_text(
        media_kind="document",
        media_type="application/json",
    )


def test_publish_lands_selected_version_file_and_history(tmp_path) -> None:
    store, base = make_store(tmp_path)

    outcome = _publish(store)

    assert outcome.replayed is False
    assert outcome.block_count == 4
    snapshot = store.read(PROJECT_ID)
    assert snapshot.generation == base.generation + 1
    slot = snapshot.project.assets.artifact_slots_by_id[
        timeline_script_slot_id("timeline:main")
    ]
    assert slot.selected_version_id == outcome.artifact_version_id
    version = snapshot.project.assets.artifact_versions_by_id[
        outcome.artifact_version_id
    ]
    assert version.metadata["source"] == "script_import"
    assert version.metadata["importedFromName"] == "我的剧本.md"
    # 正文真落在内容寻址的文件里，可直接给剧本页读取。
    indexed = snapshot.project.assets.files_by_id[outcome.file_id]
    assert indexed.relative_uri == script_file_relative_uri(outcome.file_id)
    stream = AssetFileStore(store.project_root(PROJECT_ID)).open_verified(
        indexed,
    )
    imported = normalize_script_text(SCRIPT_TEXT)[0]
    assert stream.read().decode("utf-8") == imported
    stream.close()
    # 提交返回可广播的 changedPointers，主 Agent 循环据此发 workspace 事件。
    assert outcome.changed_pointers
    assert outcome.to_payload()["resultKind"] == "script_import"


def test_repeating_identical_content_replays_without_new_generation(
    tmp_path,
) -> None:
    store, base = make_store(tmp_path)
    first = _publish(store)

    replay = _publish(store)

    assert replay.replayed is True
    assert replay.artifact_version_id == first.artifact_version_id
    assert replay.generation == store.read(PROJECT_ID).generation
    assert replay.generation == first.generation
    assert not replay.changed_pointers
    slot = store.read(PROJECT_ID).project.assets.artifact_slots_by_id[
        timeline_script_slot_id("timeline:main")
    ]
    assert slot.version_ids == [first.artifact_version_id]
    assert base.generation == 0


def test_revised_script_appends_version_and_keeps_previous(tmp_path) -> None:
    store, _base = make_store(tmp_path)
    first = _publish(store)

    second = _publish(store, text="## 场 1 · 改稿\n\n新的开场。\n")

    assert second.artifact_version_id != first.artifact_version_id
    slot = store.read(PROJECT_ID).project.assets.artifact_slots_by_id[
        timeline_script_slot_id("timeline:main")
    ]
    assert slot.version_ids == [
        first.artifact_version_id,
        second.artifact_version_id,
    ]
    assert slot.selected_version_id == second.artifact_version_id


def test_reselecting_a_previous_script_after_a_revision_is_a_new_edit(
    tmp_path,
) -> None:
    """A→B→A：重新选用旧稿是一次新编辑，绝不复用旧稿已提交的事务。

    回归：事务身份此前由正文 checksum 派生，第三次导入 A 会撞上第一次
    A 已 COMMITTED 的事务并被提交边界拒绝（ProjectCommitError）。事务
    身份改为按「本次编辑」派生后，A→B→A 三次都是独立事务。
    """
    store, base = make_store(tmp_path)
    first = _publish(store, text=SCRIPT_TEXT)
    second = _publish(store, text=REVISED_TEXT)

    # 第三次重新选用 A：当前选中的是 B，所以这不是 no-op 复放，而是
    # 一次把 selected 指回 A 的真实编辑。
    third = _publish(store, text=SCRIPT_TEXT)

    assert third.replayed is False
    assert third.artifact_version_id == first.artifact_version_id
    assert third.artifact_version_id != second.artifact_version_id
    slot = store.read(PROJECT_ID).project.assets.artifact_slots_by_id[
        timeline_script_slot_id("timeline:main")
    ]
    assert slot.selected_version_id == first.artifact_version_id
    # 内容寻址：A 不会因重新选用而被追加成重复版本。
    assert slot.version_ids == [
        first.artifact_version_id,
        second.artifact_version_id,
    ]
    assert store.read(PROJECT_ID).generation == base.generation + 3


def test_idempotency_key_dedupes_a_retry_but_each_edit_stays_distinct(
    tmp_path,
) -> None:
    """显式幂等键只识别「同一次请求的重试」，不把不同编辑并成一个事务。"""
    store, base = make_store(tmp_path)

    # 同一内容 + 同一键重试：第二次是复放，不再制造新生成。
    first = _publish(store, text=SCRIPT_TEXT, idempotency_key="key-a")
    retry = _publish(store, text=SCRIPT_TEXT, idempotency_key="key-a")
    assert retry.replayed is True
    assert retry.generation == first.generation

    # A→B→A 各用不同的键：三次都是独立事务，重新选用旧稿不会撞车。
    _publish(store, text=REVISED_TEXT, idempotency_key="key-b")
    third = _publish(store, text=SCRIPT_TEXT, idempotency_key="key-c")
    assert third.replayed is False
    assert third.artifact_version_id == first.artifact_version_id
    assert store.read(PROJECT_ID).generation == base.generation + 3


def test_reselecting_previous_content_under_review_is_a_new_edit(
    tmp_path,
) -> None:
    """待审阅场景下重新选用旧稿同样是新编辑，各拿到独立事务与审阅。"""
    store, base = make_store(tmp_path)

    def _review_publish(text: str, seq: int):
        snapshot = store.read(PROJECT_ID)
        boundary = ReviewBoundary(
            request_message_seq=seq,
            request_id=f"request-{seq}",
            interrupted_run_id=f"run-{seq - 1}",
            accepted_generation=snapshot.generation,
            accepted_etag=snapshot.etag,
        )
        return _publish(
            store,
            text=text,
            commit_metadata={
                "origin": ChangeOrigin.AGENTDOCK_INTERRUPT,
                "review_policy": ReviewPolicy.REQUIRE_REVIEW,
                "review_boundary": boundary,
                "caused_by_request_id": boundary.request_id,
                "caused_by_message_seq": boundary.request_message_seq,
            },
        )

    first = _review_publish(SCRIPT_TEXT, 2)
    _review_publish(REVISED_TEXT, 3)
    third = _review_publish(SCRIPT_TEXT, 4)

    assert first.review_id is not None and third.review_id is not None
    assert third.review_id != first.review_id
    assert third.replayed is False
    assert third.artifact_version_id == first.artifact_version_id
    slot = store.read(PROJECT_ID).project.assets.artifact_slots_by_id[
        timeline_script_slot_id("timeline:main")
    ]
    assert slot.selected_version_id == first.artifact_version_id
    assert store.read(PROJECT_ID).generation == base.generation + 3


def test_replay_file_id_points_at_the_indexed_file_not_the_version(
    tmp_path,
) -> None:
    """复放返回的 fileId 必须是文件索引里的键，而不是 artifact 版本 ID。

    回归：复放分支此前把 slot.selected_version_id（artifact-script-…）塞进
    fileId，它既不在 assets.files_by_id 里，又和同一返回体的
    artifactVersionId 重复，依赖 fileId 读文件的调用方会失败。
    """
    store, _base = make_store(tmp_path)
    first = _publish(store)

    replay = _publish(store)

    assert replay.replayed is True
    assert replay.file_id == first.file_id
    assert replay.file_id != replay.artifact_version_id
    project = store.read(PROJECT_ID).project
    assert replay.file_id in project.assets.files_by_id
    # 复放指向的版本确实解析到同一个已索引文件。
    version = project.assets.artifact_versions_by_id[
        replay.artifact_version_id
    ]
    assert version.file_id == replay.file_id
    # 该文件真实可读，正文就是导入的剧本。
    indexed = project.assets.files_by_id[replay.file_id]
    stream = AssetFileStore(store.project_root(PROJECT_ID)).open_verified(
        indexed,
    )
    assert (
        stream.read().decode("utf-8") == normalize_script_text(SCRIPT_TEXT)[0]
    )
    stream.close()


def test_unknown_and_snapshot_timeline_are_rejected(tmp_path) -> None:
    store, _base = make_store(tmp_path)

    with pytest.raises(ScriptImportError, match="timeline 不存在") as caught:
        _publish(store, timeline_id="timeline:ghost")
    assert caught.value.code == "SCRIPT_IMPORT_TIMELINE_NOT_FOUND"

    # 历史副本不进工作图，写进它的剧本用户永远看不到。
    with pytest.raises(ScriptImportError, match="历史副本") as snapshot:
        _publish(store, timeline_id="snapshot:timeline:main:1")
    assert snapshot.value.code == "SCRIPT_IMPORT_SNAPSHOT_TIMELINE"
    assert timeline_script_slot_id("snapshot:timeline:main:1") not in (
        store.read(PROJECT_ID).project.assets.artifact_slots_by_id
    )


def test_oversized_payload_is_rejected_before_write(
    tmp_path,
    monkeypatch,
) -> None:
    store, base = make_store(tmp_path)
    monkeypatch.setattr(script_import_module, "SCRIPT_IMPORT_MAX_BYTES", 32)

    with pytest.raises(ScriptImportError, match="超过直登上限"):
        _publish(store)

    assert store.read(PROJECT_ID).generation == base.generation
    assert not store.read(
        PROJECT_ID,
    ).project.assets.artifact_slots_by_id


def test_review_required_context_lands_a_review_instead_of_head(
    tmp_path,
) -> None:
    store, base = make_store(tmp_path)
    boundary = ReviewBoundary(
        request_message_seq=2,
        request_id="request-2",
        interrupted_run_id="run-1",
        accepted_generation=base.generation,
        accepted_etag=base.etag,
    )

    outcome = _publish(
        store,
        commit_metadata={
            "origin": ChangeOrigin.AGENTDOCK_INTERRUPT,
            "review_policy": ReviewPolicy.REQUIRE_REVIEW,
            "review_boundary": boundary,
            "caused_by_request_id": boundary.request_id,
            "caused_by_message_seq": boundary.request_message_seq,
        },
    )

    # 直登不越审阅：剧本进入待审，已接受基线仍停在原头部。
    assert outcome.review_id is not None
    state = read_state(store)
    assert state.accepted_etag == base.etag
    assert state.accepted_generation == base.generation
    assert state.last_project_generation == base.generation + 1


def test_tool_manifest_is_mainline_only_shape() -> None:
    manifest = import_script_tool_manifest()
    assert manifest["function"]["name"] == IMPORT_SCRIPT_TOOL_NAME
    assert manifest["function"]["parameters"]["required"] == [
        "projectId",
        "timelineId",
        "assetVersionId",
    ]


def test_agent_tool_lands_text_asset_without_a_project_source(
    tmp_path,
) -> None:
    store, _base = make_store(tmp_path)
    _ingest_asset(store)
    tools = _tools(store)

    payload = tools.invoke(
        IMPORT_SCRIPT_TOOL_NAME,
        {
            "projectId": PROJECT_ID,
            "timelineId": "timeline:main",
            "assetVersionId": "sv-import-1",
        },
    )

    assert payload["resultKind"] == "script_import"
    assert payload["blockCount"] == 4
    assert payload["reviewId"] is None
    project = store.read(PROJECT_ID).project
    # 不建 ProjectSource，因此这条素材不会再被委派素材理解。
    assert project.sources.sources.items == {}
    slot = project.assets.artifact_slots_by_id[
        timeline_script_slot_id("timeline:main")
    ]
    assert slot.selected_version_id == payload["artifactVersionId"]
    # 同一素材重复调用是复放，不制造空生成。
    again = tools.invoke(
        IMPORT_SCRIPT_TOOL_NAME,
        {
            "projectId": PROJECT_ID,
            "timelineId": "timeline:main",
            "assetVersionId": "sv-import-1",
        },
    )
    assert again["replayed"] is True
    assert again["generation"] == payload["generation"]


def test_agent_tool_rejects_non_text_and_missing_version(tmp_path) -> None:
    store, _base = make_store(tmp_path)
    _ingest_asset(
        store,
        media_kind="video",
        media_type="video/mp4",
        version_id="sv-video-1",
        file_id="file-video-1",
        name="片段.mp4",
    )
    tools = _tools(store)

    with pytest.raises(AgentProjectToolError, match="只有 UTF-8 文本") as caught:
        tools.invoke(
            IMPORT_SCRIPT_TOOL_NAME,
            {
                "projectId": PROJECT_ID,
                "timelineId": "timeline:main",
                "assetVersionId": "sv-video-1",
            },
        )
    assert caught.value.code == "SCRIPT_IMPORT_NOT_TEXT"

    with pytest.raises(AgentProjectToolError) as missing:
        tools.invoke(
            IMPORT_SCRIPT_TOOL_NAME,
            {
                "projectId": PROJECT_ID,
                "timelineId": "timeline:main",
                "assetVersionId": "sv-ghost",
            },
        )
    assert missing.value.code == "SCRIPT_IMPORT_VERSION_NOT_FOUND"


def test_agent_tool_reports_a_corrupt_source_file(tmp_path) -> None:
    """源文件损坏属 Asset 边界：要给错误码，而不是抛内部路径异常。

    ``open_verified`` 的 size/sha 校验抛 ``AssetFileCorrupt``；读路径此前
    没包 ``except``，而紧接其后的落盘路径却包了，同一个方法里两条路径
    不一致，异常会带着英文文案和文件系统路径直接逃逸给模型。
    """

    store, _base = make_store(tmp_path)
    _ingest_asset(store)
    indexed = store.read(PROJECT_ID).project.assets.files_by_id[
        "file-import-source-1"
    ]
    stored = store.project_root(PROJECT_ID) / Path(indexed.relative_uri)
    # 改变长度即可让 open_verified 的 size 校验失败。
    stored.write_bytes(b"truncated")

    tools = _tools(store)
    with pytest.raises(AgentProjectToolError) as caught:
        tools.invoke(
            IMPORT_SCRIPT_TOOL_NAME,
            {
                "projectId": PROJECT_ID,
                "timelineId": "timeline:main",
                "assetVersionId": "sv-import-1",
            },
        )

    assert caught.value.code == "SCRIPT_IMPORT_FILE_ERROR"
    assert "读取剧本源文件失败" in str(caught.value)
    # 失败发生在落版本之前，不留半个剧本 slot。
    assert timeline_script_slot_id("timeline:main") not in (
        store.read(PROJECT_ID).project.assets.artifact_slots_by_id
    )


def test_agent_tool_lands_a_word_document_script(
    tmp_path,
    monkeypatch,
) -> None:
    """写在 Word 里的剧本也能直登，不再被逐页当图片理解。

    模型仍只递交版本 ID：正文由服务端从文件索引抽出，不会被重抄进
    工具参数。真实渲染依赖不在单测范围，抽取本身另有用例覆盖。
    """

    store, _base = make_store(tmp_path)
    _ingest_document_asset(
        store,
        name="我的剧本.docx",
        media_type=(
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        ),
        version_id="sv-docx-1",
        file_id="file-docx-1",
    )
    seen: list[str] = []

    def fake_extract(data: bytes, *, name: str) -> str:
        seen.append(name)
        assert data
        return SCRIPT_TEXT

    monkeypatch.setattr(
        agent_tools_module,
        "extract_script_document_text",
        fake_extract,
    )

    payload = _tools(store).invoke(
        IMPORT_SCRIPT_TOOL_NAME,
        {
            "projectId": PROJECT_ID,
            "timelineId": "timeline:main",
            "assetVersionId": "sv-docx-1",
        },
    )

    assert payload["resultKind"] == "script_import"
    assert payload["blockCount"] == 4
    assert seen == ["我的剧本.docx"]
    project = store.read(PROJECT_ID).project
    # 同样不建 ProjectSource：文档不会再被委派素材理解。
    assert project.sources.sources.items == {}


def test_agent_tool_reports_a_document_it_cannot_extract(
    tmp_path,
    monkeypatch,
) -> None:
    """扫描件/缺 LibreOffice：错误码原样透出，模型才知道要回退。"""

    store, _base = make_store(tmp_path)
    _ingest_document_asset(
        store,
        name="扫描剧本.pdf",
        media_type="application/pdf",
        version_id="sv-pdf-1",
        file_id="file-pdf-1",
    )

    def fake_extract(data: bytes, *, name: str) -> str:
        raise ScriptImportError(
            f"{name} 抽不出可用正文：扫描件没有文字层。",
            code="SCRIPT_IMPORT_DOCUMENT_NO_TEXT",
        )

    monkeypatch.setattr(
        agent_tools_module,
        "extract_script_document_text",
        fake_extract,
    )

    with pytest.raises(AgentProjectToolError) as caught:
        _tools(store).invoke(
            IMPORT_SCRIPT_TOOL_NAME,
            {
                "projectId": PROJECT_ID,
                "timelineId": "timeline:main",
                "assetVersionId": "sv-pdf-1",
            },
        )
    assert caught.value.code == "SCRIPT_IMPORT_DOCUMENT_NO_TEXT"
    assert "文字层" in str(caught.value)
    # 失败的直登不留半个剧本版本。
    assert (
        timeline_script_slot_id("timeline:main")
        not in store.read(PROJECT_ID).project.assets.artifact_slots_by_id
    )


def test_unknown_tool_still_raises_after_dispatch_extension(tmp_path) -> None:
    """The import branch must not swallow the unknown-tool fallback."""

    store, _base = make_store(tmp_path)

    with pytest.raises(UnknownAgentProjectTool):
        _tools(store).invoke("no_such_tool", {"projectId": PROJECT_ID})
