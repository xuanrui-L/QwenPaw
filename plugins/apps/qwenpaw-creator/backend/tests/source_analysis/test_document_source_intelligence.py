# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=protected-access
"""Document source flow: read_document tool + document-flavored index."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from api.file_asset_routes import _AssetInput, _ingest_many_sync
from domain.enums import SpecialistRole, SpecialistRunStatus
from domain.errors import ValidationError
from services.media.source_intelligence import (
    parse_source_intelligence_files,
    render_source_intelligence_files,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project
from services.runtime_files.execution_models import SpecialistRunRecord
from services.source_analysis import (
    SourceAgentToolContext,
    SourceMediaAnalysisService,
)
from services.source_analysis.service import (
    document_page_path,
    document_page_ref,
    resolve_document_page_ref,
)
from services.specialist_tools import FileSpecialistToolRegistry


def _pdf_bytes(pages: int = 3) -> bytes:
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    buffer = io.BytesIO()
    with PdfPages(buffer) as pdf:
        for number in range(1, pages + 1):
            fig = plt.figure(figsize=(4, 3))
            fig.text(0.1, 0.5, f"Script page {number} Scene beats {number}")
            pdf.savefig(fig)
            plt.close(fig)
    return buffer.getvalue()


def _services_with_source(
    tmp_path: Path,
    *,
    name: str,
    content: bytes,
    media_type: str,
) -> tuple[CreatorFileServices, str, str]:
    services = CreatorFileServices.create(tmp_path.resolve())
    services.projects.create(Project.new(project_id="project-1", name="One"))
    result, _ = _ingest_many_sync(
        services,
        project_id="project-1",
        key="doc-asset-1",
        inputs=[
            _AssetInput(
                name=name,
                content=content,
                media_type=media_type,
            ),
        ],
        attach_source=True,
        scope="document-source-test",
    )
    item = result["items"][0]
    return services, item["assetId"], item["assetVersionId"]


def _services_with_document(
    tmp_path: Path,
) -> tuple[CreatorFileServices, str, str]:
    return _services_with_source(
        tmp_path,
        name="script.pdf",
        content=_pdf_bytes(pages=3),
        media_type="application/pdf",
    )


def _running_context(
    service: SourceMediaAnalysisService,
    services: CreatorFileServices,
    asset_id: str,
    *,
    tool_call_id: str,
) -> SourceAgentToolContext:
    run_id = "specialist-run-doc-vlm"
    try:
        service.executions.get_run("project-1", run_id)
    except Exception:  # pylint: disable=broad-except
        snapshot = services.projects.read("project-1")
        service.executions.create_specialist_run(
            SpecialistRunRecord(
                run_id=run_id,
                project_id="project-1",
                round_id="round-doc-vlm",
                role=SpecialistRole.SOURCE_INTELLIGENCE,
                target_refs=[f"asset:{asset_id}"],
                input_generation=snapshot.generation,
                input_etag=snapshot.etag,
            ),
        )
        service.executions.transition_specialist_run(
            "project-1",
            run_id,
            expected_status=SpecialistRunStatus.QUEUED,
            status=SpecialistRunStatus.RUNNING_MODEL,
        )
    return SourceAgentToolContext(
        specialist_run_id=run_id,
        tool_call_id=tool_call_id,
        assistant_message_id=f"assistant-{tool_call_id}",
        provider_message_id=f"provider-{tool_call_id}",
        provider="configured_vlm",
        model="vlm-v1",
    )


def _document_shot(page: int, description: str) -> dict:
    return {
        "startMs": (page - 1) * 1000,
        "endMs": page * 1000,
        "description": description,
        "events": [f"第{page}页要点"],
        "confidence": 0.95,
    }


def test_read_document_tool_renders_pages_and_boundaries(tmp_path) -> None:
    services, asset_id, version_id = _services_with_document(tmp_path)
    service = SourceMediaAnalysisService(services)
    context = _running_context(
        service,
        services,
        asset_id,
        tool_call_id="doc-call",
    )

    async def scenario():
        return await service.read_source_document(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            arguments={"fileRef": f"asset-version:{version_id}"},
            context=context,
        )

    result = asyncio.run(scenario())

    assert result["ok"] is True
    assert result["module"] == "document"
    assert result["format"] == "pdf"
    assert result["pageCount"] == 3
    assert result["pagesRendered"] == [1, 2, 3]
    assert "Scene beats 2" in result["textExcerpt"]
    project_root = services.projects.project_root("project-1")
    snapshot = services.projects.read("project-1")
    checksum = snapshot.project.assets.source_versions_by_id[
        version_id
    ].checksum
    assert result["pageImageRefs"] == [
        document_page_ref(checksum, page) for page in (1, 2, 3)
    ]
    for page in (1, 2, 3):
        assert document_page_path(project_root, checksum, page).is_file()
    resolved = resolve_document_page_ref(
        project_root,
        result["pageImageRefs"][0],
    )
    assert resolved is not None and resolved[1] == 1


def test_read_document_rejects_out_of_boundary_file_ref(tmp_path) -> None:
    services, asset_id, _version_id = _services_with_document(tmp_path)
    service = SourceMediaAnalysisService(services)
    context = _running_context(
        service,
        services,
        asset_id,
        tool_call_id="doc-call-boundary",
    )

    async def scenario():
        return await service.read_source_document(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            arguments={"fileRef": "asset-version:another-project-version"},
            context=context,
        )

    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(scenario())
    assert "准入边界" in str(excinfo.value)


def test_document_commit_produces_document_index(tmp_path) -> None:
    services, asset_id, version_id = _services_with_document(tmp_path)
    service = SourceMediaAnalysisService(services)
    read_context = _running_context(
        service,
        services,
        asset_id,
        tool_call_id="doc-read",
    )

    async def scenario():
        read_result = await service.read_source_document(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            arguments={"fileRef": f"asset-version:{version_id}"},
            context=read_context,
        )
        service.executions.append_specialist_message(
            "project-1",
            read_context.specialist_run_id,
            message_id="tool-doc-result",
            role="tool",
            content_parts=[
                {"type": "text", "text": json.dumps(read_result)},
            ],
            metadata={"tool": "read_document", "toolCallId": "doc-read"},
        )
        commit_context = _running_context(
            service,
            services,
            asset_id,
            tool_call_id="doc-commit",
        )
        committed = await service.commit_agent_intelligence(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            command_id="doc-commit-1",
            context=commit_context,
            arguments={
                "summary": "三页剧本：逐页给出场景节拍与关键动作。",
                "shots": [
                    _document_shot(1, "封面页：标题与主角介绍。"),
                    _document_shot(2, "第二页：冲突展开的场景节拍。"),
                    _document_shot(3, "第三页：结尾与情绪收束。"),
                ],
                "entities": [
                    {
                        "kind": "character",
                        "label": "主角猫",
                        "description": "贯穿全剧本的核心角色",
                        "confidence": 0.9,
                    },
                ],
                "semanticEntries": [
                    {
                        "text": "第 2 页给出冲突场景的节拍列表",
                        "tags": ["page-2", "冲突"],
                        "confidence": 0.9,
                    },
                ],
                "moduleResultRefs": {"document": read_result["resultRef"]},
            },
        )
        return read_result, committed

    read_result, committed = asyncio.run(scenario())

    assert committed["status"] == "SUCCEEDED"
    assert committed["shotCount"] == 3
    index = service.load("project-1", asset_id)
    assert index.media.media_kind == "document"
    assert index.media.document is not None
    assert index.media.document.format == "pdf"
    assert index.media.document.page_count == 3
    visual = index.coverage["visual"]
    assert visual.mode == "available"
    assert visual.producer == "document_reader"
    assert visual.ratio == 1.0
    assert [item.keyframe_ref for item in index.shots] == list(
        read_result["pageImageRefs"],
    )
    assert [(item.start_ms, item.end_ms) for item in index.shots] == [
        (0, 1000),
        (1000, 2000),
        (2000, 3000),
    ]

    # Extracted document text is indexed deterministically alongside the
    # model-authored entries, attributed to the document reader module run.
    doc_text_entries = [
        item for item in index.semantic_entries if "document-text" in item.tags
    ]
    assert doc_text_entries
    assert any("Scene beats 2" in item.text for item in doc_text_entries)
    assert "document_reader" in {run.provider for run in index.model_runs}
    model_entries = [
        item
        for item in index.semantic_entries
        if "document-text" not in item.tags
    ]
    assert len(model_entries) == 1

    # The canonical text workspace round-trips document metadata.
    files = render_source_intelligence_files(index)
    assert "documentFormat\tpdf" in files["index.txt"]
    assert parse_source_intelligence_files(files) == index


def test_document_commit_requires_read_document_module(tmp_path) -> None:
    services, asset_id, _version_id = _services_with_document(tmp_path)
    service = SourceMediaAnalysisService(services)
    context = _running_context(
        service,
        services,
        asset_id,
        tool_call_id="doc-commit-missing",
    )

    async def scenario():
        return await service.commit_agent_intelligence(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            command_id="doc-commit-missing",
            context=context,
            arguments={
                "summary": "缺少 read_document 引用的提交。",
                "shots": [_document_shot(1, "第一页")],
                "entities": [],
                "semanticEntries": [],
            },
        )

    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(scenario())
    assert "read_document" in str(excinfo.value)


def test_document_commit_rejects_wrong_page_intervals(tmp_path) -> None:
    services, asset_id, version_id = _services_with_document(tmp_path)
    service = SourceMediaAnalysisService(services)
    read_context = _running_context(
        service,
        services,
        asset_id,
        tool_call_id="doc-read-mismatch",
    )

    async def scenario():
        read_result = await service.read_source_document(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            arguments={"fileRef": f"asset-version:{version_id}"},
            context=read_context,
        )
        service.executions.append_specialist_message(
            "project-1",
            read_context.specialist_run_id,
            message_id="tool-doc-result-mismatch",
            role="tool",
            content_parts=[
                {"type": "text", "text": json.dumps(read_result)},
            ],
            metadata={
                "tool": "read_document",
                "toolCallId": "doc-read-mismatch",
            },
        )
        commit_context = _running_context(
            service,
            services,
            asset_id,
            tool_call_id="doc-commit-mismatch",
        )
        return await service.commit_agent_intelligence(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            command_id="doc-commit-mismatch",
            context=commit_context,
            arguments={
                "summary": "页区间错误的提交。",
                "shots": [_document_shot(1, "只有一页的提交")],
                "entities": [],
                "semanticEntries": [],
                "moduleResultRefs": {"document": read_result["resultRef"]},
            },
        )

    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(scenario())
    assert "页伪" in str(excinfo.value)


def test_commit_tool_contract_admits_document_module_ref(tmp_path) -> None:
    registry = FileSpecialistToolRegistry(
        CreatorFileServices.create(tmp_path.resolve()),
    )
    manifest = registry.manifest_for(
        SpecialistRole.SOURCE_INTELLIGENCE,
        admitted_target_refs=["asset:doc-1"],
    )
    tools = {item["function"]["name"]: item["function"] for item in manifest}
    assert "read_document" in tools
    module_refs = tools["commit_source_intelligence"]["parameters"][
        "properties"
    ]["arguments"]["properties"]["moduleResultRefs"]
    assert set(module_refs["properties"]) == {"asr", "document"}
    assert module_refs["additionalProperties"] is False


def test_csv_source_enters_document_flow_end_to_end(tmp_path) -> None:
    services, asset_id, version_id = _services_with_source(
        tmp_path,
        name="budget.csv",
        content=b"scene,cost\nopening,120\nfinale,340\n",
        media_type="text/csv",
    )
    snapshot = services.projects.read("project-1")
    version = snapshot.project.assets.source_versions_by_id[version_id]
    assert version.media_kind == "document"
    service = SourceMediaAnalysisService(services)
    read_context = _running_context(
        service,
        services,
        asset_id,
        tool_call_id="csv-read",
    )

    async def scenario():
        read_result = await service.read_source_document(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            arguments={"fileRef": f"asset-version:{version_id}"},
            context=read_context,
        )
        service.executions.append_specialist_message(
            "project-1",
            read_context.specialist_run_id,
            message_id="tool-csv-result",
            role="tool",
            content_parts=[
                {"type": "text", "text": json.dumps(read_result)},
            ],
            metadata={"tool": "read_document", "toolCallId": "csv-read"},
        )
        commit_context = _running_context(
            service,
            services,
            asset_id,
            tool_call_id="csv-commit",
        )
        committed = await service.commit_agent_intelligence(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            command_id="csv-commit-1",
            context=commit_context,
            arguments={
                "summary": "预算表：两场戏的成本对比。",
                "shots": [_document_shot(1, "预算数据表：场次与成本。")],
                "entities": [],
                "semanticEntries": [],
                "moduleResultRefs": {"document": read_result["resultRef"]},
            },
        )
        return read_result, committed

    read_result, committed = asyncio.run(scenario())

    assert read_result["format"] == "csv"
    assert read_result["pageCount"] == 1
    assert read_result["pagesRendered"] == [1]
    assert committed["status"] == "SUCCEEDED"
    index = service.load("project-1", asset_id)
    assert index.media.media_kind == "document"
    assert index.media.document is not None
    assert index.media.document.format == "csv"
    assert index.shots[0].keyframe_ref == read_result["pageImageRefs"][0]
    doc_text = [
        item for item in index.semantic_entries if "document-text" in item.tags
    ]
    assert any("finale" in item.text for item in doc_text)


def test_srt_text_only_document_flow(tmp_path) -> None:
    services, asset_id, version_id = _services_with_source(
        tmp_path,
        name="dialogue.srt",
        content=(
            "1\n00:00:01,000 --> 00:00:02,500\n猫走进画面\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n镜头拉远\n"
        ).encode("utf-8"),
        media_type="application/x-subrip",
    )
    snapshot = services.projects.read("project-1")
    version = snapshot.project.assets.source_versions_by_id[version_id]
    assert version.media_kind == "document"
    service = SourceMediaAnalysisService(services)
    read_context = _running_context(
        service,
        services,
        asset_id,
        tool_call_id="srt-read",
    )

    async def scenario():
        read_result = await service.read_source_document(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            arguments={"fileRef": f"asset-version:{version_id}"},
            context=read_context,
        )
        service.executions.append_specialist_message(
            "project-1",
            read_context.specialist_run_id,
            message_id="tool-srt-result",
            role="tool",
            content_parts=[
                {"type": "text", "text": json.dumps(read_result)},
            ],
            metadata={"tool": "read_document", "toolCallId": "srt-read"},
        )
        commit_context = _running_context(
            service,
            services,
            asset_id,
            tool_call_id="srt-commit",
        )
        committed = await service.commit_agent_intelligence(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            command_id="srt-commit-1",
            context=commit_context,
            arguments={
                "summary": "字幕全文：猫入画与镜头拉远两条台词。",
                "shots": [_document_shot(1, "字幕全文概括。")],
                "entities": [],
                "semanticEntries": [],
                "moduleResultRefs": {"document": read_result["resultRef"]},
            },
        )
        return read_result, committed

    read_result, committed = asyncio.run(scenario())

    assert read_result["format"] == "srt"
    assert read_result["pagesRendered"] == []
    assert read_result["pageImageRefs"] == []
    assert committed["status"] == "SUCCEEDED"
    index = service.load("project-1", asset_id)
    assert index.media.media_kind == "document"
    assert index.media.document is not None
    assert index.media.document.format == "srt"
    assert index.media.document.page_count == 1
    assert [(item.start_ms, item.end_ms) for item in index.shots] == [
        (0, 1000),
    ]
    assert index.shots[0].keyframe_ref == f"asset://{asset_id}@{version_id}"
    visual = index.coverage["visual"]
    assert visual.producer == "document_reader"
    assert visual.ratio == 1.0
    doc_text = [
        item for item in index.semantic_entries if "document-text" in item.tags
    ]
    assert any("猫走进画面" in item.text for item in doc_text)


def test_srt_text_only_commit_rejects_page_image_intervals(tmp_path) -> None:
    services, asset_id, version_id = _services_with_source(
        tmp_path,
        name="dialogue.srt",
        content=b"1\n00:00:01,000 --> 00:00:02,500\nhello\n",
        media_type="text/plain",
    )
    service = SourceMediaAnalysisService(services)
    read_context = _running_context(
        service,
        services,
        asset_id,
        tool_call_id="srt-read-bad",
    )

    async def scenario():
        read_result = await service.read_source_document(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            arguments={"fileRef": f"asset-version:{version_id}"},
            context=read_context,
        )
        service.executions.append_specialist_message(
            "project-1",
            read_context.specialist_run_id,
            message_id="tool-srt-result-bad",
            role="tool",
            content_parts=[
                {"type": "text", "text": json.dumps(read_result)},
            ],
            metadata={"tool": "read_document", "toolCallId": "srt-read-bad"},
        )
        commit_context = _running_context(
            service,
            services,
            asset_id,
            tool_call_id="srt-commit-bad",
        )
        return await service.commit_agent_intelligence(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            command_id="srt-commit-bad",
            context=commit_context,
            arguments={
                "summary": "页区间错误的文本型提交。",
                "shots": [
                    _document_shot(1, "第一页"),
                    _document_shot(2, "不存在的第二页"),
                ],
                "entities": [],
                "semanticEntries": [],
                "moduleResultRefs": {"document": read_result["resultRef"]},
            },
        )

    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(scenario())
    assert "页伪" in str(excinfo.value)
