# -*- coding: utf-8 -*-
"""Small, dependency-free CSS-only interaction contract (mirrored in IVB).

HTMLParser counts real interactive nodes, never comments or text. Browser
consumers additionally enforce CSP and a script-disabled iframe sandbox.
"""

from html.parser import HTMLParser
import re

_ALLOWED = set(
    (
        "html head body title meta style div span p h1 h2 h3 h4 button a "
        "section main article header footer nav aside figure figcaption "
        "strong b em i small h5 h6 br hr ul ol li dl dt dd code pre time "
        "svg g path circle rect line polyline polygon ellipse defs "
        "lineargradient radialgradient stop clippath text tspan"
    ).split(),
)


class _InteractionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs = []
        self.problems = []
        self.in_style = False
        self.countdowns = 0
        self.has_document = False
        self.in_document = False
        self.outside_text = []

    def handle_starttag(self, tag, attrs):
        if tag == "html":
            self.has_document = True
            self.in_document = True
        if tag not in _ALLOWED:
            self.problems.append(f"forbidden HTML tag: {tag}")
        values = dict(attrs)
        if "data-interaction-countdown" in values:
            self.countdowns += 1
        if len(values) != len(attrs):
            self.problems.append("duplicate HTML attributes")
        for key, value in attrs:
            if key.startswith("on") or key in {
                "href",
                "src",
                "srcset",
                "xlink:href",
                "action",
                "formaction",
                "srcdoc",
                "http-equiv",
                "contenteditable",
            }:
                self.problems.append(f"forbidden HTML attribute: {key}")
            if key == "style":
                self.check_css(value or "")
        if "data-edge-ref" in values:
            if tag != "button" or "disabled" in values or "hidden" in values:
                self.problems.append(
                    "data-edge-ref must be on an enabled visible button",
                )
            self.refs.append(values["data-edge-ref"])
        self.in_style = tag == "style"

    def handle_endtag(self, tag):
        if tag == "html":
            self.in_document = False
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if not self.in_document:
            self.outside_text.append(data)
        if self.in_style:
            self.check_css(data)

    def validate_document_text(self):
        # Browsers reparent prose outside <html> into the visible body.
        # Keep legacy choice fragments valid, but never accept a document
        # wrapped in model commentary or Markdown code fences.
        if self.has_document and any(s.strip() for s in self.outside_text):
            self.problems.append("text outside the HTML document is forbidden")

    def check_css(self, css):
        # CSS escapes/comments can disguise url()/@import; allow neither.
        if re.search(
            r"url\s*\(|@import|expression\s*\(|-moz-binding|"
            r"behavior\s*:|\\|/\*",
            css,
            re.I,
        ):
            self.problems.append(
                "external resources, CSS comments or escapes are forbidden",
            )


def validate_interaction_html(
    html: str,
    edge_refs: list[str] | None,
    *,
    require_countdown: bool = False,
) -> list[str]:
    parser = _InteractionParser()
    if not 32 <= len(html) <= 200_000:
        parser.problems.append("HTML length must be between 32 and 200000")
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        parser.problems.append("malformed HTML")
    parser.validate_document_text()
    if edge_refs is not None and sorted(
        str(ref) for ref in parser.refs
    ) != sorted(edge_refs):
        parser.problems.append(
            "data-edge-ref buttons must exactly match options",
        )
    if require_countdown and parser.countdowns != 1:
        parser.problems.append(
            "timed choice requires one data-interaction-countdown node",
        )
    return list(dict.fromkeys(parser.problems))
