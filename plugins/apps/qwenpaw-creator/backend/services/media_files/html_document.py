# -*- coding: utf-8 -*-
"""Extract one complete authored document from a model's prose/code reply."""

from html.parser import HTMLParser


class _DocumentBounds(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_offsets = [0] + [
            i + 1 for i, char in enumerate(source) if char == "\n"
        ]
        self.starts = []
        self.ends = []
        self.doctypes = []

    def source_offset(self):
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag == "html":
            self.starts.append(self.source_offset())

    def handle_endtag(self, tag):
        if tag == "html":
            self.ends.append(self.source.index(">", self.source_offset()) + 1)

    def handle_decl(self, decl):
        if decl.lower().split() == ["doctype", "html"]:
            start = self.source_offset()
            self.doctypes.append(
                (start, self.source.index(">", start) + 1),
            )


def extract_html_document(raw: str) -> str:
    """Keep document bytes intact; exclude Markdown and surrounding prose.

    Parse real tags so comments, attributes and CSS containing ``</html>``
    cannot truncate the document. Never guess between multiple documents or
    silently repair a truncated answer; the caller can retry validation.
    """

    parser = _DocumentBounds(raw)
    try:
        parser.feed(raw)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise ValueError("HTML 文档无法解析，请返回完整的 HTML") from exc
    if len(parser.starts) != 1 or len(parser.ends) != 1:
        raise ValueError("必须返回唯一、完整的 HTML 文档（包含 <html> 和 </html>）")
    start, end = parser.starts[0], parser.ends[0]
    if end <= start:
        raise ValueError("HTML 文档的起止标签顺序错误")
    preceding = [a for a, b in parser.doctypes if b <= start]
    if preceding:
        start = preceding[-1]
    return raw[start:end]
