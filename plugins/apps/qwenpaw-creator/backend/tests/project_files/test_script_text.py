# -*- coding: utf-8 -*-
"""Extracting uploaded script documents and parsing script Markdown."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from domain.errors import ValidationError
from services import document_reader
from services.document_reader import DocumentReadResult
from services.project_files.script_document_text import (
    SCRIPT_IMPORT_DOCUMENT_MIN_CHARS,
    SCRIPT_IMPORT_DOCUMENT_SUFFIXES,
    extract_script_document_text,
    is_script_document_name,
)
from services.project_files.script_import import ScriptImportError
from services.project_files.script_markdown import (
    ScriptBlock,
    parse_script_markdown,
    serialize_script_blocks,
)

pytestmark = pytest.mark.unit

# pdf 渲染器给每页正文加的 `[Page N]` 前缀、以及页间的长空行，都必须被
# 清掉；CRLF 也要归一，否则剧本块里会留下不可见字符。
SCRIPT_TEXT = (
    "[Page 1]\n"
    "第一场 日 内 教室\n"
    "小明：你今天怎么来这么早？\n"
    "\n\n\n\n"
    "[Page 2]\n"
    "小红：我想先把台词背完。\r\n"
    "[Page 3]\n"
    "小明：那我先陪你把第一场的走位过一遍。\n"
)
CLEANED_TEXT = (
    "第一场 日 内 教室\n"
    "小明：你今天怎么来这么早？\n\n"
    "小红：我想先把台词背完。\n"
    # 页标记被剔后上下两页正文直接相邻，不再多出空行。
    "小明：那我先陪你把第一场的走位过一遍。"
)

# 上面这份清洗后的正文必须长于 SCRIPT_IMPORT_DOCUMENT_MIN_CHARS，否则
# 「抽正文成功」的用例会被当成扫描件拒掉，看不出真正失败的原因。
assert len(CLEANED_TEXT) > SCRIPT_IMPORT_DOCUMENT_MIN_CHARS


def _result(
    text: str,
    *,
    complete: bool = True,
    notes: tuple[str, ...] = (),
    unextractable_pages: tuple[int, ...] = (),
) -> DocumentReadResult:
    """A text-only render: no page image is produced or persisted."""

    return DocumentReadResult(
        format="pdf",
        page_count=3,
        pages_rendered=(),
        page_images=(),
        text_excerpt=text[:200],
        indexed_text=text,
        extracted_chars=len(text),
        extraction_complete=complete,
        extraction_fraction=1.0 if complete else 0.5,
        notes=tuple(notes),
        unextractable_pages=tuple(unextractable_pages),
    )


def _patch_reader(monkeypatch, result=None, *, error=None):
    """Stub the renderer and record exactly what it was handed."""

    seen: list[tuple[Path, bytes]] = []

    def fake_read(file_path: Path) -> DocumentReadResult:
        seen.append((file_path, file_path.read_bytes()))
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(document_reader, "read_document_text", fake_read)
    return seen


def test_only_word_and_pdf_names_count_as_script_documents() -> None:
    """ppt/xlsx 的语义不是剧本，md/txt 本来就能直接解码。"""

    assert SCRIPT_IMPORT_DOCUMENT_SUFFIXES == (".pdf", ".doc", ".docx")
    for name in ("剧本.docx", "第一集.PDF", "manuscript.doc", "script.pdf"):
        assert is_script_document_name(name), name
    for name in (
        "剧本.md",
        "剧本.txt",
        "分镜.pptx",
        "设定表.xlsx",
        "片段.mp4",
        "无后缀",
        "",
    ):
        assert not is_script_document_name(name), name


def test_extract_strips_page_markers_and_collapses_blank_runs(
    monkeypatch,
) -> None:
    _patch_reader(monkeypatch, _result(SCRIPT_TEXT))

    text = extract_script_document_text(b"%PDF-1.7", name="剧本.pdf")

    assert text == CLEANED_TEXT
    assert "[Page" not in text
    assert "\r" not in text
    assert "\n\n\n" not in text


def test_upload_name_never_reaches_the_scratch_path(monkeypatch) -> None:
    """上传名只进报错文案；落盘用固定名，穿越与空格都伤不到路径。"""

    seen = _patch_reader(monkeypatch, _result(SCRIPT_TEXT))

    extract_script_document_text(b"payload", name="../../evil 剧本.docx")

    assert len(seen) == 1
    path, data = seen[0]
    assert path.name == "script-import.docx"
    assert data == b"payload"
    # 临时目录随调用回收：长剧本不会留下一地中间产物。
    assert not path.parent.exists()


def test_truncated_extraction_refuses_to_land_half_a_script(
    monkeypatch,
) -> None:
    """页数被渲染器截断时宁可失败，也不登记半份剧本。"""

    _patch_reader(
        monkeypatch,
        _result(SCRIPT_TEXT, complete=False, notes=("page cap 2/40",)),
    )

    with pytest.raises(ScriptImportError) as caught:
        extract_script_document_text(b"payload", name="剧本.pdf")

    assert caught.value.code == "SCRIPT_IMPORT_DOCUMENT_TRUNCATED"
    message = str(caught.value)
    assert "page cap 2/40" in message
    assert "素材" in message, "必须指出还能走素材理解这条退路"


def test_document_without_a_text_layer_is_reported(monkeypatch) -> None:
    """扫描件/图片型 PDF：渲染成功但一个字都抽不到。"""

    _patch_reader(monkeypatch, _result("[Page 1]\n[Page 2]\n"))

    with pytest.raises(ScriptImportError) as caught:
        extract_script_document_text(b"payload", name="扫描剧本.pdf")

    assert caught.value.code == "SCRIPT_IMPORT_DOCUMENT_NO_TEXT"
    assert "文字层" in str(caught.value)


def test_image_page_without_a_text_layer_refuses_direct_landing(
    monkeypatch,
) -> None:
    """混合扫描 PDF：有可见内容却抽不出文字层的页必须拒绝直登。

    回归：直登此前把 extraction_complete 当成「内容完整」，但渲染器只在
    超页数上限时才置它为 False；图片页/扫描页没有文字层却被静默略过，
    全局字符数又超过下限，于是半份剧本被当成完整剧本登记。现在渲染器
    逐页报告无法抽取文字的页，直登据此拒绝并指路素材理解/人工确认。
    """

    # 封面文字层完整、extraction_complete 仍为 True、正文也长过下限，
    # 但第 2 页是图片正文：单靠 complete / 字符数都看不出缺页。
    _patch_reader(
        monkeypatch,
        _result(SCRIPT_TEXT, unextractable_pages=(2,)),
    )

    with pytest.raises(ScriptImportError) as caught:
        extract_script_document_text(b"payload", name="混合剧本.pdf")

    assert caught.value.code == "SCRIPT_IMPORT_DOCUMENT_IMAGE_PAGE"
    message = str(caught.value)
    assert "第 2 页" in message
    assert "素材" in message, "必须指出还能走素材理解/人工确认这条退路"


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        (ValidationError("unsupported document"), "素材上传"),
        (RuntimeError("soffice not found"), "LibreOffice"),
    ],
)
def test_reader_failure_names_a_way_forward(
    monkeypatch,
    error,
    hint,
) -> None:
    """缺依赖或格式不支持时，报错要给出可执行的下一步。"""

    _patch_reader(monkeypatch, error=error)

    with pytest.raises(ScriptImportError) as caught:
        extract_script_document_text(b"payload", name="剧本.docx")

    assert caught.value.code == "SCRIPT_IMPORT_DOCUMENT_UNREADABLE"
    assert hint in str(caught.value)


def test_conversion_timeout_is_not_blamed_on_a_missing_dependency(
    monkeypatch,
) -> None:
    """LibreOffice 超时是「太慢」，不能叫用户去装已装好的依赖。

    渲染器对 soffice 子进程设了 180s 上限，macOS 首次启动光 Gatekeeper
    扫描就可能超过 80s，所以这是一条真会走到的路径；而 TimeoutExpired
    既不是 OSError 也不是 RuntimeError，很容易从 except 元组里漏掉。
    """

    _patch_reader(
        monkeypatch,
        error=subprocess.TimeoutExpired(cmd="soffice", timeout=180),
    )

    with pytest.raises(ScriptImportError) as caught:
        extract_script_document_text(b"payload", name="剧本.docx")

    assert caught.value.code == "SCRIPT_IMPORT_DOCUMENT_TIMEOUT"
    message = str(caught.value)
    assert "超时" in message
    assert "重试" in message, "超时唯一对的下一步就是再试一次"
    assert "素材上传" in message, "也要给出绕开文档链路的出路"
    assert "缺少文档解析依赖" not in message


def test_bad_input_fails_before_touching_the_renderer(monkeypatch) -> None:
    seen = _patch_reader(monkeypatch, _result(SCRIPT_TEXT))

    with pytest.raises(ScriptImportError) as unsupported:
        extract_script_document_text(b"payload", name="分镜.pptx")
    assert unsupported.value.code == "SCRIPT_IMPORT_DOCUMENT_UNSUPPORTED"

    with pytest.raises(ScriptImportError) as empty:
        extract_script_document_text(b"", name="剧本.docx")
    assert empty.value.code == "SCRIPT_IMPORT_DOCUMENT_NO_TEXT"

    assert not seen, "抽不出正文的上传不该启动渲染链路"


SCENE_SCRIPT = """\
## 场 1 · 内景 · 旧宅大厅 · 夜

