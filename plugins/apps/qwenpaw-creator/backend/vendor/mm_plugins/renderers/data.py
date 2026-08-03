# -*- coding: utf-8 -*-
# flake8: noqa: E501
# Vendored from Qwen-MM-Plugins (Apache-2.0), release commit 077aea6.
# Upstream path: src/capabilities/core/qwen_mm_plugins_core/renderers/data.py
# Modified for QwenPaw Creator. See backend/vendor/NOTICE.md.
"""Render tabular data (CSV, XLSX) as markdown table text + a table image.

Creator modifications: image blocks carry PIL images tagged with a 1-based
sheet ordinal as the page number; the leading block is a meta block.
"""

from __future__ import annotations

import os
from typing import Any

MAX_ROWS = 100

_PANDAS_HINT = (
    "Missing dependency pandas — install with: pip install pandas openpyxl"
)


def render(path: str, **opts: Any) -> list[dict[str, Any]]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return _render_csv(path, **opts)
    if ext == ".xlsx":
        return _render_xlsx(path, **opts)
    raise ValueError(f"Unsupported data format: {ext}")


def _render_csv(path: str, **opts: Any) -> list[dict[str, Any]]:
    del opts
    try:
        import pandas as pd
    except ImportError as error:
        raise RuntimeError(_PANDAS_HINT) from error

    from vendor.mm_plugins.renderers import meta_block

    df = pd.read_csv(path, nrows=MAX_ROWS + 1)
    blocks = [meta_block("csv", 1, [1])]
    blocks.extend(_dataframe_to_blocks(df, os.path.basename(path), page=1))
    return blocks


def _render_xlsx(path: str, **opts: Any) -> list[dict[str, Any]]:
    try:
        import pandas as pd
    except ImportError as error:
        raise RuntimeError(_PANDAS_HINT) from error

    from vendor.mm_plugins.renderers import (
        DEFAULT_MAX_PAGES,
        meta_block,
        parse_pages,
    )

    xls = pd.ExcelFile(path, engine="openpyxl")
    names = xls.sheet_names
    pages = opts.get("pages")
    indices = (
        parse_pages(pages, len(names))
        if pages
        else range(min(len(names), opts.get("max_pages", DEFAULT_MAX_PAGES)))
    )
    indices = list(indices)
    result: list[dict[str, Any]] = [
        meta_block("xlsx", len(names), [i + 1 for i in indices]),
    ]
    for i in indices:
        df = pd.read_excel(xls, sheet_name=names[i], nrows=MAX_ROWS + 1)
        result.extend(
            _dataframe_to_blocks(
                df,
                f"{os.path.basename(path)} [{names[i]}]",
                page=i + 1,
            ),
        )
    return result


def _dataframe_to_blocks(
    df,
    title: str,
    *,
    page: int,
) -> list[dict[str, Any]]:
    """Return markdown table text + matplotlib table image as content blocks."""
    total_rows = len(df)
    truncated = total_rows > MAX_ROWS
    if truncated:
        df = df.head(MAX_ROWS)

    n_rows, n_cols = df.shape
    header = (
        f"**{title}** ({n_rows}{'+' if truncated else ''} rows, {n_cols} cols)"
    )

    if df.empty:
        return [{"type": "text", "text": f"{header}\n\n(empty)", "page": page}]

    try:
        table_md = df.to_markdown(index=False)
    except Exception:  # pylint: disable=broad-except
        table_md = df.to_string(index=False, max_rows=MAX_ROWS)

    parts = [header, table_md]
    if truncated:
        parts.append(f"... (showing first {MAX_ROWS} rows)")
    text_block = {
        "type": "text",
        "text": "\n\n".join(parts),
        "page": page,
    }

    # Image first, then text (consistent with PDF/page renderers).
    result: list[dict[str, Any]] = []
    img = _dataframe_to_image(df, title)
    if img is not None:
        result.append(
            {
                "type": "image",
                "image": img,
                "page": page,
                "label": f"[Sheet {page} View]",
            },
        )
    result.append(text_block)

    return result


def _dataframe_to_image(df, title: str):
    """Render a DataFrame as a matplotlib table image."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    display_df = df.head(50)
    n_rows, n_cols = display_df.shape
    col_width = max(1.5, min(3.0, 18.0 / max(n_cols, 1)))
    fig_w = min(24, col_width * n_cols + 1)
    fig_h = min(20, 0.4 * n_rows + 1.2)

    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.axis("off")
    ax.set_title(title, fontsize=12, fontweight="bold", pad=10)

    col_labels = [str(c) for c in display_df.columns]
    cell_text = display_df.astype(str).values.tolist()

    table = ax.table(
        cellText=cell_text,
        colLabels=col_labels,
        loc="center",
        cellLoc="left",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1, 1.3)

    for (row, _col), cell in table.get_celld().items():
        if row == 0:
            cell.set_facecolor("#4472C4")
            cell.set_text_props(color="white", fontweight="bold")
        elif row % 2 == 0:
            cell.set_facecolor("#D9E2F3")
        else:
            cell.set_facecolor("#FFFFFF")
        cell.set_edgecolor("#B4C7E7")

    from vendor.mm_plugins.renderers import fig_to_image

    return fig_to_image(fig, pad_inches=0.1)
