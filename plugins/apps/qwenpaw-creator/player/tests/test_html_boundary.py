# -*- coding: utf-8 -*-
"""Standalone validation must also reject model prose outside a document."""

from pathlib import Path

import pytest

from ivb.format.interaction_html import validate_interaction_html
from ivb.format.presentation_html import validate_presentation_html


@pytest.mark.parametrize("presentation", [False, True])
@pytest.mark.parametrize(
    "prefix,suffix",
    [("这是完整的页面。\n```html\n", ""), ("", "\n```\n### 优化建议")],
)
def test_rejects_commentary_around_html(presentation, prefix, suffix):
    if presentation:
        html = (
            (Path(__file__).parent / "fixtures/authored-presentation.html")
            .read_text()
            .replace("__NODES__", "")
        )
        validate = validate_presentation_html
    else:
        html = '<html><body><button data-edge-ref="a">A</button></body></html>'
        validate = lambda value: validate_interaction_html(value, ["a"])
    assert not validate(html)
    assert "text outside the HTML document is forbidden" in validate(
        prefix + html + suffix,
    )
    assert not validate(
        html.replace("</body>", "<p>正文中的优化建议和 ``` 保留。</p></body>"),
    )


def test_legacy_choice_fragment_remains_valid():
    assert not validate_interaction_html(
        '<button data-edge-ref="a">选择这条分支</button>',
        ["a"],
    )
