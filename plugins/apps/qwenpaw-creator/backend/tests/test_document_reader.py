# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Per-format fixtures for the vendored document reader."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from domain.errors import ValidationError
from services.document_reader import (
    MAX_TEXT_EXCERPT_CHARS,
    is_supported_document,
    read_document,
)
from vendor.mm_plugins.image_budget import TOKEN_SIZE


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


def _read(path: Path, output_dir: Path, **kwargs):
    return asyncio.run(read_document(path, output_dir=output_dir, **kwargs))


def _png_size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as img:
        return img.size


def test_supported_extension_probe() -> None:
    assert is_supported_document("script.pdf")
    assert is_supported_document("deck.PPTX")
    assert is_supported_document("data.xlsx")
    assert is_supported_document("dialogue.srt")
    assert is_supported_document("notes.txt")
    assert not is_supported_document("archive.zip")


def test_pdf_renders_pages_text_layer_and_grid_alignment(tmp_path) -> None:
    source = tmp_path / "script.pdf"
    _make_pdf(source, pages=3)
    result = _read(source, tmp_path / "pages")

    assert result.format == "pdf"
    assert result.page_count == 3
    assert result.pages_rendered == (1, 2, 3)
    assert len(result.page_images) == 3
    for page, image_path in zip(result.pages_rendered, result.page_images):
        assert image_path.name == f"page-{page:04d}.png"
        assert image_path.is_file()
        width, height = _png_size(image_path)
        assert width % TOKEN_SIZE == 0
        assert height % TOKEN_SIZE == 0
    assert "Creator Doc Page 1: storyboard beats" in result.text_excerpt
    assert len(result.text_excerpt) <= MAX_TEXT_EXCERPT_CHARS


def test_pdf_page_range_selection(tmp_path) -> None:
    source = tmp_path / "script.pdf"
    _make_pdf(source, pages=4)
    result = _read(source, tmp_path / "pages", pages="2,4")

    assert result.page_count == 4
    assert result.pages_rendered == (2, 4)
    assert [item.name for item in result.page_images] == [
        "page-0002.png",
        "page-0004.png",
    ]
    assert any(
        "request more via the pages parameter" in n for n in result.notes
    )


def test_csv_renders_table_image_and_markdown(tmp_path) -> None:
    source = tmp_path / "budget.csv"
    source.write_text(
        "scene,cost\nopening,120\nfinale,340\n",
        encoding="utf-8",
    )
    result = _read(source, tmp_path / "pages")

    assert result.format == "csv"
    assert result.page_count == 1
    assert len(result.page_images) == 1
    width, height = _png_size(result.page_images[0])
    assert width % TOKEN_SIZE == 0 and height % TOKEN_SIZE == 0
    assert "finale" in result.text_excerpt
    assert "340" in result.text_excerpt


def test_xlsx_sheets_map_to_pages(tmp_path) -> None:
    import pandas as pd

    source = tmp_path / "plan.xlsx"
    with pd.ExcelWriter(source, engine="openpyxl") as writer:
        pd.DataFrame({"shot": ["s1", "s2"], "len": [3, 5]}).to_excel(
            writer,
            sheet_name="Shots",
            index=False,
        )
        pd.DataFrame({"role": ["cat"], "voice": ["warm"]}).to_excel(
            writer,
            sheet_name="Cast",
            index=False,
        )
    result = _read(source, tmp_path / "pages")

    assert result.format == "xlsx"
    assert result.page_count == 2
    assert result.pages_rendered == (1, 2)
    assert len(result.page_images) == 2
    assert "Shots" in result.text_excerpt
    assert "Cast" in result.text_excerpt


def test_srt_subtitle_text_only(tmp_path) -> None:
    source = tmp_path / "dialogue.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:02,500\n猫走进画面\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n镜头拉远\n",
        encoding="utf-8",
    )
    result = _read(source, tmp_path / "pages")

    assert result.format == "srt"
    assert result.page_images == ()
    assert "2 entries" in result.text_excerpt
    assert "猫走进画面" in result.text_excerpt


def test_plain_text_uses_code_renderer(tmp_path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("act one\nact two\n", encoding="utf-8")
    result = _read(source, tmp_path / "pages")

    assert result.format == "txt"
    assert result.page_images == ()
    assert "act two" in result.text_excerpt


def test_unsupported_extension_gives_readable_error(tmp_path) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"PK\x03\x04")
    with pytest.raises(ValidationError) as excinfo:
        _read(source, tmp_path / "pages")
    assert "unsupported document format" in str(excinfo.value)


def test_docx_without_libreoffice_degrades_readably(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.document_reader._resolve_soffice",
        lambda: None,
    )
    source = tmp_path / "brief.docx"
    source.write_bytes(b"PK\x03\x04fake-docx")
    with pytest.raises(ValidationError) as excinfo:
        _read(source, tmp_path / "pages")
    assert "LibreOffice is required" in str(excinfo.value)


def test_html_rendering_disabled_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CREATOR_DOC_READER_WEB_ENABLED", raising=False)
    source = tmp_path / "page.html"
    source.write_text("<html><body>hello</body></html>", encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        _read(source, tmp_path / "pages")
    assert "CREATOR_DOC_READER_WEB_ENABLED" in str(excinfo.value)
