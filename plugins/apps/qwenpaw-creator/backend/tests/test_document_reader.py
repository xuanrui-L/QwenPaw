# -*- coding: utf-8 -*-
# pylint: disable=protected-access
# flake8: noqa: E501
"""Document reading and indexing through the source-intelligence flow."""

from __future__ import annotations

import asyncio
import itertools
import json
import random
from pathlib import Path

import pytest
from PIL import Image

from api.file_asset_routes import _AssetInput, _ingest_many_sync
from domain.enums import SpecialistRole, SpecialistRunStatus
from domain.errors import ValidationError
from services.document_reader import (
    MAX_INDEXED_TEXT_CHARS,
    is_supported_document,
    read_document,
    read_document_text,
)
from services.project_files.facade import CreatorFileServices
from services.project_files.models import Project
from services.runtime_files.execution_models import SpecialistRunRecord
from services.source_analysis import (
    SourceAgentToolContext,
    SourceMediaAnalysisService,
)
from services.source_analysis.service import (
    document_indexed_text_path,
    resolve_document_page_ref,
)
from vendor.media_toolkit.image_budget import (
    IMAGE_BUDGET_TOKENS,
    IMAGE_MIN_PIXELS,
    TOKEN_SIZE,
    budget_to_pixels,
    smart_resize,
)


def _make_pdf(path: Path, pages: int = 3) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    with PdfPages(path) as pdf:
        for number in range(1, pages + 1):
            fig = plt.figure(figsize=(4, 3))
            fig.text(0.1, 0.5, f"Creator Doc Page {number}: storyboard beats")
            pdf.savefig(fig)
            plt.close(fig)


def _make_mixed_pdf(path: Path) -> None:
    """Page 1 selectable text; page 2 rasterized dialogue (no text layer)."""
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from PIL import ImageDraw

    with PdfPages(path) as pdf:
        fig = plt.figure(figsize=(4, 3))
        fig.text(0.08, 0.5, "SCENE ONE. Cover: a selectable text page.")
        pdf.savefig(fig)
        plt.close(fig)

        img = Image.new("RGB", (600, 200), "white")
        ImageDraw.Draw(img).text(
            (20, 80),
            "SCENE TWO. Alice: the missing ending must be in the script.",
            fill="black",
        )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        fig2 = plt.figure(figsize=(4, 3))
        ax = fig2.add_axes([0, 0, 1, 1])
        ax.axis("off")
        ax.imshow(Image.open(buf))
        pdf.savefig(fig2)
        plt.close(fig2)


def _make_text_then_blank_pdf(path: Path) -> None:
    """Page 1 selectable text; page 2 genuinely blank (no text, no image)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    with PdfPages(path) as pdf:
        fig = plt.figure(figsize=(4, 3))
        fig.text(0.08, 0.5, "SCENE ONE. Cover: a selectable text page.")
        pdf.savefig(fig)
        plt.close(fig)
        blank = plt.figure(figsize=(4, 3))
        pdf.savefig(blank)
        plt.close(blank)


def _make_text_then_vector_pdf(path: Path) -> None:
    """Page 1 selectable text; page 2 = text outlined to curves (转曲).

    A producer that outlines the body text into vector paths leaves no text
    layer and no raster image -- only many small glyph-shaped paths.  Such a
    page looks blank to a text/image check, yet its body cannot be extracted,
    so it must be reported rather than silently dropped.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.patches import Rectangle

    rng = random.Random(20261022)
    with PdfPages(path) as pdf:
        fig = plt.figure(figsize=(4, 3))
        fig.text(0.08, 0.5, "SCENE ONE. Cover: a selectable text page.")
        pdf.savefig(fig)
        plt.close(fig)

        # Page 2: 40 varied glyph-like vector shapes, no text layer.  Varied
        # geometry keeps matplotlib from batching them into a single path, the
        # way real outlined text emits one path per glyph stroke.
        fig2 = plt.figure(figsize=(4, 3))
        ax = fig2.add_axes([0, 0, 1, 1])
        ax.axis("off")
        for _ in range(40):
            ax.add_patch(
                Rectangle(
                    (rng.random() * 0.8, rng.random() * 0.8),
                    0.01 + rng.random() * 0.03,
                    0.04 + rng.random() * 0.05,
                    facecolor="black",
                    edgecolor="none",
                ),
            )
        pdf.savefig(fig2)
        plt.close(fig2)


