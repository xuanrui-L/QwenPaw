# -*- coding: utf-8 -*-
"""Model prose must not become part of the authored preview document."""

import pytest

from services.media_files.html_document import extract_html_document

pytestmark = pytest.mark.unit

DOCUMENT = """<!DOCTYPE html>
<html lang="zh-CN"><head><style>
p::after { content: "</html>"; }
</style></head><body>
<!-- A literal </html> in a comment is not a document boundary. -->
<p title="</html>">正文里的 ``` 和优化建议都应该保留。</p>
</body></html>"""


@pytest.mark.parametrize(
    "reply",
    [
        DOCUMENT,
        f"\ufeff \n{DOCUMENT}\n",
        f"```html\n{DOCUMENT}\n```",
        f"这是完整的互动页面。\n```html\n{DOCUMENT}\n```\n### 优化建议\n说明文字。",
        f"下面是页面：\n{DOCUMENT}\n页面说明。",
        f"前言\r\n~~~html\r\n{DOCUMENT}\r\n~~~\r\n后记",
    ],
)
def test_extract_preserves_document_and_excludes_only_surrounding_text(reply):
    assert extract_html_document(reply) == DOCUMENT


@pytest.mark.parametrize(
    "reply",
    [
        "这里只返回了说明文字。",
        DOCUMENT.removesuffix("</html>"),
        DOCUMENT + "\n" + DOCUMENT,
        "<html><html><body>Ambiguous</body></html></html>",
        "</html><html>",
        "<p>A fragment is not a complete generated document.</p>",
        "<![invalid declaration]>" + DOCUMENT,
    ],
)
def test_incomplete_or_ambiguous_reply_is_not_silently_repaired(reply):
    with pytest.raises(ValueError, match="HTML"):
        extract_html_document(reply)


def test_case_insensitive_document_without_doctype_is_preserved():
    html = '<HTML data-note="a > b"><HEAD></HEAD><BODY>页面</BODY></HTML >'
    assert extract_html_document(f"前言\n{html}\n后记") == html


def test_doctype_and_document_comments_are_preserved():
    html = DOCUMENT.replace("\n<html", "\n<!-- authored page -->\n<html")
    assert extract_html_document(f"说明\n```html\n{html}\n```\n后记") == html