烛光摇曳，林晚推开吱呀作响的木门，灰尘在光柱中翻滚。

**林晚**（低声）：这里……和二十年前一模一样。

**管家**：小姐，老爷临终前只留下这把钥匙。

> 钥匙上刻着的家徽，和母亲遗物上的一模一样。

## 场 2 · 外景 · 后山小径 · 黎明

两人沿小径疾行，远处传来犬吠。
"""

VOICEOVER_SCRIPT = """\
三秒抓住注意力：你知道吗？90% 的人第一步就选错了。

**旁白**：今天这支视频，教你三招选对第一台相机。

第一招，看传感器尺寸，别被像素数字骗了。

> 记住：底大一级压死人。

第二招，镜头群比机身更重要。
"""

EDIT_SCRIPT = """\
## 段 1 · 开场钩子

[访谈A 01:02:13–01:02:21](source-version://sv-interview-a?in=3733&out=3741)
受访者说出全片最有冲击力的一句话，直接切入。

**受访者**（哽咽）：那一天，我永远忘不了。

## 段 2 · 背景铺垫

[空镜·老街](source-version://sv-broll-street?in=120&out=480) 叠加旁白，
交代事件发生的时间与地点。
"""


def test_parse_scene_script_blocks() -> None:
    blocks = parse_script_markdown(SCENE_SCRIPT)
    kinds = [block.kind for block in blocks]
    assert kinds == [
        "scene",
        "action",
        "line",
        "line",
        "hook",
        "scene",
        "action",
    ]
    assert blocks[0].text == "场 1 · 内景 · 旧宅大厅 · 夜"
    assert blocks[2].character == "林晚"
    assert blocks[2].parenthetical == "低声"
    assert blocks[2].text == "这里……和二十年前一模一样。"
    assert blocks[3].character == "管家"
    assert blocks[3].parenthetical == ""
    assert blocks[4].kind == "hook"
    assert "家徽" in blocks[4].text


def test_parse_voiceover_script_blocks() -> None:
    blocks = parse_script_markdown(VOICEOVER_SCRIPT)
    kinds = [block.kind for block in blocks]
    assert kinds == ["action", "line", "action", "hook", "action"]
    assert blocks[1].character == "旁白"
    assert blocks[3].text == "记住：底大一级压死人。"


def test_parse_edit_script_extracts_source_refs() -> None:
    blocks = parse_script_markdown(EDIT_SCRIPT)
    refs = [ref for block in blocks for ref in block.source_refs]
    assert refs == [
        ("sv-interview-a", 3733, 3741),
        ("sv-broll-street", 120, 480),
    ]
    # 链接原文保留在块文本中，序列化不丢失。
    linked = next(block for block in blocks if block.source_refs)
    assert "source-version://sv-interview-a?in=3733&out=3741" in linked.text


@pytest.mark.parametrize(
    "sample",
    [SCENE_SCRIPT, VOICEOVER_SCRIPT, EDIT_SCRIPT],
    ids=["scene", "voiceover", "edit"],
)
def test_round_trip_is_lossless(sample: str) -> None:
    parsed = parse_script_markdown(sample)
    assert parse_script_markdown(serialize_script_blocks(parsed)) == parsed


def test_edge_blocks_normalize_and_round_trip() -> None:
    # 半角冒号输入解析成同一台词块；序列化输出全角冒号后再解析等值。
    parsed = parse_script_markdown("**林晚**: 你来了。\n")
    assert parsed == [
        ScriptBlock(kind="line", text="你来了。", character="林晚"),
    ]
    assert serialize_script_blocks(parsed) == "**林晚**：你来了。\n"
    # 多行 hook 归并为单块且往返无损。
    hooks = parse_script_markdown("> 第一行悬念\n> 第二行悬念\n")
    assert hooks == [
        ScriptBlock(kind="hook", text="第一行悬念\n第二行悬念"),
    ]
    assert parse_script_markdown(serialize_script_blocks(hooks)) == hooks