def _make_text_then_merged_outline_pdf(path: Path) -> None:
    """Page 1 selectable text; page 2 = 转曲 body merged into ONE path.

    Some producers (Ghostscript/ps2pdf, Illustrator/InDesign flatten) outline
    the text and merge every glyph into a single vector path.  The page then
    has no text layer, no raster image and only *one* non-background path, so
    an object-count check alone would call it blank -- yet that path carries
    many segments (the merged glyph outlines) and its body cannot be
    extracted, so it must still be reported.
    """
    import math

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.patches import Polygon

    with PdfPages(path) as pdf:
        fig = plt.figure(figsize=(4, 3))
        fig.text(0.08, 0.5, "SCENE ONE. Cover: a selectable text page.")
        pdf.savefig(fig)
        plt.close(fig)

        # Page 2: one closed polygon with many vertices => a single path with
        # many segments, the shape a merged 转曲 outline takes.  No text layer.
        fig2 = plt.figure(figsize=(4, 3))
        ax = fig2.add_axes([0, 0, 1, 1])
        ax.axis("off")
        points = [
            (
                0.5 + 0.3 * math.cos(2 * math.pi * i / 40),
                0.5 + 0.3 * math.sin(2 * math.pi * i / 40),
            )
            for i in range(40)
        ]
        ax.add_patch(
            Polygon(
                points,
                closed=True,
                facecolor="black",
                edgecolor="none",
            ),
        )
        pdf.savefig(fig2)
        plt.close(fig2)


def _make_header_plus_scan_pdf(path: Path) -> None:
    """Page 1 selectable text; page 2 = copyable header + rasterized body.

    The running header is a real text layer, so a naive "page has any text"
    test would call page 2 complete -- yet the dialogue only exists inside the
    full-page image.  This is the mixed page that must still be reported.
    """
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from PIL import ImageDraw

    with PdfPages(path) as pdf:
        fig = plt.figure(figsize=(4, 3))
        fig.text(0.08, 0.5, "SCENE ONE. Cover: a selectable text page.")
        pdf.savefig(fig)
        plt.close(fig)

        img = Image.new("RGB", (600, 400), "white")
        ImageDraw.Draw(img).text(
            (20, 150),
            "SCENE TWO. Alice: the missing ending must be in the script.",
            fill="black",
        )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        fig2 = plt.figure(figsize=(4, 3))
        # A copyable running header: sparse text that must not mask the scan.
        fig2.text(0.1, 0.93, "Page 2 - Screenplay")
        ax = fig2.add_axes([0, 0, 1, 0.85])
        ax.axis("off")
        ax.imshow(Image.open(buf))
        pdf.savefig(fig2)
        plt.close(fig2)


def _make_tiled_scan_pdf(path: Path) -> None:
    """Page 1 selectable text; page 2 = copyable header + body split in tiles.

    A producer that slices a scanned body into several image tiles defeats a
    "largest single image" coverage test: no one tile dominates the page, yet
    together they cover it.  The accumulated coverage must still be reported.
    """
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from PIL import ImageDraw

    def _tile(text: str) -> io.BytesIO:
        img = Image.new("RGB", (300, 500), "white")
        ImageDraw.Draw(img).text((20, 230), text, fill="black")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf

    with PdfPages(path) as pdf:
        fig = plt.figure(figsize=(4, 3))
        fig.text(0.08, 0.5, "SCENE ONE. Cover: a selectable text page.")
        pdf.savefig(fig)
        plt.close(fig)

        fig2 = plt.figure(figsize=(4, 3))
        # A copyable running header: sparse text that must not mask the scan.
        fig2.text(0.1, 0.94, "Page 2 - Screenplay")
        left = fig2.add_axes([0.03, 0.05, 0.47, 0.85])
        left.axis("off")
        left.imshow(Image.open(_tile("SCENE TWO. Alice: the")), aspect="auto")
        right = fig2.add_axes([0.5, 0.05, 0.47, 0.85])
        right.axis("off")
        right.imshow(
            Image.open(_tile("missing ending stays")),
            aspect="auto",
        )
        pdf.savefig(fig2)
        plt.close(fig2)


def _read(path: Path, output_dir: Path, **kwargs):
    return asyncio.run(read_document(path, output_dir=output_dir, **kwargs))


def _write_csv(path: Path, rows: int) -> None:
    lines = ["scene,cost"] + [f"scene-{n},{n}" for n in range(1, rows)]
    lines.append("final-unique-scene,9999")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_smart_resize_never_exceeds_budget() -> None:
    # The grid snap must never overshoot the tier's pixel budget (A3).
    rng = random.Random(7)
    dims = [1, 2, 10, 31, 32, 33, 100, 864, 1216, 4096, 10000, 50000]
    cases = list(itertools.product(dims, dims)) + [
        (rng.randint(1, 60000), rng.randint(1, 60000)) for _ in range(500)
    ]
    for budget in ("small", "normal", "large"):
        max_pixels = budget_to_pixels(budget, IMAGE_BUDGET_TOKENS)
        for height, width in cases:
            h, w = smart_resize(height, width, IMAGE_MIN_PIXELS, max_pixels)
            assert h % TOKEN_SIZE == 0 and w % TOKEN_SIZE == 0
            assert h >= TOKEN_SIZE and w >= TOKEN_SIZE
            assert h * w <= max_pixels, (budget, height, width)


def test_pdf_render_grid_alignment_page_range_and_text_cap(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "script.pdf"
    _make_pdf(source, pages=4)
    result = _read(source, tmp_path / "pages")

    assert result.format == "pdf" and result.page_count == 4
    assert result.pages_rendered == (1, 2, 3, 4)
    normal_budget = budget_to_pixels("normal", IMAGE_BUDGET_TOKENS)
    for page, image_path in zip(result.pages_rendered, result.page_images):
        assert image_path.name == f"page-{page:04d}.png"
        with Image.open(image_path) as img:
            width, height = img.size
        assert width % TOKEN_SIZE == 0 and height % TOKEN_SIZE == 0
        assert width * height <= normal_budget
    assert "Creator Doc Page 1: storyboard beats" in result.text_excerpt
    subset = _read(source, tmp_path / "subset", pages="2,4")
    assert subset.pages_rendered == (2, 4)
    # Full text is decoupled from the render subset: pages outside the
    # range still contribute their text layer to indexed_text.
    marker = "Creator Doc Page 3: storyboard beats"
    assert marker in subset.indexed_text
    assert marker not in subset.text_excerpt
    # With a known page total a text-page cap reports the exact share.
    target = "vendor.media_toolkit.renderers.pdf.DEFAULT_MAX_TEXT_PAGES"
    monkeypatch.setattr(target, 2)
    capped = _read(source, tmp_path / "capped")
    assert capped.extraction_complete is False
    assert capped.extraction_fraction == 0.5
    assert any("capped at 2 of 4 pages" in note for note in capped.notes)


@pytest.mark.parametrize(
    ("make_pdf", "unextractable", "visible_text", "rasterized_text"),
    [
        pytest.param(_make_pdf, (), "Creator Doc Page 1", None, id="text"),
        pytest.param(
            _make_text_then_blank_pdf,
            (),
            "SCENE ONE",
            None,
            id="blank-page",
        ),
        pytest.param(
            _make_mixed_pdf,
            (2,),
            "SCENE ONE",
            "SCENE TWO",
            id="image-only",
        ),
        pytest.param(
            _make_text_then_vector_pdf,
            (2,),
            "SCENE ONE",
            None,
            id="outlines",
        ),
        pytest.param(
            _make_text_then_merged_outline_pdf,
            (2,),
            "SCENE ONE",
            None,
            id="merged-outline",
        ),
        pytest.param(
            _make_header_plus_scan_pdf,
            (2,),
            "Page 2",
            "missing ending",
            id="scan-with-header",
        ),
        pytest.param(
            _make_tiled_scan_pdf,
            (2,),
            "Page 2",
            "missing ending",
            id="tiled-scan-with-header",
        ),
    ],
)
def test_pdf_reports_pages_whose_body_cannot_be_extracted(
    tmp_path,
    make_pdf,
    unextractable,
    visible_text,
    rasterized_text,
) -> None:
    document = tmp_path / "script.pdf"
    make_pdf(document)

    result = read_document_text(document)

    # Exhausting the page range can still leave part of the script unreadable.
    assert result.extraction_complete is True
    assert result.unextractable_pages == unextractable
    assert visible_text in result.indexed_text
    if rasterized_text is not None:
        assert rasterized_text not in result.indexed_text


def test_csv_display_cap_and_row_cap_extraction_reporting(
    tmp_path,
    monkeypatch,
) -> None:
    # CR repro: a 2002-row CSV keeps its last row in the indexed full
    # text even though the display table caps far earlier.
    source = tmp_path / "big.csv"
    _write_csv(source, 2002)
    result = _read(source, tmp_path / "pages")

    assert result.format == "csv" and result.page_count == 1
    assert len(result.page_images) == 1
    assert "final-unique-scene" not in result.text_excerpt
    assert "final-unique-scene" in result.indexed_text
    # CR repro: a row-capped table must not pretend complete extraction;
    # the true row total is unknowable, so the fraction is None.
    target = "vendor.media_toolkit.renderers.data.FULL_TEXT_ROW_CAP"
    monkeypatch.setattr(target, 50)
    capped = _read(source, tmp_path / "capped")
    assert "final-unique-scene" not in capped.indexed_text
    assert capped.extraction_complete is False
    assert capped.extraction_fraction is None
    assert any("capped at 50 rows" in note for note in capped.notes)


def test_indexed_text_bound_is_honest_about_truncation(tmp_path) -> None:
    # Indexing is intentionally bounded: oversized text is cut at the
    # bound and the coverage numbers report it, not "full text".
    source = tmp_path / "huge.txt"
    marker = "末尾唯一标记：星光不灭。"
    text = ("剧情推进。" * 100 + "\n") * 4200 + marker + "\n"
    source.write_text(text, encoding="utf-8")
    result = _read(source, tmp_path / "pages")

    assert result.extracted_chars > MAX_INDEXED_TEXT_CHARS
    assert len(result.indexed_text) == MAX_INDEXED_TEXT_CHARS
    assert marker not in result.indexed_text
    assert any("indexing bound" in note for note in result.notes)
    # The indexing bound is separate from renderer-stage extraction.
    assert result.extraction_complete is True


def test_xlsx_sheets_map_to_pages(tmp_path) -> None:
    import pandas as pd

    source = tmp_path / "plan.xlsx"
    frame = pd.DataFrame({"shot": ["s1", "s2"], "len": [3, 5]})
    with pd.ExcelWriter(source, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Shots", index=False)
        frame.to_excel(writer, sheet_name="Cast", index=False)
    result = _read(source, tmp_path / "pages")

    assert result.format == "xlsx" and result.page_count == 2
    assert result.pages_rendered == (1, 2)
    assert "Shots" in result.text_excerpt and "Cast" in result.text_excerpt


def test_text_only_renderer_srt(tmp_path) -> None:
    srt = tmp_path / "dialogue.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,500\n猫走进画面\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n镜头拉远\n",
        encoding="utf-8",
    )
    result = _read(srt, tmp_path / "pages")
    assert result.format == "srt" and result.page_images == ()
    assert "2 entries" in result.text_excerpt
    assert "猫走进画面" in result.text_excerpt


def test_readable_errors_for_unsupported_inputs(tmp_path, monkeypatch):
    assert is_supported_document("deck.PPTX")
    assert not is_supported_document("archive.zip")
    zipped = tmp_path / "bundle.zip"
    zipped.write_bytes(b"PK\x03\x04")
    with pytest.raises(ValidationError, match="unsupported document format"):
        _read(zipped, tmp_path / "pages")
    target = "services.document_reader._resolve_soffice"
    monkeypatch.setattr(target, lambda: None)
    docx = tmp_path / "brief.docx"
    docx.write_bytes(b"PK\x03\x04fake-docx")
    with pytest.raises(ValidationError, match="LibreOffice is required"):
        _read(docx, tmp_path / "pages")
    monkeypatch.delenv("CREATOR_DOC_READER_WEB_ENABLED", raising=False)
    html = tmp_path / "page.html"
    html.write_text("<html><body>hello</body></html>", encoding="utf-8")
    with pytest.raises(ValidationError, match="WEB_ENABLED"):
        _read(html, tmp_path / "pages")


def _pdf_bytes(pages: int = 3, *, text: str | None = None) -> bytes:
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    buffer = io.BytesIO()
    with PdfPages(buffer) as pdf:
        for number in range(1, pages + 1):
            fig = plt.figure(figsize=(4, 3))
            fig.text(
                0.1,
                0.5,
                text or f"Script page {number} Scene beats {number}",
            )
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
            _AssetInput(name=name, content=content, media_type=media_type),
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


def _read_then_commit(
    service: SourceMediaAnalysisService,
    services: CreatorFileServices,
    asset_id: str,
    version_id: str,
    *,
    tag: str,
    shots: list[dict],
    between_read_and_commit=None,
    mutate_stored=None,
    extra_arguments: dict | None = None,
):
    """Shared read -> (mutate) -> commit flow for document tests."""
    read_context = _running_context(
        service,
        services,
        asset_id,
        tool_call_id=f"{tag}-read",
    )

    async def scenario():
        read_result = await service.read_source_document(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            arguments={"fileRef": f"asset-version:{version_id}"},
            context=read_context,
        )
        stored = dict(read_result)
        if mutate_stored is not None:
            mutate_stored(stored)
        service.executions.append_specialist_message(
            "project-1",
            read_context.specialist_run_id,
            message_id=f"tool-{tag}-result",
            role="tool",
            content_parts=[{"type": "text", "text": json.dumps(stored)}],
            metadata={"tool": "read_document", "toolCallId": f"{tag}-read"},
        )
        if between_read_and_commit is not None:
            between_read_and_commit(read_result)
        commit_context = _running_context(
            service,
            services,
            asset_id,
            tool_call_id=f"{tag}-commit",
        )
        committed = await service.commit_agent_intelligence(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            command_id=f"{tag}-commit-1",
            context=commit_context,
            arguments={
                "summary": f"{tag} 场景的文档理解。",
                "shots": shots,
                "entities": [],
                "semanticEntries": [],
                "moduleResultRefs": {"document": read_result["resultRef"]},
                **(extra_arguments or {}),
            },
        )
        return read_result, committed

    return asyncio.run(scenario())


def test_document_commit_produces_document_index(tmp_path) -> None:
    services, asset_id, version_id = _services_with_document(tmp_path)
    service = SourceMediaAnalysisService(services)
    read_result, committed = _read_then_commit(
        service,
        services,
        asset_id,
        version_id,
        tag="doc",
        shots=[
            _document_shot(1, "封面页：标题与主角介绍。"),
            _document_shot(2, "第二页：冲突展开的场景节拍。"),
            _document_shot(3, "第三页：结尾与情绪收束。"),
        ],
        extra_arguments={
            "summary": "三页剧本：逐页给出场景节拍与关键动作。",
            "semanticEntries": [
                {
                    "text": "第 2 页给出冲突场景的节拍列表",
                    "tags": ["page-2", "冲突"],
                    "confidence": 0.9,
                },
            ],
        },
    )

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
    assert read_result["format"] == "pdf"
    assert read_result["pageCount"] == 3
    assert "Scene beats 2" in read_result["textExcerpt"]
    resolved = resolve_document_page_ref(
        services.projects.project_root("project-1"),
        read_result["pageImageRefs"][0],
    )
    assert resolved is not None and resolved[1] == 1
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

    # Admission boundary: a fileRef outside the admitted asset is rejected.
    async def out_of_boundary():
        return await service.read_source_document(
            project_id="project-1",
            target_ref=f"asset:{asset_id}",
            arguments={"fileRef": "asset-version:another-project-version"},
            context=_running_context(
                service,
                services,
                asset_id,
                tool_call_id="doc-call-boundary",
            ),
        )

    with pytest.raises(ValidationError, match="准入边界"):
        asyncio.run(out_of_boundary())


def test_document_commit_rejects_wrong_page_intervals(tmp_path) -> None:
    services, asset_id, version_id = _services_with_document(tmp_path)
    service = SourceMediaAnalysisService(services)
    with pytest.raises(ValidationError, match="页伪"):
        _read_then_commit(
            service,
            services,
            asset_id,
            version_id,
            tag="doc-mismatch",
            shots=[_document_shot(1, "只有一页的提交")],
        )


_LONG_TEXT_MARKER = "结尾彩蛋：星光电影院重新亮灯。"


def _long_text_body() -> bytes:
    # A text source larger than the 20k model excerpt: the deterministic
    # semantic index must still contain content past the excerpt boundary.
    body = "\n\n".join(
        f"段落 {number}：" + "剧情推进。" * 120 for number in range(1, 50)
    )
    content = f"{body}\n\n{_LONG_TEXT_MARKER}\n"
    assert len(content) > 25_000
    return content.encode("utf-8")


def test_long_text_source_enters_document_flow_end_to_end(tmp_path) -> None:
    # The tool result stays bounded for model context, while the indexed
    # text is persisted separately with honest coverage numbers.
    services, asset_id, version_id = _services_with_source(
        tmp_path,
        name="long-script.txt",
        content=_long_text_body(),
        media_type="text/plain",
    )
    snapshot = services.projects.read("project-1")
    assert (
        snapshot.project.assets.source_versions_by_id[version_id].media_kind
        == "document"
    )
    service = SourceMediaAnalysisService(services)
    read_result, committed = _read_then_commit(
        service,
        services,
        asset_id,
        version_id,
        tag="long-txt",
        shots=[_document_shot(1, "全文概括。")],
    )
    assert committed["status"] == "SUCCEEDED"
    index = service.load("project-1", asset_id)
    assert index.media.media_kind == "document"
    assert index.media.document is not None
    assert len(read_result["textExcerpt"]) <= 20_000
    assert read_result["textCoverage"]["extractedChars"] > 25_000
    assert (
        read_result["textCoverage"]["indexedChars"]
        == read_result["textCoverage"]["extractedChars"]
    )
    assert _LONG_TEXT_MARKER not in read_result["textExcerpt"]
    doc_text = [
        item for item in index.semantic_entries if "document-text" in item.tags
    ]
    assert any(_LONG_TEXT_MARKER in item.text for item in doc_text)
    # Text-extraction coverage is persisted on the ocr modality.
    ocr = index.coverage["ocr"]
    assert ocr.mode == "available"
    assert ocr.producer == "document_reader"
    assert ocr.ratio == 1.0


def test_carriage_return_text_survives_commit_integrity_check(
    tmp_path,
) -> None:
    # Regression: the indexed text is persisted/verified byte-for-byte.
    # pdfium emits \r\n for line breaks inside a text block, and commits
    # previously failed the sha256 integrity check on every attempt
    # (read_text() collapsed \r\n to \n), locking the source-intelligence
    # agent into a retry loop.
    services, asset_id, version_id = _services_with_source(
        tmp_path,
        name="windows-notes.pdf",
        content=_pdf_bytes(
            pages=1,
            text="Scene 1: cat enters frame\nScene 2: camera pulls back\nScene 3: rooftop finale",
        ),
        media_type="application/pdf",
    )
    service = SourceMediaAnalysisService(services)
    read_result, committed = _read_then_commit(
        service,
        services,
        asset_id,
        version_id,
        tag="cr",
        shots=[_document_shot(1, "全文概括。")],
    )

    coverage = read_result["textCoverage"]
    stored = document_indexed_text_path(
        services.projects.project_root("project-1"),
        read_result["sourceChecksum"],
        read_result["resultRef"],
    ).read_bytes()
    assert b"\r" in stored, "fixture must exercise carriage returns"
    assert len(stored.decode("utf-8")) == coverage["indexedChars"]
    assert committed["status"] == "SUCCEEDED"


@pytest.mark.parametrize(
    ("tag", "break_runtime", "coverage_mutation", "message"),
    [
        pytest.param(
            "doc-missing-file",
            "delete",
            None,
            "Runtime 文件缺失",
            id="missing-runtime-file",
        ),
        pytest.param(
            "doc-tampered-file",
            "tamper",
            None,
            "不一致",
            id="tampered-runtime-file",
        ),
        pytest.param(
            "doc-null-coverage",
            "swap",
            "null",
            "textCoverage 不合法",
            id="coverage-null",
        ),
    ],
)
def test_document_commit_fails_closed_on_indexed_text_integrity(
    tmp_path,
    tag,
    break_runtime,
    coverage_mutation,
    message,
) -> None:
    # Fail-close: a new-format result must be backed by the intact Runtime
    # indexed-text file, and an explicit "textCoverage": null must hit the
    # strict model instead of the legacy branch — otherwise a same-length
    # content swap could be committed with ratio=1.0.
    services, asset_id, version_id = _services_with_source(
        tmp_path,
        name="notes.txt",
        content=("剧情推进。" * 2000).encode("utf-8"),
        media_type="text/plain",
    )
    service = SourceMediaAnalysisService(services)
    project_root = services.projects.project_root("project-1")
    checksum = (
        services.projects.read("project-1")
        .project.assets.source_versions_by_id[version_id]
        .checksum
    )

    def break_runtime_text(read_result):
        path = document_indexed_text_path(
            project_root,
            checksum,
            read_result["resultRef"],
        )
        if break_runtime == "delete":
            path.unlink()
        elif break_runtime == "tamper":
            path.write_text("被替换的内容", encoding="utf-8")
        else:  # same-length content swap
            path.write_text(
                "Z" * len(path.read_text(encoding="utf-8")),
                encoding="utf-8",
            )

    def mutate_stored(stored):
        stored["textCoverage"] = None

    with pytest.raises(ValidationError, match=message):
        _read_then_commit(
            service,
            services,
            asset_id,
            version_id,
            tag=tag,
            shots=[_document_shot(1, "全文概括。")],
            between_read_and_commit=break_runtime_text,
            mutate_stored=mutate_stored if coverage_mutation else None,
        )


def test_unknown_ratio_is_confined_to_document_ocr() -> None:
    # CR P2: the honest-unknown ratio must not weaken every modality's
    # frozen invariant.
    from schemas.assets import SourceCoverage

    with pytest.raises(ValueError):
        SourceCoverage.model_validate(
            {"mode": "available", "producer": "model_native", "ratio": None},
        )
    SourceCoverage.model_validate(
        {"mode": "available", "producer": "document_reader", "ratio": None},
    )


def test_truncated_indexing_persists_partial_ocr_ratio(
    tmp_path,
    monkeypatch,
) -> None:
    # Indexing truncation must surface as coverage.ocr.ratio < 1 and the
    # cut tail must not appear in the semantic entries.
    monkeypatch.setattr(
        "services.document_reader.MAX_INDEXED_TEXT_CHARS",
        5000,
    )
    marker = "末尾唯一标记：星光不灭。"
    services, asset_id, version_id = _services_with_source(
        tmp_path,
        name="long.txt",
        content=("剧情推进。" * 1600 + marker).encode("utf-8"),
        media_type="text/plain",
    )
    service = SourceMediaAnalysisService(services)
    read_result, committed = _read_then_commit(
        service,
        services,
        asset_id,
        version_id,
        tag="doc-truncated",
        shots=[_document_shot(1, "全文概括。")],
    )
    coverage = read_result["textCoverage"]
    assert coverage["indexedChars"] == 5000
    assert coverage["extractedChars"] > 5000
    assert committed["status"] == "SUCCEEDED"
    index = service.load("project-1", asset_id)
    ocr = index.coverage["ocr"]
    assert ocr.mode == "available"
    assert ocr.producer == "document_reader"
    assert ocr.ratio == pytest.approx(5000 / coverage["extractedChars"])
    assert all(
        marker not in item.text
        for item in index.semantic_entries
        if "document-text" in item.tags
    )


def test_one_specialist_run_commits_multiple_assets(tmp_path) -> None:
    """A batch delegation commits several assets from one run.

    Round provenance (caused_by_request_id) is immutable per Round, so
    each commit must own its Round; reusing the specialist run id across
    commits rejected every asset after the first.
    """

    services, first_asset, first_version = _services_with_document(tmp_path)
    result, _ = _ingest_many_sync(
        services,
        project_id="project-1",
        key="doc-asset-2",
        inputs=[
            _AssetInput(
                name="script-two.pdf",
                content=_pdf_bytes(pages=2),
                media_type="application/pdf",
            ),
        ],
        attach_source=True,
        scope="document-source-test",
    )
    second_asset = result["items"][0]["assetId"]
    second_version = result["items"][0]["assetVersionId"]
    service = SourceMediaAnalysisService(services)

    for ordinal, (asset_id, version_id, page_count) in enumerate(
        [(first_asset, first_version, 3), (second_asset, second_version, 2)],
        start=1,
    ):
        _read_result, committed = _read_then_commit(
            service,
            services,
            asset_id,
            version_id,
            tag=f"doc-batch-{ordinal}",
            shots=[
                _document_shot(page, f"第 {page} 页节拍。")
                for page in range(1, page_count + 1)
            ],
            extra_arguments={"summary": f"第 {ordinal} 份剧本：逐页节拍。"},
        )
        assert committed["status"] == "SUCCEEDED"
    assert service.load("project-1", first_asset).summary.startswith("第 1")
    assert service.load("project-1", second_asset).summary.startswith("第 2")
